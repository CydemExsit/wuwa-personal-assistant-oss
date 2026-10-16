"""Load the bundled night pink-purple desktop theme."""

from __future__ import annotations

from importlib.resources import files

from PySide6.QtWidgets import QApplication, QWidget


def load_dark_stylesheet() -> str:
    resources = files("wuwa_assistant.ui.styles")
    stylesheet = resources.joinpath("dark.qss").read_text(encoding="utf-8")
    checkmark = str(resources.joinpath("checkbox-check.svg")).replace("\\", "/")
    stylesheet = stylesheet.replace("__WUWA_CHECKMARK_ASSET__", checkmark)
    for direction in ("up", "down"):
        asset = str(resources.joinpath(f"time-{direction}.svg")).replace("\\", "/")
        stylesheet = stylesheet.replace(f"__WUWA_TIME_{direction.upper()}_ASSET__", asset)
    return stylesheet


def apply_dark_theme(target: QApplication | QWidget) -> None:
    """Apply the complete dark purple theme to an application or one widget tree."""

    stylesheet = load_dark_stylesheet()
    if isinstance(target, QApplication) and target.style().objectName().casefold() != "fusion":
        target.setStyle("Fusion")
    if target.styleSheet() != stylesheet:
        target.setStyleSheet(stylesheet)
