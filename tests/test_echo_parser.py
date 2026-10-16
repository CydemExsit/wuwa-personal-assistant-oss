from __future__ import annotations

import cv2
import numpy as np
import pytest

from wuwa_assistant.models import EchoObservation, OcrToken, PageClassification, PageType, StatKind
from wuwa_assistant.models.names import canonical_character_name, character_name_key
from wuwa_assistant.ocr import FakeOcrEngine
from wuwa_assistant.parsers import EchoPageParser
from wuwa_assistant.parsers.common import parse_stat_row, strict_level
from wuwa_assistant.parsers.echo import ENHANCEMENT_PARSE_PANEL, _resolve_equipped_evidence
from wuwa_assistant.parsers.equipment import equipped_owner_lines, parse_equipped_owner
from wuwa_assistant.parsers.selection_text import (
    parse_selection_cost,
    parse_selection_header,
    parse_set_name,
    resolve_selection_name_retry,
    selection_name_retry_bounds,
)
from wuwa_assistant.vision.equipped import EquippedDetection
from wuwa_assistant.vision.equipped_slot import EquippedSlotDetection
from wuwa_assistant.vision.rois import SELECTION_COST_ROI, SELECTION_GRID_ROI


def token(text: str, x: int, y: int, confidence: float = 0.99) -> OcrToken:
    return OcrToken(text=text, confidence=confidence, bbox=(x, y, x + 100, y + 30))


def frame() -> np.ndarray:
    return np.zeros((1440, 2560, 3), dtype=np.uint8)


@pytest.mark.parametrize(
    ("raw_name", "raw_confidence", "expected_name"),
    [
        ("+生命", 0.77278, "生命"),
        ("\uff0b生命", 0.76034, "生命"),
        ("+防禦", 0.79104, "防禦"),
        ("、防禦", 0.73689, "防禦"),
        ("X攻擊", 0.79653, "攻擊"),
        ("+ 攻擊", 0.75990, "攻擊"),
        ("十暴擊", 0.84818, "暴擊"),
    ],
)
def test_stat_name_confidence_uses_exact_lexicon_and_paired_number_evidence(
    raw_name: str,
    raw_confidence: float,
    expected_name: str,
) -> None:
    stat, locked = parse_stat_row(
        [
            token(raw_name, 10, 20, raw_confidence),
            token("40", 500, 20, 0.99999),
        ],
        y1=0,
        y2=100,
        stat_kind=StatKind.SUBSTAT,
    )

    assert locked is False
    assert stat is not None
    assert stat.name == expected_name
    assert stat.raw_name == raw_name
    assert stat.name_confidence is not None
    assert 0.85 <= stat.name_confidence <= 0.95
    assert stat.value_confidence == 0.99999


@pytest.mark.parametrize(
    ("raw_name", "raw_confidence", "value_confidence"),
    [
        ("攻擊", 0.73689, 0.99999),
        ("+攻擊", 0.69, 0.99999),
        ("+攻擊", 0.80, 0.94),
        ("A攻擊", 0.99, 0.99999),
        ("錯攻擊", 0.99, 0.99999),
    ],
)
def test_stat_name_confidence_does_not_promote_unsupported_evidence(
    raw_name: str,
    raw_confidence: float,
    value_confidence: float,
) -> None:
    stat, _locked = parse_stat_row(
        [
            token(raw_name, 10, 20, raw_confidence),
            token("40", 500, 20, value_confidence),
        ],
        y1=0,
        y2=100,
        stat_kind=StatKind.SUBSTAT,
    )

    assert stat is not None
    assert stat.name_confidence is not None
    assert stat.name_confidence < 0.85


def test_ambiguous_stat_fragment_remains_unknown() -> None:
    stat, locked = parse_stat_row(
        [token("傷害加成", 10, 20), token("9.4%", 500, 20)],
        y1=0,
        y2=100,
        stat_kind=StatKind.SUBSTAT,
    )

    assert stat is None
    assert locked is False


def selection_frame(*, equipped_badge: bool) -> np.ndarray:
    image = frame()
    grid = SELECTION_GRID_ROI.crop(image)
    x, y, width, height = 190, 120, 152, 191
    cv2.rectangle(
        grid,
        (x + 2, y + 2),
        (x + width - 3, y + height - 3),
        (100, 200, 220),
        4,
    )
    if equipped_badge:
        grid[y + 3 : y + 47, x + 3 : x + 47] = (30, 36, 44)
        cv2.rectangle(grid, (x + 5, y + 5), (x + 41, y + 42), (70, 190, 245), 4)
    return image


