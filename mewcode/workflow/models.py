"""Workflow 编排引擎的数据模型。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# WorkflowDef — 一个 workflow 的元信息
# ---------------------------------------------------------------------------


@dataclass
class WorkflowDef:
    """Workflow 定义（从 .mewcode/workflows/*.py 文件中提取）。"""

    name: str
    """Workflow 名称（文件名去 .py 或 META.name）。"""

    description: str = ""
    """一行描述。"""

    phases: list[str] = field(default_factory=list)
    """Phase 标题列表（用于进度展示）。"""

    source_path: str = ""
    """源文件路径。"""


# ---------------------------------------------------------------------------
# BudgetInfo — token 预算追踪
# ---------------------------------------------------------------------------


@dataclass
class BudgetInfo:
    """实时 token 预算追踪器。"""

    total: int | None = None
    """预算上限（None = 无限）。"""

    _spent_input: int = 0
    _spent_output: int = 0

    def spent(self) -> int:
        """已消耗 token 总数。"""
        return self._spent_input + self._spent_output

    def remaining(self) -> int:
        """剩余 token 数。total 为 None 时返回一个极大值表示无限。"""
        if self.total is None:
            return 2_000_000_000  # ~2B，足够大表示"无限"
        return max(0, self.total - self.spent())

    def consume(self, input_tokens: int, output_tokens: int) -> None:
        """记录一次消耗。"""
        self._spent_input += input_tokens
        self._spent_output += output_tokens

    @property
    def is_exhausted(self) -> bool:
        """预算是否已耗尽。"""
        if self.total is None:
            return False
        return self.spent() >= self.total


# ---------------------------------------------------------------------------
# AgentCallRecord — 单次 agent() 调用的记录
# ---------------------------------------------------------------------------


@dataclass
class AgentCallRecord:
    """单次 agent() 调用的完整记录。"""

    call_id: str
    """唯一标识（uuid hex 12 位）。"""

    prompt_sha256: str
    """prompt 的 SHA-256 哈希（用于缓存匹配）。"""

    opts_sha256: str
    """opts 的 SHA-256 哈希。"""

    status: str = "running"
    """状态：running / completed / failed。"""

    result_json: str | None = None
    """结果的 JSON 序列化（失败时为 None）。"""

    error_message: str | None = None
    """失败时的错误信息。"""

    phase: str = ""
    """所属 phase 标题。"""

    label: str = ""
    """显示标签。"""

    started_at: str = ""
    """开始时间 ISO 格式。"""

    completed_at: str = ""
    """完成时间 ISO 格式。"""

    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（用于 JSONL 写入）。"""
        return {
            "call_id": self.call_id,
            "prompt_sha256": self.prompt_sha256,
            "opts_sha256": self.opts_sha256,
            "status": self.status,
            "result_json": self.result_json,
            "error_message": self.error_message,
            "phase": self.phase,
            "label": self.label,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCallRecord:
        """从字典反序列化。"""
        return cls(
            call_id=data.get("call_id", ""),
            prompt_sha256=data.get("prompt_sha256", ""),
            opts_sha256=data.get("opts_sha256", ""),
            status=data.get("status", "running"),
            result_json=data.get("result_json"),
            error_message=data.get("error_message"),
            phase=data.get("phase", ""),
            label=data.get("label", ""),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
        )

    @staticmethod
    def compute_prompt_hash(prompt: str) -> str:
        """计算 prompt 的 SHA-256 哈希。"""
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_opts_hash(opts: dict[str, Any]) -> str:
        """计算 opts 字典的 SHA-256 哈希（按键排序以保证确定性）。"""
        canonical = json.dumps(opts, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# JournalEntry — Journal 中的一行
# ---------------------------------------------------------------------------


@dataclass
class JournalEntry:
    """Journal 文件中的一条记录（对应 JSONL 中的一行）。"""

    record: AgentCallRecord
    """Agent 调用记录。"""

    run_id: str
    """所属 workflow run 的 ID。"""

    workflow_name: str
    """所属 workflow 名称。"""


# ---------------------------------------------------------------------------
# WorkflowState — workflow 执行状态
# ---------------------------------------------------------------------------


@dataclass
class WorkflowState:
    """Workflow 执行的完整状态。"""

    run_id: str
    workflow_name: str
    status: str = "running"
    """running / completed / failed / interrupted。"""

    phase: str = ""
    """当前 active phase。"""

    started_at: str = ""
    completed_at: str = ""


# ---------------------------------------------------------------------------
# StructuredOutputConfig
# ---------------------------------------------------------------------------


@dataclass
class StructuredOutputConfig:
    """agent() 的 structured output 配置。"""

    schema: type
    """Pydantic 模型类。"""

    max_retries: int = 3
    """校验失败最大重试次数。"""

    retry_count: int = 0
    """当前已重试次数。"""


# ---------------------------------------------------------------------------
# Pipeline types
# ---------------------------------------------------------------------------

# pipeline stage: (item) -> result  或  (prev_result, original_item, index) -> result
StageFunc = Callable[..., Any]
