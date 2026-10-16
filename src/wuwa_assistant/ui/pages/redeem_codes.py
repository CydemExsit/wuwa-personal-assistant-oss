"""In-window code list with explicit clipboard actions and provenance."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.public_data.redeem_models import COMMUNITY_URL, RedeemSnapshot

_STATUS = {
    "listed": "來源列為可用 · 兌換結果以遊戲為準",
    "unconfirmed": "官方曾公布 · 目前有效性與期限待確認",
    "expired": "來源列為已失效",
    "unlisted": "來源已未列出 · 有效性待確認",
}


class RedeemCodesPage(QWidget):
    refresh_requested = Signal()
    automatic_changed = Signal(bool)
    acknowledged = Signal()
    session_mute_requested = Signal()
    completion_changed = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("RedeemCodesPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        title = QLabel("兌換碼")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        description = QLabel("發現新碼時只在工具內提醒。複製後, 請至遊戲內貼上兌換。")
        description.setWordWrap(True)
        layout.addWidget(description)
        actions = QHBoxLayout()
        self.auto_check = QCheckBox("啟動時及每 30 分鐘檢查新碼")
        self.auto_check.setChecked(True)
        self.auto_check.toggled.connect(self.automatic_changed.emit)
        actions.addWidget(self.auto_check)
        actions.addStretch()
        self.refresh_button = QPushButton("立即更新")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        actions.addWidget(self.refresh_button)
        layout.addLayout(actions)
        self.muted_label = QLabel("本次啟動不再提醒; 清單仍會更新, 下次啟動恢復提醒。")
        self.muted_label.setWordWrap(True)
        self.muted_label.hide()
        layout.addWidget(self.muted_label)
        self.status_label = QLabel("尚未檢查兌換碼。")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        self.copy_status = QLabel("")
        self.copy_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.copy_status)
        self.code_scroll = QScrollArea()
        self.code_scroll.setWidgetResizable(True)
        layout.addWidget(self.code_scroll, 1)
        self._snapshot = RedeemSnapshot()
        self._done_codes: frozenset[str] = frozenset()
        self.show_old = QCheckBox("顯示已完成、已失效 / 過期或來源未列出的碼")
        self.show_old.toggled.connect(lambda _checked: self.set_snapshot(self._snapshot))
        layout.addWidget(self.show_old)
        self.set_snapshot(self._snapshot)

    def set_busy(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        if busy:
            self.status_label.setText("正在檢查公開兌換碼…")

    def set_snapshot(
        self, snapshot: RedeemSnapshot, *, completed: frozenset[str] | None = None
    ) -> None:
        self._snapshot = snapshot
        if completed is not None:
            self._done_codes = completed
        stamp = f"\n上次成功取得資料: {snapshot.checked_at}" if snapshot.checked_at else ""
        self.status_label.setText(snapshot.message + stamp)
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        visible = [
            c
            for c in snapshot.codes
            if (c.available and c.key not in self._done_codes) or self.show_old.isChecked()
        ]
        if not visible:
            empty = QLabel("目前沒有待處理的兌換碼。可立即更新, 或勾選下方選項查看歷史紀錄。")
            empty.setWordWrap(True)
            rows.addWidget(empty)
        for item in visible:
            card = QFrame()
            card.setObjectName("MetricCard")
            card_layout = QVBoxLayout(card)
            top = QHBoxLayout()
            code = QLabel(item.code)
            code.setTextFormat(Qt.TextFormat.PlainText)
            code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            code.setObjectName("RedeemCodeText")
            top.addWidget(code, 1)
            done = QCheckBox("已完成")
            done.setAccessibleName(f"兌換碼 {item.code} 已完成")
            done.setProperty("redeemCode", item.code)
            done.setChecked(item.key in self._done_codes)
            done.toggled.connect(
                lambda checked, key=item.key: self.completion_changed.emit(key, checked)
            )
            top.addWidget(done)
            copy = QPushButton("複製")
            copy.setAccessibleName(f"複製兌換碼 {item.code}")
            copy.setProperty("redeemCode", item.code)
            copy.setEnabled(item.status != "expired")
            copy.clicked.connect(lambda _checked=False, value=item.code: self.copy_code(value))
            top.addWidget(copy)
            card_layout.addLayout(top)
            status = QLabel(_STATUS[item.status])
            status.setWordWrap(True)
            card_layout.addWidget(status)
            source = QPushButton(
                "查看來源 · Pocket Tactics (公開彙整)"
                if item.source_url == COMMUNITY_URL
                else "查看來源 · 鳴潮官方公告"
            )
            source.clicked.connect(
                lambda _checked=False, url=item.source_url: QDesktopServices.openUrl(QUrl(url))
            )
            card_layout.addWidget(source)
            rows.addWidget(card)
        old = self.code_scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.code_scroll.setWidget(content)

    def copy_code(self, code: str) -> None:
        QApplication.clipboard().setText(code)
        self.copy_status.setText(f"已複製 {code}。複製不代表已兌換。")
