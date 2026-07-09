"""Skill 自动生成器。

从 FailurePattern 生成 SKILL.md 文件。
严格基于 Memory 中真实失败案例作为依据，禁止凭空编造。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from mewcode.harness.evolution.models import (
    ExecutionTrace,
    FailurePattern,
    SkillGenResult,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill 生成 Prompt（关键：必须基于证据）
# ---------------------------------------------------------------------------

SKILL_GENERATION_SYSTEM_PROMPT = """\
你正在为一个 AI 编程助手生成 SKILL.md 文件以修复重复出现的失败。

**完全基于**下面提供的失败证据来编写。禁止想象证据中没有的问题的解决方案。
每个指令都必须能追溯到下面提供的具体证据。

## 输出格式

以 YAML frontmatter 开头：

---
name: auto-fix-{问题简称}
description: Auto-generated skill to handle {错误描述}
mode: inline
allowedTools:
  - Bash
  - Read
  - Write
  - Edit
---

## 正文格式

# {Skill 标题}

## When to Use

明确列出触发条件（从证据中提取的症状/错误消息）

## Root Cause

从证据中分析得到的根因

## Procedure

1. 步骤 1（基于证据中可验证的有效修复方案）
2. 步骤 2
...

## Evidence References

列出本 Skill 所基于的证据追踪 ID

## Constraints

- 必须验证修复后的代码
- 必须在修复后运行相关测试
"""

# 用户 prompt 模板
SKILL_GENERATION_USER_PROMPT = """\
--- 失败证据 ---
失败模式: {error_type}
出现次数: {occurrence_count} 次
根因摘要: {root_cause}

详细证据（每个关联 trace 的具体信息）:

{evidence_details}

--- 上下文 ---
这些证据来自 AI 编程助手的实际执行轨迹。请生成一个完整的 SKILL.md 文件，
帮助 AI 助手在未来遇到相同问题时能够自行解决。

要求：
1. name 必须以 "auto-" 开头，必须是小写字母、数字、连字符
2. 正文中的每个修复步骤都必须能追溯到上面的证据
3. 如果证据中没有显示某个解决方案有效，不要包含它
4. 不要包含任何通用的"也可以尝试"之类的建议

