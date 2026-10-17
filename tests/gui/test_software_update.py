from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtWidgets import QMainWindow, QMessageBox

from tests.gui.helpers import MemorySettings
from wuwa_assistant.ui.controllers.software_update import SoftwareUpdatePresenter
from wuwa_assistant.ui.widgets.software_update import SoftwareUpdatePanel
from wuwa_assistant.updates import PreparedUpdate, Release, UpdateError


def test_worker_failure_cancel_retry_and_close_are_safe(qtbot: Any, tmp_path: Path) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SoftwareUpdatePanel()
    window.setCentralWidget(panel)
    presenter = SoftwareUpdatePresenter(panel, window, MemorySettings(), tmp_path)
    presenter.panel.auto_check.setChecked(False)

    def fail() -> None:
        raise UpdateError("離線")

    presenter._submit(fail, "checking")
    qtbot.waitUntil(lambda: not presenter._busy)
    assert panel.status.text() == "離線"
    started, cancelled = Event(), Event()

    def wait_for_cancel() -> None:
        started.set()
        presenter._cancel.wait(3)
        cancelled.set()

    presenter._submit(wait_for_cancel, "downloading")
    qtbot.waitUntil(started.is_set)
    presenter.prepare_shutdown()
    assert presenter.finish_shutdown()
    assert cancelled.is_set()


def test_install_is_deferred_to_app_shutdown_and_can_be_declined(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    import wuwa_assistant.ui.controllers.software_update as module

    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SoftwareUpdatePanel()
    window.setCentralWidget(panel)
    presenter = SoftwareUpdatePresenter(panel, window, MemorySettings(), tmp_path)
    panel.auto_check.setChecked(False)
    window.show()
    presenter._show_result(PreparedUpdate(tmp_path / "setup.exe", "hash", "9.0.0"), "")
    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    presenter.install()
    assert presenter.pending is None and window.isVisible()
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    presenter.install()
    assert presenter.pending == presenter.prepared and not window.isVisible()
    presenter.prepare_shutdown()
    presenter.finish_shutdown()


def test_release_offer_and_prepared_state_are_distinct(qtbot: Any, tmp_path: Path) -> None:
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SoftwareUpdatePanel()
    window.setCentralWidget(panel)
    presenter = SoftwareUpdatePresenter(panel, window, MemorySettings(), tmp_path)
    panel.auto_check.setChecked(False)
    presenter._show_result(Release("9.0.0", "url", "hash", 100), "")
    assert not panel.download.isHidden() and panel.install.isHidden()
    assert presenter.prepared is None
    presenter._show_result(PreparedUpdate(tmp_path / "setup.exe", "hash", "9.0.0"), "")
    assert not panel.install.isHidden()
    presenter.prepare_shutdown()
    presenter.finish_shutdown()


def test_update_waits_for_required_save_and_failed_shutdown_can_retry(
    qtbot: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    import sqlite3

    import wuwa_assistant.ui.controllers.software_update as module
    from wuwa_assistant.ui.main_window import MainWindow

    window = MainWindow(settings=MemorySettings(), data_root=tmp_path)
    qtbot.addWidget(window)
    presenter = SoftwareUpdatePresenter(
        window.settings_page.software_update, window, MemorySettings(), tmp_path / "cache"
    )
    presenter.panel.auto_check.setChecked(False)
    started, release = Event(), Event()
    attempts = []
    database = tmp_path / "required-save.sqlite3"

    def save() -> bool:
        attempts.append(True)
        if len(attempts) == 1:
            return False
        started.set()
        assert release.wait(5)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE saved(value TEXT)")
            connection.execute("INSERT INTO saved VALUES ('committed-before-update')")
        return True

    window.register_shutdown(save, prepare=lambda: None)
    window.register_shutdown(presenter.finish_shutdown, prepare=presenter.prepare_shutdown)
    presenter.prepared = PreparedUpdate(tmp_path / "setup.exe", "hash", "9.0.0")
    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    window.show()
    presenter.install()
    qtbot.waitUntil(lambda: window._shutdown_retry.isVisible())
    assert window.isVisible() and not database.exists()
    window._shutdown_retry.click()
    qtbot.waitUntil(started.is_set)
    assert window.isVisible() and presenter.pending is not None
    release.set()
    qtbot.waitUntil(lambda: not window.isVisible())
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM saved").fetchone() == (
            "committed-before-update",
        )
