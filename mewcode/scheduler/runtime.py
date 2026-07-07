"""后台调度运行时。

SchedulerRuntime 每 60 秒检查一次到期任务，触发后注入 Agent 会话。
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Callable

from mewcode.scheduler.store import CronJob, CronStore
from mewcode.scheduler.wakeup import WakeupScheduler

log = logging.getLogger(__name__)

# 检查间隔（秒）
_CHECK_INTERVAL = 60

# 抖动范围（秒），避免所有任务同时触发
_JITTER_RANGE = 90


class SchedulerRuntime:
    """后台调度运行时。

    在 MewCode App 启动时创建，作为后台 asyncio.Task 运行。
    """

    def __init__(
        self,
        cron_store: CronStore,
        wakeup_scheduler: WakeupScheduler | None = None,
        *,
        on_fire: Callable[[CronJob], None] | None = None,
    ) -> None:
        self._store = cron_store
        self._wakeup = wakeup_scheduler
        self._on_fire = on_fire
        self._task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动调度循环（后台任务）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("[scheduler] runtime started (interval=%ds)", _CHECK_INTERVAL)

    async def shutdown(self) -> None:
        """优雅关闭。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("[scheduler] runtime shutdown complete")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """调度主循环。"""
        while self._running:
            try:
                await self._check_and_fire()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("[scheduler] error in check cycle: %s", e)

            # 等待下一个检查周期
            try:
                await asyncio.sleep(_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _check_and_fire(self) -> None:
        """检查到期任务并触发。"""
        now = datetime.now(timezone.utc)

        # 1. Cron 任务
        log.debug("[scheduler] checking due tasks at %s", now.isoformat())
        due_jobs = self._store.get_due(now)

        for job in due_jobs:
            # 添加抖动：一次性任务 ±90 秒随机偏移
            if not job.recurring:
                jitter = random.randint(-_JITTER_RANGE, _JITTER_RANGE)
                if jitter > 0:
                    log.debug("[scheduler] delaying one-shot job %s by %ds", job.id, jitter)
                    continue  # 下次检查时触发

            log.info("[scheduler] firing job %s: %s", job.id, job.prompt[:80])

            # 标记触发
            self._store.mark_fired(job.id)

            # 回调
            if self._on_fire:
                try:
                    self._on_fire(job)
                except Exception as e:
                    log.error("[scheduler] on_fire callback error: %s", e)

        # 2. Wakeup 任务
        if self._wakeup:
            due_wakeups = self._wakeup.get_due(now)
            for wk in due_wakeups:
                log.info("[scheduler] firing wakeup: %s", wk.reason)
                self._wakeup.cancel(wk.id)
                if self._on_fire:
                    try:
                        # 将 wakeup 转为 CronJob 形式传递
                        wakeup_job = CronJob(
                            id=wk.id,
                            cron="",
                            prompt=wk.prompt,
                            recurring=False,
                            created_at=wk.scheduled_at,
                        )
                        self._on_fire(wakeup_job)
                    except Exception as e:
                        log.error("[scheduler] wakeup callback error: %s", e)

    # ------------------------------------------------------------------
    # 注入给 Agent
    # ------------------------------------------------------------------

    def inject_job(self, job: CronJob) -> str:
        """将任务转为可注入 Agent 会话的系统消息文本。

        Returns:
            格式化的系统提醒消息。
        """
        if job.recurring:
            return (
                f"<system-reminder>\n"
                f"Scheduled recurring task fired: {job.prompt}\n"
                f"Cron: {job.cron}\n"
                f"</system-reminder>"
            )
        else:
            return (
                f"<system-reminder>\n"
                f"Scheduled one-shot task fired: {job.prompt}\n"
                f"</system-reminder>"
            )
