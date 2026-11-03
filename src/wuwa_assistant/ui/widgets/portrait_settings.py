"""Optional user-owned local portraits; no artwork or network downloader ships."""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from wuwa_assistant.application.character_catalog import load_bundled_character_catalog
from wuwa_assistant.ui.character_portraits import (
    CharacterPortraitLabel,
    import_local_portrait,
    remove_local_portrait,
)
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector


class PortraitSettings(QGroupBox):
    def __init__(self, *, enabled: bool = True) -> None:
        super().__init__("角色頭像")
        self.setObjectName("PortraitSettings")
        self.selector = ChoiceSelector()
        for entry in load_bundled_character_catalog().entries:
            self.selector.addItem(entry.name, entry.name)
        self.preview = CharacterPortraitLabel(self.selector.currentText(), 56)
        self.selector.currentIndexChanged.connect(
            lambda: self.preview.set_character_name(self.selector.currentText())
        )
        self.import_button = QPushButton("匯入圖片")
        self.import_button.clicked.connect(self._import)
        self.reset_button = QPushButton("恢復預設頭像")
        self.reset_button.clicked.connect(self._reset)
        self.status = QLabel("只存於本機;請使用你有權使用的圖片。")
        self.status.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.preview)
        row.addWidget(self.selector, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.import_button)
        actions.addWidget(self.reset_button)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        self.setEnabled(enabled)

    def _import(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self, "選擇角色頭像", "", "圖片 (*.png *.jpg *.jpeg *.webp)"
        )
        if not source:
            return
        try:
            import_local_portrait(self.selector.currentText(), Path(source))
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.status.setText("頭像已更新;本機圖片不包含在資料庫備份中。")

    def _reset(self) -> None:
        try:
            remove_local_portrait(self.selector.currentText())
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.status.setText("已恢復預設頭像。")
