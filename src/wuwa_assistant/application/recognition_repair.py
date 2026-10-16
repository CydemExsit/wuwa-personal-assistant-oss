"""Previewable, capture-scoped repairs supported by retained local OCR evidence.

No database is opened implicitly. Callers back up their chosen database before
applying a plan. Raw observations, historical decisions, and all rows remain;
field changes have correction records and duplicate characters are only archived.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from math import isfinite
from typing import Any, TypeGuard

from sqlalchemy import select
from sqlalchemy.orm import Session

from wuwa_assistant.models import OcrToken
from wuwa_assistant.models.names import canonical_character_name, character_name_key
from wuwa_assistant.parsers.selection_text import parse_selection_cost, parse_selection_header
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    CharacterSkillRecord,
    CultivationTargetRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    ObservationCorrectionRecord,
    TeamMemberRecord,
    WeaponRecord,
    utc_now,
)
from wuwa_assistant.vision.rois import SELECTION_COST_ROI, SELECTION_DETAIL

from .characters import DEFAULT_SKILLS
from .observation_review import observation_review_issues
from .recognized_equipment import register_recognized_equipment

_ACTOR = "recognition_repair"
_MIN_CONFIDENCE = 0.90
_SUFFIX = re.compile(r"\+\s*(?:0|5|10|15|20|25)$")


@dataclass(frozen=True, slots=True)
class RecognitionFieldRepair:
    observation_id: str
    echo_session_id: str
    field: str
    old_value: str | int | None
    new_value: str | int
    confidence: float


@dataclass(frozen=True, slots=True)
class DuplicateCharacterArchive:
    character_id: str
    retained_character_id: str
    name: str


@dataclass(frozen=True, slots=True)
class RecognizedOwnerRepair:
    echo_session_id: str
    evidence_observation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class RecognitionRepairPreview:
    capture_session_id: str
    data_set_id: str
    field_repairs: tuple[RecognitionFieldRepair, ...]
    character_archives: tuple[DuplicateCharacterArchive, ...]
    owner_registrations: tuple[RecognizedOwnerRepair, ...]
    possible_duplicate_sessions: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class RecognitionRepairResult:
    preview: RecognitionRepairPreview
    corrected_fields: int
    archived_characters: int
    registered_equipment: int


class RecognitionRepairService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def preview(
        self, capture_session_id: str, *, missing_names_only: bool = False
    ) -> RecognitionRepairPreview:
        """Inspect the selected stopped capture without changing any record."""

        with self._session_factory() as session:
            plan = _preview(session, capture_session_id)
            return _missing_name_plan(plan) if missing_names_only else plan

    def apply(
        self, capture_session_id: str, *, missing_names_only: bool = False
    ) -> RecognitionRepairResult:
        """Revalidate and apply all supported changes in one caller-selected database."""

        with self._session_factory.begin() as session:
            plan = _preview(session, capture_session_id)
            if missing_names_only:
                plan = _missing_name_plan(plan)
            for archive in plan.character_archives:
                character = session.get(CharacterRecord, archive.character_id)
                assert character is not None
                character.archived_at = utc_now()
                character.is_candidate = False
                character.updated_at = utc_now()
            session.flush()
            by_observation: dict[str, list[RecognitionFieldRepair]] = defaultdict(list)
            for repair in plan.field_repairs:
                by_observation[repair.observation_id].append(repair)
            for observation_id, repairs in by_observation.items():
                observation = session.get(EchoObservationRecord, observation_id)
                assert observation is not None
                effective = deepcopy(observation.effective_payload_json)
                confidence = dict(observation.confidence_json)
                for repair in repairs:
                    effective[repair.field] = repair.new_value
                    confidence[repair.field] = repair.confidence
                    setattr(observation, repair.field, repair.new_value)
                    session.add(
                        ObservationCorrectionRecord(
                            observation_id=observation.id,
                            field_path=f"/{repair.field}",
                            old_value_json=repair.old_value,
                            new_value_json=repair.new_value,
                            reason="Reparsed explicit retained selection OCR; no inferred values.",
                            actor=_ACTOR,
                        )
                    )
                    _repair_echo_summary(session, repair)
                effective["confidence"] = confidence
                observation.effective_payload_json = effective
                observation.confidence_json = confidence
                observation.overall_confidence = min(confidence.values()) if confidence else None
                observation.payload_version += 1
                if any(r.field == "name" and r.old_value is None for r in repairs):
                    # Only release the parser-rejected name when every remaining
                    # field is still supported. Never fabricate a manual confirmation.
                    observation.requires_review = bool(observation_review_issues(effective)) or (
                        observation.overall_confidence is None
                        or observation.overall_confidence < _MIN_CONFIDENCE
                    )
            session.flush()
            registrations = 0
            for owner in plan.owner_registrations:
                echo_session = session.get(EchoSessionRecord, owner.echo_session_id)
                assert echo_session is not None
                result = register_recognized_equipment(
                    session,
                    echo_session,
                    data_set_id=plan.data_set_id,
                    name=owner.name,
                    slot_index=None,
                )
                registrations += int(result.changed)
            session.flush()
            return RecognitionRepairResult(
                plan, len(plan.field_repairs), len(plan.character_archives), registrations
            )


def _missing_name_plan(plan: RecognitionRepairPreview) -> RecognitionRepairPreview:
    return replace(
        plan,
        field_repairs=tuple(
            r for r in plan.field_repairs if r.field == "name" and r.old_value is None
        ),
        character_archives=(),
        owner_registrations=(),
    )


def _preview(session: Session, capture_id: str) -> RecognitionRepairPreview:
    capture = session.get(CaptureSessionRecord, capture_id)
    if capture is None:
        raise ValueError("capture session does not exist")
    if capture.status == "active":
        raise ValueError("stop capture before repairing recognition history")
    sessions = session.scalars(
        select(EchoSessionRecord)
        .where(EchoSessionRecord.capture_session_id == capture.id)
        .order_by(EchoSessionRecord.started_at, EchoSessionRecord.id)
    ).all()
    observations = session.scalars(
        select(EchoObservationRecord)
        .join(EchoSessionRecord, EchoObservationRecord.echo_session_id == EchoSessionRecord.id)
        .where(EchoSessionRecord.capture_session_id == capture.id)
        .order_by(EchoObservationRecord.sequence_no, EchoObservationRecord.id)
    ).all()
    grouped: dict[str, list[EchoObservationRecord]] = defaultdict(list)
    manual: dict[str, set[str]] = defaultdict(set)
    for observation in observations:
        grouped[observation.echo_session_id].append(observation)
    for correction in session.scalars(
        select(ObservationCorrectionRecord)
        .join(EchoObservationRecord)
        .join(EchoSessionRecord)
        .where(
            EchoSessionRecord.capture_session_id == capture.id,
            ObservationCorrectionRecord.actor != _ACTOR,
        )
    ):
        manual[correction.observation_id].add(correction.field_path)
    repairs = tuple(
        repair
        for observation in observations
        for repair in _field_repairs(observation, manual[observation.id])
    )
    archives = _duplicate_characters(session, capture, observations)
    owners: list[RecognizedOwnerRepair] = []
    for echo_session in sessions:
        echo = session.get(EchoRecord, echo_session.echo_id)
        if (
            echo is None
            or echo.data_set_id != capture.data_set_id
            or echo_session.was_consumed
            or echo.archived_at is not None
            or echo.final_status in {"consumed", "discarded"}
        ):
            continue
        history = grouped[echo_session.id]
        if any(
            manual[item.id].intersection({"/equipped_character_name", "/is_equipped"})
            for item in history
        ):
            continue
        # An explicit existing assignment always wins over historical recognition.
        if echo.character_id is not None:
            continue
        for observation in reversed(history):
            payload = observation.effective_payload_json
            if payload.get("page_type") != "echo_selection":
                continue
            action = _game_action(observation.raw_payload_json)
            if action == "裝配":
                break
            owner = _strong_owner(observation)
            if owner is not None and action == "卸下":
                owners.append(RecognizedOwnerRepair(echo_session.id, observation.id, owner))
                break
    duplicates: dict[tuple[object, ...], list[str]] = defaultdict(list)
    for echo_session in sessions:
        echo = session.get(EchoRecord, echo_session.echo_id)
        if echo is not None:
            key = (
                canonical_character_name(echo.name or ""),
                echo.level,
                echo.cost,
                _stat_key(echo.main_stats_json),
                _stat_key(echo.sub_stats_json),
            )
            duplicates[key].append(echo_session.id)
    return RecognitionRepairPreview(
        capture.id,
        capture.data_set_id,
        repairs,
        archives,
        tuple(owners),
        tuple(tuple(ids) for ids in duplicates.values() if len(ids) > 1),
    )


def _field_repairs(
    observation: EchoObservationRecord, manual_fields: set[str]
) -> list[RecognitionFieldRepair]:
    payload = observation.effective_payload_json
    if payload.get("page_type") != "echo_selection":
        return []
    tokens = _tokens(observation.raw_payload_json, "selection_detail")
    name, name_confidence, level, level_confidence = parse_selection_header(tokens)
    candidates: list[tuple[str, str | int, float]] = []
    old_name = payload.get("name")
    if (
        old_name is None
        and name is not None
        and name_confidence is not None
        and name_confidence >= _MIN_CONFIDENCE
    ):
        candidates.append(("name", name, name_confidence))
    if (
        isinstance(old_name, str)
        and _SUFFIX.search(old_name)
        and name is not None
        and level is not None
        and name_confidence is not None
        and name_confidence >= _MIN_CONFIDENCE
        and level_confidence is not None
        and level_confidence >= _MIN_CONFIDENCE
        and canonical_character_name(_SUFFIX.sub("", old_name)) == canonical_character_name(name)
    ):
        candidates.append(("name", name, name_confidence))
        if payload.get("level") is None:
            candidates.append(("level", level, level_confidence))
    if payload.get("cost") is None:
        left = SELECTION_COST_ROI.x1 - SELECTION_DETAIL.x1
        top = SELECTION_COST_ROI.y1 - SELECTION_DETAIL.y1
        retry = [
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
            for token in _tokens(observation.raw_payload_json, "selection_cost_retry")
        ]
        cost, cost_confidence = parse_selection_cost(tokens + retry)
        if cost is not None and cost_confidence is not None and cost_confidence >= _MIN_CONFIDENCE:
            candidates.append(("cost", cost, cost_confidence))
    return [
        RecognitionFieldRepair(
            observation.id,
            observation.echo_session_id,
            field,
            payload.get(field),
            value,
            confidence,
        )
        for field, value, confidence in candidates
        if f"/{field}" not in manual_fields and payload.get(field) != value
    ]


def _repair_echo_summary(session: Session, repair: RecognitionFieldRepair) -> None:
    echo_session = session.get(EchoSessionRecord, repair.echo_session_id)
    assert echo_session is not None
    echo = session.get(EchoRecord, echo_session.echo_id)
    if echo is None or getattr(echo, repair.field) != repair.old_value:
        return
    manual = session.scalar(
        select(ObservationCorrectionRecord.id)
        .join(EchoObservationRecord)
        .where(
            EchoObservationRecord.echo_session_id == echo_session.id,
            ObservationCorrectionRecord.field_path == f"/{repair.field}",
            ObservationCorrectionRecord.actor != _ACTOR,
        )
        .limit(1)
    )
    if manual is not None:
        return
    setattr(echo, repair.field, repair.new_value)
    echo.updated_at = utc_now()
    echo.score = None
    echo.score_profile_id = None
    echo.score_profile_version = None
    echo.score_observation_id = None


def _duplicate_characters(
    session: Session,
    capture: CaptureSessionRecord,
    observations: Sequence[EchoObservationRecord],
) -> tuple[DuplicateCharacterArchive, ...]:
    recognized_names = {
        observation.effective_payload_json.get("equipped_character_name")
        for observation in observations
        if _strong_owner(observation) is not None
    }
    groups: dict[str, list[CharacterRecord]] = defaultdict(list)
    for character in session.scalars(
        select(CharacterRecord)
        .where(
            CharacterRecord.data_set_id == capture.data_set_id,
            CharacterRecord.archived_at.is_(None),
        )
        .order_by(CharacterRecord.created_at, CharacterRecord.id)
    ):
        groups[character_name_key(character.name)].append(character)
    archives: list[DuplicateCharacterArchive] = []
    for characters in groups.values():
        if len(characters) < 2:
            continue
        retained = next(
            (item for item in characters if item.name == canonical_character_name(item.name)),
            characters[0],
        )
        for character in characters:
            if character.id == retained.id or character.name not in recognized_names:
                continue
            if character.created_at < capture.started_at:
                continue
            if capture.stopped_at is None or character.created_at > capture.stopped_at:
                continue
            if _is_unedited_default_character(session, character):
                archives.append(
                    DuplicateCharacterArchive(character.id, retained.id, character.name)
                )
    return tuple(archives)


def _is_unedited_default_character(session: Session, character: CharacterRecord) -> bool:
    if (
        character.level != 1
        or character.priority != 100
        or character.resonance_chain != 0
        or not character.is_candidate
        or character.element is not None
        or character.role is not None
        or character.scoring_profile_id is not None
        or character.notes
        or character.cultivation_goal_json
        != {
            "small_stat_nodes": [False] * 10,
            "skill_target_defaults": {"core": 10, "secondary": 6},
        }
    ):
        return False
    for column in (
        EchoRecord.character_id,
        WeaponRecord.character_id,
        TeamMemberRecord.character_id,
        CultivationTargetRecord.character_id,
        CaptureSessionRecord.assigned_character_id,
    ):
        if session.scalar(select(column).where(column == character.id).limit(1)) is not None:
            return False
    skills = session.scalars(
        select(CharacterSkillRecord)
        .where(CharacterSkillRecord.character_id == character.id)
        .order_by(CharacterSkillRecord.display_order)
    ).all()
    return len(skills) == 5 and all(
        record.skill_key == definition.skill_key
        and record.name == definition.name
        and record.skill_type == definition.skill_type
        and record.is_core == definition.is_core
        and record.current_level == 1
        and record.target_level == (10 if definition.is_core else 6)
        and not record.is_completed
        and record.display_order == index
        for index, (record, definition) in enumerate(zip(skills, DEFAULT_SKILLS, strict=True))
    )


def _tokens(payload: dict[str, Any], region: str) -> list[OcrToken]:
    raw = payload.get("raw_ocr")
    values = raw.get(region, []) if isinstance(raw, dict) else []
    if not isinstance(values, list):
        return []
    tokens: list[OcrToken] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        text, confidence, bbox = value.get("text"), value.get("confidence"), value.get("bbox")
        if (
            not isinstance(text, str)
            or not _valid_confidence(confidence)
            or not isinstance(bbox, (list, tuple))
            or len(bbox) != 4
            or not all(isinstance(number, int) and not isinstance(number, bool) for number in bbox)
        ):
            continue
        tokens.append(OcrToken(text, float(confidence), (bbox[0], bbox[1], bbox[2], bbox[3])))
    return tokens


def _valid_confidence(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        and 0 <= value <= 1
    )


def _game_action(payload: dict[str, Any]) -> str | None:
    actions = {
        token.text.strip()
        for token in _tokens(payload, "selection_primary_action")
        if token.text.strip() in {"裝配", "卸下"} and token.confidence >= _MIN_CONFIDENCE
    }
    return next(iter(actions)) if len(actions) == 1 else None


def _strong_owner(observation: EchoObservationRecord) -> str | None:
    payload = observation.effective_payload_json
    name = payload.get("equipped_character_name")
    confidence = payload.get("confidence", {}).get("equipped_character_name")
    if (
        payload.get("page_type") != "echo_selection"
        or payload.get("is_equipped") is not True
        or not isinstance(name, str)
        or not name.strip()
        or not _valid_confidence(confidence)
        or confidence < _MIN_CONFIDENCE
    ):
        return None
    for token in _tokens(observation.raw_payload_json, "selection_equipped_owner"):
        text = canonical_character_name(token.text)
        if (
            token.confidence >= _MIN_CONFIDENCE
            and text.endswith("裝配中")
            and character_name_key(text[:-3]) == character_name_key(name)
        ):
            return canonical_character_name(name)
    return None


def _stat_key(stats: list[dict[str, Any]]) -> tuple[tuple[object, object, object], ...]:
    return tuple((stat.get("name"), stat.get("value"), stat.get("unit")) for stat in stats)
