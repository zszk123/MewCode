"""问题分类器。

对失败执行轨迹进行模式分类，使用 LLM 识别重复出现的系统性失败模式。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from mewcode.harness.evolution.models import (
    ExecutionTrace,
    FailurePattern,
    ProblemCategory,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 分类 Prompt
# ---------------------------------------------------------------------------

PROBLEM_CLASSIFY_SYSTEM_PROMPT = """\
你是一个进化分析助手。分析以下 AI 编程助手的执行轨迹，识别系统性失败模式。

分类规则：
- **missing_capability**：Agent 因缺少某项具体能力/技能而失败（不会处理特定错误、不懂特定规范）
- **pattern_repetition**：Agent 在不同任务中重复犯相同的编程错误
- **tool_misuse**：Agent 使用了正确的工具但参数/方式不对
- **knowledge_gap**：Agent 缺乏特定领域的知识（框架、库、语言的特定用法）
- **no_issue**：没有发现系统性模式，都是孤立错误

规则：
- 只有至少出现 3 次的模式才算有效
- 必须引用轨迹中的实际错误信息作为证据（复制 error_message 或 stack_trace 的关键行）
- 禁止编造问题——只报告数据中可见的内容
- 对每个发现模式给出 confidence（0.0~1.0）

输出纯 JSON（不要 markdown 包裹）：
{
  "patterns": [
    {
      "category": "missing_capability",
      "error_type": "ImportError",
      "stack_signature": "import X -> ModuleNotFoundError: No module named X",
      "summary": "Agent repeatedly fails to handle missing Python imports",
      "evidence": ["error line 1", "error line 2"],
      "missing_capability": "ability to automatically install missing packages",
      "occurrence_trace_indices": [0, 3, 7],
      "common_files": ["src/main.py"],
      "confidence": 0.9
    }
  ]
}
"""


# ---------------------------------------------------------------------------
# ProblemClassifier
# ---------------------------------------------------------------------------


class ProblemClassifier:
    """使用 LLM 对失败轨迹进行分类，识别 FailurePattern。"""

    def __init__(self, client_factory: Any = None) -> None:
        """初始化。

        Args:
            client_factory: LLM 客户端工厂，签名为 async def(prompt, system) -> str。
        """
        self._client_factory = client_factory

    async def classify(
        self,
        traces: list[ExecutionTrace],
        min_recurrence: int = 3,
    ) -> list[FailurePattern]:
        """分析轨迹列表，返回发现的 FailurePattern。

        Args:
            traces: 执行轨迹列表（至少应包含失败记录）。
            min_recurrence: 最小重复次数阈值。

        Returns:
            发现的失败模式列表。
        """
        failures = [t for t in traces if not t.success]
        if len(failures) < min_recurrence:
            log.info("[classifier] only %d failures, need >= %d", len(failures), min_recurrence)
            return []

        # 先做本地预分组（按 error_type）
        by_type = self._group_by_error_type(failures)

        # 对每组使用 LLM 深入分析
        patterns: list[FailurePattern] = []
        for error_type, group in by_type.items():
            if len(group) < min_recurrence:
                continue

            try:
                group_patterns = await self._analyze_group(error_type, group, traces)
                for p in group_patterns:
                    if p.occurrence_count >= min_recurrence and p.confidence >= 0.5:
                        patterns.append(p)
            except Exception as e:
                log.warning("[classifier] LLM analysis failed for %s: %s", error_type, e)
                # 降级：用本地规则生成基础 pattern
                fallback = self._fallback_classify(error_type, group)
                if fallback and fallback.occurrence_count >= min_recurrence:
                    patterns.append(fallback)

        log.info("[classifier] found %d patterns from %d failures", len(patterns), len(failures))
        return patterns

    # ------------------------------------------------------------------
    # LLM 分析
    # ------------------------------------------------------------------

    async def _analyze_group(
        self,
        error_type: str,
        group: list[ExecutionTrace],
        all_traces: list[ExecutionTrace],
    ) -> list[FailurePattern]:
        """使用 LLM 深入分析一个错误类型分组。"""
        if self._client_factory is None:
            return [self._fallback_classify(error_type, group)]

        # 构建上下文
        trace_summaries = self._build_trace_summaries(group)
        prompt = (
            f"## 错误类型: {error_type}\n\n"
            f"## 执行轨迹（共 {len(group)} 条）\n\n"
            + "\n---\n".join(trace_summaries)
        )

        response = await self._client_factory(
            prompt=prompt,
            system=PROBLEM_CLASSIFY_SYSTEM_PROMPT,
        )

        return self._parse_response(response, group)

    def _build_trace_summaries(self, traces: list[ExecutionTrace]) -> list[str]:
        """为每条轨迹构建简短的文本摘要。"""
        summaries: list[str] = []
        for i, t in enumerate(traces):
            parts = [
                f"### Trace {i} ({t.trace_id})",
                f"Task: {t.task_description[:200]}",
                f"Error: {t.error_message[:300]}",
            ]
            if t.error_stacktrace:
                # 只取堆栈的前 10 行
                stack_lines = t.error_stacktrace.split("\n")[:10]
                parts.append(f"Stack:\n" + "\n".join(stack_lines))
            parts.append(f"Tools used: {', '.join(t.tools_used)}")
            summaries.append("\n".join(parts))
        return summaries

    def _parse_response(
        self,
        response: str,
        group: list[ExecutionTrace],
    ) -> list[FailurePattern]:
        """解析 LLM 返回的 JSON。"""
        # 提取 JSON 对象
        json_str = self._extract_json(response)
        if not json_str:
            log.debug("[classifier] no JSON found in response: %s", response[:200])
            return [self._fallback_classify(group[0].error_type, group)]

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            log.debug("[classifier] invalid JSON: %s", json_str[:200])
            return [self._fallback_classify(group[0].error_type, group)]

        raw_patterns = data.get("patterns", [])
        if not isinstance(raw_patterns, list):
            return []

        patterns: list[FailurePattern] = []
        for rp in raw_patterns:
            try:
                pattern = FailurePattern(
                    error_type=rp.get("error_type", ""),
                    stack_signature=rp.get("stack_signature", ""),
                    occurrence_count=len(rp.get("occurrence_trace_indices", [])),
                    trace_ids=self._resolve_trace_indices(
                        rp.get("occurrence_trace_indices", []), group
                    ),
                    common_files=rp.get("common_files", []),
                    root_cause_summary=rp.get("summary", ""),
                    missing_capability=rp.get("missing_capability", ""),
                    confidence=rp.get("confidence", 0.5),
                )
                patterns.append(pattern)
            except Exception as e:
                log.warning("[classifier] failed to parse pattern: %s", e)

        return patterns

    # ------------------------------------------------------------------
    # 降级分类（纯本地规则）
    # ------------------------------------------------------------------

    def _fallback_classify(
        self, error_type: str, group: list[ExecutionTrace]
    ) -> FailurePattern | None:
        """不使用 LLM 的本地规则分类。"""
        if not group:
            return None

        # 基于堆栈签名进行简单聚类
        signatures: dict[str, list[ExecutionTrace]] = {}
        for t in group:
            sig = self._compute_stack_signature(t.error_stacktrace)
            signatures.setdefault(sig, []).append(t)

        # 取最大的 cluster
        best_sig = max(signatures, key=lambda s: len(signatures[s]))
        best_group = signatures[best_sig]

        if len(best_group) < 2:
            return None

        return FailurePattern(
            error_type=error_type,
            stack_signature=best_sig,
            occurrence_count=len(best_group),
            trace_ids=[t.trace_id for t in best_group],
            common_files=list({f for t in best_group for f in t.files_modified}),
            root_cause_summary=f"Recurring {error_type}: {best_group[0].error_message[:200]}",
            missing_capability=self._infer_missing_capability(error_type),
            confidence=0.4,
        )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_error_type(
        traces: list[ExecutionTrace],
    ) -> dict[str, list[ExecutionTrace]]:
        """按 error_type 分组。"""
        groups: dict[str, list[ExecutionTrace]] = {}
        for t in traces:
            et = t.error_type or "unknown_error"
            groups.setdefault(et, []).append(t)
        return groups

    @staticmethod
    def _compute_stack_signature(stacktrace: str) -> str:
        """计算堆栈的去参数化签名。

        只保留文件路径和函数名，去掉行号和参数值。
        """
        if not stacktrace:
            return "no_stack"

        lines: list[str] = []
        for line in stacktrace.split("\n"):
            line = line.strip()
            # 匹配 Python traceback 行: File "path", line N, in func
            m = re.match(r'File\s+"([^"]+)"(?:,\s*line\s+\d+)?(?:,\s*in\s+(\w+))?', line)
            if m:
                path = m.group(1).split("/")[-1]
                func = m.group(2) or "?"
                lines.append(f"{path}:{func}")
                continue
            # 匹配异常行
            m = re.match(r"(\w+(?:Error|Exception|Warning))(?::\s*(.*))?", line)
            if m:
                lines.append(f"{m.group(1)}")
                continue

        return " -> ".join(lines[:10]) if lines else "no_stack"

    @staticmethod
    def _infer_missing_capability(error_type: str) -> str:
        """从错误类型推断缺失的能力。"""
        mapping = {
            "ImportError": "missing_package_installation",
            "ModuleNotFoundError": "missing_package_installation",
            "SyntaxError": "syntax_correction",
            "NameError": "undefined_variable_detection",
            "TypeError": "type_correctness",
            "AttributeError": "attribute_discovery",
            "FileNotFoundError": "file_path_verification",
            "PermissionError": "permission_handling",
            "TimeoutError": "timeout_handling",
            "ConnectionError": "network_resilience",
        }
        for key, value in mapping.items():
            if key.lower() in error_type.lower():
                return value
        return f"unknown_capability_for_{error_type}"

    @staticmethod
    def _resolve_trace_indices(
        indices: list[int], group: list[ExecutionTrace]
    ) -> list[str]:
        """把 trace 在 group 中的索引转为实际 trace_id。"""
        result: list[str] = []
        for idx in indices:
            if 0 <= idx < len(group):
                result.append(group[idx].trace_id)
        return result

    @staticmethod
    def _extract_json(text: str) -> str:
        """从文本中提取 JSON 对象。"""
        text = str(text).strip()
        # 移除 markdown code block
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]
        return ""
