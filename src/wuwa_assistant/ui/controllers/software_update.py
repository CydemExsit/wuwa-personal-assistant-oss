"""Cancellable update work; installation is deferred until safe application exit."""

from __future__ import annotations

import sys
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox

from wuwa_assistant.distribution import ASSET_PREFIX, UPDATE_REPOSITORY
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.settings_store import SettingsStore, setting_bool
from wuwa_assistant.ui.widgets.software_update import SoftwareUpdatePanel
from wuwa_assistant.updates import PreparedUpdate, Release, UpdateClient, UpdateError

AUTO_CHECK_KEY = "software_updates/auto_check"


class SoftwareUpdatePresenter(QObject):
    _completed = Signal(object, str)
    _progress = Signal(int)
    available = Signal(str)

    def __init__(
        self, panel: SoftwareUpdatePanel, window: QMainWindow, settings: SettingsStore, cache: Path
    ) -> None:
        super().__init__(window)
        self.panel, self.window = panel, window
        self.client = UpdateClient(cache)
        self._executor = BackgroundExecutor(thread_name_prefix="wuwa-update")
        self._cancel = Event()
        self._closing = False
        self._busy = False
        self.release: Release | None = None
        self.prepared: PreparedUpdate | None = None
        self.pending: PreparedUpdate | None = None
        self._local_file = False
        self._completed.connect(self._show_result, Qt.ConnectionType.QueuedConnection)
        self._progress.connect(panel.progress.setValue, Qt.ConnectionType.QueuedConnection)
        panel.check_requested.connect(self.check)
        panel.import_requested.connect(self.choose_installer)
        panel.download_requested.connect(self.download)
        panel.install_requested.connect(self.install)
        panel.cancel_requested.connect(self._cancel.set)
        panel.auto_check.setChecked(setting_bool(settings.get(AUTO_CHECK_KEY), True))
        panel.auto_check.toggled.connect(lambda value: settings.set(AUTO_CHECK_KEY, value))
        if UPDATE_REPOSITORY is not None:
            QTimer.singleShot(2000, self, self._automatic_check)

    def _automatic_check(self) -> None:
        if not self._closing and self.panel.auto_check.isChecked():
            self.check()

    def check(self) -> None:
        if UPDATE_REPOSITORY is not None:
            self._submit(lambda: self.client.check(self._cancel), "正在檢查正式新版…")

    def download(self) -> None:
        release = self.release
        if release is not None:
            self._local_file = False
            self._submit(
                lambda: self.client.download(release, self._cancel, self._progress.emit),
                "正在下載新版, 完成後會驗證 SHA-256…",
            )

    def choose_installer(self) -> None:
        if self._busy or self._closing:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self.window,
            "選擇收到的正式新版安裝檔",
            "",
            f"同系列安裝檔 ({ASSET_PREFIX}-*-Setup.exe)",
        )
        if path:
            self._local_file = True
            self._submit(
                lambda: self.client.import_installer(Path(path), self._cancel), "正在準備安裝檔…"
            )

    def _submit(self, work: Callable[[], Release | PreparedUpdate | None], message: str) -> None:
        if self._closing or self._busy:
            return
        self._busy = True
        self._cancel.clear()
        self.prepared = None
        self.release = None
        self.panel.install.hide()
        self.panel.download.hide()
        self.panel.status.setText(message)
        self.panel.set_busy(True)
        self._executor.submit(work, read_only=True).add_done_callback(self._done)

    def _done(self, future: Future[Release | PreparedUpdate | None]) -> None:
        if self._closing:
            return
        try:
            result, error = future.result(), ""
        except UpdateError as exc:
            result, error = None, str(exc)
        except Exception:
            result, error = None, "暫時無法完成更新, 請重試; 原版本仍可正常使用。"
        self._completed.emit(result, error)

    def _show_result(self, result: Release | PreparedUpdate | None, error: str) -> None:
        if self._closing:
            return
        self._busy = False
        self.panel.set_busy(False)
        if error or self._cancel.is_set():
            self.panel.status.setText(error or "已取消, 原版本仍可使用。")
        elif isinstance(result, Release):
            self.release = result
            self.panel.status.setText(
                f"有新版 {result.version}, 下載約 {result.size / 1024 / 1024:.0f} MB。"
                "安裝會保留資料與設定。"
            )
            self.panel.download.show()
            self.available.emit(result.version)
        elif isinstance(result, PreparedUpdate):
            self.prepared = result
            self.panel.status.setText(
                f"新版 {result.version} 已準備好。安裝時會先保存並關閉工具, 完成後重新開啟。"
            )
            self.panel.install.show()
        else:
            self.panel.status.setText("目前已是最新正式版。")

    def install(self) -> None:
        if self._busy or self._closing or self.prepared is None:
            return
        if not getattr(sys, "frozen", False):
            self.panel.status.setText("請在 Windows 安裝版使用更新; 開發環境請自行更新原始碼。")
            return
        warning = (
            "請確認此安裝檔由工具作者提供。\n"
            if self._local_file
            else "安裝檔已通過正式發布來源的 SHA-256 驗證。\n"
        )
        answer = QMessageBox.question(
            self.window,
            "安裝新版",
            warning + "現在保存並關閉工具、安裝新版後重新開啟?\n角色、歷史、設定與備份都會保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.pending = self.prepared
            self.window.close()

    def prepare_shutdown(self) -> None:
        self._closing = True
        self._cancel.set()
        self._executor.prepare_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        return True
