from __future__ import annotations

import zipfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot
from sqlalchemy import func, select

from tests.gui.helpers import MemorySettings
from wuwa_assistant.application.data_management import DataManagementService
from wuwa_assistant.application.onboarding import DemoDataService
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord
from wuwa_assistant.ui.controllers.data_management import DataManagementPresenter
from wuwa_assistant.ui.pages.settings import SettingsPage


def test_presenter_runs_explicit_backup_restore_diagnostics_and_demo_actions(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "local-data")
    database = create_database(paths)
    page = SettingsPage(MemorySettings(), data_root=paths.root)
    confirmations: list[tuple[str, str]] = []

    def choose_import() -> Path | None:
        exports = tuple(paths.exports_dir.glob("*.json"))
        return exports[0] if exports else None

    def confirm(title: str, message: str) -> bool:
        confirmations.append((title, message))
        return True

    presenter = DataManagementPresenter(
        service=DataManagementService(engine=database.engine, paths=paths),
        demo_service=DemoDataService(database.session_factory),
        page=page,
        import_file_chooser=choose_import,
        confirm=confirm,
        active_demo=True,
        parent=page,
    )
    restart_spy = QSignalSpy(presenter.restart_required)
    try:
        page.backup_requested.emit()
        qtbot.waitUntil(lambda: bool(tuple(paths.backups_dir.rglob("*.sqlite3"))))
        qtbot.waitUntil(lambda: "備份完成" in page.status_label.text())

        page.export_requested.emit()
        qtbot.waitUntil(lambda: bool(tuple(paths.exports_dir.glob("*.json"))))
        qtbot.waitUntil(lambda: "匯出完成" in page.status_label.text())

        database.settings.set("capture/sample_rate_hz", 8.0)
        page.import_requested.emit()
        pending = paths.restore_staging_dir / "pending-restore.sqlite3"
        qtbot.waitUntil(pending.exists)
        qtbot.waitUntil(lambda: "重新啟動" in page.status_label.text())
        assert database.settings.get("capture/sample_rate_hz") == 8.0
        assert any("不會熱替換" in message for _title, message in confirmations)

        page.diagnostics_requested.emit()
        qtbot.waitUntil(lambda: bool(tuple(paths.diagnostics_dir.glob("*.zip"))))
        qtbot.waitUntil(lambda: "診斷包完成" in page.status_label.text())
        diagnostic = next(paths.diagnostics_dir.glob("*.zip"))
        with zipfile.ZipFile(diagnostic) as archive:
            names = set(archive.namelist())
        assert "diagnostic-manifest.json" in names
        assert "database-health.json" in names
        assert all(not name.endswith((".sqlite3", ".png", ".jpg")) for name in names)
        assert any("明確排除: database" in message for _title, message in confirmations)

        page.demo_create_requested.emit()
        qtbot.waitUntil(lambda: "示範資料已建立" in page.status_label.text())
        with database.session_factory() as session:
            assert (
                session.scalar(
                    select(func.count(DataSetRecord.id)).where(DataSetRecord.kind == "demo")
                )
                == 1
            )

        page.demo_clear_requested.emit()
        qtbot.waitUntil(lambda: restart_spy.count() == 1)
        with database.session_factory() as session:
            assert (
                session.scalar(
                    select(func.count(DataSetRecord.id)).where(DataSetRecord.kind == "demo")
                )
                == 0
            )
        assert any("程式會安全關閉" in message for _title, message in confirmations)
    finally:
        presenter.shutdown()
        page.close()
        QCoreApplication.processEvents()
        database.dispose()
