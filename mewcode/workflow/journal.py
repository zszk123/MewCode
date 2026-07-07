"""Workflow Journal — 追加式执行日志 + 缓存命中查询。

每个 workflow run 对应一个 .jsonl 文件，存储在
.mewcode/workflows/journals/{workflow_name}/{run_id}.jsonl

Journal 是 append-only 的：新的 agent() 调用追加到文件末尾，
断点恢复时通过 (prompt_sha256, opts_sha256) 哈希匹配已完成的调用。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mewcode.workflow.models import AgentCallRecord, JournalEntry

# 单个 journal 文件的大小上限（10 MB）
MAX_JOURNAL_BYTES = 10 * 1024 * 1024

# journal 根目录（相对于项目根目录）
JOURNALS_DIR = ".mewcode/workflows/journals"


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


class Journal:
    """一个 workflow run 的追加式执行日志。

    线程安全：写操作由调用方保证串行（workflow 引擎内 agent() 调用是串行的）。
    """

    def __init__(self, journal_path: Path, run_id: str, workflow_name: str) -> None:
        self._path = journal_path
        self._run_id = run_id
        self._workflow_name = workflow_name
        self._file: Any = None

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls, work_dir: str, workflow_name: str, run_id: str | None = None
    ) -> Journal:
        """创建新的 Journal（新 run）。

        Args:
            work_dir: 项目根目录。
            workflow_name: workflow 名称。
            run_id: run ID（None 则自动生成）。
        """
        if run_id is None:
            run_id = uuid.uuid4().hex[:12]

        journal_dir = Path(work_dir) / JOURNALS_DIR / workflow_name
        journal_dir.mkdir(parents=True, exist_ok=True)

        journal_path = journal_dir / f"{run_id}.jsonl"
        journal = cls(journal_path=journal_path, run_id=run_id, workflow_name=workflow_name)
        journal._open()
        return journal

    @classmethod
    def load(
        cls, work_dir: str, workflow_name: str, run_id: str
    ) -> Journal | None:
        """加载已有的 Journal（用于恢复）。

        若文件不存在则返回 None。
        """
        journal_path = Path(work_dir) / JOURNALS_DIR / workflow_name / f"{run_id}.jsonl"
        if not journal_path.exists():
            return None

        journal = cls(journal_path=journal_path, run_id=run_id, workflow_name=workflow_name)
        journal._open()
        return journal

    @classmethod
    def list_journals(
        cls, work_dir: str, workflow_name: str
    ) -> list[str]:
        """列出某个 workflow 的所有 run_id。"""
        journal_dir = Path(work_dir) / JOURNALS_DIR / workflow_name
        if not journal_dir.exists():
            return []
        run_ids: list[str] = []
        for f in journal_dir.glob("*.jsonl"):
            run_ids.append(f.stem)
        return sorted(run_ids)

    # ------------------------------------------------------------------
    # 文件操作
    # ------------------------------------------------------------------

    def _open(self) -> None:
        """打开 journal 文件（追加模式）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8")

    def close(self) -> None:
        """关闭 journal 文件。"""
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()

    def flush(self) -> None:
        """强制刷盘。"""
        if self._file and not self._file.closed:
            self._file.flush()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(self, record: AgentCallRecord) -> None:
        """追加一条 agent 调用记录。"""
        entry = {
            "call_id": record.call_id,
            "prompt_sha256": record.prompt_sha256,
            "opts_sha256": record.opts_sha256,
            "status": record.status,
            "result_json": record.result_json,
            "error_message": record.error_message,
            "phase": record.phase,
            "label": record.label,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "run_id": self._run_id,
            "workflow_name": self._workflow_name,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        self._file.write(line)
        self._file.flush()

    def update(self, call_id: str, **kwargs: Any) -> None:
        """更新一条已存在的记录（重写整个 journal 文件）。

        仅在记录完成（status 变为 completed/failed）时调用。
        对于小 journal（< 10MB），重写开销可接受。
        """
        records = self._read_all_raw()
        updated = False
        for entry in records:
            if entry.get("call_id") == call_id:
                entry.update(kwargs)
                updated = True
                break

        if updated:
            self._write_all_raw(records)

    # ------------------------------------------------------------------
    # 查询（缓存命中）
    # ------------------------------------------------------------------

    def lookup(self, prompt_sha256: str, opts_sha256: str) -> AgentCallRecord | None:
        """按 (prompt_hash, opts_hash) 查找已完成的记录。

        仅返回 status == "completed" 的记录。
        status == "running" 的记录视为未完成（不命中）。
        status == "failed" 的记录不命中（需重试）。

        Returns:
            匹配的 AgentCallRecord，无匹配则返回 None。
        """
        for entry in self._read_all_raw():
            if (
                entry.get("prompt_sha256") == prompt_sha256
                and entry.get("opts_sha256") == opts_sha256
                and entry.get("status") == "completed"
            ):
                return AgentCallRecord.from_dict(entry)
        return None

    def get_incomplete_runs(self) -> list[str]:
        """获取本 journal 中所有未完成的 run_id。

        检查是否有 status == "running" 且无 completed_at 的记录。
        """
        # 对于单个 journal 文件，只检查自己的 run_id
        has_running = False
        for entry in self._read_all_raw():
            if entry.get("status") == "running" and not entry.get("completed_at"):
                has_running = True
                break
        return [self._run_id] if has_running else []

    def get_all_records(self) -> list[AgentCallRecord]:
        """获取本 journal 中所有记录。"""
        return [AgentCallRecord.from_dict(entry) for entry in self._read_all_raw()]

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def prune(self, max_bytes: int = MAX_JOURNAL_BYTES) -> int:
        """裁剪 journal 文件，使其不超过 max_bytes。

        保留所有 status == "running" 的记录（未完成，不可丢弃）。
        从最旧的 completed/failed 记录开始删除。

        Returns:
            删除的记录数。
        """
        records = self._read_all_raw()

        # 分离 running 和 completed/failed
        running_records = [
            r for r in records
            if r.get("status") == "running" and not r.get("completed_at")
        ]
        done_records = [
            r for r in records
            if not (r.get("status") == "running" and not r.get("completed_at"))
        ]

        # 构建最小集合：running + 尽可能多的 done（从最新开始保留）
        kept = list(running_records)  # 必须保留所有 running
        remaining_done = list(done_records)

        # 从最新到最旧排序（用 JSON 行号间接判断：在原始列表中的 index 越大越新）
        # done_records 保持了原始顺序，从后往前取
        for entry in reversed(remaining_done):
            kept.insert(len(running_records), entry)  # running 在前
            # 估算大小
            estimated = sum(len(json.dumps(r, ensure_ascii=False)) + 1 for r in kept)
            if estimated > max_bytes:
                kept.pop(len(running_records))  # 移除刚加的
                break

        removed = len(records) - len(kept)
        if removed > 0:
            self._write_all_raw(kept)

        return removed

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _read_all_raw(self) -> list[dict[str, Any]]:
        """读取 journal 文件中所有 JSON 行（容错：跳过损坏行）。"""
        records: list[dict[str, Any]] = []
        if not self._path.exists():
            return records

        # 先 flush 确保写入的内容可读
        if self._file and not self._file.closed:
            self._file.flush()

        with open(self._path, encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # 跳过损坏行，记录警告
                    import logging
                    logging.getLogger("mewcode.workflow").warning(
                        "[journal] skipping corrupted line %d in %s",
                        line_no,
                        self._path,
                    )
        return records

    def _write_all_raw(self, records: list[dict[str, Any]]) -> None:
        """全量重写 journal 文件。"""
        # 关闭当前追加模式的 file handle
        if self._file and not self._file.closed:
            self._file.close()

        # 写入所有记录
        with open(self._path, "w", encoding="utf-8") as f:
            for entry in records:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 重新以追加模式打开
        self._file = open(self._path, "a", encoding="utf-8")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def cleanup_old_journals(work_dir: str, max_age_days: int = 30) -> int:
    """清理超过 max_age_days 天的旧 journal 文件和目录。

    Returns:
        删除的文件数。
    """
    import time

    journals_root = Path(work_dir) / JOURNALS_DIR
    if not journals_root.exists():
        return 0

    cutoff = time.time() - (max_age_days * 86400)
    removed = 0

    for workflow_dir in journals_root.iterdir():
        if not workflow_dir.is_dir():
            continue
        for journal_file in workflow_dir.glob("*.jsonl"):
            try:
                if journal_file.stat().st_mtime < cutoff:
                    journal_file.unlink()
                    removed += 1
            except OSError:
                pass

        # 清理空目录
        try:
            remaining = list(workflow_dir.glob("*.jsonl"))
            if not remaining:
                workflow_dir.rmdir()
        except OSError:
            pass

    return removed
