from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import wuwa_assistant.storage.restore as restore_module
from wuwa_assistant.storage import (
    AppPaths,
    RestoreActivationError,
    activate_staged_restore,
    create_database,
)
from wuwa_assistant.storage.backup import (
    create_consistent_sqlite_backup,
    inspect_live_database_shape,
)
from wuwa_assistant.storage.orm import CharacterRecord, DataSetRecord

NOW = datetime(2026, 8, 8, 9, 15, tzinfo=UTC)


def _create_database_file(paths: AppPaths, marker: str) -> None:
    with create_database(paths) as database:
        database.settings.set("restore.marker", marker)
        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name=f"dataset-{marker}", kind="primary", is_active=True)
            session.add(data_set)
            session.flush()
            session.add(CharacterRecord(data_set_id=data_set.id, name=f"character-{marker}"))


def _stage_database(paths: AppPaths, tmp_path: Path, marker: str) -> Path:
    source_paths = AppPaths.from_root(tmp_path / f"source-{marker}")
    source_database = create_database(source_paths)
    source_database.settings.set("restore.marker", marker)
    with source_database.session_factory.begin() as session:
        data_set = DataSetRecord(name=f"dataset-{marker}", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        session.add(CharacterRecord(data_set_id=data_set.id, name=f"character-{marker}"))
    paths.restore_staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = paths.restore_staging_dir / "pending-restore.sqlite3"
    try:
        create_consistent_sqlite_backup(
            source_database.engine,
            staged_path,
            expected_shape=inspect_live_database_shape(source_database.engine),
        )
    finally:
        source_database.dispose()
    return staged_path


def _read_marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value_json FROM app_settings WHERE key = ?",
            ("restore.marker",),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0]).strip('"')


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_activate_staged_restore_creates_rollback_and_atomically_replaces_live(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "Windows 鳴潮 資料")
    _create_database_file(paths, "current")
    staged_path = _stage_database(paths, tmp_path, "restored")
    staged_sha256 = _sha256(staged_path)

    result = activate_staged_restore(paths, clock=lambda: NOW)

    assert result is not None
    assert result.live_path == paths.database_path
    assert result.previous_sha256 == _sha256(result.rollback_path)
    assert result.restored_sha256 == staged_sha256
    assert result.schema_revision == "0004_tasks_and_gacha_v1"
    assert result.rollback_path.is_file()
    assert not staged_path.exists()
    assert _read_marker(paths.database_path) == "restored"
    assert _read_marker(result.rollback_path) == "current"
    assert not result.rollback_path.with_name(f"{result.rollback_path.name}-wal").exists()
    assert not result.rollback_path.with_name(f"{result.rollback_path.name}-shm").exists()
    with create_database(paths) as reopened:
        assert reopened.settings.get("restore.marker") == "restored"


def test_activate_is_a_noop_when_no_stage_exists(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "no-stage")
    _create_database_file(paths, "current")
    before = _read_marker(paths.database_path)

    assert activate_staged_restore(paths, clock=lambda: NOW) is None
    assert _read_marker(paths.database_path) == before == "current"
    assert tuple((paths.backups_dir / "restore-rollbacks").iterdir()) == ()


@pytest.mark.parametrize("invalid_bytes", [b"", b"not sqlite", b"SQLite format 3\x00bad"])
def test_invalid_stage_never_modifies_live_or_creates_rollback(
    tmp_path: Path,
    invalid_bytes: bytes,
) -> None:
    paths = AppPaths.from_root(tmp_path / "invalid-stage")
    _create_database_file(paths, "current")
    paths.restore_staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = paths.restore_staging_dir / "pending-restore.sqlite3"
    staged_path.write_bytes(invalid_bytes)
    before = _read_marker(paths.database_path)

    with pytest.raises(RestoreActivationError, match="staged database is invalid"):
        activate_staged_restore(paths, clock=lambda: NOW)

    assert _read_marker(paths.database_path) == before == "current"
    assert staged_path.read_bytes() == invalid_bytes
    assert tuple((paths.backups_dir / "restore-rollbacks").iterdir()) == ()


