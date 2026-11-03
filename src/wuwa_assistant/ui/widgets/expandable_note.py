"""Short page guidance with keyboard-accessible, in-place details."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class ExpandableNote(QWidget):
    def __init__(self, summary: str, details: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary = QLabel(summary)
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton("使用說明")
        self.button.setCheckable(True)
        self.button.setProperty("role", "quiet")
        self.body = QLabel(details)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.hide()
        self.button.toggled.connect(self._toggle)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.addWidget(self.summary, 1)
        header.addWidget(self.button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        layout.addWidget(self.body)

    def _toggle(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.button.setText("收合說明" if expanded else "使用說明")

    def text(self) -> str:
        return self.body.text()

    def setText(self, text: str) -> None:
        self.body.setText(text)
        self.summary.setText(text.split("\n", 1)[0])

    def setWordWrap(self, enabled: bool) -> None:
        self.summary.setWordWrap(enabled)
        self.body.setWordWrap(enabled)
