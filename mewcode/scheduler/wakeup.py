"""自步进 Wakeup 调度。

Agent 可以在 Loop 中声明"N 秒后唤醒我"，
用于等待外部条件（CI 完成、部署就绪等）。

缓存感知：
- ≤300 秒：cache warm（prompt 缓存仍有效）
- >300 秒：cache cold（缓存已过期）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# Wakeup delay 的合法范围（秒）
MIN_DELAY = 60
MAX_DELAY = 3600

# Prompt 缓存 TTL
CACHE_TTL = 300


@dataclass
class WakeupTask:
    """一个 wakeup 任务。"""

    id: str
    """唯一标识。"""

    reason: str
    """唤醒原因（用于日志和 UI 展示）。"""

    prompt: str
    """唤醒时注入的提示词。"""

    scheduled_at: str
    """创建时间 ISO 格式。"""

    fire_at: datetime
    """目标触发时间。"""

    cache_status: str = "unknown"
    """缓存状态：warm / cold。"""

    def is_due(self, now: datetime) -> bool:
        """检查是否到期。"""
        return now >= self.fire_at


class WakeupScheduler:
    """管理动态 Wakeup 任务。"""

    def __init__(self) -> None:
        self._tasks: dict[str, WakeupTask] = {}

    def schedule(
        self,
        delay_seconds: int,
        reason: str,
        prompt: str,
    ) -> WakeupTask:
        """创建一个 wakeup 任务。

        Args:
            delay_seconds: 延迟秒数（clamp 到 [60, 3600]）。
            reason: 原因描述。
            prompt: 触发时注入的提示词。

        Returns:
            创建的 WakeupTask。
        """
        # Clamp delay
        original = delay_seconds
        delay_seconds = max(MIN_DELAY, min(MAX_DELAY, delay_seconds))
        if delay_seconds != original:
            log.info(
                "[wakeup] delay clamped: %d → %d",
                original, delay_seconds,
            )

        now = datetime.now(timezone.utc)
        fire_at = now + timedelta(seconds=delay_seconds)

        # 缓存感知
        cache_status = "warm" if delay_seconds <= CACHE_TTL else "cold"
        log.info(
            "[wakeup] scheduled: delay=%ds reason=%s cache=%s fire_at=%s",
            delay_seconds, reason, cache_status, fire_at.isoformat(),
        )

        task = WakeupTask(
            id=uuid.uuid4().hex[:12],
            reason=reason,
            prompt=prompt,
            scheduled_at=now.isoformat(),
            fire_at=fire_at,
            cache_status=cache_status,
        )

        self._tasks[task.id] = task
        return task

    def cancel(self, task_id: str) -> bool:
        """取消 wakeup 任务。

        Returns:
            是否成功取消。
        """
        if task_id in self._tasks:
            del self._tasks[task_id]
            log.info("[wakeup] cancelled: %s", task_id)
            return True
        return False

    def get_due(self, now: datetime | None = None) -> list[WakeupTask]:
        """获取所有到期的 wakeup 任务。"""
        if now is None:
            now = datetime.now(timezone.utc)

        due: list[WakeupTask] = []
        for task in list(self._tasks.values()):
            if task.is_due(now):
                due.append(task)
        return due

    def list_all(self) -> list[WakeupTask]:
        """列出所有活跃的 wakeup 任务。"""
        return list(self._tasks.values())

    def clear(self) -> None:
        """清除所有任务。"""
        self._tasks.clear()
