"""Read-only equipment inspection: observed stats, score explanation, next target."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.ui.score_colors import apply_score_color
from wuwa_assistant.ui.widgets.text_details import TextDetailsButton


@dataclass(frozen=True, slots=True)
class SubstatLineView:
    name: str
    value: str
    relevance: str
    contribution: float | None
    detail: str = ""
    roll_quality: str = ""


@dataclass(frozen=True, slots=True)
class GearPieceDetailView:
    position: int
    echo_id: str | None
    name: str
    level: int | None
    cost: int | None
    set_name: str
    main_stats: tuple[str, ...]
    score: float | None
    stats: tuple[SubstatLineView, ...]
    fit: str
    action: str
    reason: str
    target: str
    score_note: str = ""
    is_passing: bool | None = None


@dataclass(frozen=True, slots=True)
class GearCharacterDetailView:
    identifier: str
    name: str
    subtitle: str
    score_summary: str
    set_summary: str
    basis: str
    context_note: str
    pieces: tuple[GearPieceDetailView, ...]
    pending_echoes: tuple[str, ...] = ()
    priority_stats: str = ""
    effective_weights: str = ""

    @property
    def average_score(self) -> float | None:
        scores = [piece.score for piece in self.pieces if piece.score is not None]
        return sum(scores) / len(scores) if scores else None


class EquipmentDetailPage(QWidget):
    edit_requested = Signal(str)
    training_requested = Signal(str, int)
    back_requested = Signal()
    sources_requested = Signal(str)
    slot_selected = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._detail: GearCharacterDetailView | None = None
        self._focused_slot: int | None = None
        self.cards: dict[int, QFrame] = {}
        self._cards_grid: QGridLayout | None = None
        self._columns = 0
        self._compact_inspector = False
        self.slot_buttons: dict[int, QPushButton] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        controls = QHBoxLayout()
        back = QPushButton("返回聲骸總覽")
        self.back_button = back
        back.clicked.connect(self.back_requested)
        controls.addWidget(back)
        controls.addStretch(1)
        edit = QPushButton("角色養成")
        edit.setProperty("role", "primary")
        edit.clicked.connect(
            lambda: self.edit_requested.emit(self._detail.identifier) if self._detail else None
        )
        controls.addWidget(edit)
        layout.addLayout(controls)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidget(QLabel("從聲骸總覽選擇角色或低分聲骸, 查看完整配裝。"))
        layout.addWidget(self.scroll_area)

    def set_compact_inspector(self, compact: bool) -> None:
        if compact == self._compact_inspector:
            return
        self._compact_inspector = compact
        self.back_button.setVisible(not compact)
        self._reflow_cards()

    def clear_detail(self) -> None:
        self._detail = None
        self._focused_slot = None
        self.cards = {}
        self.slot_buttons = {}
        self._cards_grid = None
        self.scroll_area.setWidget(QLabel("選擇左側角色或聲骸, 在這裡查看配裝與副詞條。"))

    def apply_detail(self, detail: GearCharacterDetailView) -> None:
        if self._detail is None or self._detail.identifier != detail.identifier:
            self._focused_slot = None
        self._detail = detail
        self.cards = {}
        self.slot_buttons = {}
        old = self.scroll_area.widget()
        if old is not None:
            old.hide()
            self.scroll_area.takeWidget()
            old.deleteLater()
        body = QWidget(self.scroll_area.viewport())
        body.setObjectName("EquipmentInspectorContent")
        layout = QVBoxLayout(body)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 4, 8, 8)
        layout.setSpacing(10)
        header = QHBoxLayout()
        from wuwa_assistant.ui.character_portraits import CharacterPortraitLabel

        portrait = CharacterPortraitLabel(detail.name, 56)
        header.addWidget(portrait)
        labels = QVBoxLayout()
        title = QLabel(detail.name)
        title.setObjectName("EquipmentCharacterName")
        labels.addWidget(title)
        subtitle = QLabel(detail.subtitle)
        subtitle.setWordWrap(True)
        labels.addWidget(subtitle)
        header.addLayout(labels, 1)
        summary = QLabel(detail.score_summary)
        summary.setObjectName("EquipmentDetailScoreSummary")
        apply_score_color(summary, detail.average_score)
        summary.setWordWrap(True)
        layout.addLayout(header)
        layout.addWidget(summary)
        common = QHBoxLayout()
        priority = detail.priority_stats or next(
            (piece.target.split("優先詞條: ", 1)[-1] for piece in detail.pieces if piece.target),
            "",
        )
        if priority:
            priority_label = QLabel("優先詞條: " + priority)
            priority_label.setObjectName("EquipmentPriorityStats")
            priority_label.setWordWrap(True)
            common.addWidget(priority_label, 1)
        else:
            common.addStretch(1)
        weights = TextDetailsButton("目前有效權重", body)
        weights.setObjectName("EquipmentEffectiveWeightsDetails")
        weights.set_details(
            f"{detail.name} · 目前有效權重",
            "\n\n".join(
                text
                for text in (detail.effective_weights, detail.basis, detail.context_note)
                if text
            ),
        )
        common.addWidget(weights)
        layout.addLayout(common)
        if detail.set_summary:
            sets = QLabel(detail.set_summary)
            sets.setObjectName("PageDescription")
            sets.setWordWrap(True)
            layout.addWidget(sets)
        if detail.pending_echoes:
            pending = QLabel(
                "另有已登記、位置待確認的聲骸 (不計入五件均分):\n"
                + "、".join(detail.pending_echoes)
                + "\n可在遊戲左側五個裝備圓格逐一點選再掃描; 或到角色資料調整位置。"
            )
            pending.setWordWrap(True)
            pending.setObjectName("EquipmentPendingPositions")
            layout.addWidget(pending)
        self.slot_selector = QWidget(body)
        self.slot_selector.setObjectName("EquipmentSlotSelector")
        slots_layout = QVBoxLayout(self.slot_selector)
        slots_layout.setContentsMargins(0, 0, 0, 0)
        slots_layout.setSpacing(4)
        for piece in detail.pieces:
            score = "待評分" if piece.score is None else f"{piece.score:.1f} 分"
            main = piece.main_stats[0] if piece.main_stats else "主詞條待辨識"
            button = QPushButton(f"位置 {piece.position} · {main} · {score}")
            button.setObjectName("EquipmentSlotSelectorButton")
            apply_score_color(button, piece.score)
            button.setCheckable(True)
            button.setToolTip(piece.name)
            button.clicked.connect(
                lambda _checked=False, position=piece.position: self._select_slot(position)
            )
            self.slot_buttons[piece.position] = button
            slots_layout.addWidget(button)
        layout.addWidget(self.slot_selector)
        self._cards_grid = QGridLayout()
        self._cards_grid.setSpacing(10)
        layout.addLayout(self._cards_grid)
        for piece in detail.pieces:
            card = self._piece_card(detail.identifier, piece)
            self.cards[piece.position] = card
        layout.addStretch(1)
        self.scroll_area.setWidget(body)
        if self._focused_slot not in self.cards:
            self._focused_slot = None
        self._reflow_cards()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow_cards()

    def _reflow_cards(self) -> None:
        if self._cards_grid is None:
            return
        columns = 1 if self._compact_inspector else max(1, min(3, self.width() // 480))
        self._columns = columns
        while self._cards_grid.count():
            self._cards_grid.takeAt(0)
        self.slot_selector.setVisible(self._compact_inspector)
        selected = self._focused_slot or next(iter(self.cards), None)
        visible = 0
        for position, card in self.cards.items():
            shown = not self._compact_inspector or position == selected
            card.setVisible(shown)
            self.slot_buttons[position].setChecked(position == selected)
            if shown:
                self._cards_grid.addWidget(card, visible // columns, visible % columns)
                visible += 1
        for column in range(3):
            self._cards_grid.setColumnStretch(column, 1 if column < columns else 0)

    def focus_slot(self, position: int) -> None:
        if position in self.cards:
            self._focused_slot = position
            self._reflow_cards()
            if not self._compact_inspector:
                self.scroll_area.ensureWidgetVisible(self.cards[position], 0, 20)

    def _select_slot(self, position: int) -> None:
        self.focus_slot(position)
        self.slot_selected.emit(position)

    def _piece_card(self, character_id: str, piece: GearPieceDetailView) -> QFrame:
        card = QFrame(self.scroll_area.viewport())
        card.setObjectName("EquipmentWeakPiece")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel(f"位置 {piece.position} · {piece.name}")
        title.setWordWrap(True)
        title.setObjectName("EquipmentPieceName")
        header.addWidget(title, 1)
        score = QLabel("資料待補" if piece.score is None else f"{piece.score:.2f} 分")
        score.setObjectName("EquipmentPieceScore")
        apply_score_color(score, piece.score)
        header.addWidget(score)
        layout.addLayout(header)
        meta = QLabel(
            f"+{piece.level if piece.level is not None else '?'}"
            f" · COST {piece.cost or '?'} · {piece.set_name}"
        )
        meta.setWordWrap(True)
        layout.addWidget(meta)
        mains = QLabel("主詞條: " + (" + ".join(piece.main_stats) or "尚未辨識"))
        mains.setWordWrap(True)
        layout.addWidget(mains)
        grid = QGridLayout()
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)
        for col, text in enumerate(("副詞條", "數值", "檔位", "得分")):
            grid.addWidget(QLabel(text), 0, col)
        for row, stat in enumerate(piece.stats, 1):
            for col, text in enumerate(
                (
                    stat.name,
                    stat.value,
                    stat.roll_quality or "—",
                    "—" if stat.contribution is None else f"+{stat.contribution:.2f}",
                )
            ):
                cell = QLabel(text)
                cell.setWordWrap(True)
                cell.setToolTip(" · ".join(text for text in (stat.relevance, stat.detail) if text))
                cell.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(cell, row, col)
        if not piece.stats:
            grid.addWidget(QLabel("尚未有已開啟的副詞條紀錄"), 1, 0, 1, 4)
        layout.addLayout(grid)
        advice = QLabel(piece.action)
        advice.setWordWrap(True)
        layout.addWidget(advice)
        actions = QHBoxLayout()
        details = TextDetailsButton("未評分原因" if piece.score is None else "判斷理由", card)
        details.setObjectName(f"EquipmentSlot{piece.position}Details")
        details.set_details(
            f"位置 {piece.position} · 評分與建議",
            "\n\n".join(
                text for text in (piece.score_note, piece.reason, piece.fit, piece.target) if text
            ),
        )
        actions.addWidget(details)
        actions.addStretch(1)
        train = QPushButton("以此聲骸為目標開始替換")
        train.setObjectName(f"TrainReplacementSlot{piece.position}")
        train.clicked.connect(lambda: self.training_requested.emit(character_id, piece.position))
        actions.addWidget(train)
        layout.addLayout(actions)
        return card
