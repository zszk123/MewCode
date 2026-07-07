"""运行时权限规则管理器。

允许 Agent 在运行时添加/移除权限规则。
规则持久化到 .mewcode/permissions.local.yaml（最高优先级文件）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

LOCAL_PERMISSIONS_FILE = ".mewcode/permissions.local.yaml"


class PermissionManager:
    """运行时权限规则管理。"""

    def __init__(
        self,
        work_dir: str,
        rule_engine: Any = None,
    ) -> None:
        self._work_dir = work_dir
        self._local_path = Path(work_dir) / LOCAL_PERMISSIONS_FILE
        self._rule_engine = rule_engine

    def set_rule_engine(self, engine: Any) -> None:
        """注入 RuleEngine 引用。"""
        self._rule_engine = engine

    def add_rule(
        self,
        tool_name: str,
        pattern: str,
        effect: str = "allow",
    ) -> tuple[bool, str]:
        """添加权限规则。

        Args:
            tool_name: 工具名称。
            pattern: fnmatch 模式。
            effect: allow 或 deny。

        Returns:
            (成功标志, 消息)
        """
        if effect not in ("allow", "deny"):
            return False, f"Invalid effect '{effect}', must be 'allow' or 'deny'"

        rule = {"rule": f"{tool_name}({pattern})", "effect": effect}

        # 更新内存中的规则引擎
        if self._rule_engine and hasattr(self._rule_engine, "add_rule"):
            self._rule_engine.add_rule(tool_name, pattern, effect)

        # 持久化
        try:
            rules = self._load_local_rules()
            rules.append(rule)
            self._save_local_rules(rules)
            log.info(
                "[harness] permission rule added: %s(%s) → %s",
                tool_name, pattern, effect,
            )
        except Exception as e:
            return False, f"Failed to persist rule: {e}"

        return True, f"Permission rule added: {tool_name}({pattern}) → {effect}"

    def remove_rule(
        self,
        tool_name: str,
        pattern: str,
    ) -> tuple[bool, str]:
        """移除权限规则。

        Returns:
            (成功标志, 消息)
        """
        rule_str = f"{tool_name}({pattern})"

        # 从规则引擎移除
        if self._rule_engine and hasattr(self._rule_engine, "remove_rule"):
            self._rule_engine.remove_rule(tool_name, pattern)

        # 持久化
        try:
            rules = self._load_local_rules()
            rules = [r for r in rules if r.get("rule") != rule_str]
            self._save_local_rules(rules)
            log.info("[harness] permission rule removed: %s", rule_str)
        except Exception as e:
            return False, f"Failed to persist rule removal: {e}"

        return True, f"Permission rule removed: {rule_str}"

    def list_rules(self) -> list[dict[str, Any]]:
        """列出所有本地权限规则。"""
        return self._load_local_rules()

    def _load_local_rules(self) -> list[dict[str, Any]]:
        """加载本地权限规则文件。"""
        if not self._local_path.exists():
            return []
        try:
            raw = yaml.safe_load(self._local_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "rules" in raw:
                return raw["rules"]
            if isinstance(raw, list):
                return raw
        except (yaml.YAMLError, OSError) as e:
            log.error("[harness] failed to load permissions: %s", e)
        return []

    def _save_local_rules(self, rules: list[dict[str, Any]]) -> None:
        """保存本地权限规则。"""
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        content = yaml.dump(
            {"rules": rules},
            allow_unicode=True,
            default_flow_style=False,
        )
        self._local_path.write_text(content, encoding="utf-8")
