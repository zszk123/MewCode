"""Completeness Critic — 轻量级遗漏检查。

在 Agent 无工具调用即将结束轮次时，使用独立轻量模型检查：
- 是否有未使用的工具类别？
- 是否有未验证的假设？
- 是否有未确认的副作用？

Critic 默认关闭，需通过 critic.enabled 配置开启。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mewcode.conversation import ConversationManager, Message

log = logging.getLogger(__name__)

# Critic 超时（秒）
CRITIC_TIMEOUT = 8

# 连续抑制阈值
MAX_REPEATED_SUGGESTIONS = 3


CRITIC_PROMPT = """你是一个代码助手质量检查器。请检查以下对话的最后一条助手回复，判断是否有遗漏。

检查维度：
1. **未使用的工具**：是否有明显的工具类别没用到？（例如：有 Bash 但从没执行过命令？有 ReadFile 但没读过关键文件？）
2. **未验证的假设**：助手是否做出了未经证实的假设？（例如："应该能工作" 但没运行测试）
3. **未确认的副作用**：助手是否修改了代码但没有验证修改结果？

只输出一个 JSON 对象：
{"status": "clean", "suggestions": []}
或
{"status": "suggestions", "suggestions": ["建议1", "建议2"]}

不要输出其他内容。不要调用工具。"""


@dataclass
class CriticResult:
    """Critic 检查结果。"""

    status: str  # "clean" | "suggestions"
    suggestions: list[str] = field(default_factory=list)


class CompletenessCritic:
    """轻量级遗漏检查器。"""

    def __init__(
        self,
        *,
        enabled: bool = False,
        agent_factory: Any = None,
    ) -> None:
        self.enabled = enabled
        self._agent_factory = agent_factory
        self._last_suggestions: list[str] = []
        self._repeat_count: int = 0

    async def check(
        self,
        conversation: ConversationManager,
        last_response: str,
    ) -> CriticResult:
        """执行遗漏检查。

        Args:
            conversation: 当前对话管理器。
            last_response: Agent 的最后一条文本回复。

        Returns:
            CriticResult 实例。
        """
        if not self.enabled:
            return CriticResult(status="clean")

        if self._agent_factory is None:
            log.warning("[critic] agent_factory not set, skipping check")
            return CriticResult(status="clean")

        log.info("[critic] checking completeness...")

        try:
            # 使用独立的轻量模型调用
            prompt = (
                f"{CRITIC_PROMPT}\n\n"
                f"最后一条助手回复:\n{last_response[:3000]}\n\n"
                f"对话上下文（最近几轮）:\n"
                f"{self._build_context_summary(conversation)}"
            )

            result_text = await self._agent_factory(
                prompt=prompt,
                schema=None,
                model_hint="haiku",  # 优先用轻量模型
            )
        except Exception as e:
            log.warning("[critic] check failed: %s", e)
            return CriticResult(status="clean")

        # 解析结果
        parsed = self._parse_result(result_text)
        return self._apply_repeat_filter(parsed)

    def _build_context_summary(self, conversation: ConversationManager) -> str:
        """构建对话上下文的简短摘要。"""
        recent = conversation.history[-6:]  # 最近 6 条消息
        lines: list[str] = []
        for msg in recent:
            role = msg.role
            if msg.content:
                text = msg.content[:200]
                lines.append(f"[{role}] {text}")
            if msg.tool_uses:
                tools = [tu.tool_name for tu in msg.tool_uses]
                lines.append(f"[{role}→tools] {', '.join(tools)}")
            if msg.tool_results:
                lines.append(f"[{role}→results] ({len(msg.tool_results)} results)")
        return "\n".join(lines)

    def _parse_result(self, text: str) -> CriticResult:
        """解析 Critic 的 JSON 输出。"""
        import json

        # 尝试提取 JSON
        text = str(text).strip()
        # 移除可能的 markdown code block
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            data = json.loads(text)
            return CriticResult(
                status=data.get("status", "clean"),
                suggestions=data.get("suggestions", []),
            )
        except (json.JSONDecodeError, TypeError):
            log.debug("[critic] failed to parse output: %s", text[:200])
            return CriticResult(status="clean")

    def _apply_repeat_filter(self, result: CriticResult) -> CriticResult:
        """防止相同建议重复注入。"""
        if result.status != "suggestions" or not result.suggestions:
            self._repeat_count = 0
            self._last_suggestions = []
            return result

        # 检查是否与上次建议相同
        current = sorted(result.suggestions)
        if current == sorted(self._last_suggestions):
            self._repeat_count += 1
            log.info(
                "[critic] repeated suggestions (count=%d/%d)",
                self._repeat_count, MAX_REPEATED_SUGGESTIONS,
            )
            if self._repeat_count >= MAX_REPEATED_SUGGESTIONS:
                log.info("[critic] suppressed: same suggestion repeated %d times",
                         self._repeat_count)
                return CriticResult(status="clean")
        else:
            self._repeat_count = 0
            self._last_suggestions = current

        log.info("[critic] result: %d suggestions", len(result.suggestions))
        return result

    def format_suggestions(self, result: CriticResult) -> str:
        """将 Critic 建议格式化为可注入的系统提醒消息。"""
        if result.status != "suggestions" or not result.suggestions:
            return ""

        lines = [
            "<system-reminder>",
            "Here are some suggestions to consider before finishing:",
            "",
        ]
        for i, s in enumerate(result.suggestions, 1):
            lines.append(f"{i}. {s}")
        lines.append("</system-reminder>")
        return "\n".join(lines)
