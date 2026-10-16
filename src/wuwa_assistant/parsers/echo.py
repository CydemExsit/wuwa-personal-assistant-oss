"""Parsers for enhancement, selection, and material-selection echo observations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from wuwa_assistant.models import (
    EchoObservation,
    EchoStat,
    OcrToken,
    PageClassification,
    PageType,
    StatKind,
)
from wuwa_assistant.ocr.base import OcrEngine
from wuwa_assistant.vision.debug import DebugWriter
from wuwa_assistant.vision.discard import DiscardDetection, detect_selection_discard_toggle
from wuwa_assistant.vision.equipped import EquippedDetection, detect_selected_echo_equipped
from wuwa_assistant.vision.equipped_slot import detect_selected_equipped_slot
from wuwa_assistant.vision.preprocess import crop_roi, validate_image
from wuwa_assistant.vision.rois import (
    MATERIAL_SELECTED_CARD_ROI,
    MATERIAL_SOURCE_SLOT_ROI,
    SELECTION_COST_ROI,
    SELECTION_DETAIL,
    SELECTION_DISCARD_TOAST_TEXT_ROI,
    SELECTION_DISCARD_TOGGLE_ROI,
    SELECTION_EQUIPPED_OWNER_ROI,
    SELECTION_EQUIPPED_SLOTS_ROI,
    SELECTION_GRID_ROI,
    SELECTION_PRIMARY_ACTION_ROI,
    SELECTION_SET_EFFECTS_ROI,
    Roi,
)
from wuwa_assistant.vision.selection_identity import extract_selection_identity

from .common import (
    MIN_NUMBER_CONFIDENCE,
    null_stat,
    parse_level_band,
    parse_name,
    parse_stat_row,
    strict_level,
    strict_number,
    token_center,
    tokens_in_band,
)
from .equipment import (
    MIN_EQUIPPED_TEXT_CONFIDENCE,
    equipped_owner_lines,
    equipped_suffix_confidence,
    parse_equipped_owner,
)
from .lexicon import calibrated_stat_name_confidence, match_stat_name
from .selection_text import (
    has_selection_overlay,
    parse_selection_cost,
    parse_selection_header,
    parse_set_name,
    resolve_selection_name_retry,
    selection_name_retry_bounds,
)

ENHANCEMENT_PARSE_PANEL = Roi("echo_info_panel", 240, 150, 920, 750)
SELECTION_PARSE_PANEL = SELECTION_DETAIL
MATERIAL_PARSE_PANEL = Roi("material_target_panel", 240, 150, 920, 760)

ENHANCEMENT_SUBSTAT_BANDS = tuple((300 + 57 * index, 357 + 57 * index) for index in range(5))


def _resolve_discard_evidence(
    visual: DiscardDetection,
    toast_tokens: list[OcrToken],
) -> tuple[bool | None, float | None]:
    evidence: list[tuple[bool, float]] = []
    if visual.is_discarded is not None and visual.confidence is not None:
        evidence.append((visual.is_discarded, visual.confidence))
    evidence.extend(
        (True, token.confidence)
        for token in toast_tokens
        if "已棄置" in token.text.replace(" ", "")
    )
    if not evidence or len({value for value, _confidence in evidence}) != 1:
        return (None, None)
    return (evidence[0][0], min(confidence for _value, confidence in evidence))


def _resolve_equipped_evidence(
    visual: EquippedDetection,
    owner_tokens: list[OcrToken],
    action_tokens: list[OcrToken],
) -> tuple[bool | None, float | None, str | None, float | None]:
    """Prefer a clear owner label over badge geometry; reject text contradictions.

    A missed portrait outline cannot overturn an exact, confident owner label.
    Conflicting owner/action text still fails closed, as does a badge/action
    conflict without strong owner text. Never identify a character from a badge.
    """

    evidence: list[tuple[bool, float]] = []
    owner_lines = equipped_owner_lines(owner_tokens)
    owner_name, owner_confidence = parse_equipped_owner(owner_lines)
    suffix_confidence = equipped_suffix_confidence(owner_lines)
    if suffix_confidence is not None:
        evidence.append((True, suffix_confidence))

    action_values = {
        "\u5378\u4e0b": True,  # 卸下
        "\u88dd\u914d": False,  # 裝配
        "\u88c5\u914d": False,  # 装配
    }
    for token in action_tokens:
        normalized = "".join(token.text.split())
        action_value = action_values.get(normalized)
        if action_value is not None and token.confidence >= MIN_EQUIPPED_TEXT_CONFIDENCE:
            evidence.append((action_value, token.confidence))

    if len({value for value, _confidence in evidence}) > 1:
        return None, None, None, None
    # When no exact owner is available, retain the existing conservative
    # badge/action agreement policy. A suffix alone cannot override geometry.
    if owner_name is None and visual.is_equipped is not None and visual.confidence is not None:
        evidence.append((visual.is_equipped, visual.confidence))
    elif visual.is_equipped is True and visual.confidence is not None:
        evidence.append((True, visual.confidence))

    values = {value for value, _confidence in evidence}
    if len(values) != 1:
        return None, None, None, None
    resolved = values.pop()
    return (
        resolved,
        round(max(confidence for _value, confidence in evidence), 4),
        owner_name if resolved else None,
        owner_confidence if resolved else None,
    )


def _field_confidences(stats: list[EchoStat], prefix: str) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for index, stat in enumerate(stats):
        result[f"{prefix}.{index}.name"] = stat.name_confidence
        result[f"{prefix}.{index}.value"] = stat.value_confidence
    return result


def _locked_row_confidence(tokens: list[OcrToken], y1: float, y2: float) -> float | None:
    matches = [
        token.confidence
        for token in tokens_in_band(tokens, y1, y2)
        if "強化至" in token.text and "啟用" in token.text
    ]
    return max(matches) if matches else None


class EchoPageParser:
    def __init__(self, ocr: OcrEngine, debug_writer: DebugWriter | None = None) -> None:
        self.ocr = ocr
        self.debug_writer = debug_writer or DebugWriter(None)

    def parse(
        self,
        frame: NDArray[np.uint8],
        classification: PageClassification,
        *,
        source_file: str | Path,
        source_timestamp: str | None,
    ) -> EchoObservation:
        rois: dict[str, NDArray[np.uint8]] = {}
        if classification.page_type is PageType.ECHO_ENHANCEMENT:
            rois[ENHANCEMENT_PARSE_PANEL.name] = crop_roi(frame, ENHANCEMENT_PARSE_PANEL)
        elif classification.page_type is PageType.ECHO_SELECTION:
            rois[SELECTION_PARSE_PANEL.name] = crop_roi(frame, SELECTION_PARSE_PANEL)
            rois[SELECTION_GRID_ROI.name] = crop_roi(frame, SELECTION_GRID_ROI)
            rois[SELECTION_DISCARD_TOGGLE_ROI.name] = crop_roi(frame, SELECTION_DISCARD_TOGGLE_ROI)
            rois[SELECTION_DISCARD_TOAST_TEXT_ROI.name] = crop_roi(
                frame, SELECTION_DISCARD_TOAST_TEXT_ROI
            )
            rois[SELECTION_EQUIPPED_OWNER_ROI.name] = crop_roi(frame, SELECTION_EQUIPPED_OWNER_ROI)
            rois[SELECTION_PRIMARY_ACTION_ROI.name] = crop_roi(frame, SELECTION_PRIMARY_ACTION_ROI)
            rois[SELECTION_SET_EFFECTS_ROI.name] = crop_roi(frame, SELECTION_SET_EFFECTS_ROI)
            rois[SELECTION_EQUIPPED_SLOTS_ROI.name] = crop_roi(frame, SELECTION_EQUIPPED_SLOTS_ROI)
        elif classification.page_type is PageType.ENHANCEMENT_MATERIAL_SELECTION:
            rois[MATERIAL_PARSE_PANEL.name] = crop_roi(frame, MATERIAL_PARSE_PANEL)
            rois[MATERIAL_SOURCE_SLOT_ROI.name] = crop_roi(frame, MATERIAL_SOURCE_SLOT_ROI)
            rois[MATERIAL_SELECTED_CARD_ROI.name] = crop_roi(frame, MATERIAL_SELECTED_CARD_ROI)
        return self.parse_rois(
            rois,
            classification,
            source_file=source_file,
            source_timestamp=source_timestamp,
        )

    def parse_rois(
        self,
        rois: Mapping[str, NDArray[np.uint8]],
        classification: PageClassification,
        *,
        source_file: str | Path | None = None,
        source_timestamp: str | None,
    ) -> EchoObservation:
        """Parse allowlisted ROI copies; no monitor-sized frame is accepted or retained."""

        debug_source: str | Path = source_file or "live-capture"
        try:
            observation = self._parse_known_rois(
                rois,
                classification,
                debug_source,
                source_file,
                source_timestamp,
            )
        except (KeyError, TypeError, ValueError):
            observation = EchoObservation(
                page_type=classification.page_type,
                confidence={"page_type": classification.confidence},
                source_timestamp=source_timestamp,
                source_file=str(source_file) if source_file is not None else None,
                raw_ocr=dict(classification.raw_ocr),
                debug_roi_paths=list(classification.debug_roi_paths),
            )

        observation.debug_ocr_path = self.debug_writer.save_json(
            debug_source, "ocr_raw", observation.raw_ocr
        )
        return observation

    def _parse_known_rois(
        self,
        rois: Mapping[str, NDArray[np.uint8]],
        classification: PageClassification,
        debug_source: str | Path,
        source_file: str | Path | None,
        source_timestamp: str | None,
    ) -> EchoObservation:
        if classification.page_type is PageType.ECHO_ENHANCEMENT:
            observation = self._parse_enhancement(
                _required_roi(rois, ENHANCEMENT_PARSE_PANEL),
                classification,
                debug_source,
                source_file,
                source_timestamp,
            )
        elif classification.page_type is PageType.ECHO_SELECTION:
            observation = self._parse_selection(
                _required_roi(rois, SELECTION_PARSE_PANEL),
                _required_roi(rois, SELECTION_GRID_ROI),
                _required_roi(rois, SELECTION_DISCARD_TOGGLE_ROI),
                _required_roi(rois, SELECTION_DISCARD_TOAST_TEXT_ROI),
                _required_roi(rois, SELECTION_EQUIPPED_OWNER_ROI),
                _required_roi(rois, SELECTION_PRIMARY_ACTION_ROI),
                classification,
                debug_source,
                source_file,
                source_timestamp,
                set_effects=_optional_roi(rois, SELECTION_SET_EFFECTS_ROI),
                equipped_slots=_optional_roi(rois, SELECTION_EQUIPPED_SLOTS_ROI),
            )
        elif classification.page_type is PageType.ENHANCEMENT_MATERIAL_SELECTION:
            observation = self._parse_material(
                _required_roi(rois, MATERIAL_PARSE_PANEL),
                _required_roi(rois, MATERIAL_SOURCE_SLOT_ROI),
                _required_roi(rois, MATERIAL_SELECTED_CARD_ROI),
                classification,
                debug_source,
                source_file,
                source_timestamp,
            )
        else:
            observation = EchoObservation(
                page_type=classification.page_type,
                confidence={"page_type": classification.confidence},
                source_timestamp=source_timestamp,
                source_file=str(source_file) if source_file is not None else None,
                raw_ocr=dict(classification.raw_ocr),
                debug_roi_paths=list(classification.debug_roi_paths),
            )
        return observation

    def _parse_enhancement(
        self,
        panel: NDArray[np.uint8],
        classification: PageClassification,
        debug_source: str | Path,
        source_file: str | Path | None,
        source_timestamp: str | None,
    ) -> EchoObservation:
        tokens = self.ocr.recognize(panel, detect=True)
        debug_paths = list(classification.debug_roi_paths)
        debug_paths.extend(self.debug_writer.save_roi(debug_source, "echo_info_panel", panel))

        name, name_confidence = parse_name(tokens, 30, 105)
        level, level_confidence = parse_level_band(tokens, 105, 175)

        main_stats: list[EchoStat] = []
        for index, (y1, y2, kind) in enumerate(
            ((180, 243, StatKind.PRIMARY), (243, 300, StatKind.FIXED))
        ):
            stat, _locked = parse_stat_row(
                tokens,
                y1=y1,
                y2=y2,
                stat_kind=kind,
                slot_index=index,
                value_split_x=450,
            )
            main_stats.append(stat or null_stat(kind, index))

        sub_stats: list[EchoStat] = []
        locked_count = 0
        resolved_rows = 0
        row_confidences: list[float] = []
        for index, (y1, y2) in enumerate(ENHANCEMENT_SUBSTAT_BANDS):
            stat, locked = parse_stat_row(
                tokens,
                y1=y1,
                y2=y2,
                stat_kind=StatKind.SUBSTAT,
                slot_index=index,
                value_split_x=450,
            )
            if stat is not None:
                sub_stats.append(stat)
                resolved_rows += 1
                if stat.confidence is not None:
                    row_confidences.append(stat.confidence)
            elif locked:
                locked_count += 1
                resolved_rows += 1
                row_confidence = _locked_row_confidence(tokens, y1, y2)
                if row_confidence is not None:
                    row_confidences.append(row_confidence)

        locked_slots = locked_count if resolved_rows == 5 else None
        confidence: dict[str, float | None] = {
            "page_type": classification.confidence,
            "name": name_confidence,
            "level": level_confidence,
            "cost": None,
            "locked_substat_slots": min(row_confidences) if resolved_rows == 5 else None,
        }
        confidence.update(_field_confidences(main_stats, "main_stats"))
        confidence.update(_field_confidences(sub_stats, "sub_stats"))

        return EchoObservation(
            page_type=classification.page_type,
            name=name,
            level=level,
            cost=None,
            main_stats=main_stats,
            sub_stats=sub_stats,
            locked_substat_slots=locked_slots,
            confidence=confidence,
            source_timestamp=source_timestamp,
            source_file=str(source_file) if source_file is not None else None,
            raw_ocr={**classification.raw_ocr, "echo_info_panel": tokens},
            debug_roi_paths=debug_paths,
        )

    def _parse_selection(
        self,
        panel: NDArray[np.uint8],
        grid: NDArray[np.uint8],
        discard_toggle: NDArray[np.uint8],
        toast: NDArray[np.uint8],
        owner: NDArray[np.uint8],
        primary_action: NDArray[np.uint8],
        classification: PageClassification,
        debug_source: str | Path,
        source_file: str | Path | None,
        source_timestamp: str | None,
        *,
        set_effects: NDArray[np.uint8] | None = None,
        equipped_slots: NDArray[np.uint8] | None = None,
    ) -> EchoObservation:
        tokens = self.ocr.recognize(panel, detect=True)
        toast_tokens = self.ocr.recognize(toast, detect=True)
        owner_tokens = self.ocr.recognize(owner, detect=True)
        action_tokens = self.ocr.recognize(primary_action, detect=True)
        if has_selection_overlay(owner_tokens, action_tokens):
            # A notification can leave the page header visible while covering
            # selected-echo fields. Do not combine that frame with old identity.
            return EchoObservation(
                page_type=classification.page_type,
                confidence={"page_type": classification.confidence},
                source_timestamp=source_timestamp,
                source_file=str(source_file) if source_file is not None else None,
            )
        debug_paths = list(classification.debug_roi_paths)
        debug_paths.extend(self.debug_writer.save_roi(debug_source, "selection_detail", panel))
        debug_paths.extend(
            self.debug_writer.save_roi(debug_source, "selection_discard_toast", toast)
        )
        debug_paths.extend(
            self.debug_writer.save_roi(debug_source, "selection_discard_toggle", discard_toggle)
        )
        debug_paths.extend(self.debug_writer.save_roi(debug_source, "selection_owner", owner))
        debug_paths.extend(
            self.debug_writer.save_roi(debug_source, "selection_primary_action", primary_action)
        )

        name, name_confidence, level, level_confidence = parse_selection_header(tokens)
        cost, cost_confidence = self._parse_cost(tokens)
        cost_tokens: list[OcrToken] = []
        if cost is None:
            # A broad detector can find COST but miss the isolated single digit.
            # Retry only this bounded line from the same already-owned panel;
            # never infer cost from the echo name, neighboring cards, or history.
            left = SELECTION_COST_ROI.x1 - SELECTION_PARSE_PANEL.x1
            top = SELECTION_COST_ROI.y1 - SELECTION_PARSE_PANEL.y1
            cost_line = panel[
                top : top + SELECTION_COST_ROI.height,
                left : left + SELECTION_COST_ROI.width,
            ].copy()
            cost_tokens = self.ocr.recognize(cost_line, detect=True)
            panel_relative_cost_tokens = [
                OcrToken(
                    text=token.text,
                    confidence=token.confidence,
                    bbox=(
                        token.bbox[0] + left,
                        token.bbox[1] + top,
                        token.bbox[2] + left,
                        token.bbox[3] + top,
                    ),
                )
                for token in cost_tokens
            ]
            retry_cost, _retry_confidence = self._parse_cost(panel_relative_cost_tokens)
            if retry_cost is not None:
                cost, cost_confidence = self._parse_cost(tokens + panel_relative_cost_tokens)

        set_tokens = self.ocr.recognize(set_effects, detect=True) if set_effects is not None else []
        set_name, set_confidence = parse_set_name(set_tokens)

        name_tokens: list[OcrToken] = []
        name_bounds = selection_name_retry_bounds(tokens)
        if name_bounds is not None:
            left, top, right, bottom = name_bounds
            name_tokens = self.ocr.recognize(panel[top:bottom, left:right].copy(), detect=False)
            panel_name_tokens = [
                OcrToken(
                    token.text,
                    token.confidence,
                    (
                        token.bbox[0] + left,
                        token.bbox[1] + top,
                        token.bbox[2] + left,
                        token.bbox[3] + top,
                    ),
                )
                for token in name_tokens
            ]
            name, name_confidence = resolve_selection_name_retry(tokens, panel_name_tokens)

        main_stats: list[EchoStat] = []
        for index, (y1, y2, kind) in enumerate(
            ((145, 205, StatKind.PRIMARY), (205, 255, StatKind.FIXED))
        ):
            stat, _locked = parse_stat_row(
                tokens,
                y1=y1,
                y2=y2,
                stat_kind=kind,
                slot_index=index,
                value_split_x=380,
            )
            main_stats.append(stat or null_stat(kind, index))

        sub_stats: list[EchoStat] = []
        for index in range(5):
            y1 = 250 + index * 50
            y2 = y1 + 50
            stat, _locked = parse_stat_row(
                tokens,
                y1=y1,
                y2=y2,
                stat_kind=StatKind.SUBSTAT,
                slot_index=index,
                value_split_x=380,
            )
            if stat is not None:
                sub_stats.append(stat)

        is_discarded, discard_confidence = _resolve_discard_evidence(
            detect_selection_discard_toggle(discard_toggle),
            toast_tokens,
        )
        equipped_visual = detect_selected_echo_equipped(grid)
        selection_identity = extract_selection_identity(
            grid,
            equipped_visual.selected_card_bbox,
        )
        is_equipped, equipped_confidence, owner_name, owner_confidence = _resolve_equipped_evidence(
            equipped_visual,
            owner_tokens,
            action_tokens,
        )
        equipped_slot_index: int | None = None
        equipped_slot_confidence: float | None = None
        if equipped_slots is not None and is_equipped is True and owner_name is not None:
            slot_detection = detect_selected_equipped_slot(
                equipped_slots, grid, equipped_visual.selected_card_bbox
            )
            if slot_detection.slot_index is not None and slot_detection.confidence is not None:
                equipped_slot_index = slot_detection.slot_index
                equipped_slot_confidence = min(slot_detection.confidence, owner_confidence or 0.0)

        confidence: dict[str, float | None] = {
            "page_type": classification.confidence,
            "name": name_confidence,
            "set_name": set_confidence,
            "level": level_confidence,
            "cost": cost_confidence,
            "locked_substat_slots": None,
            "is_discarded": discard_confidence,
            "is_equipped": equipped_confidence,
            "equipped_character_name": owner_confidence,
            "equipped_slot_index": equipped_slot_confidence,
        }
        confidence.update(_field_confidences(main_stats, "main_stats"))
        confidence.update(_field_confidences(sub_stats, "sub_stats"))

        return EchoObservation(
            page_type=classification.page_type,
            name=name,
            set_name=set_name,
            level=level,
            cost=cost,
            main_stats=main_stats,
            sub_stats=sub_stats,
            locked_substat_slots=None,
            confidence=confidence,
            source_timestamp=source_timestamp,
            source_file=str(source_file) if source_file is not None else None,
            is_discarded=is_discarded,
            is_equipped=is_equipped,
            equipped_character_name=owner_name,
            equipped_slot_index=equipped_slot_index,
            selection_identity=selection_identity,
            raw_ocr={
                **classification.raw_ocr,
                "selection_detail": tokens,
                "selection_discard_toast": toast_tokens,
                "selection_equipped_owner": owner_tokens,
                "selection_primary_action": action_tokens,
                "selection_cost_retry": cost_tokens,
                "selection_name_retry": name_tokens,
                "selection_set_effects": set_tokens,
            },
            debug_roi_paths=debug_paths,
        )

    @staticmethod
    def _parse_cost(tokens: list[OcrToken]) -> tuple[int | None, float | None]:
        return parse_selection_cost(tokens)

    def _parse_material(
        self,
        panel: NDArray[np.uint8],
        source_slot: NDArray[np.uint8],
        selected_card: NDArray[np.uint8],
        classification: PageClassification,
        debug_source: str | Path,
        source_file: str | Path | None,
        source_timestamp: str | None,
    ) -> EchoObservation:
        tokens = self.ocr.recognize(panel, detect=True)
        source_tokens = self.ocr.recognize(source_slot, detect=True)
        selected_card_tokens = self.ocr.recognize(selected_card, detect=True)

        debug_paths = list(classification.debug_roi_paths)
        debug_paths.extend(self.debug_writer.save_roi(debug_source, "material_target_panel", panel))
        debug_paths.extend(
            self.debug_writer.save_roi(debug_source, "material_source_slot", source_slot)
        )

        name, name_confidence = parse_name(tokens, 25, 105)
        level, projected_level, level_confidence = self._parse_level_projection(tokens)

        main_stats: list[EchoStat] = []
        projected_main_stats: list[EchoStat] = []
        for index, (y1, y2, kind) in enumerate(
            ((180, 243, StatKind.PRIMARY), (243, 300, StatKind.FIXED))
        ):
            current, projected = self._parse_material_stat_row(
                tokens, y1=y1, y2=y2, stat_kind=kind, slot_index=index
            )
            main_stats.append(current or null_stat(kind, index))
            projected_main_stats.append(projected or null_stat(kind, index))

        locked_count = 0
        resolved_rows = 0
        locked_confidences: list[float] = []
        for y1, y2 in ENHANCEMENT_SUBSTAT_BANDS:
            stat, locked = parse_stat_row(
                tokens,
                y1=y1,
                y2=y2,
                stat_kind=StatKind.SUBSTAT,
                value_split_x=450,
            )
            if stat is not None:
                resolved_rows += 1
            elif locked:
                resolved_rows += 1
                locked_count += 1
                row_confidence = _locked_row_confidence(tokens, y1, y2)
                if row_confidence is not None:
                    locked_confidences.append(row_confidence)

        source_level, source_confidence = parse_level_band(source_tokens, 100, 185)
        if source_level is None:
            source_level, source_confidence = parse_level_band(selected_card_tokens, 180, 280)
        source_selected = source_level is not None

        confidence: dict[str, float | None] = {
            "page_type": classification.confidence,
            "name": name_confidence,
            "level": level_confidence,
            "projected_level": level_confidence,
            "cost": None,
            "locked_substat_slots": (
                min(locked_confidences) if resolved_rows == 5 and locked_confidences else None
            ),
            "material_source_level": source_confidence,
            "material_source_selected": source_confidence,
        }
        confidence.update(_field_confidences(main_stats, "main_stats"))
        confidence.update(_field_confidences(projected_main_stats, "projected_main_stats"))

        return EchoObservation(
            page_type=classification.page_type,
            name=name,
            level=level,
            projected_level=projected_level,
            cost=None,
            main_stats=main_stats,
            projected_main_stats=projected_main_stats,
            sub_stats=[],
            locked_substat_slots=locked_count if resolved_rows == 5 else None,
            confidence=confidence,
            source_timestamp=source_timestamp,
            source_file=str(source_file) if source_file is not None else None,
            material_source_selected=source_selected,
            material_source_level=source_level,
            raw_ocr={
                **classification.raw_ocr,
                "material_target_panel": tokens,
                "material_source_slot": source_tokens,
                "material_selected_card": selected_card_tokens,
            },
            debug_roi_paths=debug_paths,
        )

    @staticmethod
    def _parse_level_projection(
        tokens: list[OcrToken],
    ) -> tuple[int | None, int | None, float | None]:
        for token in tokens_in_band(tokens, 100, 175):
            if token.confidence < MIN_NUMBER_CONFIDENCE:
                continue
            normalized = token.text.replace(" ", "")
            if ">" not in normalized and "→" not in normalized:
                continue
            raw_values = re.findall(r"\+([0-9]+)", normalized)
            values = [strict_level(value) for value in raw_values]
            if len(values) == 2:
                current, projected = values
                if current is not None and projected is not None:
                    return current, projected, token.confidence
        return None, None, None

    @staticmethod
    def _parse_material_stat_row(
        tokens: list[OcrToken],
        *,
        y1: float,
        y2: float,
        stat_kind: StatKind,
        slot_index: int,
    ) -> tuple[EchoStat | None, EchoStat | None]:
        row = tokens_in_band(tokens, y1, y2)
        name_token: OcrToken | None = None
        vocabulary_match = None
        for token in row:
            if token_center(token)[0] >= 250:
                continue
            match = match_stat_name(token.text)
            if match is not None:
                name_token = token
                vocabulary_match = match
                break
        if name_token is None or vocabulary_match is None:
            return None, None

        values: list[tuple[OcrToken, int | float, str | None]] = []
        for token in sorted(row, key=lambda item: token_center(item)[0]):
            if token_center(token)[0] < 250 or token.confidence < MIN_NUMBER_CONFIDENCE:
                continue
            parsed = strict_number(token.text)
            if parsed is not None:
                values.append((token, parsed[0], parsed[1]))
        if len(values) < 2:
            return None, None

        def make_stat(item: tuple[OcrToken, int | float, str | None]) -> EchoStat:
            token, value, unit = item
            name_confidence = calibrated_stat_name_confidence(
                vocabulary_match,
                ocr_confidence=name_token.confidence,
                value_confidence=token.confidence,
            )
            return EchoStat(
                stat_kind=stat_kind,
                name=vocabulary_match.value,
                value=value,
                unit=unit,
                confidence=min(name_confidence, token.confidence),
                name_confidence=name_confidence,
                value_confidence=token.confidence,
                raw_name=name_token.text,
                raw_value=token.text,
                slot_index=slot_index,
            )

        return make_stat(values[0]), make_stat(values[-1])


def _required_roi(
    images: Mapping[str, NDArray[np.uint8]],
    roi: Roi,
) -> NDArray[np.uint8]:
    try:
        image = images[roi.name]
    except KeyError as exc:
        raise KeyError(f"missing ROI {roi.name!r}") from exc
    validate_image(image)
    if image.shape[:2] != (roi.height, roi.width):
        height, width = image.shape[:2]
        raise ValueError(f"ROI {roi.name!r} must be {roi.width}x{roi.height}, got {width}x{height}")
    return image


def _optional_roi(
    images: Mapping[str, NDArray[np.uint8]],
    roi: Roi,
) -> NDArray[np.uint8] | None:
    return _required_roi(images, roi) if roi.name in images else None
