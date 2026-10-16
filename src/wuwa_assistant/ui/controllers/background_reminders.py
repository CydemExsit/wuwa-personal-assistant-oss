"""Configure the user-authorized Windows reminder task without blocking Qt."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTime, Signal, Slot

from wuwa_assistant.reminders.background import ENABLED_KEY, HOUR_KEY, MINUTE_KEY
from wuwa_assistant.reminders.windows_task import configure_task
from wuwa_assistant.storage.settings import SettingsService
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.pages.settings import SettingsPage


class BackgroundRemindersPresenter(QObject):
    _done = Signal(bool, int, int, str, bool)

    def __init__(
        self,
        *,
        settings: SettingsService,
        page: SettingsPage,
        data_root: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._page = page
        self._data_root = data_root
        self._closing = False
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-reminder-setup")
        self._done.connect(self._handle, Qt.ConnectionType.QueuedConnection)
        page.background_reminders_requested.connect(self.configure)

    @Slot(bool, int, int)
    def configure(self, enabled: bool, hour: int, minute: int) -> None:
        if self._closing:
            return
        self._page.reminder_apply_button.setEnabled(False)
        self._page.reminder_status_label.setText("正在更新 Windows 週末提醒排程…")

        def work() -> None:
            try:
                message = configure_task(self._data_root, enabled=enabled, hour=hour, minute=minute)
                self._settings.set(ENABLED_KEY, enabled)
                self._settings.set(HOUR_KEY, hour)
                self._settings.set(MINUTE_KEY, minute)
                success = True
            except (OSError, ValueError, RuntimeError) as exc:
                message = str(exc)
                success = False
            if not self._closing:
                self._done.emit(enabled, hour, minute, message, success)

        self._executor.submit(work)

    @Slot(bool, int, int, str, bool)
    def _handle(self, enabled: bool, hour: int, minute: int, message: str, success: bool) -> None:
        if self._closing:
            return
        self._page.reminder_apply_button.setEnabled(True)
        self._page.reminder_status_label.setText(message)
        if success:
            self._page.background_reminders_check.setChecked(enabled)
            self._page.reminder_time_edit.setTime(QTime(hour, minute))

    def prepare_shutdown(self) -> None:
        self._closing = True
        self._executor.prepare_shutdown()

    def shutdown(self) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        return True