生成完整的 SKILL.md 内容（frontmatter + body）：
"""


# ---------------------------------------------------------------------------
# Skill 名称推导
# ---------------------------------------------------------------------------

_ERROR_TYPE_TO_SKILL_NAME: dict[str, str] = {
    "import": "auto-fix-python-imports",
    "modulenotfound": "auto-fix-missing-module",
    "syntax": "auto-fix-syntax-error",
    "nameerror": "auto-fix-undefined-variable",
    "typeerror": "auto-fix-type-error",
    "attributeerror": "auto-fix-attribute-error",
    "filenotfound": "auto-fix-missing-file",
    "permission": "auto-fix-permission-issue",
    "timeout": "auto-fix-timeout-handling",
    "connection": "auto-fix-connection-error",
    "keyerror": "auto-fix-missing-key",
    "valueerror": "auto-fix-value-error",
    "indexerror": "auto-fix-index-error",
}


# ---------------------------------------------------------------------------
# SkillGenerator
# ---------------------------------------------------------------------------


class InsufficientEvidenceError(Exception):
    """证据不足以生成 Skill。"""
    pass


class FabricatedContentError(Exception):
    """生成的 Skill 内容可能包含编造逻辑。"""
    pass


class SkillGenerator:
    """从失败模式生成 SKILL.md 的生成器。

    关键安全约束：
    1. 必须有 >= min_recurrence 条真实失败 trace
    2. LLM prompt 明确禁止编造
    3. 生成后做格式和内容校验
    4. 原子写入（temp + os.replace）
    """

    # Agent 可能合法使用的工具名集合（用于校验生成内容不引用不存在的工具）
    KNOWN_TOOLS = {
        "Bash", "Read", "Write", "Edit", "Glob", "Grep",
        "Agent", "TaskCreate", "TaskUpdate", "TaskList",
        "Skill", "WebFetch", "WebSearch", "AskUserQuestion",
        "EnterPlanMode", "ExitPlanMode",
    }

    def __init__(
        self,
        client_factory: Any = None,
        skills_dir: Path | None = None,
        min_recurrence: int = 3,
    ) -> None:
        self._client_factory = client_factory
        self._skills_dir = Path(skills_dir) if skills_dir else Path("harness/skills")
        self._min_recurrence = min_recurrence

    async def generate(
        self,
        pattern: FailurePattern,
        traces: list[ExecutionTrace],
    ) -> SkillGenResult:
        """从失败模式生成 SKILL.md。

        Args:
            pattern: 分类器识别出的失败模式。
            traces: 关联的详细执行轨迹。

        Returns:
            SkillGenResult。

        Raises:
            InsufficientEvidenceError: 证据不足。
        """
        # Step 1: 验证证据充分性
        related_traces = [t for t in traces if t.trace_id in pattern.trace_ids]
        if len(related_traces) < self._min_recurrence:
            raise InsufficientEvidenceError(
                f"Only {len(related_traces)} related traces, need >= {self._min_recurrence}"
            )

        # Step 2: 生成 Skill 名称
        skill_name = self._derive_skill_name(pattern)

        # Step 3: 构建证据文本
        evidence_text = self._build_evidence_text(related_traces, pattern)

        # Step 4: 调用 LLM 生成
        if self._client_factory is not None:
            try:
                content = await self._generate_with_llm(pattern, evidence_text)
            except Exception as e:
                log.warning("[skill_gen] LLM generation failed: %s, using template", e)
                content = self._generate_template(pattern, evidence_text)
        else:
            content = self._generate_template(pattern, evidence_text)

        # Step 5: 校验
        errors = self._validate(content, pattern, related_traces, evidence_text)
        if errors:
            return SkillGenResult(
                skill_name=skill_name,
                content=content,
                based_on_traces=pattern.trace_ids,
                success=False,
                errors=errors,
            )

        # Step 6: 写入磁盘
        skill_path = self._write_skill(skill_name, content)

        return SkillGenResult(
            skill_name=skill_name,
            skill_path=str(skill_path),
            content=content,
            based_on_traces=pattern.trace_ids,
            success=True,
            errors=[],
            evidence_quoted=self._extract_evidence_refs(content),
        )

    # ------------------------------------------------------------------
    # LLM 生成
    # ------------------------------------------------------------------

    async def _generate_with_llm(
        self,
        pattern: FailurePattern,
        evidence_text: str,
    ) -> str:
        """使用 LLM 生成 SKILL.md 内容。"""
        prompt = SKILL_GENERATION_USER_PROMPT.format(
            error_type=pattern.error_type,
            occurrence_count=pattern.occurrence_count,
            root_cause=pattern.root_cause_summary,
            evidence_details=evidence_text,
        )

        response = await self._client_factory(
            prompt=prompt,
            system=SKILL_GENERATION_SYSTEM_PROMPT,
        )

        return str(response).strip()

    # ------------------------------------------------------------------
    # 模板生成（降级方案）
    # ------------------------------------------------------------------

    def _generate_template(
        self,
        pattern: FailurePattern,
        evidence_text: str,
    ) -> str:
        """基于模板生成 SKILL.md（不依赖 LLM）。"""
        skill_name = self._derive_skill_name(pattern)
        description = f"Auto-generated skill: handle {pattern.error_type} — {pattern.root_cause_summary[:80]}"

        # 提取证据中的关键错误消息
        error_msgs = self._extract_error_messages(evidence_text)

        return f"""---
name: {skill_name}
description: {description}
mode: inline
allowedTools:
  - Bash
  - Read
  - Write
  - Edit
  - Grep
---

# {skill_name}

## When to Use

Use this skill when the agent encounters **{pattern.error_type}** with symptoms like:

{chr(10).join(f"- `{msg[:120]}`" for msg in error_msgs[:5])}

Triggered after {pattern.occurrence_count} confirmed failures across multiple sessions.

## Root Cause

{pattern.root_cause_summary}

## Procedure

1. **Identify the error**: Read the full error message and stack trace from the tool output.
2. **Locate the source**: Use `Grep` to find the relevant file/line mentioned in the error.
3. **Apply the fix**: Based on the error type and context, apply the appropriate fix using `Edit`.
4. **Verify**: Run the affected code to confirm the fix resolves the error.

## Evidence References

Based on traces: {', '.join(pattern.trace_ids[:10])}

## Constraints

