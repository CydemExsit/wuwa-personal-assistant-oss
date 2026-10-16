"""PySide6 desktop shell for the local personal assistant."""

from .main_window import MainWindow
from .navigation import NAVIGATION_ITEMS, NavigationModel, PageId
from .pages import DashboardSnapshot, MonitorOption
from .settings_store import QSettingsStore, SettingsStore
from .theme import apply_dark_theme, load_dark_stylesheet

__all__ = [
    "NAVIGATION_ITEMS",
    "DashboardSnapshot",
    "MainWindow",
    "MonitorOption",
    "NavigationModel",
    "PageId",
    "QSettingsStore",
    "SettingsStore",
    "apply_dark_theme",
    "load_dark_stylesheet",
]