def test_enhancement_parser_corrects_icon_noise_but_preserves_raw_text() -> None:
    panel_tokens = [
        token("不熄獵手", 30, 45),
        token("+15", 30, 115),
        token("生命", 90, 195),
        token("15.5%", 530, 195),
        token("生命", 90, 250),
        token("1550", 535, 250),
        token("十.暴擊", 50, 310, 0.85),
        token("7.5%", 535, 310),
        token("共鳴解放傷害加成", 90, 370),
        token("9.4%", 535, 370),
        token("、 攻擊", 50, 425, 0.70),
        token("10.1%", 520, 425),
        token("強化至+20並啟用輔音屬性", 90, 480),
        token("強化至+25並啟用輔音屬性", 90, 540),
    ]
    parser = EchoPageParser(FakeOcrEngine([panel_tokens]))
    classification = PageClassification(PageType.ECHO_ENHANCEMENT, 0.99)

    observation = parser.parse(
        frame(), classification, source_file="synthetic.png", source_timestamp=None
    )

    assert observation.name == "不熄獵手"
    assert observation.level == 15
    assert observation.cost is None
    assert [(stat.name, stat.value, stat.unit) for stat in observation.main_stats] == [
        ("生命", 15.5, "%"),
        ("生命", 1550, None),
    ]
    assert [(stat.name, stat.value, stat.unit) for stat in observation.sub_stats] == [
        ("暴擊", 7.5, "%"),
        ("共鳴解放傷害加成", 9.4, "%"),
        ("攻擊", 10.1, "%"),
    ]
    assert observation.sub_stats[0].raw_name == "十.暴擊"
    assert observation.sub_stats[2].raw_name == "、 攻擊"
    assert observation.locked_substat_slots == 2


def test_material_parser_keeps_current_and_projected_values_separate() -> None:
    panel_tokens = [
        token("不熄獵手", 30, 45),
        token("+0 > +15", 30, 115),
        token("生命", 90, 195),
        token("4.5%", 315, 195),
        token("15.5%", 530, 195),
        token("生命", 90, 250),
        token("456", 340, 250),
        token("1550", 535, 250),
        *[
            token(f"強化至+{level}並啟用輔音屬性", 90, 310 + index * 57)
            for index, level in enumerate((5, 10, 15, 20, 25))
        ],
    ]
    source_tokens = [token("+15", 20, 130)]
    parser = EchoPageParser(FakeOcrEngine([panel_tokens, source_tokens, []]))
    classification = PageClassification(PageType.ENHANCEMENT_MATERIAL_SELECTION, 0.99)

    observation = parser.parse(
        frame(), classification, source_file="synthetic.png", source_timestamp=None
    )

    assert (observation.level, observation.projected_level) == (0, 15)
    assert [stat.value for stat in observation.main_stats] == [4.5, 456]
    assert [stat.value for stat in observation.projected_main_stats] == [15.5, 1550]
    assert observation.material_source_selected is True
    assert observation.material_source_level == 15
    assert observation.locked_substat_slots == 5


def test_live_parser_consumes_owned_panel_roi_without_source_path() -> None:
    panel_tokens = [
        token("不熄獵手", 30, 45),
        token("+5", 30, 115),
        token("生命", 90, 195),
        token("8.2%", 530, 195),
        token("生命", 90, 250),
        token("820", 535, 250),
        token("暴擊", 90, 310),
        token("7.5%", 535, 310),
        *[
            token(f"強化至+{level}並啟用輔音屬性", 90, 370 + index * 57)
            for index, level in enumerate((10, 15, 20, 25))
        ],
    ]
    panel = np.zeros(
        (ENHANCEMENT_PARSE_PANEL.height, ENHANCEMENT_PARSE_PANEL.width, 3),
        dtype=np.uint8,
    )
    parser = EchoPageParser(FakeOcrEngine([panel_tokens]))

    observation = parser.parse_rois(
        {ENHANCEMENT_PARSE_PANEL.name: panel.copy()},
        PageClassification(PageType.ECHO_ENHANCEMENT, 0.99),
        source_file=None,
        source_timestamp="2026-08-01T00:00:00Z",
    )

    assert observation.source_file is None
    assert observation.name == "不熄獵手"
    assert observation.level == 5
    assert [(stat.name, stat.value) for stat in observation.sub_stats] == [("暴擊", 7.5)]
    assert observation.locked_substat_slots == 4