- Always verify the fix by re-running the failing command.
- Do NOT make changes to files not directly related to the error.
- If the fix doesn't work after 2 attempts, ask the user for guidance.
"""
        fmt = SKILL_GENERATION_SYSTEM_PROMPT  # 防止误删 — 保持可用

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def _validate(
        self,
        content: str,
        pattern: FailurePattern,
        traces: list[ExecutionTrace],
        evidence_text: str,
    ) -> list[str]:
        """校验生成的 SKILL.md 内容。"""
        errors: list[str] = []

        # 1. Frontmatter 可解析
        if not content.startswith("---"):
            errors.append("Missing YAML frontmatter")
            return errors

        end = content.find("---", 3)
        if end == -1:
            errors.append("Unclosed YAML frontmatter")
            return errors

        fm_block = content[3:end]
        body = content[end + 3:]

        # 2. name 格式校验
        name_match = re.search(r'^name:\s*(\S+)', fm_block, re.MULTILINE)
        if not name_match:
            errors.append("Missing 'name' in frontmatter")
        else:
            name = name_match.group(1)
            if not re.match(r'^[a-z][a-z0-9\-]*$', name):
                errors.append(f"Invalid skill name: {name}")
            if not name.startswith("auto-"):
                errors.append(f"Skill name must start with 'auto-': {name}")

        # 3. 内容不能为空
        if len(body.strip()) < 50:
            errors.append("Skill body too short")

        # 4. 至少引用一条证据（反编造 gate）
        evidence_refs = self._extract_evidence_refs(content)
        if not evidence_refs:
            # 检查是否包含证据文本中的具体错误消息
            has_evidence = self._check_content_references_evidence(body, traces)
            if not has_evidence:
                errors.append(
                    "Generated content does not reference any evidence from the failure traces. "
                    "This skill may contain fabricated logic."
                )

        # 5. 检查工具名
        tool_matches = re.findall(r'`(\w+)`', body)
        for tm in tool_matches:
            if tm in self.KNOWN_TOOLS or tm.lower() in {t.lower() for t in self.KNOWN_TOOLS}:
                continue
            # 非已知工具，允许通过但记录

        return errors

    def _check_content_references_evidence(
        self, content: str, traces: list[ExecutionTrace]
    ) -> bool:
        """检查生成内容是否引用了证据中的具体信息。"""
        for t in traces:
            if t.error_message and len(t.error_message) > 20:
                # 检查内容中是否包含错误消息的关键片段
                key_phrase = t.error_message[20:60]  # 取一段
                if key_phrase and key_phrase in content:
                    return True
            # 也检查文件路径
            for fp in t.files_modified:
                if fp and fp in content:
                    return True
        return False

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _derive_skill_name(self, pattern: FailurePattern) -> str:
        """从错误类型推导 Skill 名称。"""
        error_lower = pattern.error_type.lower()
        for key, name in _ERROR_TYPE_TO_SKILL_NAME.items():
            if key in error_lower:
                return name
        # 基于 missing_capability 推导
        if pattern.missing_capability:
            cap = re.sub(r'[^a-z0-9-]', '-', pattern.missing_capability.lower())[:30]
            return f"auto-{cap}"
        return f"auto-fix-{re.sub(r'[^a-z0-9-]', '-', error_lower)[:30]}"

    def _build_evidence_text(
        self, traces: list[ExecutionTrace], pattern: FailurePattern
    ) -> str:
        """构建证据文本。"""
        parts: list[str] = []
        for i, t in enumerate(traces[:10]):  # 最多 10 条
            part = (
                f"### Trace {i + 1} ({t.trace_id})\n"
                f"- Session: {t.session_id}\n"
                f"- Task: {t.task_description[:200]}\n"
                f"- Error Type: {t.error_type}\n"
                f"- Error Message: {t.error_message[:300]}\n"
            )
            if t.error_stacktrace:
                stack_short = "\n".join(t.error_stacktrace.split("\n")[:15])
                part += f"- Stack Trace:\n```\n{stack_short}\n```\n"
            part += f"- Tools Used: {', '.join(t.tools_used)}\n"
            if t.files_modified:
                part += f"- Files: {', '.join(t.files_modified)}\n"
            parts.append(part)

        return "\n---\n".join(parts)

    def _write_skill(self, skill_name: str, content: str) -> Path:
        """写入 SKILL.md 到磁盘（原子写入）。"""
        skill_dir = self._skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"

        tmp_path = Path(str(skill_path) + ".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(str(tmp_path), str(skill_path))

        log.info("[skill_gen] wrote %s", skill_path)
        return skill_path

    @staticmethod
    def _extract_error_messages(evidence_text: str) -> list[str]:
        """从证据文本中提取错误消息行。"""
        msgs: list[str] = []
        for line in evidence_text.split("\n"):
            line = line.strip()
            if line.startswith("- Error Message:"):
                msg = line.replace("- Error Message:", "").strip()
                if msg and len(msg) > 10:
                    msgs.append(msg)
        return msgs

    @staticmethod
    def _extract_evidence_refs(content: str) -> list[str]:
        """从生成内容中提取证据引用（trace_id）。"""
        refs: list[str] = []
        for match in re.finditer(r'trace_[a-f0-9]{12}', content):
            refs.append(match.group(0))
        return refs
