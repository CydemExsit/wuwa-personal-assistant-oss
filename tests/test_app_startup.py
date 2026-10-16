from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray

from wuwa_assistant.application.data_management import DataManagementService
from wuwa_assistant.storage import AppPaths, StorageError, create_database

app_main = importlib.import_module("wuwa_assistant.app.main")


class MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any, **_kwargs: object) -> object:
        self.values[key] = value
        return value


def test_database_settings_adapter_round_trips_qbytearray() -> None:
    service = MemorySettings()
    settings = app_main.DatabaseSettingsStore(service)
    geometry = QByteArray(b"\x00\xffordinary-window-geometry")

    settings.set("ui/main_window_geometry", geometry)

    stored = service.values["ui/main_window_geometry"]
    assert isinstance(stored, dict)
    assert bytes(settings.get("ui/main_window_geometry")) == bytes(geometry)
    settings.sync()


def test_ocr_self_test_mode_does_not_create_storage_or_qt_application(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    unused_root = tmp_path / "must-not-be-created"
    calls: list[bool] = []
    monkeypatch.setattr(app_main, "_run_ocr_self_test", lambda: calls.append(True) or 0)
    monkeypatch.setattr(
        app_main,
        "_application",
        lambda: pytest.fail("OCR self-test must not create a Qt application"),
    )

    assert app_main.main(["--data-root", str(unused_root), "--ocr-self-test"]) == 0
    assert calls == [True]
    assert not unused_root.exists()


def test_reminder_mode_skips_onboarding_restore_window_and_capture(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import wuwa_assistant.reminders.background as background

    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        background,
        "run_background_reminders",
        lambda paths, *, dry_run: calls.append((paths.root, dry_run)) or 0,
    )
    monkeypatch.setattr(
        app_main, "_application", lambda: pytest.fail("reminder creates main Qt app")
    )
    monkeypatch.setattr(
        app_main, "activate_staged_restore", lambda *_: pytest.fail("reminder restores DB")
    )
    assert (
        app_main.main(["--data-root", str(tmp_path), "--check-reminders", "--reminder-dry-run"])
        == 0
    )
    assert calls == [(tmp_path, True)]
    assert not (tmp_path / "data").exists()


def test_smoke_startup_uses_injected_data_root_without_loading_capture_or_ocr(
    tmp_path: Path,
) -> None:
    loaded_before = set(sys.modules)

    assert app_main.main(["--data-root", str(tmp_path), "--smoke-test"]) == 0
    # A second start proves that database-backed QByteArray window state can be restored.
    assert app_main.main(["--data-root", str(tmp_path), "--smoke-test"]) == 0

    application = app_main._application()
    assert not application.windowIcon().isNull()
    assert {
        (size.width(), size.height()) for size in application.windowIcon().availableSizes()
    } == {(size, size) for size in (16, 24, 32, 48, 64, 128, 256)}

    paths = AppPaths.from_root(tmp_path)
    assert paths.database_path.is_file()
    assert paths.logs_dir.is_dir()
    assert paths.backups_dir.is_dir()
    assert paths.exports_dir.is_dir()
    assert paths.low_confidence_debug_dir.is_dir()
    log_text = (paths.logs_dir / "application.log").read_text(encoding="utf-8")
    assert "application_start version=" in log_text
    assert str(tmp_path) not in log_text

    newly_loaded = set(sys.modules) - loaded_before
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in newly_loaded
        for prefix in ("wuwa_assistant.capture", "wuwa_assistant.ocr")
    )

    with create_database(paths) as database:
        assert database.health.is_healthy
        encoded_geometry = database.settings.get("ui/main_window_geometry")
        assert isinstance(encoded_geometry, dict)


def test_smoke_startup_reports_storage_error_without_opening_a_dialog(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    def fail_to_open(_paths: AppPaths) -> None:
        raise StorageError("synthetic storage failure")

    monkeypatch.setattr(app_main, "create_database", fail_to_open)

    result = app_main.main(["--data-root", str(tmp_path), "--smoke-test"])

    assert result == 2
    error = capsys.readouterr().err
    assert "無法開啟本機資料" in error
    assert "synthetic storage failure" in error
    assert "沒有刪除或重設任何既有資料" in error


def test_smoke_startup_activates_confirmed_staged_restore_with_rollback(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "restore-on-start")
    with create_database(paths) as database:
        database.settings.set("restore-test", "exported")
        service = DataManagementService(engine=database.engine, paths=paths)
        exported = service.export_json("startup-restore.json")
        database.settings.set("restore-test", "current")
        preview = service.preview_import(exported.path)
        service.stage_import(
            exported.path,
            expected_document_sha256=preview.document_sha256,
        )

    assert app_main.main(["--data-root", str(paths.root), "--smoke-test"]) == 0

    with create_database(paths) as restored:
        assert restored.settings.get("restore-test") == "exported"
    assert not (paths.restore_staging_dir / "pending-restore.sqlite3").exists()
    assert len(tuple((paths.backups_dir / "restore-rollbacks").glob("*.sqlite3"))) == 1
