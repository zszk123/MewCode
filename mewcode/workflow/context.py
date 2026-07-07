"""WorkflowContext — workflow DSL 的核心原语。

Workflow 函数接收一个 WorkflowContext 实例，通过它调用：
- ctx.agent(prompt, ...) — 执行单次 Agent 调用
- ctx.pipeline(items, *stages) — 流水线模式
- ctx.parallel(thunks) — 并发屏障模式
- ctx.phase(title) — 设置当前 phase
- ctx.log(message) — 进度日志
- ctx.budget — Token 预算追踪器
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from pydantic import BaseModel, ValidationError

from mewcode.workflow.journal import Journal
from mewcode.workflow.models import AgentCallRecord, BudgetInfo, StructuredOutputConfig

log = logging.getLogger(__name__)

# 并发 agent() 调用的上限
_MAX_CONCURRENCY = min(16, max(2, (os.cpu_count() or 4) - 2))


@dataclass
class _PhaseState:
    """当前 phase 状态。"""

    title: str = ""


class BudgetExhaustedError(Exception):
    """Token 预算已耗尽。"""

    def __init__(self, spent: int, total: int) -> None:
        self.spent = spent
        self.total = total
        super().__init__(
            f"Budget exhausted: spent {spent:,}/{total:,} tokens"
        )


class WorkflowContext:
    """Workflow 的执行上下文，提供编排原语。

    每个 workflow run 持有一个 WorkflowContext 实例。
    """

    def __init__(
        self,
        *,
        workflow_name: str,
        run_id: str,
        journal: Journal,
        budget: BudgetInfo | None = None,
        agent_factory: Callable[..., Any] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_phase_change: Callable[[str], None] | None = None,
        on_agent_start: Callable[[str, str], None] | None = None,
        on_agent_complete: Callable[[str, str], None] | None = None,
    ) -> None:
        self._workflow_name = workflow_name
        self._run_id = run_id
        self._journal = journal
        self._budget = budget or BudgetInfo()
        self._agent_factory = agent_factory
        self._on_log = on_log
        self._on_phase_change = on_phase_change
        self._on_agent_start = on_agent_start
        self._on_agent_complete = on_agent_complete

        self._phase = _PhaseState()
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    # ------------------------------------------------------------------
    # 公共属性
    # ------------------------------------------------------------------

    @property
    def budget(self) -> BudgetInfo:
        """Token 预算追踪器。"""
        return self._budget

    @property
    def workflow_name(self) -> str:
        return self._workflow_name

    @property
    def run_id(self) -> str:
        return self._run_id

    # ------------------------------------------------------------------
    # agent() — 核心原语
    # ------------------------------------------------------------------

    async def agent(
        self,
        prompt: str,
        *,
        schema: type[BaseModel] | None = None,
        label: str = "",
        phase: str = "",
        model: str | None = None,
        effort: str | None = None,
        isolation: str | None = None,
    ) -> Any:
        """执行一次 Agent 调用。

        Args:
            prompt: 发送给 Agent 的提示词。
            schema: 可选的 Pydantic 模型，用于强制结构化输出。
            label: 显示标签（用于进度展示）。
            phase: 所属 phase（覆盖当前的 ctx.phase() 设置）。
            model: 模型覆盖（None 表示使用默认模型）。
            effort: reasoning effort 覆盖。
            isolation: 隔离模式（"worktree" 表示在 git worktree 中执行）。

        Returns:
            如果没有 schema，返回字符串。
            如果有 schema，返回校验后的 Pydantic 实例。
        """
        if self._budget.is_exhausted:
            raise BudgetExhaustedError(
                spent=self._budget.spent(), total=self._budget.total or 0
            )

        # 计算哈希（用于 journal 缓存）
        prompt_hash = AgentCallRecord.compute_prompt_hash(prompt)
        opts = {
            "schema": schema.__name__ if schema else None,
            "label": label,
            "phase": phase or self._phase.title,
            "model": model,
            "effort": effort,
            "isolation": isolation,
        }
        opts_hash = AgentCallRecord.compute_opts_hash(opts)

        # 检查缓存
        cached = self._journal.lookup(prompt_hash, opts_hash)
        if cached is not None:
            log.info(
                "[workflow] cache hit: agent call %s (label=%s)",
                cached.call_id,
                label or "(none)",
            )
            if schema and cached.result_json:
                try:
                    return schema.model_validate_json(cached.result_json)
                except Exception:
                    pass  # 降级到实际执行
            if not schema and cached.result_json:
                try:
                    data = json.loads(cached.result_json)
                    if isinstance(data, dict) and "text" in data:
                        return data["text"]
                    return cached.result_json
                except Exception:
                    pass

        log.info(
            "[workflow] cache miss: agent call (label=%s), executing",
            label or "(none)",
        )

        call_id = uuid.uuid4().hex[:12]
        effective_phase = phase or self._phase.title
        started_at = _now_iso()

        # 通知进度
        if self._on_agent_start:
            self._on_agent_start(call_id, label or prompt[:60])

        # 写入 running 记录
        record = AgentCallRecord(
            call_id=call_id,
            prompt_sha256=prompt_hash,
            opts_sha256=opts_hash,
            status="running",
            phase=effective_phase,
            label=label,
            started_at=started_at,
        )
        self._journal.append(record)

        # 执行
        try:
            async with self._semaphore:
                if self._agent_factory is None:
                    raise RuntimeError(
                        "agent_factory is not set — workflow engine must provide it"
                    )

                result_text, usage = await self._agent_factory(
                    prompt=prompt,
                    schema=schema,
                    model=model,
                    effort=effort,
                    isolation=isolation,
                )
        except Exception as e:
            # 更新记录为 failed
            record.status = "failed"
            record.error_message = str(e)
            record.completed_at = _now_iso()
            self._journal.update(call_id,
                status="failed",
                error_message=str(e),
                completed_at=record.completed_at,
            )
            raise

        # 更新记录为 completed
        record.status = "completed"
        record.completed_at = _now_iso()
        record.input_tokens = usage.get("input_tokens", 0) if usage else 0
        record.output_tokens = usage.get("output_tokens", 0) if usage else 0

        # 序列化结果
        if schema and isinstance(result_text, BaseModel):
            record.result_json = result_text.model_dump_json()
        else:
            record.result_json = json.dumps({"text": str(result_text)}, ensure_ascii=False)

        self._journal.update(call_id,
            status="completed",
            completed_at=record.completed_at,
            result_json=record.result_json,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
        )

        # 更新预算
        self._budget.consume(record.input_tokens, record.output_tokens)

        # 通知完成
        if self._on_agent_complete:
            self._on_agent_complete(call_id, "completed")

        return result_text

    # ------------------------------------------------------------------
    # pipeline() — 流水线模式
    # ------------------------------------------------------------------

    async def pipeline(
        self,
        items: list[Any],
        *stages: Callable[..., Coroutine[Any, Any, Any]],
    ) -> list[Any]:
        """流水线执行：每个 item 独立流经所有 stage。

        Item A 进入 stage2 时 Item B 可仍在 stage1 — 无同步屏障。
        若某 stage 抛出异常，对应 item 变为 None 并跳过后续 stage。

        Args:
            items: 输入数据项列表。
            *stages: 异步 stage 函数。
                stage1(item) -> result1
                stage2(prev_result, original_item, index) -> result2

        Returns:
            与 items 等长的结果列表（失败项为 None）。
        """
        n = len(items)
        results: list[Any] = [None] * n

        async def _process_one(idx: int, item: Any) -> None:
            current: Any = item
            for stage_idx, stage_fn in enumerate(stages):
                try:
                    if stage_idx == 0:
                        current = await stage_fn(current)
                    else:
                        current = await stage_fn(current, item, idx)
                except Exception as e:
                    log.warning(
                        "[workflow] pipeline item %d failed at stage %d: %s",
                        idx, stage_idx, e,
                    )
                    current = None
                    break
            results[idx] = current

        tasks = [_process_one(i, item) for i, item in enumerate(items)]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results

    # ------------------------------------------------------------------
    # parallel() — 并发屏障模式
    # ------------------------------------------------------------------

    async def parallel(
        self,
        thunks: list[Callable[[], Coroutine[Any, Any, Any]]],
    ) -> list[Any]:
        """并发执行多个任务，等待全部完成后返回。

        单个 thunk 抛出异常 → 对应槽位为 None。
        这是同步屏障：所有 thunk 完成后才返回。

        Args:
            thunks: 异步 thunk 列表（无参数的可调用对象）。

        Returns:
            与 thunks 等长的结果列表（失败项为 None）。
        """
        async def _safe(thunk: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
            try:
                return await thunk()
            except Exception as e:
                log.warning("[workflow] parallel task failed: %s", e)
                return None

        tasks = [_safe(t) for t in thunks]
        return list(await asyncio.gather(*tasks))

    # ------------------------------------------------------------------
    # phase() / log() — 进度控制
    # ------------------------------------------------------------------

    def phase(self, title: str) -> None:
        """设置当前 phase 标题。

        之后发起的 agent() 调用自动归属此 phase。
        """
        self._phase.title = title
        if self._on_phase_change:
            self._on_phase_change(title)

    def log(self, message: str) -> None:
        """输出进度消息。"""
        log.info("[workflow] %s", message)
        if self._on_log:
            self._on_log(message)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
