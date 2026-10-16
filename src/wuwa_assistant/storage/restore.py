"""Crash-safe activation of a validated restore before opening the app engine.

This module intentionally accepts paths, never an SQLAlchemy engine.  Call
``activate_staged_restore`` during startup *before* ``create_database``.  It
will refuse activation while WAL/SHM companions indicate that the live
database may still be open, creates a self-contained rollback snapshot, and
only then atomically replaces the live database.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .backup import (
    BackupError,
    DatabaseShape,
    SqliteSnapshotInfo,
    inspect_sqlite_snapshot,
)
from .errors import StorageError
from .paths import AppPaths


class RestoreActivationError(StorageError):
    """Raised when a staged restore cannot be activated without risking data."""

    def __init__(
        self,
        *,
        live_path: Path,
        staged_path: Path,
        detail: str,
        rollback_path: Path | None = None,
    ) -> None:
        self.live_path = live_path
        self.staged_path = staged_path
        self.rollback_path = rollback_path
        self.detail = detail
        super().__init__(f"Cannot safely activate staged restore: {detail}")


@dataclass(frozen=True, slots=True)
class RestoreActivationResult:
    """Receipt for one completed, pre-engine restore activation."""

    live_path: Path
    rollback_path: Path
    schema_revision: str
    restored_sha256: str
    previous_sha256: str


def activate_staged_restore(
    paths: AppPaths,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RestoreActivationResult | None:
    """Activate the fixed staged snapshot before any database engine is opened.

    ``None`` means no pending restore exists.  The function never hot-replaces
    an engine-owned database: it takes no engine, rejects live WAL/SHM
    companions after making the consistent rollback snapshot, and relies on
    Windows file locking as a final guard.  Any failure before ``os.replace``
    leaves both the live database and staged snapshot in place.
    """

    if not isinstance(paths, AppPaths):
        raise TypeError("paths must be AppPaths")
    live_path = paths.database_path
    staged_path = paths.restore_staging_dir / "pending-restore.sqlite3"
    rollback_dir = paths.backups_dir / "restore-rollbacks"

    try:
        _ensure_managed_directory(paths.root, paths.data_dir)
        _ensure_managed_directory(paths.root, paths.restore_staging_dir)
        _ensure_managed_directory(paths.root, rollback_dir)
    except (OSError, ValueError) as exc:
        raise _restore_error(live_path, staged_path, f"unsafe application path: {exc}") from exc

    if staged_path.is_symlink():
        raise _restore_error(live_path, staged_path, "staged database is a symbolic link")
    if not staged_path.exists():
        return None
    if not staged_path.is_file():
        raise _restore_error(live_path, staged_path, "staged database is not a regular file")
    if live_path.is_symlink():
        raise _restore_error(live_path, staged_path, "live database is a symbolic link")
    if not live_path.is_file():
        raise _restore_error(live_path, staged_path, "live database does not exist")
    _reject_companion_files(live_path, staged_path, subject="staged")

    try:
        staged_info = _inspect_detached_snapshot(staged_path)
    except (BackupError, OSError) as exc:
        raise _restore_error(live_path, staged_path, f"staged database is invalid: {exc}") from exc

    try:
        rollback_path = rollback_dir / _rollback_filename(clock, staged_info.schema_revision)
    except ValueError as exc:
        raise _restore_error(live_path, staged_path, str(exc)) from exc
    try:
        live_info = _create_consistent_path_backup(live_path, rollback_path)
    except (BackupError, OSError, sqlite3.Error) as exc:
        raise _restore_error(
            live_path,
            staged_path,
            f"cannot create rollback snapshot: {exc}",
            rollback_path=rollback_path if rollback_path.exists() else None,
        ) from exc

    try:
        _reject_companion_files(
            live_path,
            staged_path,
            subject="live",
            rollback_path=rollback_path,
        )
        _assert_compatible_schema(live_info, staged_info)
        if staged_path.stat().st_dev != live_path.parent.stat().st_dev:
            raise RestoreActivationError(
                live_path=live_path,
                staged_path=staged_path,
                rollback_path=rollback_path,
                detail="staged and live databases are not on the same filesystem",
            )

        expected_shape = DatabaseShape(
            schema_revision=live_info.schema_revision,
            table_names=tuple(name for name, _count in live_info.table_counts),
            schema_sha256=live_info.schema_sha256,
        )
        confirmed_stage = _inspect_detached_snapshot(
            staged_path,
            expected_shape=expected_shape,
            expected_counts=staged_info.counts,
        )
        if confirmed_stage.sha256 != staged_info.sha256:
            raise RestoreActivationError(
                live_path=live_path,
                staged_path=staged_path,
                rollback_path=rollback_path,
                detail="staged database changed during activation",
            )
        _reject_companion_files(
            live_path,
            staged_path,
            subject="staged",
            rollback_path=rollback_path,
        )
        _reject_companion_files(
            live_path,
            staged_path,
            subject="live",
            rollback_path=rollback_path,
        )
        _verify_live_matches_rollback(live_path, live_info)
        _reject_companion_files(
            live_path,
            staged_path,
            subject="live",
            rollback_path=rollback_path,
        )
        _flush_file(staged_path)
        if _sha256(staged_path) != confirmed_stage.sha256:
            raise RestoreActivationError(
                live_path=live_path,
                staged_path=staged_path,
                rollback_path=rollback_path,
                detail="staged database changed immediately before activation",
            )
        os.replace(staged_path, live_path)
    except RestoreActivationError:
        raise
    except BackupError as exc:
        raise _restore_error(
            live_path,
            staged_path,
            f"restore validation failed: {exc}",
            rollback_path=rollback_path,
        ) from exc
    except (OSError, sqlite3.Error) as exc:
        raise _restore_error(
            live_path,
            staged_path,
            f"atomic replacement failed; close all database users and retry: {exc}",
            rollback_path=rollback_path,
        ) from exc

    return RestoreActivationResult(
        live_path=live_path,
        rollback_path=rollback_path,
        schema_revision=confirmed_stage.schema_revision,
        restored_sha256=confirmed_stage.sha256,
        previous_sha256=live_info.sha256,
    )


def _create_consistent_path_backup(source: Path, destination: Path) -> SqliteSnapshotInfo:
    if source.is_symlink() or not source.is_file():
        raise BackupError("live database must be a regular, non-symlink file")
    if destination.exists() or destination.is_symlink():
        raise BackupError("rollback destination already exists")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-",
        suffix=".tmp.sqlite3",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=rw"
        source_connection = sqlite3.connect(source_uri, uri=True, timeout=0.25)
        checkpoint = source_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise BackupError("live database WAL is busy; another database user may be open")
        target_connection = sqlite3.connect(temporary)
        source_connection.backup(target_connection)
        target_connection.commit()
        target_connection.close()
        target_connection = None
        source_connection.close()
        source_connection = None
        _flush_file(temporary)
        inspected = _inspect_detached_snapshot(temporary)
        if destination.exists() or destination.is_symlink():
            raise BackupError("rollback destination appeared while snapshotting")
        os.replace(temporary, destination)
        return SqliteSnapshotInfo(
            path=destination,
            size_bytes=inspected.size_bytes,
            sha256=inspected.sha256,
            schema_revision=inspected.schema_revision,
            table_counts=inspected.table_counts,
            schema_sha256=inspected.schema_sha256,
        )
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)


def _inspect_detached_snapshot(
    source: Path,
    *,
    expected_shape: DatabaseShape | None = None,
    expected_counts: dict[str, int] | None = None,
) -> SqliteSnapshotInfo:
    """Validate a byte-stable copy so inspection cannot create source sidecars."""

    before_sha256 = _sha256(source)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.stem}-inspect-",
        suffix=".tmp.sqlite3",
        dir=source.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        _flush_file(temporary)
        if _sha256(source) != before_sha256 or _sha256(temporary) != before_sha256:
            raise BackupError("SQLite snapshot changed while it was being validated")
        inspected = inspect_sqlite_snapshot(
            temporary,
            expected_shape=expected_shape,
            expected_counts=expected_counts,
        )
        return SqliteSnapshotInfo(
            path=source,
            size_bytes=inspected.size_bytes,
            sha256=inspected.sha256,
            schema_revision=inspected.schema_revision,
            table_counts=inspected.table_counts,
            schema_sha256=inspected.schema_sha256,
        )
    finally:
        _remove_snapshot_files(temporary)


def _verify_live_matches_rollback(
    live_path: Path,
    rollback: SqliteSnapshotInfo,
) -> None:
    verification_path = rollback.path.parent / (f".live-verification-{os.urandom(8).hex()}.sqlite3")
    try:
        verified = _create_consistent_path_backup(live_path, verification_path)
        if (
            verified.sha256 != rollback.sha256
            or verified.schema_revision != rollback.schema_revision
            or verified.schema_sha256 != rollback.schema_sha256
            or verified.table_counts != rollback.table_counts
        ):
            raise BackupError("live database changed after the rollback snapshot was created")
    finally:
        _remove_snapshot_files(verification_path)


def _remove_snapshot_files(path: Path) -> None:
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        candidate.unlink(missing_ok=True)


def _assert_compatible_schema(
    live: SqliteSnapshotInfo,
    staged: SqliteSnapshotInfo,
) -> None:
    live_tables = tuple(name for name, _count in live.table_counts)
    staged_tables = tuple(name for name, _count in staged.table_counts)
    if (
        live.schema_revision != staged.schema_revision
        or live.schema_sha256 != staged.schema_sha256
        or live_tables != staged_tables
    ):
        raise BackupError("staged schema does not match the existing database schema")


def _reject_companion_files(
    live_path: Path,
    staged_path: Path,
    *,
    subject: str,
    rollback_path: Path | None = None,
) -> None:
    target = staged_path if subject == "staged" else live_path
    for suffix in ("-wal", "-shm"):
        companion = target.with_name(f"{target.name}{suffix}")
        if companion.exists() or companion.is_symlink():
            raise _restore_error(
                live_path,
                staged_path,
                f"{subject} database has a {suffix} companion; another database user may be open",
                rollback_path=rollback_path,
            )


def _ensure_managed_directory(root: Path, directory: Path) -> None:
    managed_root = Path(root)
    managed_directory = Path(directory)
    if not managed_root.is_absolute() or not managed_directory.is_absolute():
        raise ValueError("application paths must be absolute")
    if managed_root.is_symlink():
        raise ValueError("application root must not be a symbolic link")
    managed_root.mkdir(parents=True, exist_ok=True)
    if not managed_root.is_dir():
        raise ValueError("application root is not a directory")
    try:
        relative = managed_directory.relative_to(managed_root)
    except ValueError as exc:
        raise ValueError("managed directory escapes the application root") from exc
    current = managed_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"managed path component is a symbolic link: {current.name}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"managed path component is not a directory: {current.name}")
        else:
            current.mkdir()


def _rollback_filename(clock: Callable[[], datetime], schema_revision: str) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    timestamp = value.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    safe_revision = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in schema_revision
    )[:80]
    nonce = os.urandom(4).hex()
    return f"pre-restore-{timestamp}-{safe_revision}-{nonce}.sqlite3"


def _flush_file(path: Path) -> None:
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


def _restore_error(
    live_path: Path,
    staged_path: Path,
    detail: str,
    *,
    rollback_path: Path | None = None,
) -> RestoreActivationError:
    return RestoreActivationError(
        live_path=live_path,
        staged_path=staged_path,
        rollback_path=rollback_path,
        detail=detail,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "RestoreActivationError",
    "RestoreActivationResult",
    "activate_staged_restore",
]
