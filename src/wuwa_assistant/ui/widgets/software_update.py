"""Small foreground-only update entry point shared by the two editions."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from wuwa_assistant import __version__
from wuwa_assistant.distribution import EDITION_NAME, UPDATE_REPOSITORY


class SoftwareUpdatePanel(QGroupBox):
    check_requested = Signal()
    import_requested = Signal()
    download_requested = Signal()
    install_requested = Signal()
    cancel_requested = Signal()

    def __init__(self) -> None:
        super().__init__("程式更新")
        self.setObjectName("SoftwareUpdatePanel")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{EDITION_NAME} · 目前版本 {__version__}"))
        self.status = QLabel(
            "可檢查正式新版, 下載後再決定何時安裝。"
            if UPDATE_REPOSITORY
            else "收到新版安裝檔後, 可直接開啟或在這裡選取更新。"
        )
        self.status.setWordWrap(True)
        self.status.setObjectName("SoftwareUpdateStatus")
        layout.addWidget(self.status)
        self.auto_check = QCheckBox("開啟工具時檢查新版(不自動下載)", self)
        self.auto_check.setVisible(UPDATE_REPOSITORY is not None)
        layout.addWidget(self.auto_check)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.check = QPushButton("檢查新版", self)
        self.check.setVisible(UPDATE_REPOSITORY is not None)
        self.local = QPushButton("選取新版安裝檔")
        self.download = QPushButton("下載新版")
        self.download.hide()
        self.install = QPushButton("安裝並重新開啟")
        self.install.hide()
        self.cancel = QPushButton("取消下載")
        self.cancel.hide()
        # Separate rows keep the translated controls visible at 200% scaling.
        actions = QHBoxLayout()
        for button in (self.check, self.local):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        pending = QHBoxLayout()
        for button in (self.download, self.install, self.cancel):
            pending.addWidget(button)
        pending.addStretch(1)
        layout.addLayout(pending)
        self.check.clicked.connect(self.check_requested.emit)
        self.local.clicked.connect(self.import_requested.emit)
        self.download.clicked.connect(self.download_requested.emit)
        self.install.clicked.connect(self.install_requested.emit)
        self.cancel.clicked.connect(self.cancel_requested.emit)

    def set_busy(self, busy: bool) -> None:
        for button in (self.check, self.local, self.download, self.install):
            button.setEnabled(not busy)
        self.cancel.setVisible(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setValue(0)
