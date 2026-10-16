"""Shared, lightweight widgets for the desktop pages."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QWidget):
    """Honest placeholder for a page scheduled in a later milestone."""

    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(f"{title}Page")

        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        description_label = QLabel(description)
        description_label.setObjectName("PageDescription")
        description_label.setWordWrap(True)
        description_label.setMaximumWidth(760)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 36)
        layout.setSpacing(12)
        layout.addWidget(title_label)
        layout.addWidget(description_label)
        layout.addStretch(1)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