def test_live_parser_wrong_sized_roi_returns_null_fields() -> None:
    parser = EchoPageParser(FakeOcrEngine())
    observation = parser.parse_rois(
        {ENHANCEMENT_PARSE_PANEL.name: np.zeros((1, 1, 3), dtype=np.uint8)},
        PageClassification(PageType.ECHO_ENHANCEMENT, 0.91),
        source_file=None,
        source_timestamp=None,
    )

    assert observation.page_type is PageType.ECHO_ENHANCEMENT
    assert observation.name is None
    assert observation.level is None
    assert observation.main_stats == []
    assert observation.confidence == {"page_type": 0.91}


def test_selection_parser_keeps_all_five_unlocked_substat_rows() -> None:
    panel_tokens = [
        token("不熄獵手", 30, 45),
        token("+25", 450, 45),
        token("COST 1", 90, 115),
        token("生命", 90, 165),
        token("22.8%", 500, 165),
        token("生命", 90, 215),
        token("2280", 500, 215),
        token("暴擊", 90, 260),
        token("10.5%", 500, 260),
        token("暴擊傷害", 90, 310),
        token("21.0%", 500, 310),
        token("共鳴效率", 90, 360),
        token("12.4%", 500, 360),
        token("共鳴技能傷害加成", 90, 410),
        token("11.6%", 500, 410),
        token("攻擊", 90, 460),
        token("11.6%", 500, 460),
    ]
    parser = EchoPageParser(FakeOcrEngine([panel_tokens, []]))

    observation = parser.parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic.png",
        source_timestamp=None,
    )

    assert observation.level == 25
    assert len(observation.sub_stats) == 5
    assert [stat.slot_index for stat in observation.sub_stats] == [0, 1, 2, 3, 4]
    assert [stat.value for stat in observation.sub_stats] == [10.5, 21.0, 12.4, 11.6, 11.6]
    assert observation.is_equipped is None
    assert observation.confidence["is_equipped"] is None


def test_selection_parser_cross_checks_badge_owner_suffix_and_primary_action() -> None:
    positive_parser = EchoPageParser(
        FakeOcrEngine(
            [
                [],
                [],
                [token("\u611b\u5f4c\u65af\u88dd\u914d\u4e2d", 10, 10, 0.98)],
                [token("\u5378\u4e0b", 10, 10, 1.0)],
            ]
        )
    )
    positive = positive_parser.parse(
        selection_frame(equipped_badge=True),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-equipped.png",
        source_timestamp=None,
    )

    negative_parser = EchoPageParser(
        FakeOcrEngine([[], [], [], [token("\u88dd\u914d", 10, 10, 0.88)]])
    )
    negative = negative_parser.parse(
        selection_frame(equipped_badge=False),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-unequipped.png",
        source_timestamp=None,
    )

    assert positive.is_equipped is True
    assert positive.confidence["is_equipped"] == 1.0
    assert positive.equipped_character_name == "愛彌斯"
    assert positive.confidence["equipped_character_name"] == 0.98
    assert positive.raw_ocr["selection_equipped_owner"][0].text.endswith("\u88dd\u914d\u4e2d")
    assert negative.is_equipped is False
    assert negative.equipped_character_name is None
    assert negative.confidence["is_equipped"] is not None
    assert negative.confidence["is_equipped"] >= 0.90


def test_selection_parser_returns_unknown_when_badge_and_action_disagree() -> None:
    parser = EchoPageParser(FakeOcrEngine([[], [], [], [token("\u88dd\u914d", 10, 10, 0.99)]]))

    observation = parser.parse(
        selection_frame(equipped_badge=True),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-conflict.png",
        source_timestamp=None,
    )

    assert observation.is_equipped is None
    assert observation.confidence["is_equipped"] is None


def test_exact_owner_label_overrides_missing_portrait_outline() -> None:
    observation = EchoPageParser(
        FakeOcrEngine(
            [[], [], [token("愛彌斯裝配中", 70, 45, 0.97871)], [token("卸下", 98, 24, 0.99975)]]
        )
    ).parse(
        selection_frame(equipped_badge=False),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-owner.png",
        source_timestamp=None,
    )

    assert observation.is_equipped is True
    assert observation.equipped_character_name == "愛彌斯"
    assert observation.confidence["equipped_character_name"] == 0.97871


