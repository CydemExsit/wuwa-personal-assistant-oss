"""Friendly storage exceptions suitable for presentation by the desktop UI."""

from __future__ import annotations

from pathlib import Path


class StorageError(RuntimeError):
    """Base class for recoverable local persistence failures."""


class MigrationError(StorageError):
    """Raised when a versioned schema migration cannot complete."""

    def __init__(self, database_path: Path, detail: str) -> None:
        self.database_path = database_path
        self.detail = detail
        super().__init__(
            f"無法更新本機資料庫結構: {database_path}。原始資料不會被自動清除。詳細資訊: {detail}"
        )


class DatabaseHealthError(StorageError):
    """Raised when an opened SQLite database is not safe to use."""

    def __init__(self, database_path: Path, detail: str) -> None:
        self.database_path = database_path
        self.detail = detail
        super().__init__(
            f"本機資料庫目前無法安全使用: {database_path}。"
            "程式不會覆寫或重設這個檔案; 請從備份還原或匯出診斷資訊。"
            f"詳細資訊: {detail}"
        )


class DatabaseCorruptionError(DatabaseHealthError):
    """Raised for malformed pages or a failing SQLite integrity check."""
