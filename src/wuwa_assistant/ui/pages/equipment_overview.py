"""Passive, score-first character browsing and weakest-equipped-piece view."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QStyle,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from wuwa_assistant.ui.score_colors import apply_score_color
from wuwa_assistant.ui.widgets.choice_selector import ChoiceSelector as QComboBox
from wuwa_assistant.ui.widgets.keyed_grid import KeyedGrid


@dataclass(frozen=True, slots=True)
class EquipmentPieceView:
    echo_id: str
    character_id: str
    character_name: str
    name: str
    slot: int | None
    level: int | None
    set_name: str | None
    score: float | None
    reason: str = ""
    scan_count: int = 1
    is_passing: bool | None = None


@dataclass(frozen=True, slots=True)
class EquipmentCharacterView:
    identifier: str
    name: str
    element: str
    average: float | None
    total: float | None
    scored_count: int
    equipped_count: int
    pieces: tuple[EquipmentPieceView, ...]
    level_done: bool
    weapon_done: bool
    skills_done: bool
    echoes_done: bool
    level_label: str = ""
    weapon_label: str = ""
    skills_label: str = ""
    archived: bool = False
    profile_label: str = ""
    scoring_notes: str = ""
    guidance_html: str = ""
    role_label: str = ""
    priority_label: str = ""


def _score_text(score: float | None) -> str:
    return "待評分" if score is None else f"{score:.1f}"


class _CharacterCard(QFrame):
    activated = Signal()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit()
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


def _portrait(name: str, size: int) -> QLabel:
    from wuwa_assistant.ui.character_portraits import CharacterPortraitLabel

    return CharacterPortraitLabel(name, size)


class EquipmentOverviewPage(QWidget):
    character_requested = Signal(str)
    piece_requested = Signal(str, int)
    profile_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EquipmentOverviewPage")
        self._characters: tuple[EquipmentCharacterView, ...] = ()
        self._weakest: tuple[EquipmentPieceView, ...] = ()
        self._columns = 0
        self._selected_character: str | None = None
        self._selected_slot: int | None = None
        self.visible_character_ids: tuple[str, ...] = ()
        # Keep the controller's comparison API without exposing an unused control.
        self.profile_combo = QComboBox(self)
        self.profile_combo.setObjectName("EquipmentComparisonProfile")
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.profile_combo.hide()
        self.view_combo = QComboBox()
        self.view_combo.addItem("角色總分", "characters")
        self.view_combo.addItem("低分單件", "pieces")
        self.view_combo.currentIndexChanged.connect(self._render)
        self.filter_combo = QComboBox()
        for label, value in (
            ("全部養成項目", "all"),
            ("聲骸尚未達標", "echoes"),
            ("角色等級未完成", "level"),
            ("武器未完成", "weapon"),
            ("技能未完成", "skills"),
            ("聲骸資料待補齊", "missing"),
        ):
            self.filter_combo.addItem(label, value)
        self.filter_combo.currentIndexChanged.connect(self._render)
        self.sort_combo = QComboBox()
        for label, value in (
            ("總分由低到高", "total_ascending"),
            ("總分由高到低", "total_descending"),
        ):
            self.sort_combo.addItem(label, value)
        self.sort_combo.currentIndexChanged.connect(self._render)
        controls = QHBoxLayout()
        controls.addWidget(self.view_combo)
        controls.addStretch(1)
        filters = QHBoxLayout()
        filters.addWidget(self.filter_combo)
        filters.addStretch(1)
        filters.addWidget(self.sort_combo)
        self.summary_label = QLabel("等待聲骸資料")
        self.summary_label.setObjectName("EquipmentOverviewSummary")
        self.summary_label.setWordWrap(True)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.content = QWidget()
        body = QVBoxLayout(self.content)
        body.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        body.setContentsMargins(0, 0, 8, 8)
        body.setSpacing(8)
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(10)
        body.addLayout(self.cards_grid)
        self.empty_label = QLabel("還沒有符合條件的角色。掃描裝備中的聲骸後會自動更新。")
        self.empty_label.setWordWrap(True)
        body.addWidget(self.empty_label)
        self.weakest_title = QLabel("目前裝備中 · 低分聲骸")
        self.weakest_title.setObjectName("EquipmentSectionTitle")
        body.addWidget(self.weakest_title)
        self.weakest_layout = QGridLayout()
        self.weakest_layout.setSpacing(10)
        body.addLayout(self.weakest_layout)
        self.no_pieces_label = QLabel("目前沒有符合篩選條件、可評分的裝備聲骸。")
        self.no_pieces_label.hide()
        body.addWidget(self.no_pieces_label)
        self._character_grid = KeyedGrid(self.cards_grid, lambda row: row.identifier, self._card)
        self._piece_grid = KeyedGrid(
            self.weakest_layout,
            lambda row: (row.character_id, row.slot, row.echo_id),
            self._piece_row,
        )
        body.addStretch(1)
        self.scroll_area.setWidget(self.content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(controls)
        layout.addLayout(filters)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.scroll_area, 1)

    def set_selected_character(self, identifier: str | None, slot: int | None = None) -> None:
        self._selected_character = identifier
        self._selected_slot = slot
        for card in self.content.findChildren(_CharacterCard):
            selected = card.property("characterId") == identifier and (
                card.property("slot") is None or card.property("slot") == slot
            )
            if card.property("selected") == selected:
                continue
            card.setProperty("selected", selected)
            card.style().unpolish(card)
            card.style().polish(card)
            card.update()

    def apply_data(
        self,
        characters: tuple[EquipmentCharacterView, ...],
        weakest: tuple[EquipmentPieceView, ...],
        *,
        profiles: tuple[tuple[str, str], ...],
        profile_id: str | None,
        summary: str,
    ) -> None:
        self._characters, self._weakest = characters, weakest
        self._character_grid.retain(row.identifier for row in characters)
        self._piece_grid.retain((row.character_id, row.slot, row.echo_id) for row in weakest)
        blocker = QSignalBlocker(self.profile_combo)
        self.profile_combo.clear()
        for identifier, name in profiles:
            self.profile_combo.addItem(name, identifier)
        index = self.profile_combo.findData(profile_id)
        self.profile_combo.setCurrentIndex(max(0, index))
        del blocker
        self.summary_label.setToolTip(summary)
        self._render()

    def _profile_changed(self) -> None:
        identifier = self.profile_combo.currentData()
        if isinstance(identifier, str):
            self.profile_requested.emit(identifier)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # Reserve the content margin and scrollbar, then account for gaps only
        # between cards. Three 272 px cards still fit the 1100 px main window.
        available = (
            self.width() - 8 - self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        )
        columns = max(1, min(5, (available + 10) // 282))
        if columns != self._columns:
            self._columns = columns
            self._render()

    def show_character_guidance(self, character_id: str) -> None:
        row = next((r for r in self._characters if r.identifier == character_id), None)
        if row is not None:
            self._show_guidance(row)

    def _render(self) -> None:
        if not hasattr(self, "cards_grid"):
            return
        show_pieces = self.view_combo.currentData() == "pieces"
        category = "all" if show_pieces else self.filter_combo.currentData()
        self.filter_combo.setVisible(not show_pieces)
        self.sort_combo.setVisible(not show_pieces)
        characters = [
            character
            for character in self._characters
            if not character.archived
            and (
                category == "all"
                or (category == "missing" and character.scored_count != 5)
                or (
                    category in {"echoes", "level", "weapon", "skills"}
                    and not getattr(character, f"{category}_done")
                )
            )
        ]

        def key(character: EquipmentCharacterView) -> tuple[bool, float, str]:
            score = character.total
            direction = -1 if self.sort_combo.currentData() == "total_descending" else 1
            return score is None, score * direction if score is not None else 0, character.name

        characters.sort(key=key)
        self.visible_character_ids = tuple(character.identifier for character in characters)
        columns = max(1, self._columns)
        self.empty_label.setVisible(not characters and not show_pieces)
        self.weakest_title.setVisible(show_pieces)
        visible = set(self.visible_character_ids)
        pieces = sorted(
            (piece for piece in self._weakest if piece.character_id in visible),
            key=lambda piece: (
                piece.score is None,
                piece.score if piece.score is not None else 0,
                piece.character_name,
                piece.slot or 0,
            ),
        )
        self.summary_label.setText(
            f"目前裝備共 {len(pieces)} 件 · 由低到高"
            if show_pieces
            else f"{len(characters)} 位角色 · 五件總分 / 500"
        )
        self.no_pieces_label.setVisible(show_pieces and not pieces)
        if show_pieces:
            self._character_grid.hide()
            self._piece_grid.render(pieces, columns)
        else:
            self._piece_grid.hide()
            self._character_grid.render(
                characters, columns, context=category in {"level", "weapon", "skills"}
            )

    def _card(self, character: EquipmentCharacterView) -> QFrame:
        card = _CharacterCard(self.content)
        card.setObjectName("EquipmentCharacterCard")
        card.setProperty("characterId", character.identifier)
        card.setProperty("selected", character.identifier == self._selected_character)
        card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        card.setAccessibleName(f"{character.name} · 查看配裝")
        card.setMinimumWidth(272)
        card.setMinimumHeight(172)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.activated.connect(lambda: self.character_requested.emit(character.identifier))
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(_portrait(character.name, 54))
        identity = QVBoxLayout()
        identity.setSpacing(2)
        name = QLabel(character.name + (" · 已封存" if character.archived else ""))
        name.setObjectName("EquipmentCharacterName")
        identity.addWidget(name)
        identity.addWidget(QLabel(character.element))
        head.addLayout(identity, 1)
        score_column = QVBoxLayout()
        score_column.setSpacing(0)
        score = QLabel(_score_text(character.total))
        score.setObjectName("EquipmentTotalScore")
        apply_score_color(score, character.total, maximum=500)
        score.setAlignment(Qt.AlignmentFlag.AlignRight)
        score_column.addWidget(score)
        caption = QLabel("總分 / 500" if character.total is not None else "五件尚未齊全")
        caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        caption.setObjectName("PageDescription")
        score_column.addWidget(caption)
        head.addLayout(score_column)
        layout.addLayout(head)
        scores = QHBoxLayout()
        positioned = {piece.slot: piece for piece in character.pieces if piece.slot is not None}
        for slot in range(1, 6):
            piece = positioned.get(slot)
            text = "—" if piece is None else _score_text(piece.score)
            label = QLabel(f"{slot}\n{text}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("EquipmentSlotScore")
            apply_score_color(label, None if piece is None else piece.score)
            label.setToolTip("位置待辨識" if piece is None else f"{piece.name}\n{piece.reason}")
            scores.addWidget(label, 1)
        layout.addLayout(scores)
        states = [
            "等級完成" if character.level_done else character.level_label or "等級待補",
            "武器完成" if character.weapon_done else character.weapon_label or "武器待補",
            "技能完成" if character.skills_done else character.skills_label or "技能待補",
        ]
        if self.filter_combo.currentData() in {"level", "weapon", "skills"}:
            status = QLabel(" · ".join(states))
            status.setWordWrap(True)
            status.setObjectName("PageDescription")
            layout.addWidget(status)
        elif character.total is None:
            status = QLabel(
                f"已登記 {character.equipped_count} 件 · 已評分 {character.scored_count}/5"
            )
            status.setObjectName("PageDescription")
            layout.addWidget(status)
        button = QPushButton("查看配裝與詞條")
        button.setProperty("role", "quiet")
        button.clicked.connect(
            lambda _checked=False: self.character_requested.emit(character.identifier)
        )
        layout.addWidget(button)
        return card

    def _show_guidance(self, character: EquipmentCharacterView) -> None:
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(f"{character.name} · 評分依據")
        dialog.resize(760, 680)
        browser = QTextBrowser()
        browser.setObjectName("CharacterScoringGuideBrowser")
        browser.setOpenExternalLinks(True)
        browser.document().setDefaultStyleSheet("a { color: #dec5e8; }")
        browser.setHtml(character.guidance_html or "尚無已查核攻略, 請選擇自訂設定檔。")
        layout = QVBoxLayout(dialog)
        layout.addWidget(browser)
        close = QPushButton("關閉")
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        dialog.show()

    def _piece_row(self, piece: EquipmentPieceView) -> QFrame:
        row = _CharacterCard(self.content)
        row.setObjectName("EquipmentWeakPiece")
        row.setProperty("characterId", piece.character_id)
        row.setProperty("slot", piece.slot)
        row.setProperty(
            "selected",
            piece.character_id == self._selected_character and piece.slot == self._selected_slot,
        )
        row.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        row.setMinimumWidth(272)
        row.setMinimumHeight(136)
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.activated.connect(
            lambda: self.piece_requested.emit(piece.character_id, piece.slot or 0)
        )
        layout = QVBoxLayout(row)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(_portrait(piece.character_name, 40))
        position = f"位置 {piece.slot}" if piece.slot else "位置待確認"
        level = piece.level if piece.level is not None else "?"
        identity = QLabel(f"{piece.character_name}\n{position}")
        identity.setObjectName("EquipmentWeakPieceOwner")
        identity.setWordWrap(True)
        header.addWidget(identity, 1)
        score = QLabel(_score_text(piece.score))
        score.setToolTip(piece.reason)
        score.setObjectName("EquipmentPieceScore")
        apply_score_color(score, piece.score)
        header.addWidget(score)
        layout.addLayout(header)
        name = QLabel(f"{piece.name} · +{level}")
        name.setObjectName("EquipmentWeakPieceName")
        name.setWordWrap(True)
        layout.addWidget(name)
        footer = QHBoxLayout()
        set_name = QLabel(piece.set_name or "套裝待辨識")
        set_name.setObjectName("PageDescription")
        set_name.setWordWrap(True)
        footer.addWidget(set_name, 1)
        button = QPushButton("查看")
        button.clicked.connect(
            lambda _checked=False: self.piece_requested.emit(piece.character_id, piece.slot or 0)
        )
        footer.addWidget(button)
        layout.addLayout(footer)
        return row
