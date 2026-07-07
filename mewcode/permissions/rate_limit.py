"""工具级速率限制。

使用滑动窗口算法，防止 Agent 在短时间内过度调用同一工具。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# 默认每工具每分钟最大调用次数
DEFAULT_MAX_PER_MINUTE = 30

# 默认按工具的特殊限制
DEFAULT_PER_TOOL_LIMITS: dict[str, int] = {
    "Bash": 10,
    "WriteFile": 20,
}


class RateLimiter:
    """工具级滑动窗口速率限制器。

    按工具名分别追踪调用时间戳，1 分钟滑动窗口。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        default_max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
        per_tool_limits: dict[str, int] | None = None,
    ) -> None:
        self.enabled = enabled
        self.default_max = default_max_per_minute
        self.per_tool_limits = per_tool_limits or dict(DEFAULT_PER_TOOL_LIMITS)
        # tool_name -> deque of timestamps
        self._windows: dict[str, deque[float]] = {}

    def acquire(self, tool_name: str) -> bool:
        """尝试获取一次调用许可。

        Args:
            tool_name: 工具名称。

        Returns:
            True = 允许调用，False = 超限。
        """
        if not self.enabled:
            return True

        now = time.monotonic()
        max_calls = self.per_tool_limits.get(tool_name, self.default_max)

        if tool_name not in self._windows:
            self._windows[tool_name] = deque()

        window = self._windows[tool_name]

        # 清理窗口外的旧记录
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= max_calls:
            log.warning(
                "[rate_limit] %s: limit exceeded (%d/%d per minute)",
                tool_name, len(window), max_calls,
            )
            return False

        window.append(now)
        return True

    def get_usage(self, tool_name: str) -> tuple[int, int]:
        """获取某工具的当前使用情况。

        Returns:
            (当前调用次数, 每分钟上限)
        """
        max_calls = self.per_tool_limits.get(tool_name, self.default_max)

        if tool_name not in self._windows:
            return (0, max_calls)

        window = self._windows[tool_name]
        cutoff = time.monotonic() - 60.0
        # 清理过期记录（不修改原 deque，只计数）
        count = sum(1 for t in window if t >= cutoff)
        return (count, max_calls)

    def reset(self, tool_name: str | None = None) -> None:
        """重置限流状态。

        Args:
            tool_name: 指定工具名（None = 全部重置）。
        """
        if tool_name is None:
            self._windows.clear()
        elif tool_name in self._windows:
            del self._windows[tool_name]

    def get_status_text(self, tool_name: str) -> str:
        """获取限流状态描述文本。"""
        count, max_calls = self.get_usage(tool_name)
        return f"{tool_name}: {count}/{max_calls} per minute"
