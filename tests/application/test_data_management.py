from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wuwa_assistant.application.data_management import (
    DataManagementError,
    DataManagementService,
    ExportValidationError,
)
from wuwa_assistant.storage import AppPaths, Database, create_database
from wuwa_assistant.storage.backup import inspect_live_database_shape
from wuwa_assistant.storage.orm import CharacterRecord, DataSetRecord

NOW = datetime(2026, 8, 8, 6, 30, tzinfo=UTC)


def _service(tmp_path: Path) -> tuple[Database, AppPaths, DataManagementService]:
    paths = AppPaths.from_root(tmp_path / "鳴潮 本機資料")
    database = create_database(paths)
    database.settings.set("capture.frequency_hz", 5)
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        session.add(
            CharacterRecord(
                data_set_id=data_set.id,
                name="測試角色",
                level=90,
                notes="只保存在本機",
            )
        )
    service = DataManagementService(
        engine=database.engine,
        paths=paths,
        clock=lambda: NOW,
        app_version="0.9-test",
    )
    return database, paths, service


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_consistent_sqlite_backup_is_self_contained(tmp_path: Path) -> None:
    database, paths, service = _service(tmp_path)
    try:
        info = service.create_sqlite_backup("日常 備份.sqlite3")

        assert info.path == paths.backups_dir / "database" / "日常 備份.sqlite3"
        assert info.schema_revision == database.health.schema_revision
        assert info.size_bytes == info.path.stat().st_size
        assert hashlib.sha256(info.path.read_bytes()).hexdigest() == info.sha256
        assert info.counts["characters"] == 1
        assert not info.path.with_name(f"{info.path.name}-wal").exists()
        assert not info.path.with_name(f"{info.path.name}-shm").exists()
        with sqlite3.connect(info.path) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert connection.execute("SELECT name FROM characters").fetchone() == ("測試角色",)
    finally:
        database.dispose()


def test_schema_fingerprint_is_stable_across_independent_installations(tmp_path: Path) -> None:
    with (
        create_database(AppPaths.from_root(tmp_path / "first install")) as first,
        create_database(AppPaths.from_root(tmp_path / "second install")) as second,
    ):
        assert inspect_live_database_shape(first.engine) == inspect_live_database_shape(
            second.engine
        )


def test_export_can_be_validated_by_an_independent_same_schema_installation(
    tmp_path: Path,
) -> None:
    source_database, _source_paths, source_service = _service(tmp_path / "source")
    target_paths = AppPaths.from_root(tmp_path / "target")
    target_database = create_database(target_paths)
    try:
        receipt = source_service.export_json("portable.json")
        target_service = DataManagementService(
            engine=target_database.engine,
            paths=target_paths,
            clock=lambda: NOW,
            app_version="0.9-test",
        )

        preview = target_service.preview_import(receipt.path)
        staged = target_service.stage_import(
            receipt.path,
            expected_document_sha256=preview.document_sha256,
        )

        assert staged.database_sha256 == receipt.manifest.database_sha256
        assert target_database.settings.get("capture.frequency_hz") is None
    finally:
        target_database.dispose()
        source_database.dispose()


