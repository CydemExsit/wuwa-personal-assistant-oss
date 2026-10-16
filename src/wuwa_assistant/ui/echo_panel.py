"""Passive, non-activating Echo results with explicit modifier-only dragging."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout

from .native_panel import PanelCaptureExclusion
from .panel_model import EchoPanelSnapshot
from .score_colors import apply_score_color


def panel_position(bounds: QRect, size: tuple[int, int], preset: str) -> QPoint:
    margin = 16
    width, height = size
    left, top = bounds.left() + margin, bounds.top() + margin
    right = max(left, bounds.right() - width - margin + 1)
    bottom = max(top, bounds.bottom() - height - margin + 1)
    return {
        "top_right": QPoint(right, top),
        "bottom_left": QPoint(left, bottom),
        "bottom_right": QPoint(right, bottom),
        "center_left": QPoint(left, (top + bottom) // 2),
    }.get(preset, QPoint(left, top))


def clamped_position(position: QPoint, bounds: QRect, size: tuple[int, int]) -> QPoint:
    width, height = size
    return QPoint(
        max(bounds.left(), min(position.x(), bounds.right() - width + 1)),
        max(bounds.top(), min(position.y(), bounds.bottom() - height + 1)),
    )


class EchoPanel(QFrame):
    position_saved = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        protect: Callable[[], None] | None = None,
        modifiers: Callable[[], Qt.KeyboardModifier] = QGuiApplication.queryKeyboardModifiers,
    ) -> None:
        super().__init__(None)
        self.setObjectName("EchoFloatingPanel")
        self.setWindowTitle("鳴潮 · 聲骸小面板")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(330)
        self.setStyleSheet("""
            QFrame#EchoFloatingPanel { background: #151d2a; border: 1px solid #53647e;
                border-radius: 10px; }
            QLabel { color: #e4eaf5; background: transparent; border: none;
                font: 13px 'Microsoft JhengHei UI'; }
            QLabel#PanelName { font-size: 16px; font-weight: 600; }
            QLabel#PanelScore { font-size: 28px; font-weight: 600; }
            QLabel#PanelTarget { font-size: 21px; font-weight: 600; }
            QLabel#PanelStatus { color: #86d4cf; font-weight: 600; }
            QLabel#PanelDetail { color: #aebcd0; }
            QLabel#PanelHint { color: #8f9fb5; font-size: 11px; }
        """)
        self._protect = protect or PanelCaptureExclusion(self).enable
        self._modifiers = modifiers
        self._bounds = QRect()
        self._drag_offset: QPoint | None = None
        self._interactive = False
        self._ending = False
        self._preset = "top_left"
        self._snapshot = EchoPanelSnapshot()
        self.status_label = self._label("PanelStatus")
        self.name_label = self._label("PanelName")
        self.score_label = self._label("PanelScore")
        self.target_label = self._label("PanelTarget")
        self.save_label = self._label("PanelSaveAdvice")
        self.upgrade_label = self._label("PanelUpgradeAdvice")
        self.basis_label = self._label("PanelBasis")
        self.detail_label = self._label("PanelDetail")
        self.hint_label = self._label("PanelHint")
        self.hint_label.setText("Ctrl+Shift 拖曳位置 · 回工具按「結束小面板」")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(8)
        layout.addWidget(self.status_label)
        layout.addWidget(self.name_label)
        scores = QGridLayout()
        scores.addWidget(QLabel("聲骸分數"), 0, 0)
        scores.addWidget(QLabel("替換目標分數"), 0, 1)
        scores.addWidget(self.score_label, 1, 0)
        scores.addWidget(self.target_label, 1, 1)
        layout.addLayout(scores)
        for label in (
            self.basis_label,
            self.save_label,
            self.upgrade_label,
            self.detail_label,
            self.hint_label,
        ):
            layout.addWidget(label)
        self._modifier_timer = QTimer(self)
        self._modifier_timer.setInterval(50)
        self._modifier_timer.timeout.connect(self.poll_modifiers)
        self.apply_snapshot(self._snapshot)

    @staticmethod
    def _label(name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return label

    @property
    def interactive(self) -> bool:
        return self._interactive

    def start(self, bounds: QRect, preset: str, saved: QPoint | None = None) -> None:
        self._ending = False
        self._bounds = QRect(bounds)
        self._preset = preset
        self.adjustSize()
        position = (
            saved
            if preset == "custom" and saved is not None
            else panel_position(bounds, (self.width(), self.height()), preset)
        )
        self.move(clamped_position(position, bounds, (self.width(), self.height())))
        # Create/configure the native window before making it visible or starting OCR.
        self._protect()
        self.show()
        self._modifier_timer.start()

    def end(self) -> None:
        self._ending = True
        self._modifier_timer.stop()
        self._finish_drag()
        try:
            self._set_interactive(False)
        except RuntimeError:
            # Stopping must always hide the panel even if Windows no longer
            # accepts capture affinity after a display/session change.
            self._interactive = False
        finally:
            self.hide()

    def apply_snapshot(self, snapshot: EchoPanelSnapshot) -> None:
        self._snapshot = snapshot
        prefix = "上次結果 · " if snapshot.stale else ""
        self.status_label.setText(prefix + snapshot.status)
        self.status_label.setStyleSheet(
            "color: #ffd28b;" if snapshot.missing_fields or snapshot.stale else ""
        )
        self.name_label.setText(
            snapshot.name + (f"  +{snapshot.level}" if snapshot.level is not None else "")
        )
        self.score_label.setText("—" if snapshot.score is None else f"{snapshot.score:.1f}")
        self.target_label.setText(
            "—" if snapshot.target_score is None else f"{snapshot.target_score:.1f}"
        )
        apply_score_color(self.score_label, snapshot.score)
        apply_score_color(self.target_label, snapshot.target_score)
        self.basis_label.setText(snapshot.basis)
        self.save_label.setText("保存建議: " + snapshot.save_advice)
        self.upgrade_label.setText("升級建議: " + snapshot.upgrade_advice)
        detail = snapshot.detail
        if snapshot.missing_fields:
            detail = (
                "辨識不足: "
                + "、".join(snapshot.missing_fields)
                + "\n請切回工具按「修正 OCR」手動輸入。"
            )
        self.detail_label.setText(detail if len(detail) <= 90 else detail[:89] + "…")
        self.detail_label.setToolTip(detail)
        self.detail_label.setStyleSheet("color: #ffd28b;" if snapshot.missing_fields else "")
        self.adjustSize()
        if not self._bounds.isEmpty():
            position = (
                self.pos()
                if self._preset == "custom" or self._drag_offset is not None
                else panel_position(self._bounds, (self.width(), self.height()), self._preset)
            )
            self.move(clamped_position(position, self._bounds, (self.width(), self.height())))

    def poll_modifiers(self) -> None:
        required = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        enabled = self._modifiers() & required == required
        if not enabled:
            self._finish_drag()
        try:
            self._set_interactive(enabled)
        except RuntimeError as exc:
            self._modifier_timer.stop()
            self.failed.emit(str(exc))

    def _set_interactive(self, enabled: bool) -> None:
        if enabled == self._interactive:
            return
        window = self.windowHandle()
        if window is not None:
            # QWindow changes native flags in place; QWidget.setWindowFlags would
            # hide/recreate the panel and could lose capture exclusion.
            window.setFlag(Qt.WindowType.WindowTransparentForInput, not enabled)
            try:
                self._protect()
            except RuntimeError:
                # A failed native update must not leave an invisible input
                # blocker over the game while the controller stops capture.
                window.setFlag(Qt.WindowType.WindowTransparentForInput, True)
                self._interactive = False
                raise
        self._interactive = enabled
        self.setCursor(Qt.CursorShape.SizeAllCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.hint_label.setText(
            "位置調整中 · 按住滑鼠左鍵拖曳"
            if enabled
            else "Ctrl+Shift 拖曳位置 · 回工具按「結束小面板」"
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._interactive and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and self._interactive:
            self.move(
                clamped_position(
                    event.globalPosition().toPoint() - self._drag_offset,
                    self._bounds,
                    (self.width(), self.height()),
                )
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_drag()
        super().mouseReleaseEvent(event)

    def _finish_drag(self) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self._preset = "custom"
            self.position_saved.emit(QPoint(self.pos()))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._ending:
            event.accept()
        else:
            event.ignore()
