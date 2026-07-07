"""Workflow 内建循环模式。

提供三种迭代终止条件：
- loop_until_count  — 达到目标数量
- loop_until_budget — token 预算耗尽
- loop_until_dry    — 连续 N 轮无新发现
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, TypeVar

from mewcode.workflow.context import BudgetExhaustedError, WorkflowContext

log = logging.getLogger(__name__)

T = TypeVar("T")

# 默认每轮调用的最低 token 预算
DEFAULT_MIN_BUDGET_PER_CALL = 50_000

# 默认连续无新结果轮数阈值
DEFAULT_DRY_THRESHOLD = 2

# 最大轮数保护（防止无限循环）
MAX_LOOP_ITERATIONS = 200


async def loop_until_count(
    ctx: WorkflowContext,
    target: int,
    fn: Callable[[], Coroutine[Any, Any, list[T]]],
    *,
    max_iterations: int = MAX_LOOP_ITERATIONS,
    dry_protection: int = 3,
) -> list[T]:
    """循环调用 fn()，累积结果直到达到 target 数量。

    当 fn() 连续 dry_protection 轮返回空列表时提前终止。

    Args:
        ctx: WorkflowContext 实例。
        target: 目标结果数量。
        fn: 每轮调用的异步函数，返回结果列表。
        max_iterations: 最大轮数保护。
        dry_protection: 连续空结果轮数阈值。

    Returns:
        累积的结果列表（可能少于 target）。
    """
    results: list[T] = []
    dry_rounds = 0

    for iteration in range(1, max_iterations + 1):
        if ctx.budget.is_exhausted:
            log.info(
                "[loop_until_count] budget exhausted at iteration %d, "
                "collected %d/%d results",
                iteration, len(results), target,
            )
            break

        ctx.log(f"[loop_until_count] iteration {iteration}: "
                f"{len(results)}/{target} results collected")

        try:
            batch = await fn()
        except BudgetExhaustedError:
            log.info(
                "[loop_until_count] budget exhausted at iteration %d",
                iteration,
            )
            break

        if not batch:
            dry_rounds += 1
            log.info(
                "[loop_until_count] iteration %d: empty batch, "
                "dry rounds: %d/%d",
                iteration, dry_rounds, dry_protection,
            )
            if dry_rounds >= dry_protection:
                log.info(
                    "[loop_until_count] stopping: %d consecutive empty rounds",
                    dry_rounds,
                )
                break
        else:
            dry_rounds = 0
            results.extend(batch)
            if len(results) >= target:
                log.info(
                    "[loop_until_count] target reached: %d results",
                    len(results),
                )
                break

    return results


async def loop_until_budget(
    ctx: WorkflowContext,
    fn: Callable[[], Coroutine[Any, Any, list[T]]],
    *,
    min_budget_per_call: int = DEFAULT_MIN_BUDGET_PER_CALL,
    max_iterations: int = MAX_LOOP_ITERATIONS,
) -> list[T]:
    """循环调用 fn()，直到 token 预算不足以支持下一轮。

    Args:
        ctx: WorkflowContext 实例。
        fn: 每轮调用的异步函数，返回结果列表。
        min_budget_per_call: 每轮最低 token 预算（低于此值停止）。
        max_iterations: 最大轮数保护。

    Returns:
        累积的结果列表。
    """
    results: list[T] = []

    for iteration in range(1, max_iterations + 1):
        remaining = ctx.budget.remaining()

        if ctx.budget.total is not None and remaining < min_budget_per_call:
            log.info(
                "[loop_until_budget] stopping: remaining %d < min %d tokens",
                remaining, min_budget_per_call,
            )
            break

        if ctx.budget.is_exhausted:
            log.info(
                "[loop_until_budget] budget exhausted at iteration %d",
                iteration,
            )
            break

        ctx.log(f"[loop_until_budget] iteration {iteration}: "
                f"{len(results)} results, "
                f"{remaining if ctx.budget.total else '∞'} tokens remaining")

        try:
            batch = await fn()
        except BudgetExhaustedError:
            log.info(
                "[loop_until_budget] budget exhausted at iteration %d",
                iteration,
            )
            break

        if batch:
            results.extend(batch)
        else:
            log.info(
                "[loop_until_budget] iteration %d: empty batch, stopping",
                iteration,
            )
            break

    return results


async def loop_until_dry(
    ctx: WorkflowContext,
    fn: Callable[[], Coroutine[Any, Any, list[T]]],
    is_new: Callable[[T, set[Any]], bool],
    *,
    dry_threshold: int = DEFAULT_DRY_THRESHOLD,
    max_iterations: int = MAX_LOOP_ITERATIONS,
) -> list[T]:
    """循环调用 fn()，直到连续 dry_threshold 轮无新结果。

    适合"发现型"任务（找 bug、审计问题、边界情况），
    你不知道总共有多少结果，但知道何时停止发现新东西。

    Args:
        ctx: WorkflowContext 实例。
        fn: 每轮调用的异步函数，返回结果列表。
        is_new: 判定函数 (result, seen_set) -> bool。
        dry_threshold: 连续无新结果轮数阈值（默认 2）。
        max_iterations: 最大轮数保护。

    Returns:
        累积的唯一点结果列表。
    """
    results: list[T] = []
    seen: set[Any] = set()
    dry_rounds = 0

    for iteration in range(1, max_iterations + 1):
        if ctx.budget.is_exhausted:
            log.info(
                "[loop_until_dry] budget exhausted at iteration %d",
                iteration,
            )
            break

        remaining = ctx.budget.remaining() if ctx.budget.total else "∞"
        ctx.log(f"[loop_until_dry] iteration {iteration}: "
                f"{len(results)} unique results, "
                f"dry: {dry_rounds}/{dry_threshold}, "
                f"{remaining} tokens remaining")

        try:
            batch = await fn()
        except BudgetExhaustedError:
            log.info(
                "[loop_until_dry] budget exhausted at iteration %d",
                iteration,
            )
            break

        if not batch:
            dry_rounds += 1
            if dry_rounds >= dry_threshold:
                log.info(
                    "[loop_until_dry] stopping: %d consecutive empty rounds",
                    dry_rounds,
                )
                break
            continue

        fresh: list[T] = []
        for item in batch:
            if is_new(item, seen):
                fresh.append(item)
                # 用元组/字符串作为 set key
                try:
                    seen.add(_make_hashable(item))
                except Exception:
                    seen.add(id(item))

        if fresh:
            dry_rounds = 0
            results.extend(fresh)
            log.info(
                "[loop_until_dry] iteration %d: %d new results",
                iteration, len(fresh),
            )
        else:
            dry_rounds += 1
            log.info(
                "[loop_until_dry] iteration %d: no new results, "
                "dry: %d/%d",
                iteration, dry_rounds, dry_threshold,
            )
            if dry_rounds >= dry_threshold:
                log.info(
                    "[loop_until_dry] stopping: %d consecutive dry rounds",
                    dry_rounds,
                )
                break

    return results


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _make_hashable(item: Any) -> Any:
    """尝试将 item 转为可哈希类型。"""
    if isinstance(item, (str, int, float, bool, bytes, tuple)):
        return item
    if hasattr(item, "__dict__"):
        return tuple(sorted(item.__dict__.items()))
    return str(item)