def test_foreign_key_invalid_stage_is_rejected_before_rollback(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "invalid-fk")
    _create_database_file(paths, "current")
    staged_path = _stage_database(paths, tmp_path, "restored")
    connection = sqlite3.connect(staged_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE characters SET data_set_id = 'missing-dataset'")
        connection.commit()
    finally:
        connection.close()
    before = _read_marker(paths.database_path)

    with pytest.raises(RestoreActivationError, match="foreign key violations"):
        activate_staged_restore(paths, clock=lambda: NOW)

    assert _read_marker(paths.database_path) == before == "current"
    assert staged_path.exists()
    assert tuple((paths.backups_dir / "restore-rollbacks").iterdir()) == ()


def test_schema_mismatch_preserves_live_and_reports_rollback(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "schema-mismatch")
    _create_database_file(paths, "current")
    staged_path = _stage_database(paths, tmp_path, "restored")
    connection = sqlite3.connect(staged_path)
    try:
        connection.execute("DROP TABLE tasks")
        connection.commit()
    finally:
        connection.close()
    before = _read_marker(paths.database_path)

    with pytest.raises(RestoreActivationError, match="schema does not match") as caught:
        activate_staged_restore(paths, clock=lambda: NOW)

    assert _read_marker(paths.database_path) == before == "current"
    assert staged_path.exists()
    assert caught.value.rollback_path is not None
    assert caught.value.rollback_path.is_file()
    assert _read_marker(caught.value.rollback_path) == "current"


def test_atomic_replace_failure_leaves_live_and_stage_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "replace-failure")
    _create_database_file(paths, "current")
    staged_path = _stage_database(paths, tmp_path, "restored")
    live_before = _read_marker(paths.database_path)
    stage_before = _sha256(staged_path)
    real_replace = os.replace

    def fail_live_replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        if Path(source) == staged_path and Path(destination) == paths.database_path:
            raise PermissionError("simulated Windows lock")
        real_replace(source, destination)

    monkeypatch.setattr(restore_module.os, "replace", fail_live_replace)

    with pytest.raises(RestoreActivationError, match="atomic replacement failed") as caught:
        activate_staged_restore(paths, clock=lambda: NOW)

    assert _read_marker(paths.database_path) == live_before == "current"
    assert _sha256(staged_path) == stage_before
    assert caught.value.rollback_path is not None
    assert caught.value.rollback_path.is_file()


def test_live_database_change_after_rollback_aborts_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "live-race")
    _create_database_file(paths, "current")
    staged_path = _stage_database(paths, tmp_path, "restored")
    original_compare = restore_module._assert_compatible_schema

    def compare_then_change_live(
        live: restore_module.SqliteSnapshotInfo,
        staged: restore_module.SqliteSnapshotInfo,
    ) -> None:
        original_compare(live, staged)
        connection = sqlite3.connect(paths.database_path)
        try:
            connection.execute(
                "UPDATE app_settings SET value_json = ? WHERE key = ?",
                ('"raced"', "restore.marker"),
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()

    monkeypatch.setattr(restore_module, "_assert_compatible_schema", compare_then_change_live)

    with pytest.raises(RestoreActivationError, match="live database changed") as caught:
        activate_staged_restore(paths, clock=lambda: NOW)

    assert _read_marker(paths.database_path) == "raced"
    assert staged_path.exists()
    assert caught.value.rollback_path is not None
    assert _read_marker(caught.value.rollback_path) == "current"


def test_open_engine_is_not_hot_replaced(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "open-engine")
    database = create_database(paths)
    database.settings.set("restore.marker", "current")
    staged_path = _stage_database(paths, tmp_path, "restored")
    try:
        with pytest.raises(
            RestoreActivationError,
            match=r"database user may be open|close all",
        ):
            activate_staged_restore(paths, clock=lambda: NOW)

        assert database.settings.get("restore.marker") == "current"
        assert staged_path.exists()
    finally:
        database.dispose()


def test_stage_with_wal_companion_is_rejected_without_touching_live(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "stage-wal")
    _create_database_file(paths, "current")
    staged_path = _stage_database(paths, tmp_path, "restored")
    wal_path = staged_path.with_name(f"{staged_path.name}-wal")
    wal_path.write_bytes(b"unexpected companion")
    before = _sha256(paths.database_path)

    with pytest.raises(RestoreActivationError, match="staged database has a -wal companion"):
        activate_staged_restore(paths, clock=lambda: NOW)

    assert _sha256(paths.database_path) == before
    assert staged_path.exists()
    assert tuple((paths.backups_dir / "restore-rollbacks").iterdir()) == ()


def test_symlink_stage_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "symlink-stage")
    _create_database_file(paths, "current")
    source_paths = AppPaths.from_root(tmp_path / "outside-source")
    _create_database_file(source_paths, "restored")
    paths.restore_staging_dir.mkdir(parents=True, exist_ok=True)
    staged_path = paths.restore_staging_dir / "pending-restore.sqlite3"
    try:
        staged_path.symlink_to(source_paths.database_path)
    except OSError:
        staged_path.write_bytes(b"simulated symlink target")
        real_is_symlink = Path.is_symlink

        def report_staged_symlink(path: Path) -> bool:
            return path == staged_path or real_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", report_staged_symlink)
    before = _sha256(paths.database_path)

    with pytest.raises(RestoreActivationError, match="symbolic link"):
        activate_staged_restore(paths, clock=lambda: NOW)

    assert _sha256(paths.database_path) == before
    assert staged_path.is_symlink()
