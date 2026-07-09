"""EvolutionManager — 自进化子系统门面。

负责：
- 接收配置，初始化所有进化组件
- 在每次任务结束后检查触发条件
- 非阻塞地执行进化循环
- 防重入保护
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from mewcode.harness.evolution.models import EvolutionRecord, SkipEvolutionError
from mewcode.harness.evolution.backup import BackupManager
from mewcode.harness.evolution.trace_store import ExecutionTraceStore, TraceCollector
from mewcode.harness.evolution.skill_meta import SkillMetaManager
from mewcode.harness.evolution.problem_classifier import ProblemClassifier
from mewcode.harness.evolution.skill_generator import SkillGenerator
from mewcode.harness.evolution.evaluator import EvolutionEvaluator
from mewcode.harness.evolution.decision_loop import EvolutionDecisionLoop

log = logging.getLogger(__name__)


class EvolutionManager:
    """自进化子系统门面。

    用法:
        mgr = EvolutionManager(
            harness_dir=Path("mewcode/harness"),
            config=evo_config,
            client_factory=client.stream,  # LLM 调用工厂
        )
        # 每次任务完成后:
        trace_id = mgr.trace_collector.start_task("user request")
        # ... agent works ...
        mgr.trace_collector.end_task(trace_id, success=True)
        # 任务后检查:
        await mgr.check_and_evolve()
    """

    def __init__(
        self,
        harness_dir: Path,
        config: Any,  # EvolutionConfig
        client_factory: Any = None,
        session_manager: Any = None,
        memory_manager: Any = None,
        skill_loader: Any = None,
    ) -> None:
        self._harness_dir = Path(harness_dir)
        self._config = config
        self._client_factory = client_factory
        self._session_manager = session_manager
        self._memory_manager = memory_manager
        self._skill_loader = skill_loader

        # 确保子目录存在
        self._trace_dir = self._harness_dir / getattr(config, "traces_dir", "traces")
        self._skills_dir = self._harness_dir / getattr(config, "skills_dir", "skills")
        self._backup_dir = self._harness_dir / getattr(config, "backup_dir", "backup")
        self._meta_path = self._harness_dir / getattr(config, "skill_meta_file", "skills/skill_meta.json")

        for d in (self._trace_dir, self._skills_dir, self._backup_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 初始化组件
        self.trace_store = ExecutionTraceStore(self._trace_dir)
        self.trace_collector = TraceCollector(self.trace_store)
        self.backup_manager = BackupManager(self._backup_dir)
        self.skill_meta_manager = SkillMetaManager(self._meta_path)

        self.classifier = ProblemClassifier(client_factory=client_factory)
        self.skill_generator = SkillGenerator(
            client_factory=client_factory,
            skills_dir=self._skills_dir,
            min_recurrence=getattr(config, "min_failure_recurrence", 3),
        )
        self.evaluator = EvolutionEvaluator(
            token_increase_threshold=getattr(config, "token_increase_threshold", 0.15),
            agent_factory=client_factory,
        )

        self.decision_loop = EvolutionDecisionLoop(
            trace_store=self.trace_store,
            classifier=self.classifier,
            skill_generator=self.skill_generator,
            evaluator=self.evaluator,
            backup_manager=self.backup_manager,
            skill_meta_manager=self.skill_meta_manager,
            min_traces=getattr(config, "min_traces_trigger", 30),
            max_traces=getattr(config, "max_traces_per_evolution", 50),
            min_recurrence=getattr(config, "min_failure_recurrence", 3),
        )

        # 状态
        self._running: bool = False
        self._last_result: EvolutionRecord | None = None

        log.info(
            "[evolution] manager initialized: traces_dir=%s, skills_dir=%s, threshold=%d",
            self._trace_dir, self._skills_dir, getattr(config, "min_traces_trigger", 30),
        )

    # ------------------------------------------------------------------
    # 进化检查（主要钩子）
    # ------------------------------------------------------------------

    async def check_and_evolve(self) -> EvolutionRecord | None:
        """非阻塞检查：如果条件满足，执行完整进化周期。

        应在每次任务完成后调用（如 session 结束或 _send_message 完成时）。

        Returns:
            EvolutionRecord 或 None。
        """
        if self._running:
            log.debug("[evolution] already running, skipping check")
            return None

        # 先处理 Skill 废弃检查
        self._check_deprecations()

        self._running = True
        try:
            record = await self.decision_loop.run()
            self._last_result = record
            return record
        except SkipEvolutionError:
            log.debug("[evolution] skipped — insufficient traces or no patterns")
            return None
        except Exception:
            log.exception("[evolution] cycle failed")
            return None
        finally:
            self._running = False

    async def run_if_ready(self) -> EvolutionRecord | None:
        """check_and_evolve 的别名（供工具调用）。"""
        return await self.check_and_evolve()

    # ------------------------------------------------------------------
    # Skill 废弃检查
    # ------------------------------------------------------------------

    def _check_deprecations(self) -> None:
        """检查并废弃超时未使用的 Skill。"""
        # 所有活跃 Skill 的 counter ++
        self.skill_meta_manager.increment_tasks()

        threshold = getattr(self._config, "deprecation_task_threshold", 60)
        candidates = self.skill_meta_manager.check_deprecation_candidates(threshold)

        for name in candidates:
            self.skill_meta_manager.deprecate_skill(name)
            log.info("[evolution] deprecated skill: %s (not used for %d tasks)", name, threshold)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """获取当前状态摘要。"""
        last = self.skill_meta_manager.get_last_evolution()
        active_skills = self.skill_meta_manager.get_active()
        deprecated_skills = self.skill_meta_manager.get_deprecated()

        return {
            "running": self._running,
            "total_traces": self.trace_store.count(),
            "active_skills": len(active_skills),
            "deprecated_skills": len(deprecated_skills),
            "last_evolution": last,
            "skills": {
                "active": [s.get("name") for s in active_skills],
                "deprecated": [s.get("name") for s in deprecated_skills],
            },
        }

    def get_last_cycle(self) -> EvolutionRecord | None:
        return self._last_result

    def list_cycles(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.skill_meta_manager.get_evolution_records()[:limit]

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """清理：Flush 所有未完成的 trace + 清理旧文件。"""
        self.trace_collector.flush()
        self.trace_store.cleanup()
        self.backup_manager.prune_old()
        log.info("[evolution] cleanup complete")
