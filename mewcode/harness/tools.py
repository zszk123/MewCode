"""Agent 自配置工具集。

暴露给 Agent 的 Harness 管理工具：
- AddHookTool / RemoveHookTool / ListHooksTool
- UpdateConfigTool
- AddPermissionRuleTool / RemovePermissionRuleTool
- ManageMemoryTool

所有工具 category 为 "harness"，受 allow_self_modification 元权限控制。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from mewcode.harness.config_manager import ConfigManager
from mewcode.harness.hook_manager import HookManager
from mewcode.harness.permission_manager import PermissionManager
from mewcode.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hook 管理工具
# ---------------------------------------------------------------------------


class AddHookParams(BaseModel):
    event: str = Field(..., description="Lifecycle event name (e.g., 'post_tool_use')")
    action_type: str = Field(..., description="Action type: command, prompt, http, or agent")
    action_config: dict = Field(..., description="Action configuration dict")
    condition: str = Field(default="", description="Optional condition expression")
    once: bool = Field(default=False, description="Fire only once")


class AddHookTool(Tool):
    """Add a lifecycle hook at runtime."""

    name = "AddHook"
    description = "Add a lifecycle hook at runtime. The hook will fire on the specified event."
    params_model = AddHookParams
    category = "harness"
    is_concurrency_safe = True

    def __init__(self, hook_manager: HookManager) -> None:
        self._manager = hook_manager

    async def execute(self, params: AddHookParams) -> ToolResult:
        hook_id = self._manager.add_hook(
            event=params.event,
            action_type=params.action_type,
            action_config=params.action_config,
            condition=params.condition,
            once=params.once,
        )
        return ToolResult(
            output=f"Hook added successfully.\nID: {hook_id}\n"
                   f"Event: {params.event}\nAction: {params.action_type}"
        )


class RemoveHookParams(BaseModel):
    id: str = Field(..., description="Hook ID to remove")


class RemoveHookTool(Tool):
    """Remove a lifecycle hook."""

    name = "RemoveHook"
    description = "Remove a lifecycle hook by its ID."
    params_model = RemoveHookParams
    category = "harness"
    is_concurrency_safe = True

    def __init__(self, hook_manager: HookManager) -> None:
        self._manager = hook_manager

    async def execute(self, params: RemoveHookParams) -> ToolResult:
        removed = self._manager.remove_hook(params.id)
        if removed:
            return ToolResult(output=f"Hook '{params.id}' removed.")
        return ToolResult(output=f"Hook '{params.id}' not found.", is_error=True)


class ListHooksParams(BaseModel):
    pass


class ListHooksTool(Tool):
    """List all active hooks."""

    name = "ListHooks"
    description = "List all active lifecycle hooks."
    params_model = ListHooksParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, hook_manager: HookManager) -> None:
        self._manager = hook_manager

    async def execute(self, params: ListHooksParams) -> ToolResult:
        hooks = self._manager.list_hooks()
        if not hooks:
            return ToolResult(output="No active hooks.")
        lines = [f"Active hooks ({len(hooks)}):"]
        for h in hooks:
            lines.append(f"  - {h}")
        return ToolResult(output="\n".join(lines))


# ---------------------------------------------------------------------------
# Config 管理工具
# ---------------------------------------------------------------------------


class UpdateConfigParams(BaseModel):
    key: str = Field(..., description="Configuration key (dot-separated for nested)")
    value: str = Field(..., description="New value (will be coerced to appropriate type)")


class UpdateConfigTool(Tool):
    """Update a configuration value at runtime."""

    name = "UpdateConfig"
    description = (
        "Update a runtime configuration value. "
        "Allowed keys: permission_mode, compact.utilization_threshold, "
        "critic.enabled, rate_limit.enabled, max_iterations, etc."
    )
    params_model = UpdateConfigParams
    category = "harness"
    is_concurrency_safe = True

    def __init__(self, config_manager: ConfigManager) -> None:
        self._manager = config_manager

    async def execute(self, params: UpdateConfigParams) -> ToolResult:
        # 尝试类型转换
        value: Any = params.value
        lower = params.value.lower()
        if lower == "true":
            value = True
        elif lower == "false":
            value = False
        elif lower == "none":
            value = None
        else:
            try:
                value = int(params.value)
            except ValueError:
                try:
                    value = float(params.value)
                except ValueError:
                    value = params.value

        success, message = self._manager.set_config(params.key, value)
        if success:
            return ToolResult(output=message)
        else:
            return ToolResult(output=message, is_error=True)


# ---------------------------------------------------------------------------
# 权限规则管理工具
# ---------------------------------------------------------------------------


class AddPermissionRuleParams(BaseModel):
    tool_name: str = Field(..., description="Tool name, e.g. 'Bash', 'WriteFile'")
    pattern: str = Field(..., description="fnmatch pattern for arguments")
    effect: str = Field(default="allow", description="'allow' or 'deny'")


class AddPermissionRuleTool(Tool):
    """Add a permission rule at runtime."""

    name = "AddPermissionRule"
    description = (
        "Add a permission rule at runtime. "
        "Persisted to .mewcode/permissions.local.yaml."
    )
    params_model = AddPermissionRuleParams
    category = "harness"
    is_concurrency_safe = True

    def __init__(self, permission_manager: PermissionManager) -> None:
        self._manager = permission_manager

    async def execute(self, params: AddPermissionRuleParams) -> ToolResult:
        success, message = self._manager.add_rule(
            tool_name=params.tool_name,
            pattern=params.pattern,
            effect=params.effect,
        )
        return ToolResult(output=message, is_error=not success)


class RemovePermissionRuleParams(BaseModel):
    tool_name: str = Field(..., description="Tool name")
    pattern: str = Field(..., description="fnmatch pattern to remove")


class RemovePermissionRuleTool(Tool):
    """Remove a permission rule."""

    name = "RemovePermissionRule"
    description = "Remove a permission rule from .mewcode/permissions.local.yaml."
    params_model = RemovePermissionRuleParams
    category = "harness"
    is_concurrency_safe = True

    def __init__(self, permission_manager: PermissionManager) -> None:
        self._manager = permission_manager

    async def execute(self, params: RemovePermissionRuleParams) -> ToolResult:
        success, message = self._manager.remove_rule(
            tool_name=params.tool_name,
            pattern=params.pattern,
        )
        return ToolResult(output=message, is_error=not success)


# ---------------------------------------------------------------------------
# Memory 管理工具
# ---------------------------------------------------------------------------


class ManageMemoryParams(BaseModel):
    action: str = Field(..., description="Action: 'add', 'update', 'delete', or 'search'")
    name: str = Field(default="", description="Memory name (slug)")
    content: str = Field(default="", description="Memory content (for add/update)")
    description: str = Field(default="", description="Memory description")
    memory_type: str = Field(default="reference", description="Type: user, feedback, project, reference")


class ManageMemoryTool(Tool):
    """Manage long-term memories."""

    name = "ManageMemory"
    description = "Manage long-term memories: add, update, delete, or search."
    params_model = ManageMemoryParams
    category = "harness"
    is_concurrency_safe = True

    def __init__(self, memory_manager: Any = None) -> None:
        self._memory_manager = memory_manager

    async def execute(self, params: ManageMemoryParams) -> ToolResult:
        if self._memory_manager is None:
            return ToolResult(
                output="Memory manager not initialized.",
                is_error=True,
            )

        action = params.action.lower()

        if action == "add":
            if not params.name or not params.content:
                return ToolResult(
                    output="'name' and 'content' are required for 'add' action.",
                    is_error=True,
                )
            if hasattr(self._memory_manager, "add_memory"):
                self._memory_manager.add_memory(
                    name=params.name,
                    content=params.content,
                    description=params.description,
                    memory_type=params.memory_type,
                )
                return ToolResult(output=f"Memory '{params.name}' added.")
            return ToolResult(output="Memory manager does not support add.", is_error=True)

        elif action == "delete":
            if not params.name:
                return ToolResult(output="'name' is required for 'delete' action.", is_error=True)
            if hasattr(self._memory_manager, "delete_memory"):
                self._memory_manager.delete_memory(params.name)
                return ToolResult(output=f"Memory '{params.name}' deleted.")
            return ToolResult(output="Memory manager does not support delete.", is_error=True)

        elif action == "search":
            if hasattr(self._memory_manager, "search_memory"):
                results = self._memory_manager.search_memory(params.content)
                if not results:
                    return ToolResult(output="No matching memories found.")
                lines = [f"Found {len(results)} memories:"]
                for r in results:
                    lines.append(f"  - {r}")
                return ToolResult(output="\n".join(lines))
            return ToolResult(output="Memory manager does not support search.", is_error=True)

        else:
            return ToolResult(
                output=f"Unknown action '{action}'. Use: add, delete, or search.",
                is_error=True,
            )
