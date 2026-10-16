from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
)
from pytestqt.qtbot import QtBot

from wuwa_assistant.ui.pages.equipment_overview import (
    EquipmentCharacterView,
    EquipmentOverviewPage,
    EquipmentPieceView,
)


def _piece(
    character_id: str,
    score: float | None,
    *,
    slot: int | None = 1,
) -> EquipmentPieceView:
    return EquipmentPieceView(
        echo_id=f"{character_id}-echo-{slot}",
        character_id=character_id,
        character_name=character_id,
        name=f"聲骸 {character_id}",
        slot=slot,
        level=25,
        set_name="套裝名稱",
        score=score,
        reason="副詞條資料不足" if score is None else "",
    )


def _character(
    identifier: str,
    average: float | None,
    *,
    weakest: float | None = None,
    complete: bool = True,
    archived: bool = False,
) -> EquipmentCharacterView:
    pieces = tuple(
        _piece(identifier, weakest if slot == 1 else average, slot=slot)
        for slot in range(1, 6 if complete else 2)
    )
    return EquipmentCharacterView(
        identifier=identifier,
        name=identifier,
        element="冷凝",
        average=average,
        total=average * 5 if complete and average is not None else None,
        scored_count=sum(piece.score is not None for piece in pieces),
        equipped_count=len(pieces),
        pieces=pieces,
        level_done=True,
        weapon_done=True,
        skills_done=True,
        echoes_done=complete,
        archived=archived,
    )


def test_incremental_cards_reuse_unchanged_widgets_and_defer_hidden_pieces(qtbot: QtBot) -> None:
    page = EquipmentOverviewPage()
    qtbot.addWidget(page)
    rows = (_character("first", 50), _character("second", 60))

    def render(data=rows):
        page.apply_data(
            data,
            tuple(p for row in data for p in row.pieces),
            profiles=(),
            profile_id=None,
            summary="",
        )

    render()
    initial = {
        w.property("characterId"): w for w in page.findChildren(QFrame, "EquipmentCharacterCard")
    }
    assert not page.findChildren(QFrame, "EquipmentWeakPiece")
    render()
    assert page.cards_grid.itemAtPosition(0, 0).widget() is initial["first"]
    page.sort_combo.setCurrentIndex(1)
    assert page.cards_grid.itemAtPosition(0, 0).widget() is initial["second"]
    render((replace(rows[0], total=251), rows[1]))
    assert page.cards_grid.itemAtPosition(0, 0).widget() is initial["second"]
    assert page.cards_grid.itemAtPosition(1, 0).widget() is not initial["first"]
    page.view_combo.setCurrentIndex(1)
    assert page.weakest_layout.count() == 10
    render((rows[1],))
    assert page.weakest_layout.count() == 5


def test_selected_piece_remains_highlighted_after_sort_and_view_changes(qtbot: QtBot) -> None:
    page = EquipmentOverviewPage()
    qtbot.addWidget(page)
    character = _character("test", 80, weakest=30)
    page.apply_data((character,), character.pieces, profiles=(), profile_id=None, summary="")
    page.set_selected_character("test", 3)
    page.view_combo.setCurrentIndex(1)
    page.show()
    qtbot.waitUntil(lambda: page.isVisible())
    rows = page.findChildren(QFrame, "EquipmentWeakPiece")
    selected = [row for row in rows if row.property("selected") and row.isVisible()]
    assert len(selected) == 1 and selected[0].property("slot") == 3
    page.view_combo.setCurrentIndex(0)
    page.sort_combo.setCurrentIndex(1)
    cards = page.findChildren(QFrame, "EquipmentCharacterCard")
    assert any(card.property("selected") and card.isVisible() for card in cards)


def test_unchanged_refresh_preserves_scroll_position(
    overview_page: EquipmentOverviewPage, qapp: QApplication
) -> None:
    rows = tuple(_character(f"角色{i}", i * 3) for i in range(25))
    overview_page.resize(800, 700)
    _apply(overview_page, rows)
    qapp.processEvents()
    bar = overview_page.scroll_area.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum() // 2)
    previous = bar.value()
    _apply(overview_page, rows)
    qapp.processEvents()
    assert bar.value() == previous


def test_guide_dialog_shows_provenance_without_repeated_preliminary_labels(qtbot: QtBot) -> None:
    from wuwa_assistant.scoring.character_guides import guide_for_character

    guide = guide_for_character("莫寧")
    assert guide is not None
    page = EquipmentOverviewPage()
    qtbot.addWidget(page)
    row = replace(
        _character("莫寧", 50),
        profile_label="莫寧 · 攻略參考",
        scoring_notes="暫估: 未填角色面板共鳴效率",
        guidance_html=guide.html(),
    )
    page.apply_data((row,), (), profiles=(), profile_id=None, summary="測試")
    assert not any("暫估" in label.text() for label in page.findChildren(QLabel))
    page._show_guidance(row)
    dialog = page.findChild(QDialog)
    assert dialog is not None
    browser = dialog.findChild(QTextBrowser)
    assert browser is not None
    assert "小羊早睡不遭罪" in browser.toPlainText()
    assert "不會自動下載遊戲內使用率" in browser.toPlainText()
    assert "role_id=1209" in browser.toHtml()
    dialog.close()


