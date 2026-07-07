"""Workflow 断点恢复逻辑。

断点恢复的核心思路：
1. Workflow 函数是确定性的——相同的输入产生相同的 agent() 调用序列
2. 每次 agent() 调用记录在 Journal 中
3. 中断后重新执行 workflow 函数时，已完成的 agent() 调用命中缓存
4. 第一个未完成的调用开始实际执行

ResumeManager 负责：
- 检测是否有未完成的 run
- 加载 journal 中的缓存映射
- 判断是否进入恢复模式
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mewcode.workflow.journal import JOURNALS_DIR, Journal
from mewcode.workflow.models import AgentCallRecord, WorkflowState

log = logging.getLogger(__name__)


class ResumeState:
    """恢复状态：持有已完成的 agent 调用缓存映射。"""

    def __init__(self) -> None:
        # (prompt_sha256, opts_sha256) -> AgentCallRecord
        self._cache: dict[tuple[str, str], AgentCallRecord] = {}
        self._run_id: str = ""
        self._workflow_name: str = ""
        self._total_cached: int = 0

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def workflow_name(self) -> str:
        return self._workflow_name

    @property
    def total_cached(self) -> int:
        return self._total_cached

    def lookup(self, prompt_sha256: str, opts_sha256: str) -> AgentCallRecord | None:
        """查找缓存。"""
        return self._cache.get((prompt_sha256, opts_sha256))

    def add(self, record: AgentCallRecord) -> None:
        """添加记录到缓存。"""
        key = (record.prompt_sha256, record.opts_sha256)
        self._cache[key] = record

    def is_empty(self) -> bool:
        return len(self._cache) == 0


class ResumeManager:
    """管理 workflow 的断点恢复。"""

    def __init__(self, work_dir: str) -> None:
        self._work_dir = work_dir

    def check_incomplete(
        self, workflow_name: str
    ) -> list[str]:
        """检查指定 workflow 是否有未完成的 run。

        Returns:
            未完成 run 的 run_id 列表。
        """
        journals_dir = Path(self._work_dir) / JOURNALS_DIR / workflow_name
        if not journals_dir.exists():
            return []

        incomplete: list[str] = []
        for jf in journals_dir.glob("*.jsonl"):
            run_id = jf.stem
            journal = Journal.load(self._work_dir, workflow_name, run_id)
            if journal is None:
                continue
            try:
                incomplete_runs = journal.get_incomplete_runs()
                if incomplete_runs:
                    incomplete.extend(incomplete_runs)
            finally:
                journal.close()

        return incomplete

    def build_resume_state(
        self,
        workflow_name: str,
        run_id: str | None = None,
    ) -> ResumeState | None:
        """构建恢复状态。

        如果指定了 run_id，加载该 run 的缓存。
        否则选择最新的未完成 run。

        Returns:
            ResumeState（有缓存可恢复），或 None（无可恢复的状态）。
        """
        journals_dir = Path(self._work_dir) / JOURNALS_DIR / workflow_name
        if not journals_dir.exists():
            return None

        # 选择目标 journal
        target_run_id = run_id
        if target_run_id is None:
            # 选择最新的未完成 run
            incomplete = self.check_incomplete(workflow_name)
            if not incomplete:
                return None
            target_run_id = incomplete[-1]  # 最新

        journal = Journal.load(self._work_dir, workflow_name, target_run_id)
        if journal is None:
            return None

        try:
            state = ResumeState()
            state._run_id = target_run_id
            state._workflow_name = workflow_name

            records = journal.get_all_records()
            for record in records:
                if record.status == "completed":
                    state.add(record)

            state._total_cached = len(state._cache)

            if state.is_empty():
                log.info(
                    "[resume] no completed calls found in run %s, "
                    "starting fresh",
                    target_run_id,
                )
                return None

            log.info(
                "[resume] built resume state for '%s' run %s: "
                "%d cached calls",
                workflow_name, target_run_id, state._total_cached,
            )
            return state

        finally:
            journal.close()

    def clear_completed_runs(self, workflow_name: str) -> int:
        """清理指定 workflow 的所有已完成 run 的 journal 文件。

        Returns:
            删除的文件数。
        """
        journals_dir = Path(self._work_dir) / JOURNALS_DIR / workflow_name
        if not journals_dir.exists():
            return 0

        removed = 0
        for jf in journals_dir.glob("*.jsonl"):
            run_id = jf.stem
            journal = Journal.load(self._work_dir, workflow_name, run_id)
            if journal is None:
                continue
            try:
                incomplete = journal.get_incomplete_runs()
                if not incomplete:
                    journal.close()
                    jf.unlink()
                    removed += 1
                    log.info("[resume] cleared completed run %s", run_id)
            finally:
                try:
                    journal.close()
                except Exception:
                    pass

        # 清理空目录
        try:
            remaining = list(journals_dir.glob("*.jsonl"))
            if not remaining:
                journals_dir.rmdir()
        except OSError:
            pass

        return removed


# ---------------------------------------------------------------------------
# 恢复辅助：在 WorkflowContext 中注入缓存查找
# ---------------------------------------------------------------------------


class CachingAgentCallMixin:
    """为 WorkflowContext.agent() 提供缓存查找能力。

    在恢复模式下，agent() 调用前先查 ResumeState 缓存。
    """

    def __init__(self, resume_state: ResumeState | None = None) -> None:
        self._resume_state = resume_state
        self._cache_hits = 0
        self._cache_misses = 0

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    @property
    def cache_misses(self) -> int:
        return self._cache_misses

    def try_cache(
        self, prompt_sha256: str, opts_sha256: str
    ) -> AgentCallRecord | None:
        """尝试从恢复缓存中获取记录。

        Returns:
            命中的 AgentCallRecord，或 None。
        """
        if self._resume_state is None:
            self._cache_misses += 1
            return None

        record = self._resume_state.lookup(prompt_sha256, opts_sha256)
        if record is not None:
            self._cache_hits += 1
            log.info("[resume] cache hit: agent call %s", record.call_id)
        else:
            self._cache_misses += 1
            log.info("[resume] cache miss: executing new agent call")

        return record
