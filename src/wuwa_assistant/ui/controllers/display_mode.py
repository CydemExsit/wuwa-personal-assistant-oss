"""Own the passive panel and share the existing workspace capture lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from math import isfinite

from PySide6.QtCore import QObject, QPoint, QTimer
from PySide6.QtGui import QGuiApplication, QScreen

from wuwa_assistant.display import (
    DEFAULT_RESOLUTION,
    DISPLAY_MODE_KEY,
    PANEL_POSITION_KEY,
    PANEL_PRESET_KEY,
    RESOLUTION_KEY,
    DisplayPreferences,
)
from wuwa_assistant.ui.echo_panel import EchoPanel
from wuwa_assistant.ui.pages.echo_live import CaptureState, CaptureStatusView, EchoLivePage
from wuwa_assistant.ui.pages.settings import MONITOR_KEY, SettingsPage
from wuwa_assistant.ui.settings_store import SettingsStore, setting_str

from .echo_workspace import EchoWorkspacePresenter
from .live_capture import MonitorDescriptor


def monitor_screen(monitor: MonitorDescriptor) -> QScreen:
    for screen in QGuiApplication.screens():
        geometry = screen.geometry()
        if geometry.x() == monitor.left and geometry.y() == monitor.top:
            return screen
    raise RuntimeError("無法對應遊戲顯示器, 請重新選擇螢幕。")


def _ratio(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        and 0 <= value <= 1
    ):
        return float(value)
    return None


class DisplayModeController(QObject):
    def __init__(
        self,
        *,
        presenter: EchoWorkspacePresenter,
        page: EchoLivePage,
        settings_page: SettingsPage,
        settings: SettingsStore,
        panel_factory: Callable[[], EchoPanel] = EchoPanel,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._presenter = presenter
        self._page = page
        self._settings_page = settings_page
        self._settings = settings
        self._panel_factory = panel_factory
        self.panel: EchoPanel | None = None
        self._screen: QScreen | None = None
        self._panel_error = ""
        self._closed = False
        self._monitor_id = setting_str(settings.get(MONITOR_KEY), "")
        self.preferences = DisplayPreferences(
            mode=setting_str(settings.get(DISPLAY_MODE_KEY), "dual"),
            resolution=setting_str(settings.get(RESOLUTION_KEY), DEFAULT_RESOLUTION),
            panel_preset=setting_str(settings.get(PANEL_PRESET_KEY), "top_left"),
        )
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)
        page.presentation_changed.connect(self._queue_refresh)
        page.stop_requested.connect(self.end_panel)
        settings_page.settings_saved.connect(self.apply_settings)
        presenter.capture_guard = self.prepare_capture
        page.set_display_mode(self.preferences.mode)
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            app.screenRemoved.connect(self._screen_removed)

    def apply_settings(self, values: Mapping[str, object]) -> None:
        updated = DisplayPreferences(
            mode=str(values.get(DISPLAY_MODE_KEY, self.preferences.mode)),
            resolution=str(values.get(RESOLUTION_KEY, self.preferences.resolution)),
            panel_preset=str(values.get(PANEL_PRESET_KEY, self.preferences.panel_preset)),
        )
        monitor_id = str(values.get(MONITOR_KEY, self._monitor_id))
        restart = (
            updated.mode != self.preferences.mode
            or updated.resolution != self.preferences.resolution
            or monitor_id != self._monitor_id
        )
        if restart:
            self._presenter.stop_capture()
            self.end_panel()
        self.preferences = updated
        self._monitor_id = monitor_id
        self._page.set_display_mode(updated.mode)
        self._page.select_monitor(monitor_id)
        if self.panel is not None and self.panel.isVisible() and self._screen is not None:
            try:
                self._place_panel()
            except RuntimeError as exc:
                self._panel_failed(str(exc))

    def prepare_capture(self, monitor: MonitorDescriptor) -> bool:
        if self._closed or self.preferences.mode != "single":
            return not self._closed
        self._panel_error = ""
        if self.panel is None:
            self.panel = self._panel_factory()
            self.panel.position_saved.connect(self._save_position)
            self.panel.failed.connect(self._panel_failed)
        try:
            self._disconnect_screen()
            self._screen = monitor_screen(monitor)
            self._screen.availableGeometryChanged.connect(self._geometry_changed)
            self._screen.logicalDotsPerInchChanged.connect(self._geometry_changed)
            self.panel.apply_snapshot(self._page.panel_snapshot())
            self._place_panel()
            self._page.set_panel_active(True)
        except RuntimeError as exc:
            self.end_panel()
            self._page.apply_capture_status(CaptureStatusView(CaptureState.ERROR, str(exc)))
            return False
        return True

    def _place_panel(self) -> None:
        if self.panel is None or self._screen is None:
            return
        bounds = self._screen.availableGeometry()
        saved = self._settings.get(PANEL_POSITION_KEY)
        position = None
        if isinstance(saved, dict):
            x, y = _ratio(saved.get("x_ratio")), _ratio(saved.get("y_ratio"))
            if x is not None and y is not None:
                position = QPoint(
                    bounds.x() + round(float(x) * max(0, bounds.width() - self.panel.width())),
                    bounds.y() + round(float(y) * max(0, bounds.height() - self.panel.height())),
                )
        self.panel.start(bounds, self.preferences.panel_preset, position)

    def end_panel(self) -> None:
        if self.panel is not None:
            self.panel.end()
        self._disconnect_screen()
        self._page.set_panel_active(False)

    def _save_position(self, point: object) -> None:
        if not isinstance(point, QPoint) or self._screen is None or self.panel is None:
            return
        bounds = self._screen.availableGeometry()
        self._settings.set(
            PANEL_POSITION_KEY,
            {
                "x_ratio": min(
                    1.0,
                    max(
                        0.0, (point.x() - bounds.x()) / max(1, bounds.width() - self.panel.width())
                    ),
                ),
                "y_ratio": min(
                    1.0,
                    max(
                        0.0,
                        (point.y() - bounds.y()) / max(1, bounds.height() - self.panel.height()),
                    ),
                ),
            },
        )
        self._settings.set(PANEL_PRESET_KEY, "custom")
        self._settings.sync()
        self.preferences = replace(self.preferences, panel_preset="custom")
        combo = self._settings_page.panel_preset_combo
        combo.setCurrentIndex(combo.findData("custom"))

    def _queue_refresh(self) -> None:
        if not self._closed and not self._refresh_timer.isActive():
            self._refresh_timer.start(0)

    def refresh(self) -> None:
        if self.panel is None or not self.panel.isVisible():
            return
        snapshot = self._page.panel_snapshot()
        if self._panel_error:
            snapshot = replace(snapshot, status="辨識已暫停", detail=self._panel_error, stale=True)
        self.panel.apply_snapshot(snapshot)

    def _panel_failed(self, message: str) -> None:
        self._panel_error = message
        self._presenter.stop_capture()
        self._page.apply_capture_status(CaptureStatusView(CaptureState.ERROR, message))
        self.refresh()

    def _geometry_changed(self, *_args: object) -> None:
        self._panel_failed("螢幕配置已改變, 請結束小面板並到設定確認解析度後重新啟動。")
        try:
            self._place_panel()
        except RuntimeError as exc:
            self._panel_failed(str(exc))

    def _screen_removed(self, screen: QScreen) -> None:
        if screen is not self._screen:
            return
        self._disconnect_screen()
        self._screen = QGuiApplication.primaryScreen()
        self._geometry_changed()

    def _disconnect_screen(self) -> None:
        if self._screen is not None:
            try:
                self._screen.availableGeometryChanged.disconnect(self._geometry_changed)
                self._screen.logicalDotsPerInchChanged.disconnect(self._geometry_changed)
            except (RuntimeError, TypeError):
                pass
            self._screen = None

    def shutdown(self) -> bool:
        self._closed = True
        self._refresh_timer.stop()
        self.end_panel()
        if self.panel is not None:
            self.panel.deleteLater()
            self.panel = None
        return True
