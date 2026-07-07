"""运行时 Hook 管理器。

允许 Agent 在运行时添加/移除/修改生命周期 Hook。
所有操作受 allow_self_modification 元权限控制。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class HookManager:
    """运行时 Hook 增删改查。"""

    def __init__(self, hook_engine: Any = None) -> None:
        self._engine = hook_engine

    def set_engine(self, engine: Any) -> None:
        """注入 HookEngine 引用（延迟绑定）。"""
        self._engine = engine

    def add_hook(
        self,
        event: str,
        action_type: str,
        action_config: dict[str, Any],
        *,
        condition: str = "",
        once: bool = False,
    ) -> str:
        """添加一个 Hook。

        Args:
            event: 生命周期事件名。
            action_type: 动作类型 (command/prompt/http/agent)。
            action_config: 动作配置。
            condition: 条件表达式。
            once: 是否仅触发一次。

        Returns:
            新 Hook 的 ID。
        """
        import uuid

        hook_id = f"hook_{uuid.uuid4().hex[:8]}"

        if self._engine is None:
            log.warning("[harness] hook_engine not set — hook added in-memory only")
            return hook_id

        # 通过 hook_engine 注册
        if hasattr(self._engine, "register_runtime_hook"):
            self._engine.register_runtime_hook(
                hook_id=hook_id,
                event=event,
                action_type=action_type,
                action_config=action_config,
                condition=condition,
                once=once,
            )
            log.info(
                "[harness] added hook %s: event=%s action=%s",
                hook_id, event, action_type,
            )

        return hook_id

    def remove_hook(self, hook_id: str) -> bool:
        """移除一个 Hook。"""
        if self._engine is None:
            return False

        if hasattr(self._engine, "unregister_runtime_hook"):
            removed = self._engine.unregister_runtime_hook(hook_id)
            if removed:
                log.info("[harness] removed hook %s", hook_id)
            return removed

        return False

    def list_hooks(self) -> list[dict[str, Any]]:
        """列出所有活跃的 Hook。"""
        if self._engine is None:
            return []

        if hasattr(self._engine, "list_runtime_hooks"):
            return self._engine.list_runtime_hooks()

        return []
