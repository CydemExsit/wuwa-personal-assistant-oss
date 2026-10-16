from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from wuwa_assistant.ui.pages.characters import CharactersPage, PermanentEchoSlotView
from wuwa_assistant.ui.pages.echo_live import EchoLivePage, RecommendationView
from wuwa_assistant.ui.pages.equipment_detail import (
    EquipmentDetailPage,
    GearCharacterDetailView,
    GearPieceDetailView,
)
from wuwa_assistant.ui.score_colors import apply_score_color, score_color, score_span
from wuwa_assistant.ui.theme import apply_dark_theme


def _luminance(color: QColor) -> float:
    channels = (color.redF(), color.greenF(), color.blueF())
    linear = (
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def test_full_gradient_is_readable_and_smooth_with_many_distinct_colors() -> None:
    colors = [score_color(value / 10) for value in range(1001)]
    assert len({score_color(value).name() for value in range(101)}) == 101
    assert len({color.name() for color in colors}) > 300
    for background in ("#1d1628", "#2e203d", "#55305f", "#694075"):
        for color in colors:
            assert (_luminance(color) + 0.05) / (_luminance(QColor(background)) + 0.05) >= 4.5
    for score in (40, 55, 65, 75, 85, 92):
        before, after = score_color(score - 0.01), score_color(score + 0.01)
        assert (
            max(abs(a - b) for a, b in zip(before.getRgbF(), after.getRgbF(), strict=True)) < 0.003
        )
    assert len({score_color(value).name() for value in (27.3, 35.5, 48, 62.5, 70.5, 82.7, 91)}) == 7


@pytest.mark.parametrize("value", (0.0, 27.3, 51.56, 70.0, 91.0, 100.0))
def test_total_and_single_score_share_same_scale(value: float) -> None:
    assert score_color(value).name() == score_color(value * 5, maximum=500).name()


def test_unknown_is_neutral_zero_is_colored_and_out_of_range_clamps() -> None:
    assert score_color(None).name() != score_color(0).name()
    assert score_color(float("nan")) == score_color(None)
    assert score_color(float("inf")) == score_color(None)
    assert score_color(-1) == score_color(0)
    assert score_color(101) == score_color(100)
    assert "&lt;" in score_span("<unknown>", None)


@pytest.mark.parametrize("maximum", (0, -1, float("nan"), float("inf")))
def test_invalid_scale_rejected(maximum: float) -> None:
    with pytest.raises(ValueError):
        score_color(50, maximum=maximum)


@pytest.mark.parametrize(
    "name",
    ("EquipmentSlotScore", "EquipmentPieceScore", "EquipmentTotalScore", "ScoringCurrentScore"),
)
def test_refresh_and_clear_override_theme_without_losing_type_size(qtbot: QtBot, name: str) -> None:
    label = QLabel("55.5")
    label.setObjectName(name)
    qtbot.addWidget(label)
    apply_dark_theme(label)
    label.show()
    label.ensurePolished()
    font = label.font()
    for score in (55.5, 91.0, 0.0, None, 71.8):
        apply_score_color(label, score)
        label.ensurePolished()
        assert label.palette().windowText().color().name() == score_color(score).name()
        assert label.font() == font


def test_live_scores_projected_values_target_and_clear_keep_individual_colors(qtbot: QtBot) -> None:
    page = EchoLivePage()
    qtbot.addWidget(page)
    apply_dark_theme(page)
    page.apply_recommendation(
        RecommendationView(
            score=47.1,
            opened_slot_quality_score=78.5,
            expected_score=86.4,
            estimated_score_range=(82, 96),
            target_score=70.7,
            score_delta=-23.6,
        )
    )
    for label, score in ((page.cumulative_score_label, 47.1), (page.score_label, 78.5)):
        label.ensurePolished()
        assert label.palette().windowText().color().name() == score_color(score).name()
    for score in (86.4, 82, 96):
        assert score_color(score).name() in page.score_range_label.text()
    assert score_color(70.7).name() in page.score_delta_label.text()
    document = QTextDocument()
    document.setHtml(page.score_range_label.text())
    assert document.toPlainText() == "期望 86.4 · 範圍 82.0\u201396.0"
    page.apply_recommendation(RecommendationView())
    page.cumulative_score_label.ensurePolished()
    assert (
        page.cumulative_score_label.palette().windowText().color().name()
        == score_color(None).name()
    )
    assert score_color(86.4).name() not in page.score_range_label.text()


def test_detail_selector_summary_and_character_slots_refresh_from_same_scale(qtbot: QtBot) -> None:
    detail_page = EquipmentDetailPage()
    characters = CharactersPage()
    for page in (detail_page, characters):
        qtbot.addWidget(page)
        apply_dark_theme(page)
    for value in (0.0, 51.8, 91.0, None):
        piece = GearPieceDetailView(
            position=1,
            echo_id="echo",
            name="測試聲骸",
            level=25,
            cost=4,
            set_name="套裝",
            main_stats=(),
            score=value,
            stats=(),
            fit="適配待確認",
            action="",
            reason="",
            target="",
            is_passing=False,
        )
        detail = GearCharacterDetailView("owner", "角色", "", "均分", "", "", "", (piece,))
        detail_page.apply_detail(detail)
        characters.echo_cards[0].apply_slot(PermanentEchoSlotView(1, score=value, is_passing=False))
        labels = [
            detail_page.cards[1].findChild(QLabel, "EquipmentPieceScore"),
            detail_page.findChild(QLabel, "EquipmentDetailScoreSummary"),
            detail_page.slot_buttons[1],
            characters.echo_cards[0].score_label,
        ]
        for label in labels:
            assert label is not None
            label.ensurePolished()
            assert label.palette().windowText().color().name() == score_color(value).name()
        assert detail.average_score == value