@pytest.mark.parametrize("suffix", ["裝配中", "裝備中", "装配中", "装备中"])
def test_owner_label_preserves_exact_unknown_name_without_seed_lookup(suffix: str) -> None:
    owner, confidence = parse_equipped_owner(
        equipped_owner_lines([token(f" 新 角 色 {suffix} ", 10, 10, 0.96)])
    )

    assert owner == "新角色"
    assert confidence == 0.96


def test_owner_label_reassembles_adjacent_fragments_in_reading_order() -> None:
    tokens = [
        OcrToken("配中", 0.97, (157, 43, 217, 80)),
        OcrToken("愛彌斯", 0.98, (30, 42, 120, 80)),
        OcrToken("裝", 0.99, (123, 42, 154, 80)),
    ]

    assert parse_equipped_owner(equipped_owner_lines(tokens)) == ("愛彌斯", 0.97)


@pytest.mark.parametrize(
    "owner_tokens",
    [
        [token("裝配中", 10, 10)],
        [token("愛彌斯", 10, 10)],
        [token("愛彌斯裝配中", 10, 10, 0.84)],
        [token("愛彌斯裝配申", 10, 10)],
        [token("?愛彌斯裝配中", 10, 10)],
        [token("123裝配中", 10, 10)],
        [token("愛彌斯裝配中", 10, 10), token("長離裝配中", 10, 60)],
        [token("愛彌斯", 10, 10), token("裝配中", 111, 60)],
        [token("愛彌斯", 10, 10), token("裝配中", 200, 10)],
        [token("愛彌", 10, 10, 0.50), token("斯裝配中", 111, 10)],
    ],
)
def test_owner_label_rejects_weak_conflicting_or_disconnected_text(
    owner_tokens: list[OcrToken],
) -> None:
    assert parse_equipped_owner(equipped_owner_lines(owner_tokens)) == (None, None)


def test_owner_label_and_explicit_unequipped_action_conflict_remains_unknown() -> None:
    assert _resolve_equipped_evidence(
        EquippedDetection(True, 0.95),
        [token("愛彌斯裝配中", 10, 10)],
        [token("裝配", 10, 10)],
    ) == (None, None, None, None)


def test_badge_or_suffix_alone_does_not_identify_an_owner() -> None:
    assert _resolve_equipped_evidence(EquippedDetection(True, 0.95), [], []) == (
        True,
        0.95,
        None,
        None,
    )
    assert _resolve_equipped_evidence(
        EquippedDetection(False, 0.95), [token("裝配中", 10, 10)], []
    ) == (None, None, None, None)


def test_equipped_owner_changes_observation_semantics_without_changing_echo_stats() -> None:
    original = EchoObservation(
        PageType.ECHO_SELECTION, name="冠頂械隼", is_equipped=True, equipped_character_name="愛彌斯"
    )
    changed = EchoObservation(
        PageType.ECHO_SELECTION, name="冠頂械隼", is_equipped=True, equipped_character_name="長離"
    )

    assert original.semantic_key() != changed.semantic_key()


@pytest.mark.parametrize("element", ["熱熔", "冷凝", "導電", "氣動", "衍射", "湮滅"])
def test_elemental_main_stat_labels_are_preserved_exactly(element: str) -> None:
    stat, locked = parse_stat_row(
        [token(f"{element}傷害加成", 153, 163), token("30.0%", 465, 166)],
        y1=145,
        y2=205,
        stat_kind=StatKind.PRIMARY,
        value_split_x=380,
    )

    assert locked is False
    assert stat is not None
    assert stat.name == f"{element}傷害加成"
    assert stat.value == 30.0
    assert stat.unit == "%"
    assert stat.name_confidence == 0.99


def test_missing_selection_cost_retries_only_the_same_frame_cost_line() -> None:
    image = frame()
    image[240:300, 1995:2210] = (20, 30, 40)
    retry_tokens = [OcrToken("COST 1", 0.94549, (12, 14, 127, 46))]
    ocr = FakeOcrEngine([[token("COST", 109, 103)], [], [], [], retry_tokens])
    observation = EchoPageParser(ocr).parse(
        image,
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-missed-cost.png",
        source_timestamp=None,
    )

    assert observation.cost == 1
    assert observation.confidence["cost"] == 0.94549
    assert observation.raw_ocr["selection_cost_retry"] == retry_tokens
    assert ocr.call_count == 6  # Includes the independent set-effects read.
    assert ocr.seen_keys[4] == FakeOcrEngine.key_for_image(SELECTION_COST_ROI.crop(image))


