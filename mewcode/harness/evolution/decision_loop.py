"""进化决策循环 — 6 阶段自主进化主控。

严格按顺序执行：
1. Read Memory     — 读取执行轨迹
2. Problem Classify — 分类失败模式
3. Write Code      — 生成 Skill
4. Self-test Eval  — 评估效果
5. Keep/Rollback   — 决策保留或回滚
6. Archive         — 归档至 Memory
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

from mewcode.harness.evolution.models import (
    EvalResult,
    EvolutionCycle,
    EvolutionRecord,
    EvolutionStatus,
    ExecutionTrace,
    FailurePattern,
    SkillGenResult,
    SkipEvolutionError,
)
from mewcode.harness.evolution.backup import BackupManager
from mewcode.harness.evolution.trace_store import ExecutionTraceStore
from mewcode.harness.evolution.skill_meta import SkillMetaManager
from mewcode.harness.evolution.problem_classifier import ProblemClassifier
from mewcode.harness.evolution.skill_generator import (
    SkillGenerator,
    InsufficientEvidenceError,
)
from mewcode.harness.evolution.evaluator import EvolutionEvaluator

log = logging.getLogger(__name__)


class EvolutionDecisionLoop:
    """6 阶段自主进化决策循环。

    用法:
        loop = EvolutionDecisionLoop(
            trace_store=store,
            classifier=classifier,
            skill_gen=generator,
            evaluator=evaluator,
            backup_mgr=backup_mgr,
            skill_meta_mgr=meta_mgr,
            min_traces=30,
            max_traces=50,
            min_recurrence=3,
        )
        record = await loop.run()
    """

    def __init__(
        self,
        trace_store: ExecutionTraceStore,
        classifier: ProblemClassifier,
        skill_generator: SkillGenerator,
        evaluator: EvolutionEvaluator,
        backup_manager: BackupManager,
        skill_meta_manager: SkillMetaManager,
        *,
        min_traces: int = 30,
        max_traces: int = 50,
        min_recurrence: int = 3,
    ) -> None:
        self._trace_store = trace_store
        self._classifier = classifier
        self._skill_gen = skill_generator
        self._evaluator = evaluator
        self._backup_mgr = backup_manager
        self._skill_meta_mgr = skill_meta_manager
        self._min_traces = min_traces
        self._max_traces = max_traces
        self._min_recurrence = min_recurrence

        self._last_evolution: EvolutionRecord | None = None
        self._history: list[EvolutionRecord] = []

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def run(self) -> EvolutionRecord | None:
        """执行 6 阶段进化循环。

        Returns:
            EvolutionRecord 或 None（被跳过时）。
        """
        cycle = EvolutionCycle()

        try:
            # ── Phase 1: READ MEMORY ────────────────────────────
            cycle.status = EvolutionStatus.READING
            traces = await self._phase_read_memory(cycle)
            if traces is None:
                raise SkipEvolutionError("Insufficient traces")

            # ── Phase 2: PROBLEM CLASSIFICATION ─────────────────
            cycle.status = EvolutionStatus.CLASSIFYING
            patterns = await self._phase_classify(traces, cycle)
            if not patterns:
                raise SkipEvolutionError("No actionable patterns found")

            # ── Phase 3: WRITE CODE ─────────────────────────────
            cycle.status = EvolutionStatus.WRITING
            gen_results = await self._phase_write_code(patterns, traces, cycle)
            if not gen_results:
                raise SkipEvolutionError("No skills generated")

            # ── Phase 4: SELF-TEST EVALUATION ───────────────────
            cycle.status = EvolutionStatus.EVALUATING
            eval_result = await self._phase_evaluate(traces, gen_results, cycle)

            # ── Phase 5: KEEP or ROLLBACK ───────────────────────
            cycle.was_kept = self._phase_decide(eval_result, gen_results, cycle)

            # ── Phase 6: ARCHIVE TO MEMORY ──────────────────────
            record = self._phase_archive(
                traces, patterns, gen_results, eval_result, cycle,
            )

            cycle.status = EvolutionStatus.COMPLETED
            cycle.ended_at = time.time()
            return record

        except SkipEvolutionError as e:
            cycle.status = EvolutionStatus.SKIPPED
            cycle.ended_at = time.time()
            log.info("[decision_loop] skipped: %s", e)
            return self._make_skip_record(traces_read=cycle.traces_read, reason=str(e))

        except Exception as e:
            cycle.status = EvolutionStatus.FAILED
            cycle.ended_at = time.time()
            log.exception("[decision_loop] failed: %s", e)
            # 尝试回滚
            if cycle.backup_id:
                self._backup_mgr.rollback(cycle.backup_id)
            return self._make_error_record(error=str(e))

    # ------------------------------------------------------------------
    # Phase 1: Read Memory
    # ------------------------------------------------------------------

    async def _phase_read_memory(
        self, cycle: EvolutionCycle
    ) -> list[ExecutionTrace] | None:
        """读取最近 30~50 条执行轨迹。"""
        total = self._trace_store.count()
        log.info("[decision_loop] Phase 1: total traces=%d, threshold=%d", total, self._min_traces)

        if total < self._min_traces:
            log.info("[decision_loop] insufficient traces (%d < %d), skipping", total, self._min_traces)
            return None

        traces = self._trace_store.get_latest(self._max_traces)
        cycle.traces_read = len(traces)
        log.info("[decision_loop] Phase 1: read %d traces", len(traces))

        # 确保至少有 min_traces 条
        if len(traces) < self._min_traces:
            return None

        return traces

    # ------------------------------------------------------------------
    # Phase 2: Problem Classification
    # ------------------------------------------------------------------

    async def _phase_classify(
        self,
        traces: list[ExecutionTrace],
        cycle: EvolutionCycle,
    ) -> list[FailurePattern]:
        """分类失败模式。"""
        log.info("[decision_loop] Phase 2: classifying %d traces", len(traces))
        patterns = await self._classifier.classify(traces, self._min_recurrence)

        # 检查是否有 pattern 在最近 3 轮中已处理过
        actionable: list[FailurePattern] = []
        for p in patterns:
            if not self._skill_meta_mgr.has_recent_evolution_for_pattern(
                p.stack_signature, within_cycles=3
            ):
                actionable.append(p)
            else:
                log.info("[decision_loop] skipping pattern %s (recently handled)", p.pattern_id)

        cycle.patterns_found = actionable
        log.info("[decision_loop] Phase 2: found %d patterns (%d actionable)", len(patterns), len(actionable))
        return actionable

    # ------------------------------------------------------------------
    # Phase 3: Write Code
    # ------------------------------------------------------------------

    async def _phase_write_code(
        self,
        patterns: list[FailurePattern],
        traces: list[ExecutionTrace],
        cycle: EvolutionCycle,
    ) -> list[SkillGenResult]:
        """为每个模式生成 Skill。"""
        log.info("[decision_loop] Phase 3: generating skills for %d patterns", len(patterns))

        # 备份 skills 目录
        backup_id = self._backup_mgr.backup_directory(
            self._skill_gen._skills_dir, cycle.cycle_id
        )
        cycle.backup_id = backup_id

        results: list[SkillGenResult] = []
        for pattern in patterns:
            try:
                result = await self._skill_gen.generate(pattern, traces)
                if result.success:
                    # 注册到 skill_meta
                    self._skill_meta_mgr.add_skill(
                        skill_name=result.skill_name,
                        description=f"Auto-generated: handle {pattern.error_type}",
                        trace_ids=result.based_on_traces,
                        evolution_id=cycle.cycle_id,
                        failure_patterns=[pattern.error_type],
                    )
                    results.append(result)
                    log.info("[decision_loop] Phase 3: generated skill '%s'", result.skill_name)
                else:
                    log.warning(
                        "[decision_loop] Phase 3: skill '%s' generation failed: %s",
                        result.skill_name, result.errors,
                    )
            except InsufficientEvidenceError as e:
                log.info("[decision_loop] Phase 3: skipping pattern %s: %s", pattern.pattern_id, e)
            except Exception as e:
                log.error("[decision_loop] Phase 3: error generating skill: %s", e)

        cycle.skills_generated = results
        log.info("[decision_loop] Phase 3: generated %d skills", len(results))
        return results

    # ------------------------------------------------------------------
    # Phase 4: Self-test Evaluation
    # ------------------------------------------------------------------

    async def _phase_evaluate(
        self,
        traces: list[ExecutionTrace],
        gen_results: list[SkillGenResult],
        cycle: EvolutionCycle,
    ) -> EvalResult:
        """评估进化效果。"""
        log.info("[decision_loop] Phase 4: evaluating")
        new_skill_names = [r.skill_name for r in gen_results if r.success]
        result = await self._evaluator.evaluate(
            traces_before=traces,
            new_skills=new_skill_names,
            replay_enabled=False,  # 默认不实际重放，使用估计
        )
        cycle.eval_result = result
        log.info(
            "[decision_loop] Phase 4: success_rate %.2f→%.2f, tokens %+.1f%%, decision=%s",
            result.success_rate_before, result.success_rate_after,
            result.token_change_pct * 100, result.decision,
        )
        return result

    # ------------------------------------------------------------------
    # Phase 5: Keep or Rollback
    # ------------------------------------------------------------------

    def _phase_decide(
        self,
        eval_result: EvalResult,
        gen_results: list[SkillGenResult],
        cycle: EvolutionCycle,
    ) -> bool:
        """决策保留或回滚。"""
        log.info("[decision_loop] Phase 5: deciding")

        if eval_result.should_keep:
            # Keep: 确认备份
            log.info("[decision_loop] Phase 5: KEEP — committing backups")
            if cycle.backup_id:
                self._backup_mgr.commit(cycle.backup_id)
            cycle.was_kept = True
            return True
        else:
            # Rollback: 恢复文件 + 移除 Skill 条目
            log.info("[decision_loop] Phase 5: ROLLBACK — restoring from backup")
            if cycle.backup_id:
                self._backup_mgr.rollback(cycle.backup_id)
            # 删除已生成的 SKILL.md 文件
            for result in gen_results:
                if result.skill_path:
                    try:
                        import shutil
                        skill_dir = Path(result.skill_path).parent
                        if skill_dir.exists():
                            shutil.rmtree(skill_dir, ignore_errors=True)
                    except Exception as e:
                        log.warning("[decision_loop] failed to remove %s: %s", result.skill_path, e)
                # 移除 skill_meta 条目
                self._skill_meta_mgr.remove_skill(result.skill_name)
            cycle.was_kept = False
            return False

    # ------------------------------------------------------------------
    # Phase 6: Archive to Memory
    # ------------------------------------------------------------------

    def _phase_archive(
        self,
        traces: list[ExecutionTrace],
        patterns: list[FailurePattern],
        gen_results: list[SkillGenResult],
        eval_result: EvalResult,
        cycle: EvolutionCycle,
    ) -> EvolutionRecord:
        """归档进化结果。"""
        log.info("[decision_loop] Phase 6: archiving")

        record = EvolutionRecord(
            timestamp=cycle.started_at,
            traces_analyzed=[t.trace_id for t in traces],
            problems_found=len(patterns),
            patterns=[p.to_dict() for p in patterns],
            skills_created=[r.skill_name for r in gen_results if r.success],
            skills_deprecated=[],
            eval_result=eval_result.to_dict() if eval_result else {},
            decision="kept" if cycle.was_kept else "rolled_back",
            status=cycle.status.value,
        )

        # 写入 skill_meta.json
        self._skill_meta_mgr.add_evolution_record(record.to_dict())

        # 保存历史
        self._last_evolution = record
        self._history.append(record)

        log.info(
            "[decision_loop] Phase 6: archived evolution %s (decision=%s)",
            record.evolution_id, record.decision,
        )
        return record

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_last_evolution(self) -> EvolutionRecord | None:
        return self._last_evolution

    def get_evolution_history(self, limit: int = 20) -> list[EvolutionRecord]:
        return self._history[-limit:]

    # ------------------------------------------------------------------
    # 跳过/错误记录
    # ------------------------------------------------------------------

    def _make_skip_record(
        self, traces_read: int = 0, reason: str = ""
    ) -> EvolutionRecord:
        """生成跳过记录。"""
        return EvolutionRecord(
            traces_analyzed=[],
            decision="skipped",
            status="skipped",
            error_message=reason,
        )

    def _make_error_record(self, error: str = "") -> EvolutionRecord:
        """生成错误记录。"""
        return EvolutionRecord(
            traces_analyzed=[],
            decision="rolled_back",
            status="failed",
            error_message=error,
        )


