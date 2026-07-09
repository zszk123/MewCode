"""文件备份与回滚管理器。

为自进化系统提供原子化的文件备份和恢复能力：
- 修改前自动备份原文件
- 评估不达标时从备份恢复
- SHA-256 校验确保完整性
- 原子写入（temp + os.replace）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_BACKUPS = 50
BACKUP_RETENTION_DAYS = 30


class BackupVerificationError(Exception):
    """备份校验失败。"""
    pass


class BackupManager:
    """文件备份与回滚。

    使用方式：
        mgr = BackupManager(Path("harness/backup"))
        backup_id = mgr.backup(Path("some/file.py"))
        # ... 修改文件 ...
        if should_rollback:
            mgr.rollback(backup_id)
        else:
            mgr.commit(backup_id)
    """

    def __init__(self, backup_dir: Path) -> None:
        self._backup_dir = Path(backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._lock: Any = None  # asyncio.Lock, 延迟初始化

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------

    def backup(self, file_path: Path, cycle_id: str = "") -> str:
        """备份单个文件。

        Args:
            file_path: 要备份的文件路径（相对于项目根或绝对路径）。
            cycle_id: 关联的进化周期 ID（可选）。

        Returns:
            backup_id：备份标识符，用于后续 rollback/commit。
        """
        file_path = Path(file_path)
        backup_id = self._make_backup_id(cycle_id)
        backup_root = self._backup_dir / backup_id
        backup_root.mkdir(parents=True, exist_ok=True)

        manifest: dict[str, Any] = {
            "backup_id": backup_id,
            "cycle_id": cycle_id,
            "timestamp": time.time(),
            "files": [],
        }

        self._backup_single(file_path, backup_root, manifest)

        # 写 manifest
        manifest_path = backup_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        log.info("[backup] created %s (%d files)", backup_id, len(manifest["files"]))
        self._prune_if_needed()
        return backup_id

    def backup_directory(self, dir_path: Path, cycle_id: str = "") -> str:
        """递归备份整个目录。

        Args:
            dir_path: 目录路径。
            cycle_id: 关联的进化周期 ID。

        Returns:
            backup_id。
        """
        dir_path = Path(dir_path)
        backup_id = self._make_backup_id(cycle_id)
        backup_root = self._backup_dir / backup_id
        backup_root.mkdir(parents=True, exist_ok=True)

        manifest: dict[str, Any] = {
            "backup_id": backup_id,
            "cycle_id": cycle_id,
            "timestamp": time.time(),
            "files": [],
        }

        if dir_path.is_dir():
            for fp in dir_path.rglob("*"):
                if fp.is_file() and ".git" not in fp.parts:
                    self._backup_single(fp, backup_root, manifest)

        manifest_path = backup_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        log.info("[backup] created %s (%d files from %s)", backup_id, len(manifest["files"]), dir_path)
        self._prune_if_needed()
        return backup_id

    def _backup_single(
        self, file_path: Path, backup_root: Path, manifest: dict[str, Any]
    ) -> None:
        """备份单个文件到备份目录。"""
        rel = self._relative_path(file_path)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        if file_path.exists():
            content = file_path.read_bytes()
            file_hash = hashlib.sha256(content).hexdigest()
            dest.write_bytes(content)
            status = "backed_up"
        else:
            # 文件尚不存在（新创建场景）
            content = b""
            file_hash = ""
            status = "new_file"

        manifest["files"].append({
            "original_path": str(file_path),
            "relative_path": str(rel),
            "backup_path": str(dest),
            "hash": file_hash,
            "status": status,
            "size": len(content),
        })

    # ------------------------------------------------------------------
    # 回滚
    # ------------------------------------------------------------------

    def rollback(self, backup_id: str) -> bool:
        """从备份恢复所有文件。

        Args:
            backup_id: backup() 返回的备份标识符。

        Returns:
            True 表示全部恢复成功。

        Raises:
            BackupVerificationError: hash 校验失败。
        """
        backup_root = self._backup_dir / backup_id
        if not backup_root.is_dir():
            log.warning("[backup] rollback: backup %s not found", backup_id)
            return False

        manifest = self._load_manifest(backup_root)
        if manifest is None:
            return False

        all_ok = True
        for entry in manifest.get("files", []):
            try:
                self._restore_single(entry)
            except Exception as e:
                log.error("[backup] rollback failed for %s: %s", entry.get("original_path"), e)
                all_ok = False

        if all_ok:
            log.info("[backup] rollback %s succeeded (%d files)", backup_id, len(manifest["files"]))
            # 回滚成功后删除备份目录
            shutil.rmtree(backup_root, ignore_errors=True)
        else:
            log.error("[backup] rollback %s partially failed", backup_id)

        return all_ok

    def _restore_single(self, entry: dict[str, Any]) -> None:
        """从备份条目恢复单个文件。"""
        original = Path(entry["original_path"])
        backup_path = Path(entry["backup_path"])
        status = entry.get("status", "backed_up")
        expected_hash = entry.get("hash", "")

        if status == "new_file":
            # 原文件之前不存在，直接删除
            if original.exists():
                original.unlink()
            return

        if not backup_path.exists():
            raise BackupVerificationError(
                f"Backup file missing: {backup_path}"
            )

        # 写回原位置（原子写入）
        content = backup_path.read_bytes()
        if expected_hash:
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != expected_hash:
                # 备份文件损坏 — 保留备份供人工检查
                corrupted = Path(str(original) + ".corrupted")
                log.critical(
                    "[backup] hash mismatch for %s: expected=%s actual=%s",
                    original, expected_hash[:16], actual_hash[:16],
                )
                raise BackupVerificationError(
                    f"Hash mismatch during rollback for {original}"
                )

        original.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(original) + ".tmp")
        tmp.write_bytes(content)
        os.replace(str(tmp), str(original))  # 原子 rename
        log.debug("[backup] restored %s", original)

    # ------------------------------------------------------------------
    # Commit / Cleanup
    # ------------------------------------------------------------------

    def commit(self, backup_id: str) -> None:
        """标记备份为已提交（保留用于审计，但不再作为活跃回滚点）。

        commit 后备份保留在磁盘上，prune_old() 会清理超过保留期的 committed 备份。
        """
        backup_root = self._backup_dir / backup_id
        if not backup_root.is_dir():
            return
        manifest = self._load_manifest(backup_root)
        if manifest is None:
            return
        manifest["committed"] = True
        manifest["committed_at"] = time.time()
        manifest_path = backup_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("[backup] committed %s", backup_id)

    def cleanup(self, backup_id: str) -> None:
        """立即删除指定备份（进化成功保留后清理）。"""
        backup_root = self._backup_dir / backup_id
        if backup_root.is_dir():
            shutil.rmtree(backup_root, ignore_errors=True)
            log.info("[backup] cleaned up %s", backup_id)

    def prune_old(self, days: int = BACKUP_RETENTION_DAYS) -> int:
        """清理超过 N 天的 committed 备份。"""
        cutoff = time.time() - days * 86400
        removed = 0
        for entry in self._backup_dir.iterdir():
            if not entry.is_dir():
                continue
            manifest = self._load_manifest(entry)
            if manifest is None:
                continue
            if manifest.get("committed") and manifest.get("committed_at", 0) < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        if removed:
            log.info("[backup] pruned %d old backups", removed)
        return removed

    def _prune_if_needed(self) -> None:
        """如果备份数量超过上限，清理最旧的。"""
        backups = self.list_backups()
        if len(backups) > MAX_BACKUPS:
            # 按时间排序，删除最旧的 committed 备份
            committed = [b for b in backups if b.get("committed")]
            committed.sort(key=lambda b: b.get("committed_at", b.get("timestamp", 0)))
            excess = len(backups) - MAX_BACKUPS
            for b in committed[:excess]:
                self.cleanup(b["backup_id"])

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_backups(self) -> list[dict[str, Any]]:
        """列出所有备份的 manifest。"""
        results: list[dict[str, Any]] = []
        for entry in sorted(self._backup_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest = self._load_manifest(entry)
            if manifest is not None:
                results.append(manifest)
        return results

    def get_backup(self, backup_id: str) -> dict[str, Any] | None:
        """获取指定备份的 manifest。"""
        return self._load_manifest(self._backup_dir / backup_id)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _make_backup_id(self, cycle_id: str = "") -> str:
        ts = time.strftime("%Y%m%d_%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        prefix = f"{cycle_id}_" if cycle_id else ""
        return f"{prefix}{ts}_{suffix}"

    def _load_manifest(self, backup_root: Path) -> dict[str, Any] | None:
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("[backup] failed to load manifest %s: %s", backup_root.name, e)
            return None

    @staticmethod
    def _relative_path(file_path: Path) -> Path:
        """将绝对路径转为相对于项目根的路径（用于备份目录结构）。"""
        try:
            cwd = Path.cwd()
            return file_path.resolve().relative_to(cwd)
        except ValueError:
            return Path(file_path.name)