def test_json_export_preview_and_staged_restore_round_trip(tmp_path: Path) -> None:
    database, paths, service = _service(tmp_path)
    try:
        receipt = service.export_json("我的 鳴潮 備份.json")
        document = _read_json(receipt.path)
        manifest = document["manifest"]
        assert isinstance(manifest, dict)
        assert set(document) == {"manifest", "database_b64"}
        assert manifest["format"] == "wuwa-personal-assistant-json-backup"
        assert manifest["format_version"] == 1
        assert manifest["schema_revision"] == database.health.schema_revision
        assert manifest["created_at"] == "2026-08-08T06:30:00Z"
        assert manifest["table_counts"] == receipt.manifest.counts
        assert manifest["database_sha256"] == receipt.manifest.database_sha256

        preview = service.preview_import(receipt.path)
        assert preview.document_sha256 == receipt.document_sha256
        assert preview.restart_required is True

        database.settings.set("capture.frequency_hz", 8)
        staged = service.stage_import(
            receipt.path,
            expected_document_sha256=preview.document_sha256,
        )

        assert staged.staged_path == paths.restore_staging_dir / "pending-restore.sqlite3"
        assert staged.restart_required is True
        assert staged.database_sha256 == receipt.manifest.database_sha256
        assert database.settings.get("capture.frequency_hz") == 8
        with sqlite3.connect(staged.staged_path) as connection:
            setting_json = connection.execute(
                "SELECT value_json FROM app_settings WHERE key = ?",
                ("capture.frequency_hz",),
            ).fetchone()
            assert setting_json is not None
            assert setting_json[0] == 5
            assert connection.execute("SELECT name FROM characters").fetchone() == ("測試角色",)

        restored_paths = AppPaths.from_root(tmp_path / "模擬重新啟動")
        restored_paths.ensure_created()
        restored_paths.database_path.write_bytes(staged.staged_path.read_bytes())
        with create_database(restored_paths) as restored:
            assert restored.settings.get("capture.frequency_hz") == 5
            with restored.session_factory() as session:
                assert session.query(CharacterRecord).one().name == "測試角色"
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "incomplete",
        "tampered_payload",
        "corrupt_sqlite",
        "wrong_schema",
        "altered_database_schema",
        "wrong_counts",
    ],
)
def test_invalid_import_never_changes_live_database(
    tmp_path: Path,
    mutation: str,
) -> None:
    database, _paths, service = _service(tmp_path)
    try:
        receipt = service.export_json("valid.json")
        document = _read_json(receipt.path)
        manifest = document["manifest"]
        assert isinstance(manifest, dict)
        if mutation == "incomplete":
            document = {"manifest": manifest}
        elif mutation == "tampered_payload":
            encoded = document["database_b64"]
            assert isinstance(encoded, str)
            tampered_payload = bytearray(base64.b64decode(encoded))
            tampered_payload[-1] ^= 1
            document["database_b64"] = base64.b64encode(tampered_payload).decode("ascii")
        elif mutation == "corrupt_sqlite":
            corrupt_payload = b"SQLite format 3\x00" + (b"\x00" * 256)
            document["database_b64"] = base64.b64encode(corrupt_payload).decode("ascii")
            manifest["database_size_bytes"] = len(corrupt_payload)
            manifest["database_sha256"] = hashlib.sha256(corrupt_payload).hexdigest()
        elif mutation == "wrong_schema":
            manifest["schema_revision"] = "9999_foreign_schema"
        elif mutation == "altered_database_schema":
            encoded = document["database_b64"]
            assert isinstance(encoded, str)
            altered_path = tmp_path / "altered.sqlite3"
            altered_path.write_bytes(base64.b64decode(encoded))
            connection = sqlite3.connect(altered_path)
            try:
                connection.execute("DROP TABLE tasks")
                connection.commit()
            finally:
                connection.close()
            altered_payload = altered_path.read_bytes()
            document["database_b64"] = base64.b64encode(altered_payload).decode("ascii")
            manifest["database_size_bytes"] = len(altered_payload)
            manifest["database_sha256"] = hashlib.sha256(altered_payload).hexdigest()
        elif mutation == "wrong_counts":
            counts = manifest["table_counts"]
            assert isinstance(counts, dict)
            counts["characters"] = 999

        invalid = tmp_path / f"{mutation}.json"
        _write_json(invalid, document)
        before = database.settings.get("capture.frequency_hz")

        with pytest.raises(ExportValidationError):
            service.preview_import(invalid)

        assert database.settings.get("capture.frequency_hz") == before == 5
        assert not service.pending_restore_path.exists()
    finally:
        database.dispose()


