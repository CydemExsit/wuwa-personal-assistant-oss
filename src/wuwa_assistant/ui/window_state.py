"""Persist and safely restore ordinary desktop-window state."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMainWindow

from .settings_store import SettingsStore

WINDOW_GEOMETRY_KEY = "ui/main_window_geometry"
WINDOW_STATE_KEY = "ui/main_window_state"
LAST_PAGE_KEY = "ui/last_page"


def _byte_array(value: object | None) -> QByteArray | None:
    if isinstance(value, QByteArray):
        return value
    if isinstance(value, (bytes, bytearray)):
        return QByteArray(bytes(value))
    return None


class WindowStatePersistence:
    """Keep Qt geometry blobs behind the injected settings abstraction."""

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    def restore(self, window: QMainWindow) -> bool:
        geometry = _byte_array(self._settings.get(WINDOW_GEOMETRY_KEY))
        restored = geometry is not None and window.restoreGeometry(geometry)

        state = _byte_array(self._settings.get(WINDOW_STATE_KEY))
        if state is not None:
            window.restoreState(state)

        if restored:
            self._ensure_visible(window)
        return restored

    def save(self, window: QMainWindow) -> None:
        self._settings.set(WINDOW_GEOMETRY_KEY, window.saveGeometry())
        self._settings.set(WINDOW_STATE_KEY, window.saveState())

    @staticmethod
    def _ensure_visible(window: QMainWindow) -> None:
        screens = QGuiApplication.screens()
        if not screens:
            return

        frame = window.frameGeometry()
        if any(_meaningfully_intersects(frame, screen.availableGeometry()) for screen in screens):
            return

        target = QGuiApplication.primaryScreen() or screens[0]
        available = target.availableGeometry()
        window.move(available.center() - window.rect().center())


def _meaningfully_intersects(left: QRect, right: QRect) -> bool:
    intersection = left.intersected(right)
    return intersection.width() >= 80 and intersection.height() >= 80
