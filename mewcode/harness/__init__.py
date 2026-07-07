"""MewCode Agent 自配置子系统。

允许 Agent 在运行时（受元权限控制）修改自身 Harness 行为：
- Hook 增删改
- 配置项更新
- 权限规则管理
- Memory 条目管理
"""

from mewcode.harness.hook_manager import HookManager
from mewcode.harness.config_manager import ConfigManager
from mewcode.harness.permission_manager import PermissionManager

__all__ = [
    "HookManager",
    "ConfigManager",
    "PermissionManager",
]