@pytest.fixture
def overview_page(qtbot: QtBot) -> EquipmentOverviewPage:
    page = EquipmentOverviewPage()
    qtbot.addWidget(page)
    page.resize(1200, 850)
    page.show()
    return page


def _apply(
    page: EquipmentOverviewPage,
    characters: tuple[EquipmentCharacterView, ...],
    *,
    weakest: tuple[EquipmentPieceView, ...] = (),
    profile_id: str = "profile-1",
) -> None:
    page.apply_data(
        characters,
        weakest,
        profiles=(("profile-1", "輸出型"), ("profile-2", "共鳴型")),
        profile_id=profile_id,
        summary="測試配裝資料",
    )


def _card(page: EquipmentOverviewPage, identifier: str) -> QFrame:
    index = page.visible_character_ids.index(identifier)
    item = page.cards_grid.itemAt(index)
    assert item is not None
    widget = item.widget()
    assert isinstance(widget, QFrame)
    return widget


def test_total_sort_keeps_incomplete_scores_last_in_both_directions(
    overview_page: EquipmentOverviewPage,
) -> None:
    characters = (
        _character("D-unscored", None, complete=False),
        _character("C-incomplete", 1.0, weakest=1.0, complete=False),
        _character("B-strong", 80.0, weakest=5.0),
        _character("A-balanced", 50.0, weakest=40.0),
    )
    _apply(overview_page, characters)
    assert overview_page.visible_character_ids == (
        "A-balanced",
        "B-strong",
        "C-incomplete",
        "D-unscored",
    )
    overview_page.sort_combo.setCurrentIndex(overview_page.sort_combo.findData("total_descending"))
    assert overview_page.visible_character_ids == (
        "B-strong",
        "A-balanced",
        "C-incomplete",
        "D-unscored",
    )
    assert overview_page.sort_combo.findData("weakest") == -1
    assert overview_page.sort_combo.findData("name") == -1


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("all", ("level", "weapon", "skills", "echoes", "missing")),
        ("level", ("level",)),
        ("weapon", ("weapon",)),
        ("skills", ("skills",)),
        ("echoes", ("echoes", "missing")),
        ("missing", ("missing",)),
    ],
)
def test_cultivation_filters_use_their_own_state(
    overview_page: EquipmentOverviewPage, category: str, expected: tuple[str, ...]
) -> None:
    characters = (
        replace(_character("level", 10, weakest=10), level_done=False),
        replace(_character("weapon", 20, weakest=20), weapon_done=False),
        replace(_character("skills", 30, weakest=30), skills_done=False),
        replace(_character("echoes", 40, weakest=40), echoes_done=False),
        _character("missing", None, complete=False),
    )
    _apply(overview_page, characters)
    overview_page.filter_combo.setCurrentIndex(overview_page.filter_combo.findData(category))
    assert overview_page.visible_character_ids == expected


def test_piece_mode_ignores_hidden_character_filters_and_excludes_archives(
    overview_page: EquipmentOverviewPage,
) -> None:
    active = replace(_character("Alpha", 20, weakest=10), level_done=False)
    archived = _character("Alpha-old", 30, weakest=15, archived=True)
    other = _character("Beta", 40, weakest=20)
    _apply(
        overview_page,
        (active, archived, other),
        weakest=tuple(c.pieces[0] for c in (active, archived, other)),
    )
    overview_page.filter_combo.setCurrentIndex(overview_page.filter_combo.findData("level"))
    assert overview_page.visible_character_ids == ("Alpha",)
    assert overview_page.weakest_layout.count() == 0  # Hidden view is created on first use.
    overview_page.view_combo.setCurrentIndex(1)
    assert overview_page.visible_character_ids == ("Alpha", "Beta")
    assert overview_page.weakest_layout.count() == 2
    assert not overview_page.sort_combo.isVisible()
    assert not overview_page.filter_combo.isVisible()
    assert overview_page.findChildren(QLineEdit) == []
    assert not any(
        "Alpha-old" in label.text()
        for label in overview_page.content.findChildren(QLabel)
        if label.isVisible()
    )
    overview_page.view_combo.setCurrentIndex(0)
    assert overview_page.visible_character_ids == ("Alpha",)
    assert overview_page.sort_combo.isVisible()
    assert overview_page.filter_combo.isVisible()


