"""Foreground-only polling and persistent notification deduplication."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from wuwa_assistant.public_data.redeem_models import RedeemSnapshot
from wuwa_assistant.public_data.redeem_service import RedeemCodeService
from wuwa_assistant.ui.controllers.background_executor import BackgroundExecutor
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.settings_store import SettingsStore, setting_bool

AUTO_KEY = "redeem_codes/auto_update"
NOTIFIED_KEY = "redeem_codes/notified"
COMPLETED_KEY = "redeem_codes/completed"


class RedeemCodesPresenter(QObject):
    _completed = Signal(object)

    def __init__(
        self, service: RedeemCodeService, window: MainWindow, settings: SettingsStore
    ) -> None:
        super().__init__(window)
        self._service = service
        self._window = window
        self._page = window.redeem_codes_page
        self._settings = settings
        self._closing = False
        self._busy = False
        self._initial = True
        self._snapshot = RedeemSnapshot()
        self._pending: set[str] = set()
        self._muted = False
        completed = settings.get(COMPLETED_KEY, [])
        self._done_codes = (
            {code.upper() for code in completed if isinstance(code, str)}
            if isinstance(completed, list)
            else set()
        )
        saved = settings.get(NOTIFIED_KEY, [])
        self._notified = (
            {s for s in saved if isinstance(s, str)} if isinstance(saved, list) else set()
        )
        self._executor = BackgroundExecutor(max_workers=1, thread_name_prefix="wuwa-redeem")
        self._timer = QTimer(self)
        self._timer.setInterval(30 * 60 * 1000)
        self._timer.timeout.connect(self._automatic_refresh)
        self._completed.connect(self._handle_result, Qt.ConnectionType.QueuedConnection)
        self._page.auto_check.setChecked(setting_bool(settings.get(AUTO_KEY), True))
        self._page.refresh_requested.connect(self.refresh)
        self._page.automatic_changed.connect(self._set_automatic)
        self._page.acknowledged.connect(self.acknowledge)
        self._page.session_mute_requested.connect(self.mute_session)
        self._page.completion_changed.connect(self.set_completed)

    def initialize(self) -> None:
        self._submit(cached=True)
        self._timer.start()

    def _set_automatic(self, enabled: bool) -> None:
        self._settings.set(AUTO_KEY, enabled)
        if enabled:
            self._automatic_refresh()

    def _automatic_refresh(self) -> None:
        if self._page.auto_check.isChecked():
            self.refresh()

    def refresh(self) -> None:
        self._submit(cached=False)

    def _submit(self, *, cached: bool) -> None:
        if self._closing or self._busy:
            return
        self._busy = True
        self._page.set_busy(True)
        fallback = self._snapshot

        def run() -> RedeemSnapshot:
            try:
                return self._service.load_cached() if cached else self._service.synchronize()
            except Exception:
                return replace(fallback, fresh=False, message="兌換碼更新失敗; 已保留現有清單。")

        future = self._executor.submit(run)

        def completed(done: Future[RedeemSnapshot]) -> None:
            if not self._closing:
                self._completed.emit(done.result())

        future.add_done_callback(completed)

    def _handle_result(self, result: RedeemSnapshot) -> None:
        if self._closing:
            return
        self._busy = False
        self._snapshot = result
        self._page.set_busy(False)
        self._page.set_snapshot(result, completed=frozenset(self._done_codes))
        available = {c.key for c in result.codes if c.available} - self._done_codes
        self._pending.intersection_update(available)
        # Cache load or failed refresh must never manufacture a new-code event.
        if result.fresh and not self._muted:
            fresh = available.intersection(result.checked_codes) - self._notified
            self._pending.update(fresh)
            if fresh:
                self._notified.update(fresh)
                try:
                    self._settings.set(NOTIFIED_KEY, sorted(self._notified))
                    self._settings.sync()
                except Exception:
                    self._page.status_label.setText(result.message + " 提醒紀錄儲存失敗。")
        self._render_banner()
        if self._initial:
            self._initial = False
            QTimer.singleShot(0, self._automatic_refresh)

    def acknowledge(self) -> None:
        self._pending.clear()
        self._window.redeem_banner.hide()

    def _render_banner(self) -> None:
        self._window.redeem_notice.setText(f"發現 {len(self._pending)} 組新兌換碼, 可一鍵複製。")
        self._window.redeem_banner.setVisible(bool(self._pending) and not self._muted)

    def mute_session(self) -> None:
        if self._closing:
            return
        self._muted = True
        # Defer the currently displayed codes, too; a temporary mute is not acknowledgement.
        self._notified.difference_update(self._pending)
        self._pending.clear()
        self._render_banner()
        self._page.muted_label.show()
        try:
            self._settings.set(NOTIFIED_KEY, sorted(self._notified))
            self._settings.sync()
        except Exception:
            self._page.status_label.setText("本次提醒已暫停, 但下次啟動的提醒紀錄儲存失敗。")

    def set_completed(self, code: str, completed: bool) -> None:
        if self._closing or code not in {item.key for item in self._snapshot.codes}:
            return
        updated = self._done_codes | {code} if completed else self._done_codes - {code}
        try:
            self._settings.set(COMPLETED_KEY, sorted(updated))
            self._settings.sync()
        except Exception:
            self._page.set_snapshot(self._snapshot, completed=frozenset(self._done_codes))
            self._page.status_label.setText("已完成狀態儲存失敗, 請稍後再試。")
            return
        self._done_codes = updated
        if completed:
            self._pending.discard(code)
        self._page.set_snapshot(self._snapshot, completed=frozenset(self._done_codes))
        self._render_banner()

    def prepare_shutdown(self) -> None:
        self._closing = True
        self._timer.stop()
        self._executor.prepare_shutdown()

    def finish_shutdown(self) -> bool:
        self._executor.shutdown()
        return True

    def shutdown(self) -> bool:
        self.prepare_shutdown()
        return self.finish_shutdown()