def test_existing_selection_cost_does_not_add_an_ocr_retry() -> None:
    ocr = FakeOcrEngine([[token("COST 3", 109, 103)], [], [], []])
    observation = EchoPageParser(ocr).parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-cost.png",
        source_timestamp=None,
    )

    assert observation.cost == 3
    assert observation.raw_ocr["selection_cost_retry"] == []
    assert ocr.call_count == 5  # Includes the independent set-effects read.


def test_cost_retry_does_not_erase_conflicting_same_frame_evidence() -> None:
    ocr = FakeOcrEngine(
        [
            [token("COST 1", 109, 103), token("COST 3", 225, 103)],
            [],
            [],
            [],
            [OcrToken("COST 1", 0.99, (12, 14, 127, 46))],
        ]
    )
    observation = EchoPageParser(ocr).parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file=None,
        source_timestamp=None,
    )
    assert observation.cost is None


@pytest.mark.parametrize(
    "retry_tokens",
    [
        [],
        [OcrToken("COST", 0.99, (12, 14, 127, 46))],
        [OcrToken("1", 0.99, (12, 14, 127, 46))],
        [OcrToken("COST I", 0.99, (12, 14, 127, 46))],
        [OcrToken("COST 1", 0.79, (12, 14, 127, 46))],
        [OcrToken("COST 2", 0.99, (12, 14, 127, 46))],
        [
            OcrToken("COST", 0.99, (12, 14, 80, 46)),
            OcrToken("1", 0.70, (95, 14, 127, 46)),
        ],
        [
            OcrToken("COST 1", 0.99, (12, 14, 100, 46)),
            OcrToken("COST 3", 0.99, (120, 14, 210, 46)),
        ],
    ],
)
def test_selection_cost_retry_keeps_missing_weak_and_ambiguous_values_unknown(
    retry_tokens: list[OcrToken],
) -> None:
    ocr = FakeOcrEngine([[token("COST", 109, 103)], [], [], [], retry_tokens])
    observation = EchoPageParser(ocr).parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-cost.png",
        source_timestamp=None,
    )

    assert observation.cost is None
    assert observation.confidence["cost"] is None
    assert ocr.call_count == 6


@pytest.mark.parametrize("separator", ["·", "・", "･", "•", "‧", "∙", "⋅", ".", "\uff0e"])
def test_character_separator_variants_preserve_variant_name_identity(separator: str) -> None:
    raw_name = f" 秧秧 {separator} 玄翎 "
    assert canonical_character_name(raw_name) == "秧秧·玄翎"
    assert character_name_key(raw_name) == character_name_key("秧秧玄翎")
    assert character_name_key(raw_name) != character_name_key("秧秧")
    owner, confidence = parse_equipped_owner(
        equipped_owner_lines([token(f"{raw_name}裝配中", 10, 10, 0.99)])
    )
    assert owner == "秧秧·玄翎"
    assert confidence == 0.99


@pytest.mark.parametrize("level_text", ["+25", "\uff0b25", " + 25"])
def test_long_selection_name_splits_merged_explicit_level(level_text: str) -> None:
    parsed = parse_selection_header(
        [token(f"共鳴迴響·鳴式・虛造神型{level_text}", 101, 49, 0.95354)]
    )
    assert parsed == ("共鳴迴響·鳴式·虛造神型", 0.95354, 25, 0.95354)


@pytest.mark.parametrize("separator", ["·", "・", "‧", "", " "])
def test_split_nightmare_header_keeps_lower_confidence_prefix(separator: str) -> None:
    prefix = OcrToken(f"夢魘{separator}", 0.88, (102, 46, 186, 91))
    base = OcrToken("刺玫菇", 0.999, (194, 47, 328, 90))
    assert parse_selection_header([base, token("+25", 460, 48), prefix]) == (
        "夢魘·刺玫菇",
        0.88,
        25,
        0.99,
    )


def test_separate_header_separator_and_long_name_fragments_are_joined() -> None:
    tokens = [
        OcrToken("共鳴迴響", 0.99, (100, 45, 220, 90)),
        OcrToken("·", 0.98, (222, 45, 230, 90)),
        OcrToken("鳴式·虛造神型+25", 0.97, (232, 45, 550, 90)),
    ]
    assert parse_selection_header(tokens) == ("共鳴迴響·鳴式·虛造神型", 0.97, 25, 0.97)