def test_missing_scores_and_slots_are_not_presented_as_zero(
    overview_page: EquipmentOverviewPage,
) -> None:
    character = _character("unknown", None, complete=False)
    _apply(overview_page, (character,), weakest=character.pieces)
    card = _card(overview_page, "unknown")
    total = card.findChild(QLabel, "EquipmentTotalScore")
    assert total is not None and total.text() == "待評分"
    slots = card.findChildren(QLabel, "EquipmentSlotScore")
    assert [label.text() for label in slots] == ["1\n待評分", "2\n—", "3\n—", "4\n—", "5\n—"]
    assert "副詞條資料不足" in slots[0].toolTip()
    labels = [label.text() for label in card.findChildren(QLabel)]
    assert "已登記 1 件 · 已評分 0/5" in labels
    assert not any("0.0" in text for text in labels)
    overview_page.view_combo.setCurrentIndex(1)
    piece_score = overview_page.content.findChild(QLabel, "EquipmentPieceScore")
    assert piece_score is not None and piece_score.text() == "待評分"


def test_a_real_zero_score_remains_distinct_from_missing_data(
    overview_page: EquipmentOverviewPage,
) -> None:
    character = _character("已評零分", 0.0, weakest=0.0)
    _apply(overview_page, (character,))
    card = _card(overview_page, character.identifier)
    total = card.findChild(QLabel, "EquipmentTotalScore")
    assert total is not None and total.text() == "0.0"
    assert "總分 / 500" in [label.text() for label in card.findChildren(QLabel)]
    assert all(
        label.text().endswith("\n0.0") for label in card.findChildren(QLabel, "EquipmentSlotScore")
    )


def test_weak_piece_mode_shows_all_equipped_pieces_sorted_lowest_first(
    overview_page: EquipmentOverviewPage,
) -> None:
    characters = tuple(_character(f"owner-{i:02}", i, weakest=i) for i in range(20))
    pieces = tuple(piece for character in reversed(characters) for piece in character.pieces)
    _apply(overview_page, characters, weakest=pieces)
    overview_page.view_combo.setCurrentIndex(1)
    assert overview_page.weakest_layout.count() == 100
    assert "100 件" in overview_page.summary_label.text()
    row = overview_page.weakest_layout.itemAt(0).widget()
    assert isinstance(row, QFrame)
    assert any("owner-00" in label.text() for label in row.findChildren(QLabel))
    last = overview_page.weakest_layout.itemAt(99).widget()
    assert isinstance(last, QFrame)
    assert any("owner-19" in label.text() for label in last.findChildren(QLabel))
    scores = [
        float(
            overview_page.weakest_layout.itemAt(index)
            .widget()
            .findChild(QLabel, "EquipmentPieceScore")
            .text()
        )
        for index in range(100)
    ]
    assert scores == sorted(scores)


def test_unused_comparison_control_is_hidden_and_refresh_does_not_change_profile(
    overview_page: EquipmentOverviewPage,
) -> None:
    requested: list[str] = []
    overview_page.profile_requested.connect(requested.append)
    _apply(overview_page, ())
    _apply(overview_page, ())
    assert requested == []
    assert not overview_page.profile_combo.isVisible()
    _apply(overview_page, (), profile_id="profile-2")
    assert requested == []
    assert overview_page.profile_combo.currentData() == "profile-2"


def test_character_and_piece_navigation_emit_correct_identity_once(
    overview_page: EquipmentOverviewPage,
) -> None:
    character = _character("緋雪", 55, weakest=25)
    unknown_position = _piece("穗穗", 20, slot=None)
    second = replace(_character("穗穗", 20, weakest=20, complete=False), pieces=(unknown_position,))
    _apply(overview_page, (character, second), weakest=(character.pieces[0], unknown_position))
    characters: list[str] = []
    pieces: list[tuple[str, int]] = []
    overview_page.character_requested.connect(characters.append)
    overview_page.piece_requested.connect(
        lambda identifier, slot: pieces.append((identifier, slot))
    )
    button = _card(overview_page, "緋雪").findChild(QPushButton)
    assert button is not None
    button.click()
    overview_page.view_combo.setCurrentIndex(1)
    for index in range(2):
        row = overview_page.weakest_layout.itemAt(index).widget()
        assert isinstance(row, QFrame)
        piece_button = row.findChild(QPushButton)
        assert piece_button is not None
        piece_button.click()
    assert characters == ["緋雪"]
    assert pieces == [("穗穗", 0), ("緋雪", 1)]