def test_stage_revalidates_preview_digest_and_preserves_existing_stage(tmp_path: Path) -> None:
    database, _paths, service = _service(tmp_path)
    try:
        receipt = service.export_json("previewed.json")
        preview = service.preview_import(receipt.path)
        changed = _read_json(receipt.path)
        _write_json(receipt.path, changed)

        with pytest.raises(ExportValidationError, match="changed after preview"):
            service.stage_import(
                receipt.path,
                expected_document_sha256=preview.document_sha256,
            )

        assert not service.pending_restore_path.exists()
        assert database.settings.get("capture.frequency_hz") == 5

        fresh_preview = service.preview_import(receipt.path)
        first = service.stage_import(
            receipt.path,
            expected_document_sha256=fresh_preview.document_sha256,
        )
        first_bytes = first.staged_path.read_bytes()
        with pytest.raises(ExportValidationError, match="pending restore"):
            service.stage_import(
                receipt.path,
                expected_document_sha256=fresh_preview.document_sha256,
            )
        assert first.staged_path.read_bytes() == first_bytes
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../escape.json",
        "..\\escape.json",
        "C:\\outside.json",
        "nested/file.json",
        "CON.json",
        "wrong.zip",
    ],
)
def test_windows_and_traversal_output_paths_are_rejected(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    database, paths, service = _service(tmp_path)
    try:
        with pytest.raises(DataManagementError):
            service.export_json(unsafe_name)
        assert tuple(paths.exports_dir.glob("*.json")) == ()
    finally:
        database.dispose()


def test_import_rejects_directory_and_non_json_source(tmp_path: Path) -> None:
    database, _paths, service = _service(tmp_path)
    try:
        with pytest.raises(ExportValidationError):
            service.preview_import(tmp_path)
        non_json = tmp_path / "backup.sqlite3"
        non_json.write_bytes(b"not an export")
        with pytest.raises(ExportValidationError):
            service.preview_import(non_json)
    finally:
        database.dispose()


def test_diagnostic_bundle_is_empty_by_default_and_strictly_allowlisted(tmp_path: Path) -> None:
    database, paths, service = _service(tmp_path)
    try:
        (paths.logs_dir / "assistant.log").write_text("startup ok\n", encoding="utf-8")
        (paths.logs_dir / "binary.log").write_bytes(b"not\x00text")
        (paths.debug_dir / "full-frame.png").write_bytes(b"fake image")

        empty = service.preview_diagnostic_bundle()
        assert empty.categories == ()
        assert empty.entries == ()
        with pytest.raises(DataManagementError, match="explicitly confirmed"):
            service.create_diagnostic_bundle(empty, confirmed_categories=())
        for forbidden in ("database", "screenshots", "debug-images", "full-frame"):
            with pytest.raises(DataManagementError, match="not allowlisted"):
                service.preview_diagnostic_bundle((forbidden,))

        preview = service.preview_diagnostic_bundle(("logs", "version", "health"))
        assert preview.categories == ("health", "logs", "version")
        assert {entry.archive_name for entry in preview.entries} == {
            "database-health.json",
            "logs/assistant.log",
            "version.json",
        }
        with pytest.raises(DataManagementError, match="explicitly confirmed"):
            service.create_diagnostic_bundle(
                preview,
                confirmed_categories=("health", "version"),
            )

        receipt = service.create_diagnostic_bundle(
            preview,
            confirmed_categories=("health", "logs", "version"),
            filename="診斷 資料.zip",
        )
        with zipfile.ZipFile(receipt.path) as archive:
            names = set(archive.namelist())
            assert names == {
                "diagnostic-manifest.json",
                "database-health.json",
                "logs/assistant.log",
                "version.json",
            }
            assert all(
                not name.casefold().endswith((".sqlite", ".sqlite3", ".db", ".png", ".jpg"))
                for name in names
            )
            manifest = json.loads(archive.read("diagnostic-manifest.json"))
            assert manifest["confirmed_categories"] == ["health", "logs", "version"]
            assert "database" in manifest["excluded_by_design"]
            assert "full-frame" in manifest["excluded_by_design"]
        assert paths.database_path.read_bytes() not in receipt.path.read_bytes()
    finally:
        database.dispose()


def test_diagnostic_bundle_rejects_contents_changed_after_preview(tmp_path: Path) -> None:
    database, paths, service = _service(tmp_path)
    try:
        log_path = paths.logs_dir / "assistant.log"
        log_path.write_text("before\n", encoding="utf-8")
        preview = service.preview_diagnostic_bundle(("logs",))
        log_path.write_text("after\n", encoding="utf-8")

        with pytest.raises(DataManagementError, match="changed after preview"):
            service.create_diagnostic_bundle(
                preview,
                confirmed_categories=("logs",),
            )

        assert tuple(paths.diagnostics_dir.glob("*.zip")) == ()
    finally:
        database.dispose()
