"""Workflow Agent Tool — 让主 Agent 可以在对话中调用 workflow。

支持两种模式：
- 同步模式（background=false）：阻塞等待 workflow 完成，返回结果文本
- 后台模式（background=true）：立即返回 task_id，完成后以通知形式注入
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.workflow.engine import WorkflowEngine, WorkflowNotFoundError

log = logging.getLogger(__name__)


class WorkflowParams(BaseModel):
    workflow_name: str = Field(..., description="Name of the workflow to execute")
    args: dict | None = Field(default=None, description="Optional arguments to pass to the workflow")
    background: bool = Field(default=False, description="Run in background (async notification)")
    budget_total: int | None = Field(default=None, description="Optional token budget limit")


class WorkflowTool(Tool):
    """Execute a named workflow.

    Workflows are Python async functions defined in .mewcode/workflows/*.py
    that orchestrate multiple agent calls using the workflow DSL.
    """

    name = "Workflow"
    description = (
        "Execute a workflow defined in .mewcode/workflows/*.py. "
        "Workflows orchestrate multiple agent calls with pipeline/parallel patterns. "
        "Use ListWorkflows to see available workflows. "
        "Set background=true to run asynchronously."
    )
    params_model = WorkflowParams
    category = "write"
    is_concurrency_safe = False

    def __init__(
        self,
        engine: WorkflowEngine,
        task_manager: Any = None,
    ) -> None:
        self._engine = engine
        self._task_manager = task_manager

    async def execute(self, params: WorkflowParams) -> ToolResult:
        # 列出可用 workflows
        available = self._engine.list_workflows()
        available_names = [w.name for w in available]

        if params.workflow_name not in available_names:
            suggestions = ""
            if available_names:
                suggestions = f"\nAvailable workflows: {', '.join(available_names)}"
            return ToolResult(
                output=f"Workflow '{params.workflow_name}' not found.{suggestions}",
                is_error=True,
            )

        if params.background:
            return await self._execute_background(params)
        else:
            return await self._execute_sync(params)

    async def _execute_sync(self, params: WorkflowParams) -> ToolResult:
        try:
            result = await self._engine.execute(
                workflow_name=params.workflow_name,
                args=params.args,
                budget_total=params.budget_total,
                resume=True,
            )
            output = self._format_result(params.workflow_name, result)
            return ToolResult(output=output)
        except WorkflowNotFoundError as e:
            return ToolResult(output=str(e), is_error=True)
        except Exception as e:
            log.exception("[workflow] execution failed: %s", e)
            return ToolResult(
                output=f"Workflow '{params.workflow_name}' failed: {e}",
                is_error=True,
            )

    async def _execute_background(self, params: WorkflowParams) -> ToolResult:
        if self._task_manager is None:
            return ToolResult(
                output="Background workflow execution requires TaskManager to be initialized.",
                is_error=True,
            )

        async def _bg_run() -> str:
            try:
                result = await self._engine.execute(
                    workflow_name=params.workflow_name,
                    args=params.args,
                    budget_total=params.budget_total,
                    resume=True,
                )
                return self._format_result(params.workflow_name, result)
            except Exception as e:
                return f"Workflow '{params.workflow_name}' failed: {e}"

        task = asyncio.create_task(_bg_run())
        task_id = id(task)

        return ToolResult(
            output=f"Workflow '{params.workflow_name}' started in background.\n"
                   f"Task ID: {task_id}\n"
                   f"You will be notified when it completes."
        )

    def _format_result(self, workflow_name: str, result: Any) -> str:
        header = f"Workflow '{workflow_name}' completed successfully.\n\n"
        if result is None:
            return header + "(no result returned)"
        if isinstance(result, str):
            return header + result
        if isinstance(result, (list, dict)):
            try:
                body = json.dumps(result, ensure_ascii=False, indent=2)
                if len(body) > 5000:
                    body = body[:5000] + "\n… (truncated)"
                return header + body
            except Exception:
                return header + str(result)
        return header + str(result)


class ListWorkflowsParams(BaseModel):
    """No parameters needed."""
    pass


class ListWorkflowsTool(Tool):
    """List all available workflows."""

    name = "ListWorkflows"
    description = "List all workflows defined in .mewcode/workflows/ directory."
    params_model = ListWorkflowsParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def execute(self, params: ListWorkflowsParams) -> ToolResult:
        workflows = self._engine.list_workflows()
        if not workflows:
            return ToolResult(
                output="No workflows found. Create .py files in .mewcode/workflows/ "
                       "with async functions using the workflow DSL."
            )

        lines = [f"Available workflows ({len(workflows)}):", ""]
        for wf in workflows:
            lines.append(f"  **{wf.name}**")
            if wf.description:
                lines.append(f"    {wf.description}")
            if wf.phases:
                lines.append(f"    Phases: {' → '.join(wf.phases)}")
            lines.append("")

        return ToolResult(output="\n".join(lines))