def test_resize_reflows_cards_without_stale_columns_or_duplicate_widgets(
    overview_page: EquipmentOverviewPage, qapp: QApplication
) -> None:
    characters = tuple(_character(f"角色{i}", i * 10, weakest=i * 10) for i in range(1, 7))
    _apply(overview_page, characters)
    for width, columns in ((1200, 4), (874, 3), (800, 2), (560, 1), (1200, 4)):
        overview_page.resize(width, 850)
        qapp.processEvents()
        assert overview_page.cards_grid.count() == len(characters)
        assert overview_page.visible_character_ids == tuple(
            character.identifier for character in characters
        )
        for index in range(len(characters)):
            row, column, row_span, column_span = overview_page.cards_grid.getItemPosition(index)
            assert (row, column, row_span, column_span) == (index // columns, index % columns, 1, 1)
        assert (
            sum(
                card.isVisible()
                for card in overview_page.content.findChildren(QFrame, "EquipmentCharacterCard")
            )
            == 6
        )


def test_loaded_roster_scrolls_without_compressing_cards_and_switches_to_weakest(
    overview_page: EquipmentOverviewPage, qapp: QApplication
) -> None:
    characters = tuple(_character(f"角色{i}", i * 5, weakest=i * 5) for i in range(15))
    overview_page.resize(1100, 760)
    overview_page.show()
    _apply(overview_page, characters, weakest=tuple(c.pieces[0] for c in characters))
    for _ in range(3):
        qapp.processEvents()
    assert overview_page.scroll_area.verticalScrollBar().maximum() > 0
    assert all(172 <= _card(overview_page, c.identifier).height() <= 245 for c in characters)
    assert _card(overview_page, characters[5].identifier).geometry().bottom() < (
        overview_page.scroll_area.viewport().height()
    )
    assert not overview_page.weakest_title.isVisible()
    overview_page.view_combo.setCurrentIndex(1)
    for _ in range(3):
        qapp.processEvents()
    assert overview_page.weakest_title.isVisible()
    assert not _card(overview_page, characters[0].identifier).isVisible()
    assert overview_page.weakest_layout.itemAt(0).widget().height() >= 66


def test_score_gradient_uses_numeric_score_even_when_profile_fit_differs(
    overview_page: EquipmentOverviewPage,
) -> None:
    character = _character("角色", 70)
    pieces = tuple(
        replace(_piece("角色", score, slot=slot), is_passing=passing)
        for slot, score, passing in (
            (1, 0.0, False),
            (2, 69.0, True),
            (3, 85.0, False),
            (4, 90.0, True),
            (5, None, None),
        )
    )
    _apply(overview_page, (replace(character, pieces=pieces),))
    slots = _card(overview_page, "角色").findChildren(QLabel, "EquipmentSlotScore")
    from wuwa_assistant.ui.score_colors import score_color

    for label, piece in zip(slots, pieces, strict=True):
        label.ensurePolished()
        assert label.palette().windowText().color().name() == score_color(piece.score).name()
    assert len({label.palette().windowText().color().name() for label in slots}) == 5
    total = _card(overview_page, "角色").findChild(QLabel, "EquipmentTotalScore")
    assert total is not None
    total.ensurePolished()
    assert (
        total.palette().windowText().color().name()
        == score_color(character.total, maximum=500).name()
    )


def test_entire_character_card_opens_detail_once(
    qtbot: QtBot, overview_page: EquipmentOverviewPage
) -> None:
    _apply(overview_page, (_character("緋雪", 50),))
    with qtbot.waitSignal(overview_page.character_requested) as event:
        qtbot.mouseClick(_card(overview_page, "緋雪"), Qt.MouseButton.LeftButton)
    assert event.args == ["緋雪"]


def test_piece_grid_reflows_and_card_navigation_keeps_character_and_slot(
    qtbot: QtBot, overview_page: EquipmentOverviewPage, qapp: QApplication
) -> None:
    characters = tuple(_character(f"角色{i}", i * 5, weakest=i * 5) for i in range(20))
    pieces = tuple(piece for character in characters for piece in character.pieces)
    _apply(overview_page, characters, weakest=pieces)
    overview_page.view_combo.setCurrentIndex(1)
    for width, columns in ((1200, 4), (874, 3), (800, 2), (560, 1), (1200, 4)):
        overview_page.resize(width, 720)
        qapp.processEvents()
        assert overview_page.weakest_layout.count() == 100
        assert overview_page.scroll_area.horizontalScrollBar().maximum() == 0
        assert not overview_page.filter_combo.isVisible()
        assert not overview_page.sort_combo.isVisible()
        for index in (0, 1, 12, 50, 99):
            assert overview_page.weakest_layout.getItemPosition(index) == (
                index // columns,
                index % columns,
                1,
                1,
            )
    last = overview_page.weakest_layout.itemAt(99).widget()
    assert isinstance(last, QFrame)
    with qtbot.waitSignal(overview_page.piece_requested) as event:
        qtbot.mouseClick(last, Qt.MouseButton.LeftButton)
    assert event.args == ["角色19", 5]
