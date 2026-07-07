"""权限审计日志。

所有工具执行决策（allow/deny/ask）写入结构化审计日志，
存储在 .mewcode/audit/decisions.jsonl。
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

AUDIT_DIR = ".mewcode/audit"
AUDIT_FILE = "decisions.jsonl"
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_ARCHIVE_FILES = 10


class AuditLogger:
    """结构化审计日志记录器。"""

    def __init__(self, work_dir: str, session_id: str = "") -> None:
        self._audit_dir = Path(work_dir) / AUDIT_DIR
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._session_id = session_id

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def log_decision(
        self,
        *,
        tool_name: str,
        params_summary: str,
        decision: str,
        source_layer: str,
        rule_id: str = "",
        latency_ms: float = 0.0,
    ) -> None:
        """记录一次权限决策。

        Args:
            tool_name: 工具名称。
            params_summary: 参数摘要（截断到 200 字符）。
            decision: allow / deny / ask。
            source_layer: 决策来源层：safe_readonly / dangerous / sandbox /
                         rule_engine / mode / hitl / plan_mode / harness / rate_limit。
            rule_id: 若来自规则引擎，记录规则标识。
            latency_ms: 决策耗时（毫秒）。
        """
        # 截断参数摘要
        if len(params_summary) > 200:
            params_summary = params_summary[:197] + "..."

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "params_summary": params_summary,
            "decision": decision,
            "source_layer": source_layer,
            "rule_id": rule_id,
            "latency_ms": round(latency_ms, 2),
            "session_id": self._session_id,
        }

        file_path = self._get_current_file()
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("[audit] failed to write: %s", e)

        # 检查是否需要 rotate
        self._maybe_rotate(file_path)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        tool_name: str | None = None,
        decision: str | None = None,
        source_layer: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """查询审计日志。

        Args:
            tool_name: 按工具名过滤。
            decision: 按决策类型过滤。
            source_layer: 按决策来源过滤。
            limit: 返回记录数上限。
            offset: 跳过前 N 条。

        Returns:
            匹配的审计记录列表。
        """
        results: list[dict[str, Any]] = []
        file_path = self._get_current_file()

        if not file_path.exists():
            return results

        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if tool_name and entry.get("tool_name") != tool_name:
                        continue
                    if decision and entry.get("decision") != decision:
                        continue
                    if source_layer and entry.get("source_layer") != source_layer:
                        continue

                    results.append(entry)
        except OSError as e:
            log.error("[audit] failed to read: %s", e)

        return results[offset : offset + limit]

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def _get_current_file(self) -> Path:
        """获取当前审计日志文件路径。"""
        return self._audit_dir / AUDIT_FILE

    def _maybe_rotate(self, file_path: Path) -> None:
        """检查并执行日志 rotate。"""
        try:
            size = file_path.stat().st_size
        except OSError:
            return

        if size < MAX_FILE_BYTES:
            return

        # 归档当前文件
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        archive_path = self._audit_dir / f"decisions.{date_str}.jsonl"

        # 如果当天已有归档，追加
        try:
            with open(file_path, encoding="utf-8") as src:
                content = src.read()
            with open(archive_path, "a", encoding="utf-8") as dst:
                dst.write(content)
            # 清空当前文件
            file_path.write_text("", encoding="utf-8")
            log.info("[audit] rotated to %s", archive_path)
        except OSError as e:
            log.error("[audit] rotate failed: %s", e)
            return

        # 清理旧归档（保留最近 MAX_ARCHIVE_FILES 个）
        self._cleanup_archives()

    def _cleanup_archives(self) -> None:
        """删除过多的旧归档文件。"""
        archives = sorted(
            self._audit_dir.glob("decisions.*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in archives[MAX_ARCHIVE_FILES:]:
            try:
                old.unlink()
                log.info("[audit] removed old archive: %s", old)
            except OSError:
                pass


class AuditLogTool:
    """让 Agent 查询自身审计日志的工具。"""

    def __init__(self, audit_logger: AuditLogger) -> None:
        self._logger = audit_logger