@pytest.mark.parametrize("raw_prefix", ["夢魔·", "夢魔・", "夢魔 ‧ "])
def test_measured_nightmare_glyph_error_keeps_raw_ocr_confidence(raw_prefix: str) -> None:
    raw = OcrToken(raw_prefix + "刺玫菇", 0.9867, (102, 46, 328, 91))
    assert parse_selection_header([raw]) == ("夢魘·刺玫菇", 0.9867, None, None)
    assert raw.text == raw_prefix + "刺玫菇"
    assert raw.confidence == 0.9867


@pytest.mark.parametrize("name", ["刺玫菇", "不熄獵手", "夢魔花", "夢魔刺玫菇"])
def test_nightmare_prefix_is_not_inferred_for_a_bare_name(name: str) -> None:
    assert parse_selection_header([token(name, 102, 46)]) == (name, 0.99, None, None)


@pytest.mark.parametrize(
    "prefix",
    [
        OcrToken("夢魘·", 0.5, (102, 46, 186, 91)),
        OcrToken("夢魘·", 0.98, (102, 10, 186, 56)),
        OcrToken("夢魘·", 0.98, (0, 46, 50, 91)),
        OcrToken("夢魘·", 0.98, (102, 46, 230, 91)),
    ],
)
def test_uncertain_or_unrelated_prefix_never_turns_into_a_base_name(prefix: OcrToken) -> None:
    assert parse_selection_header([prefix, OcrToken("刺玫菇", 0.99, (194, 47, 328, 90))])[:2] == (
        None,
        None,
    )


@pytest.mark.parametrize("name", ["夢魘", "夢魘·", "夢魔", "夢魔·"])
def test_nightmare_marker_without_a_base_name_is_incomplete(name: str) -> None:
    assert parse_selection_header([token(name, 102, 46)])[:2] == (None, None)


def test_detector_dropped_prefix_gets_one_bounded_recognition_only_retry() -> None:
    base = OcrToken("刺玫菇", 0.999, (194, 47, 328, 90))
    retry = OcrToken("夢魔·刺玫菇", 0.97, (0, 0, 239, 65))
    panel = [base, token("+25", 460, 48), token("COST 3", 100, 105)]
    ocr = FakeOcrEngine([panel, [], [], [], [], [retry]])
    observation = EchoPageParser(ocr).parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file=None,
        source_timestamp=None,
    )
    assert observation.name == "夢魘·刺玫菇"
    assert observation.confidence["name"] == 0.97
    assert observation.level == 25
    assert observation.raw_ocr["selection_detail"][0] == base
    assert observation.raw_ocr["selection_name_retry"] == [retry]
    assert ocr.detect_history == (True, True, True, True, True, False)
    assert selection_name_retry_bounds(panel) == (95, 35, 334, 100)
    assert ocr.seen_keys[-1] == FakeOcrEngine.key_for_image(np.zeros((65, 239, 3), dtype=np.uint8))


@pytest.mark.parametrize(
    "retry",
    [[], [token("夢魘·刺玫菇", 95, 35, 0.70)], [token("夢魘·不熄獵手", 95, 35)]],
)
def test_failed_or_conflicting_prefix_retry_leaves_name_unknown(retry: list[OcrToken]) -> None:
    base = OcrToken("刺玫菇", 0.999, (194, 47, 328, 90))
    assert resolve_selection_name_retry([base], retry) == (None, None)


def test_ordinary_left_aligned_name_does_not_add_a_retry() -> None:
    assert selection_name_retry_bounds([token("刺玫菇", 102, 46)]) is None
    assert selection_name_retry_bounds([token("夢魘·刺玫菇", 102, 46)]) is None


def test_header_level_split_keeps_invalid_or_conflicting_level_unknown() -> None:
    assert parse_selection_header([token("聲骸名字+26", 101, 49)]) == ("聲骸名字", 0.99, None, None)
    assert parse_selection_header([token("聲骸名字+25", 101, 49), token("+20", 450, 49)]) == (
        "聲骸名字",
        0.99,
        None,
        None,
    )
    assert parse_selection_header([token("聲骸名字25", 101, 49)]) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("level", range(26))
def test_exact_level_is_preserved_inside_every_tuning_band(level: int) -> None:
    assert strict_level(f"\uff0b{level}") == level
    assert parse_selection_header([token(f"封庭械囿+{level}", 101, 49)]) == (
        "封庭械囿",
        0.99,
        level,
        0.99,
    )


@pytest.mark.parametrize("raw_level", ["-1", "+26", "+250", "+17.5", "+2x", "+022"])
def test_non_echo_or_malformed_levels_are_never_coerced(raw_level: str) -> None:
    assert strict_level(raw_level) is None


