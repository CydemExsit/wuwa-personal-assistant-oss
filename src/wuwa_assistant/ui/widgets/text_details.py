"""Keep verbose diagnostics behind an explicit, bounded details view."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QDialog, QPushButton, QTextBrowser, QVBoxLayout, QWidget


class TextDetailsButton(QPushButton):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(label, parent)
        self._details_title = "詳細說明"
        self._details_text = ""
        self.clicked.connect(self._show_details)
        self.hide()

    def set_details(self, title: str, text: str) -> None:
        self._details_title = title
        self._details_text = text
        self._update_visibility()

    def event(self, event: QEvent) -> bool:
        result = super().event(event)
        if event.type() == QEvent.Type.ParentChange and hasattr(self, "_details_text"):
            self._update_visibility()
        return result

    def _update_visibility(self) -> None:
        # Details are often populated before a layout adopts the button. Showing
        # an unparented QWidget creates a native top-level window, briefly
        # flashing it during equipment navigation and background OCR refreshes.
        self.setVisible(self.parentWidget() is not None and bool(self._details_text))

    def _show_details(self) -> None:
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(self._details_title)
        dialog.setObjectName("TextDetailsDialog")
        dialog.resize(640, 420)
        browser = QTextBrowser()
        browser.setObjectName("TextDetailsContent")
        browser.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        browser.setPlainText(self._details_text)
        close = QPushButton("關閉")
        close.clicked.connect(dialog.close)
        layout = QVBoxLayout(dialog)
        layout.addWidget(browser, 1)
        layout.addWidget(close)
        dialog.show()
