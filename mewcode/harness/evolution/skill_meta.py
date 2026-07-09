"""Skill 元数据管理器。

管理 harness/skills/skill_meta.json：
- 新增/废弃 Skill 时更新元数据
- 持续统计 Skill 调用频次
- 连续 60 次任务未被调用则标记为废弃
- 废弃时同时更新 SKILL.md 文档
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_META = {
    "version": 1,
    "last_updated": "",
    "skills": {},
    "evolution_records": {},
}


class SkillMetaManager:
    """自进化 Skill 的元数据管理器。"""

    def __init__(self, meta_path: Path) -> None:
        self._meta_path = Path(meta_path)
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] | None = None
        self._lock: Any = None  # asyncio.Lock（延迟初始化）

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """加载 skill_meta.json，损坏时自动创建新的。"""
        if self._data is not None:
            return self._data

        if self._meta_path.exists():
            try:
                raw = json.loads(self._meta_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "skills" in raw:
                    self._data = raw
                    # 确保必要字段存在
                    self._data.setdefault("version", 1)
                    self._data.setdefault("evolution_records", {})
                    self._data.setdefault("last_updated", "")
                    return self._data
            except (json.JSONDecodeError, OSError) as e:
                log.warning("[skill_meta] corrupted file, creating fresh: %s", e)

        self._data = dict(DEFAULT_META)  # 深拷贝
        self._data["last_updated"] = self._now_iso()
        self.save()
        return self._data

    def save(self) -> None:
        """保存到磁盘（原子写入）。"""
        data = self._data or dict(DEFAULT_META)
        data["last_updated"] = self._now_iso()
        tmp_path = Path(str(self._meta_path) + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(self._meta_path))
        except OSError as e:
            log.error("[skill_meta] failed to save: %s", e)

    # ------------------------------------------------------------------
    # Skill CRUD
    # ------------------------------------------------------------------

    def add_skill(
        self,
        skill_name: str,
        description: str = "",
        trace_ids: list[str] | None = None,
        evolution_id: str = "",
        failure_patterns: list[str] | None = None,
    ) -> None:
        """新增一个自动生成的 Skill 的元数据条目。"""
        data = self.load()
        now = self._now_iso()

        data["skills"][skill_name] = {
            "name": skill_name,
            "description": description,
            "created_at": now,
            "updated_at": now,
            "call_count": 0,
            "tasks_since_last_call": 0,
            "disabled": False,
            "source": "auto-generated",
            "based_on_traces": trace_ids or [],
            "evolution_id": evolution_id,
            "failure_patterns": failure_patterns or [],
            "deprecated_at": None,
            "deprecated_by_cycle": None,
        }
        self.save()
        log.info("[skill_meta] added skill: %s", skill_name)

    def deprecate_skill(self, skill_name: str, cycle_id: str = "") -> bool:
        """废弃一个 Skill。

        设置 disabled=True，同时尝试更新 SKILL.md 文档。
        """
        data = self.load()
        skill = data["skills"].get(skill_name)
        if skill is None:
            log.warning("[skill_meta] deprecate: skill '%s' not found", skill_name)
            return False

        if skill.get("disabled"):
            return True  # 已经废弃

        now = self._now_iso()
        skill["disabled"] = True
        skill["deprecated_at"] = now
        skill["deprecated_by_cycle"] = cycle_id
        skill["updated_at"] = now
        self.save()

        # 尝试更新 SKILL.md
        self._update_skill_md_deprecated(skill_name)
        log.info("[skill_meta] deprecated skill: %s (cycle=%s)", skill_name, cycle_id)
        return True

    def remove_skill(self, skill_name: str) -> bool:
        """移除 Skill 元数据条目（用于回滚场景）。"""
        data = self.load()
        if skill_name in data["skills"]:
            del data["skills"][skill_name]
            self.save()
            log.info("[skill_meta] removed skill: %s", skill_name)
            return True
        return False

    # ------------------------------------------------------------------
    # 调用统计
    # ------------------------------------------------------------------

    def record_call(self, skill_name: str) -> None:
        """记录一次 Skill 调用。

        call_count++，tasks_since_last_call 归零，更新 last_invoked_at。
        """
        data = self.load()
        skill = data["skills"].get(skill_name)
        if skill is None:
            return

        skill["call_count"] = skill.get("call_count", 0) + 1
        skill["tasks_since_last_call"] = 0
        skill["updated_at"] = self._now_iso()
        self.save()

    def increment_tasks(self) -> list[str]:
        """所有活跃 Skill 的 tasks_since_last_call++，返回应被废弃的 Skill 名称列表。

        应在每次任务完成后调用。
        """
        data = self.load()
        to_deprecate: list[str] = []

        for name, skill in data["skills"].items():
            if skill.get("disabled"):
                continue
            count = skill.get("tasks_since_last_call", 0) + 1
            skill["tasks_since_last_call"] = count
            skill["updated_at"] = self._now_iso()

        self.save()

        # 检查废弃阈值（默认 60，从 EvolutionConfig 可覆盖）
        # 实际阈值由 EvolutionManager 传入，这里做默认检查
        return to_deprecate

    def check_deprecation_candidates(self, threshold: int = 60) -> list[str]:
        """返回 tasks_since_last_call >= threshold 的 Skill 名称列表。"""
        data = self.load()
        candidates: list[str] = []
        for name, skill in data["skills"].items():
            if skill.get("disabled"):
                continue
            if skill.get("tasks_since_last_call", 0) >= threshold:
                candidates.append(name)
        return candidates

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_active(self) -> list[dict[str, Any]]:
        """获取所有活跃（未废弃）的 Skill。"""
        data = self.load()
        return [
            s for s in data["skills"].values()
            if not s.get("disabled")
        ]

    def get_deprecated(self) -> list[dict[str, Any]]:
        """获取所有已废弃的 Skill。"""
        data = self.load()
        return [
            s for s in data["skills"].values()
            if s.get("disabled")
        ]

    def get_stats(self, skill_name: str) -> dict[str, Any] | None:
        """获取单个 Skill 的完整统计。"""
        data = self.load()
        return data["skills"].get(skill_name)

    def get_all(self) -> dict[str, Any]:
        """获取所有 Skill 数据。"""
        return dict(self.load()["skills"])

    def has_skill(self, skill_name: str) -> bool:
        """检查 Skill 是否已存在。"""
        data = self.load()
        return skill_name in data["skills"]

    def has_recent_evolution_for_pattern(
        self, pattern_signature: str, within_cycles: int = 3
    ) -> bool:
        """检查指定 pattern 是否在最近 N 轮进化中已处理过。"""
        data = self.load()
        records = data.get("evolution_records", {})
        # 按时间排序最近的进化记录
        sorted_records = sorted(
            records.values(),
            key=lambda r: r.get("timestamp", 0),
            reverse=True,
        )
        for record in sorted_records[:within_cycles]:
            patterns = record.get("patterns", [])
            for p in patterns:
                if p.get("stack_signature") == pattern_signature:
                    return True
        return False

    # ------------------------------------------------------------------
    # 进化记录
    # ------------------------------------------------------------------

    def add_evolution_record(self, record: dict[str, Any]) -> None:
        """追加一次进化周期记录。"""
        data = self.load()
        evo_id = record.get("evolution_id", "")
        if evo_id:
            data["evolution_records"][evo_id] = record
            self.save()
            log.info("[skill_meta] added evolution record: %s", evo_id)

    def get_evolution_records(self) -> list[dict[str, Any]]:
        """获取所有进化记录（按时间降序）。"""
        data = self.load()
        records = list(data.get("evolution_records", {}).values())
        records.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return records

    def get_last_evolution(self) -> dict[str, Any] | None:
        """获取最近一次进化记录。"""
        records = self.get_evolution_records()
        return records[0] if records else None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _update_skill_md_deprecated(self, skill_name: str) -> None:
        """尝试更新 SKILL.md 文档标记为废弃。"""
        # 查找可能的 SKILL.md 位置
        skills_dir = self._meta_path.parent
        skill_md = skills_dir / skill_name / "SKILL.md"
        if not skill_md.exists():
            # 也尝试 flat 格式
            skill_md = skills_dir / f"{skill_name}.md"

        if not skill_md.exists():
            log.debug("[skill_meta] SKILL.md not found for %s, skipping update", skill_name)
            return

        try:
            content = skill_md.read_text(encoding="utf-8")
            # 在 frontmatter 中新增/更新 deprecated 字段
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    fm = content[3:end]
                    body = content[end + 3:]
                    if "deprecated:" not in fm:
                        fm += f"\ndeprecated: true\ndeprecated_at: {self._now_iso()}"
                    else:
                        # 替换已有的 deprecated 行
                        import re
                        fm = re.sub(r"deprecated:\s*false", "deprecated: true", fm)
                    new_content = f"---{fm}---{body}"
                    tmp = Path(str(skill_md) + ".tmp")
                    tmp.write_text(new_content, encoding="utf-8")
                    os.replace(str(tmp), str(skill_md))
                    log.info("[skill_meta] updated SKILL.md deprecated flag: %s", skill_md)
        except OSError as e:
            log.warning("[skill_meta] failed to update SKILL.md: %s", e)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