@pytest.mark.parametrize("level", [17, 22])
def test_selection_non_node_levels_preserve_actual_main_stats(level: int) -> None:
    main_percent, fixed_attack = (22.3, 74) if level == 17 else (27.1, 90)
    rows = [
        ("攻擊", "7.9%"),
        ("攻擊", "50"),
        ("暴擊", "8.1%"),
        ("生命", "430"),
    ][: level // 5]
    panel = [
        token("封庭械囿", 100, 40),
        token(f"+{level}", 480, 40),
        token("COST 3", 100, 105),
        token("湮滅傷害加成", 100, 160),
        token(f"{main_percent}%", 480, 160),
        token("攻擊", 100, 215),
        token(str(fixed_attack), 480, 215),
    ]
    for index, (name, value) in enumerate(rows):
        panel.extend([token(name, 100, 260 + 50 * index), token(value, 480, 260 + 50 * index)])
    observation = EchoPageParser(FakeOcrEngine([panel, [], [], []])).parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file=None,
        source_timestamp=None,
    )
    assert observation.level == level
    assert observation.confidence["level"] == 0.99
    assert [stat.value for stat in observation.main_stats] == [main_percent, fixed_attack]
    assert len(observation.sub_stats) == level // 5


def test_material_level_projection_accepts_exact_values_without_prefix_matching() -> None:
    assert EchoPageParser._parse_level_projection([token("+17 → +22", 30, 115)]) == (17, 22, 0.99)
    assert EchoPageParser._parse_level_projection([token("+17 → +250", 30, 115)]) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("offset", [0, 150, 250])
def test_set_parser_follows_heading_and_count_rows_after_variable_skill_length(offset: int) -> None:
    tokens = [
        token("聲骸技能", 103, offset),
        token("召喚阿列夫一造物,造成冷凝傷害。", 144, 45 + offset),
        token("合鳴效果", 104, 100 + offset),
        token("雪落無聲之願", 144, 150 + offset),
        token("(2/2)", 378, 150 + offset),
        token("冷凝傷害提升", 144, 190 + offset),
        token("雪落無聲之願", 144, 235 + offset),
        token("(5/5)", 378, 235 + offset),
    ]
    assert parse_set_name(tokens) == ("雪落無聲之願", 0.99)


def test_set_parser_accepts_merged_marker_and_measured_parenthesis_decoration() -> None:
    assert parse_set_name(
        [token("合鳴效果", 104, 100), token("雪落無聲之願\uff082/2\uff09", 144, 150)]
    ) == ("雪落無聲之願", 0.99)
    assert parse_set_name(
        [
            token("合鳴效果", 104, 100),
            OcrToken("羽落空塵之歌\uff08", 0.93136, (144, 154, 342, 189)),
            OcrToken("©(5/5)", 0.97742, (330, 155, 453, 191)),
        ]
    ) == ("羽落空塵之歌", 0.93136)


@pytest.mark.parametrize(
    "tokens",
    [
        [token("雪落無聲之願", 144, 150), token("(2/2)", 378, 150)],
        [token("合鳴效果", 104, 100), token("冷凝傷害提升", 144, 150)],
        [
            token("合鳴效果", 104, 100),
            token("雪落無聲之願", 144, 150, 0.84),
            token("(2/2)", 378, 150),
        ],
        [
            token("合鳴效果", 104, 100),
            token("雪落無聲之願", 144, 150),
            token("(2/2)", 378, 195),
        ],
        [
            token("合鳴效果", 104, 100),
            token("雪落無聲之願(2/2)", 144, 150),
            token("長路啟航之星(5/5)", 144, 240),
        ],
    ],
)
def test_set_parser_rejects_unanchored_weak_disconnected_or_conflicting_labels(
    tokens: list[OcrToken],
) -> None:
    assert parse_set_name(tokens) == (None, None)


@pytest.mark.parametrize("cost", [1, 3, 4])
def test_cost_parser_accepts_measured_hyphen_separator_without_inferring_digits(cost: int) -> None:
    assert parse_selection_cost([token(f"COST-{cost}", 110, 103, 0.94932)]) == (cost, 0.94932)


@pytest.mark.parametrize("text", ["COST--3", "COST-2", "COST-", "COST--", "COST-三", "-3"])
def test_cost_parser_rejects_unproved_or_invalid_hyphen_values(text: str) -> None:
    assert parse_selection_cost([token(text, 110, 103)]) == (None, None)


