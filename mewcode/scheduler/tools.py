"""Scheduler Agent Tools — 暴露给 Agent 的调度管理工具。

- CronCreateTool: 创建定时任务
- CronDeleteTool: 删除定时任务
- CronListTool: 列出所有定时任务
- ScheduleWakeupTool: 动态自步进唤醒
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, Field

from mewcode.scheduler.cron import CronExpression, CronParseError
from mewcode.scheduler.store import CronJob, CronStore
from mewcode.scheduler.wakeup import WakeupScheduler
from mewcode.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CronCreateTool
# ---------------------------------------------------------------------------


class CronCreateParams(BaseModel):
    cron: str = Field(..., description="5-field cron expression, e.g. '0 9 * * 1-5'")
    prompt: str = Field(..., description="Prompt to inject when the job fires")
    recurring: bool = Field(default=True, description="Whether this is a recurring job")
    durable: bool = Field(default=False, description="Whether to persist across restarts")


class CronCreateTool(Tool):
    """Create a scheduled cron job."""

    name = "CronCreate"
    description = (
        "Create a scheduled cron job. Uses 5-field cron expressions "
        "(minute hour day-of-month month day-of-week) in local timezone. "
        "Set recurring=false for one-shot tasks."
    )
    params_model = CronCreateParams
    category = "write"
    is_concurrency_safe = True

    def __init__(self, cron_store: CronStore) -> None:
        self._store = cron_store

    async def execute(self, params: CronCreateParams) -> ToolResult:
        # 验证 cron 表达式
        try:
            expr = CronExpression.parse(params.cron)
            if not expr.validate():
                return ToolResult(
                    output=f"Error: cron expression '{params.cron}' is valid but "
                           f"has no matching times",
                    is_error=True,
                )
        except CronParseError as e:
            return ToolResult(
                output=f"Error: invalid cron expression: {e}",
                is_error=True,
            )

        # 创建任务
        job = CronJob(
            id=uuid.uuid4().hex[:12],
            cron=params.cron,
            prompt=params.prompt,
            recurring=params.recurring,
            durable=params.durable,
        )

        self._store.add(job)

        next_fire = job.get_next_fire()
        next_str = next_fire.isoformat() if next_fire else "never"

        return ToolResult(
            output=f"Cron job created successfully.\n"
                   f"ID: {job.id}\n"
                   f"Cron: {params.cron}\n"
                   f"Recurring: {params.recurring}\n"
                   f"Durable: {params.durable}\n"
                   f"Next fire: {next_str}"
        )


# ---------------------------------------------------------------------------
# CronDeleteTool
# ---------------------------------------------------------------------------


class CronDeleteParams(BaseModel):
    id: str = Field(..., description="ID of the cron job to delete")


class CronDeleteTool(Tool):
    """Delete a scheduled cron job."""

    name = "CronDelete"
    description = "Delete a scheduled cron job by its ID."
    params_model = CronDeleteParams
    category = "write"
    is_concurrency_safe = True

    def __init__(self, cron_store: CronStore) -> None:
        self._store = cron_store

    async def execute(self, params: CronDeleteParams) -> ToolResult:
        removed = self._store.remove(params.id)
        if removed:
            return ToolResult(output=f"Cron job '{params.id}' deleted successfully.")
        else:
            return ToolResult(
                output=f"Error: no cron job found with ID '{params.id}'",
                is_error=True,
            )


# ---------------------------------------------------------------------------
# CronListTool
# ---------------------------------------------------------------------------


class CronListParams(BaseModel):
    pass


class CronListTool(Tool):
    """List all scheduled cron jobs."""

    name = "CronList"
    description = "List all active scheduled cron jobs."
    params_model = CronListParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, cron_store: CronStore) -> None:
        self._store = cron_store

    async def execute(self, params: CronListParams) -> ToolResult:
        jobs = self._store.list()
        if not jobs:
            return ToolResult(output="No active cron jobs.")

        lines = [f"Active cron jobs ({len(jobs)}):", ""]
        for j in jobs:
            next_fire = j.get_next_fire()
            next_str = next_fire.isoformat() if next_fire else "n/a"
            job_type = "recurring" if j.recurring else "one-shot"
            durable_str = "durable" if j.durable else "session-only"
            lines.append(
                f"  [{j.id}] {j.cron} — {job_type}, {durable_str}\n"
                f"    Prompt: {j.prompt[:100]}\n"
                f"    Next fire: {next_str}"
            )

        return ToolResult(output="\n".join(lines))


# ---------------------------------------------------------------------------
# ScheduleWakeupTool
# ---------------------------------------------------------------------------


class ScheduleWakeupParams(BaseModel):
    delaySeconds: int = Field(
        ...,
        ge=60,
        le=3600,
        description="Seconds from now to wake up. Clamped to [60, 3600]."
    )
    reason: str = Field(..., description="One sentence describing why (shown in UI)")
    prompt: str = Field(..., description="Prompt to inject on wake-up")


class ScheduleWakeupTool(Tool):
    """Schedule a self-paced wakeup for the agent loop."""

    name = "ScheduleWakeup"
    description = (
        "Schedule a wake-up from now. The agent loop will be "
        "re-entered with the given prompt after the delay. "
        "Use for waiting on external conditions (CI, deploy, etc.). "
        "Delays ≤300s keep the prompt cache warm."
    )
    params_model = ScheduleWakeupParams
    category = "write"
    is_concurrency_safe = True

    def __init__(self, wakeup_scheduler: WakeupScheduler) -> None:
        self._wakeup = wakeup_scheduler

    async def execute(self, params: ScheduleWakeupParams) -> ToolResult:
        task = self._wakeup.schedule(
            delay_seconds=params.delaySeconds,
            reason=params.reason,
            prompt=params.prompt,
        )

        return ToolResult(
            output=f"Wakeup scheduled.\n"
                   f"ID: {task.id}\n"
                   f"Delay: {params.delaySeconds}s\n"
                   f"Cache status: {task.cache_status}\n"
                   f"Fire at: {task.fire_at.isoformat()}\n"
                   f"Reason: {params.reason}"
        )