def test_cost_parser_does_not_resolve_conflicting_explicit_values() -> None:
    assert parse_selection_cost([token("COST-3", 110, 103), token("COST 4", 110, 103)]) == (
        None,
        None,
    )


def test_selection_parser_integrates_long_header_and_new_set_field() -> None:
    set_tokens = [
        token("合鳴效果", 104, 463),
        token("雪落無聲之願", 144, 512),
        token("(2/2)", 378, 514),
    ]
    observation = EchoPageParser(
        FakeOcrEngine(
            [
                [token("共鳴迴響·鳴式・虛造神型+25", 101, 49, 0.95354), token("COST 4", 110, 103)],
                [],
                [],
                [],
                set_tokens,
            ]
        )
    ).parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-long-header.png",
        source_timestamp=None,
    )

    assert observation.name == "共鳴迴響·鳴式·虛造神型"
    assert observation.level == 25 and observation.confidence["level"] == 0.95354
    assert observation.set_name == "雪落無聲之願"
    assert observation.confidence["set_name"] == 0.99
    assert observation.raw_ocr["selection_set_effects"] == set_tokens


@pytest.mark.parametrize(
    ("owner_text", "action_text", "expected_slot"),
    [("愛彌斯裝配中", "卸下", 3), ("裝配中", "卸下", None), ("愛彌斯裝配中", "裝配", None)],
)
def test_selection_slot_requires_consistent_explicit_owner_evidence(
    monkeypatch: pytest.MonkeyPatch, owner_text: str, action_text: str, expected_slot: int | None
) -> None:
    calls: list[bool] = []

    def slot_detector(*_args: object) -> EquippedSlotDetection:
        calls.append(True)
        return EquippedSlotDetection(3, 0.95, "active_ring_and_unique_artwork_match")

    monkeypatch.setattr("wuwa_assistant.parsers.echo.detect_selected_equipped_slot", slot_detector)
    observation = EchoPageParser(
        FakeOcrEngine(
            [
                [token("COST 3", 110, 103)],
                [],
                [token(owner_text, 10, 10)],
                [token(action_text, 10, 10)],
            ]
        )
    ).parse(
        frame(),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-slot.png",
        source_timestamp=None,
    )

    assert observation.equipped_slot_index == expected_slot
    assert bool(calls) is (expected_slot is not None)
    assert observation.confidence["equipped_slot_index"] == (0.95 if expected_slot else None)


def test_notification_overlay_rejects_all_fields_even_when_some_game_text_remains_visible() -> None:
    parser = EchoPageParser(
        FakeOcrEngine(
            [
                [
                    token("不熄獵手", 101, 49),
                    token("+25", 450, 49),
                    token("COST 1", 110, 103),
                    token("生命", 150, 165),
                    token("22.8%", 500, 165),
                ],
                [],
                [token("剪取與繪圖", 10, 10)],
                [token("儲存到剪貼簿的", 10, 10)],
            ]
        )
    )
    observation = parser.parse(
        selection_frame(equipped_badge=True),
        PageClassification(PageType.ECHO_SELECTION, 0.99),
        source_file="synthetic-overlay.png",
        source_timestamp=None,
    )

    assert observation.name is None and observation.level is None and observation.cost is None
    assert observation.main_stats == [] and observation.sub_stats == []
    assert observation.is_equipped is None and observation.is_discarded is None
    assert observation.set_name is None and observation.selection_identity is None
    assert observation.equipped_character_name is None and observation.equipped_slot_index is None
    assert observation.raw_ocr == {}


def test_set_and_equipped_slot_changes_are_semantically_distinct() -> None:
    first = EchoObservation(PageType.ECHO_SELECTION, name="不熄獵手", set_name="羽落空塵之歌")
    second = EchoObservation(PageType.ECHO_SELECTION, name="不熄獵手", set_name="長路啟航之星")
    assert first.semantic_key() != second.semantic_key()
    second.set_name = first.set_name
    assert first.semantic_key() == second.semantic_key()
    second.equipped_slot_index = 3
    assert first.semantic_key() != second.semantic_key()


@pytest.mark.parametrize("name", ["岩蛛S4型", "岩蛛 S4 型", "岩蛛Ｓ４型"])
def test_selection_header_keeps_alphanumeric_echo_names(name: str) -> None:
    from wuwa_assistant.parsers.selection_text import parse_selection_header

    parsed, confidence, level, _ = parse_selection_header(
        [token(name, 101, 49), token("+25", 500, 49)]
    )
    assert parsed == "岩蛛S4型"
    assert confidence == 0.99
    assert level == 25
