"""Read-only comparison of current equipment under one versioned scoring profile.

The complete-five-slot score and opened-slot quality are different measures.
Missing evidence never contributes zero and pending owners never imply a slot.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wuwa_assistant.cultivation import BlockerCode
from wuwa_assistant.scoring import (
    CharacterScoringProfile,
    EchoScoreBreakdown,
    EchoScoringEngine,
    ScoringContext,
)
from wuwa_assistant.scoring.character_guides import (
    AUTO_PROFILE_ID,
    STAT_LABELS,
    apply_guide_context,
    guide_for_character,
)
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
)

from .character_completion import _main_stat_validity, _set_validity, evaluate_character_detail
from .character_scoring import character_context, character_profile
from .characters import CharacterDetail, EchoSlotSnapshot, _character_details
from .echo_duplicates import EchoDuplicateGroup, group_echo_records
from .equipment_analysis import (
    EchoStatDetail,
    EchoTrainingRecommendation,
    explain_stats,
    guide_fit,
    training_advice,
)
from .observation_review import hold_reviewed_score
from .scoring_profiles import ScoringProfileService, ScoringProfileSnapshot
from .scoring_workflow import (
    _substat_completeness_note,
    observed_stats_from_payload,
    profile_from_snapshot,
)


@dataclass(frozen=True, slots=True)
class EquippedEchoScore:
    echo_id: str | None
    character_id: str
    character_name: str
    slot_index: int | None
    name: str | None
    set_name: str | None
    level: int | None
    cost: int | None
    score: float | None
    opened_slot_quality_score: float | None
    profile_id: str | None
    profile_version: int | None
    observation_id: str | None
    payload_version: int | None
    status: str
    notes: tuple[str, ...]
    set_is_valid: bool | None = None
    main_stat_is_valid: bool | None = None
    rescan_count: int = 0
    main_stats: tuple[dict[str, Any], ...] = ()
    is_passing: bool | None = None
    sub_stats: tuple[dict[str, Any], ...] = ()
    stat_details: tuple[EchoStatDetail, ...] = ()
    set_fit_reason: str = ""
    main_stat_fit_reason: str = ""
    training_recommendation: EchoTrainingRecommendation | None = None
    breakdown: EchoScoreBreakdown | None = None


@dataclass(frozen=True, slots=True)
class CultivationCategoryProgress:
    key: str
    done: int
    target: int
    percent: float
    at_goal: bool
    status: str


@dataclass(frozen=True, slots=True)
class CharacterEquipmentOverview:
    character_id: str
    character_name: str
    slots: tuple[EquippedEchoScore, ...]
    equipped_count: int
    scored_count: int
    average_score: float | None
    total_score: float | None
    partial_average_score: float | None
    lowest_score: float | None
    pending_echo_count: int
    categories: tuple[CultivationCategoryProgress, ...]
    pending_echoes: tuple[EquippedEchoScore, ...] = ()
    profile_name: str | None = None
    scoring_notes: tuple[str, ...] = ()
    template_kind: str | None = None
    weight_summary: str = ""
    profile_is_system: bool = False
    element: str = ""
    role: str = ""
    set_summary: str = ""


@dataclass(frozen=True, slots=True)
class EquipmentOverview:
    profile_id: str | None
    profile_name: str | None
    profile_version: int | None
    characters: tuple[CharacterEquipmentOverview, ...]
    weakest_echoes: tuple[EquippedEchoScore, ...]
    logical_echo_count: int
    physical_echo_count: int


class EquipmentOverviewService:
    """Recompute a comparable snapshot without modifying saved scoring provenance."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._profiles = ScoringProfileService(session_factory)
        self._scorer = EchoScoringEngine()

    def get_overview(
        self,
        *,
        data_set_id: str,
        profile_id: str | None = None,
        character_details: tuple[CharacterDetail, ...] | None = None,
    ) -> EquipmentOverview:
        if character_details is not None and any(
            detail.summary.data_set_id != data_set_id for detail in character_details
        ):
            raise ValueError("character does not belong to the requested data set")
        automatic = profile_id == AUTO_PROFILE_ID
        snapshot = (
            None
            if automatic
            else self._profiles.get_default(data_set_id=data_set_id)
            if profile_id is None
            else self._profiles.get_profile(profile_id, data_set_id=data_set_id)
        )
        profile = None if snapshot is None else profile_from_snapshot(snapshot)
        if snapshot is not None and not self._scorer.supports_version(snapshot.roll_data_version):
            # A historical profile cannot be silently evaluated using different roll data.
            profile = None
        profile_snapshots = (
            self._profiles.list_profiles(data_set_id=data_set_id) if automatic else None
        )
        with self._session_factory() as session:
            session.connection().exec_driver_sql("BEGIN")
            groups = group_echo_records(session, data_set_id=data_set_id)
            observations = _latest_observations(session, data_set_id)
            if character_details is None:
                records = session.scalars(
                    select(CharacterRecord)
                    .where(
                        CharacterRecord.data_set_id == data_set_id,
                        CharacterRecord.archived_at.is_(None),
                    )
                    .order_by(CharacterRecord.name, CharacterRecord.id)
                ).all()
                character_details = _character_details(session, records)
            # Keep representative records alive and fetch them in one dataset-scoped
            # read; Session's weak identity map otherwise causes per-slot queries.
            echoes = {
                echo.id: echo
                for echo in session.scalars(
                    select(EchoRecord).where(EchoRecord.data_set_id == data_set_id)
                )
            }
            characters = tuple(
                self._character(
                    detail,
                    tuple(
                        group for group in groups if group.owner_character_id == detail.summary.id
                    ),
                    echoes,
                    observations,
                    snapshot,
                    profile,
                    automatic=automatic,
                    profile_snapshots=profile_snapshots,
                )
                for detail in sorted(
                    character_details, key=lambda row: (row.summary.name, row.summary.id)
                )
                if detail.summary.archived_at is None
            )
        weakest = tuple(
            sorted(
                (piece for row in characters for piece in row.slots if piece.score is not None),
                key=lambda item: (
                    item.score if item.score is not None else float("inf"),
                    item.character_name,
                    item.slot_index or 0,
                    item.echo_id or "",
                ),
            )
        )
        return EquipmentOverview(
            profile_id=AUTO_PROFILE_ID if automatic else None if snapshot is None else snapshot.id,
            profile_name="依角色需求(攻略參考)"
            if automatic
            else None
            if snapshot is None
            else snapshot.name,
            profile_version=None if snapshot is None else snapshot.version,
            characters=characters,
            weakest_echoes=weakest,
            logical_echo_count=len(groups),
            physical_echo_count=sum(len(group.member_echo_ids) for group in groups),
        )

    def _character(
        self,
        detail: CharacterDetail,
        groups: tuple[EchoDuplicateGroup, ...],
        echoes: dict[str, EchoRecord],
        observations: dict[str, EchoObservationRecord],
        snapshot: ScoringProfileSnapshot | None,
        profile: CharacterScoringProfile | None,
        *,
        automatic: bool = False,
        profile_snapshots: tuple[ScoringProfileSnapshot, ...] | None = None,
    ) -> CharacterEquipmentOverview:
        context = character_context(detail.cultivation_goal) if automatic else ScoringContext()
        notes: tuple[str, ...] = ()
        if automatic:
            snapshot = character_profile(
                self._profiles,
                data_set_id=detail.summary.data_set_id,
                name=detail.summary.name,
                explicit_profile_id=detail.summary.scoring_profile_id,
                profile_snapshots=profile_snapshots,
            )
            profile = None if snapshot is None else profile_from_snapshot(snapshot)
            if snapshot is not None and not self._scorer.supports_version(
                snapshot.roll_data_version
            ):
                profile = None
        if (
            automatic
            and profile is not None
            and snapshot is not None
            and snapshot.is_system_template
        ):
            profile, notes = apply_guide_context(profile, context)
        if automatic and snapshot is None:
            notes = ("尚無已查核的角色攻略, 請在角色編輯區選擇評分設定檔",)
        owned = [group for group in groups if group.owner_character_id == detail.summary.id]
        by_slot = {group.slot_index: group for group in owned if group.slot_index is not None}
        pieces: list[EquippedEchoScore] = []
        current_slots: list[EchoSlotSnapshot] = []
        for slot_index in range(1, 6):
            group = by_slot.get(slot_index)
            record = None if group is None else echoes.get(group.representative_echo_id)
            slot = _slot_snapshot(slot_index, record, group)
            piece = self._piece(detail, slot, group, observations, snapshot, profile, context)
            piece = replace(piece, notes=(*notes, *piece.notes))
            pieces.append(piece)
            current_slots.append(
                replace(
                    slot,
                    score=piece.score,
                    score_profile_id=piece.profile_id,
                    score_profile_version=piece.profile_version,
                    score_observation_id=piece.observation_id,
                )
            )
        pending: list[EquippedEchoScore] = []
        for group in owned:
            if group.slot_index is not None:
                continue
            record = echoes.get(group.representative_echo_id)
            piece = self._piece(
                detail,
                _slot_snapshot(0, record, group),
                group,
                observations,
                snapshot,
                profile,
                context,
            )
            pending.append(
                replace(piece, slot_index=None, set_is_valid=None, main_stat_is_valid=None)
            )
        scores = [piece.score for piece in pieces if piece.score is not None]
        complete = len(scores) == 5
        total = round(sum(scores), 2)
        partial_average = None if not scores else round(sum(scores) / len(scores), 2)
        completion_goal = deepcopy(detail.cultivation_goal)
        completion_goal["echo_slot_confirmations"] = {
            str(p.slot_index): {
                "echo_id": p.echo_id,
                "set": p.set_is_valid,
                "main_stat": p.main_stat_is_valid,
            }
            for p in pieces
        }
        completion = evaluate_character_detail(
            replace(detail, echo_slots=tuple(current_slots), cultivation_goal=completion_goal),
            passing_score=70.0 if snapshot is None else snapshot.thresholds.passing,
            scoring_profile_id=None if snapshot is None else snapshot.id,
            scoring_profile_version=None if snapshot is None else snapshot.version,
        ).completion
        unknown_codes = {
            BlockerCode.WEAPON_MISSING,
            BlockerCode.SKILLS_MISSING,
            BlockerCode.SMALL_NODE_UNKNOWN,
            BlockerCode.ECHO_SET_UNKNOWN,
            BlockerCode.ECHO_MAIN_STAT_UNKNOWN,
            BlockerCode.ECHO_SCORE_MISSING,
        }
        categories = tuple(
            CultivationCategoryProgress(
                key=str(section.section),
                done=sum(component.at_goal for component in section.components),
                target=len(section.components),
                percent=section.percent,
                at_goal=section.at_goal,
                status=(
                    "complete"
                    if section.at_goal
                    else "unknown"
                    if any(
                        blocker.section == section.section and blocker.code in unknown_codes
                        for blocker in completion.blockers
                    )
                    else "incomplete"
                ),
            )
            for section in completion.sections
        )
        guide = guide_for_character(detail.summary.name)
        set_counts = Counter(
            guide.canonical_set_name(p.set_name) if guide is not None else p.set_name
            for p in pieces
            if p.echo_id is not None and p.set_name
        )
        mixed_set_plan = guide is not None and any(
            len(combination) > 1 for combination in guide.set_combinations
        )
        set_summary = " · ".join(
            f"{name} {count}件" if mixed_set_plan else f"{name} {count}/5"
            for name, count in set_counts.items()
        )
        custom_set_plan = bool(detail.cultivation_goal.get("echo_set_targets")) or all(
            _set_validity(s, detail.cultivation_goal) is True for s in current_slots
        )
        if (
            guide is not None
            and (guide.allowed_sets or guide.set_combinations)
            and not custom_set_plan
        ):
            full_set = all(p.echo_id is not None for p in pieces) and guide.sets_complete(
                set_counts
            )
            if not full_set:
                missing_label = (
                    f"尚未湊齊參考混搭: {guide.set_requirement_label}"
                    if mixed_set_plan
                    else "尚未湊齊參考五件套"
                )
                set_summary = " · ".join(filter(None, (set_summary, missing_label)))
            categories = tuple(
                replace(
                    c,
                    done=c.done + int(full_set),
                    target=c.target + 1,
                    percent=round(100 * (c.done + int(full_set)) / (c.target + 1), 2),
                    at_goal=c.at_goal and full_set,
                    status=c.status if full_set else "incomplete",
                )
                if c.key == "echoes"
                else c
                for c in categories
            )
        return CharacterEquipmentOverview(
            character_id=detail.summary.id,
            character_name=detail.summary.name,
            slots=tuple(pieces),
            equipped_count=sum(piece.echo_id is not None for piece in pieces),
            scored_count=len(scores),
            average_score=partial_average if complete else None,
            total_score=total if complete else None,
            partial_average_score=partial_average if not complete else None,
            lowest_score=min(scores) if scores else None,
            pending_echo_count=len(pending),
            categories=categories,
            pending_echoes=tuple(pending),
            profile_name=None if snapshot is None else snapshot.name,
            scoring_notes=notes,
            template_kind=None if snapshot is None else snapshot.template_kind,
            profile_is_system=False if snapshot is None else snapshot.is_system_template,
            element=detail.summary.element or ("" if guide is None else guide.element),
            role=(
                (detail.summary.role or "")
                if detail.summary.role not in {None, "", "待設定"}
                else ""
                if guide is None
                else guide.role
            ),
            set_summary=set_summary,
            weight_summary=""
            if profile is None
            else " · ".join(
                f"{STAT_LABELS[w.stat]} {w.weight:g}"
                for w in sorted(profile.weights, key=lambda w: -w.weight)
                if w.weight > 0
            ),
        )

    def _piece(
        self,
        detail: CharacterDetail,
        slot: EchoSlotSnapshot,
        group: EchoDuplicateGroup | None,
        observations: dict[str, EchoObservationRecord],
        snapshot: ScoringProfileSnapshot | None,
        profile: CharacterScoringProfile | None,
        context: ScoringContext,
    ) -> EquippedEchoScore:
        result = self._score_piece(detail, slot, group, observations, snapshot, profile, context)
        observation = observations.get(slot.echo_id or "")
        payload = {} if observation is None else observation.effective_payload_json
        substats = tuple(payload.get("sub_stats") or slot.sub_stats)
        mains = tuple(payload.get("main_stats") or slot.main_stats)
        guide = guide_for_character(detail.summary.name)
        set_fit, main_fit, set_reason, main_reason = guide_fit(
            guide,
            cost=slot.cost,
            set_name=slot.set_name,
            main_stats=mains,
        )
        if result.set_is_valid is not None:
            set_fit, set_reason = result.set_is_valid, "依自訂配裝判定"
        if result.main_stat_is_valid is not None:
            main_fit, main_reason = result.main_stat_is_valid, "依自訂配裝判定"
        passing = 70 if profile is None else profile.thresholds.passing
        return replace(
            result,
            main_stats=mains,
            sub_stats=substats,
            stat_details=explain_stats(
                substats, result.breakdown, profile, result.score is not None
            ),
            set_is_valid=set_fit,
            main_stat_is_valid=main_fit,
            set_fit_reason=set_reason,
            main_stat_fit_reason=main_reason,
            is_passing=(
                False
                if set_fit is False
                or main_fit is False
                or (result.score is not None and result.score < passing)
                else True
                if result.score is not None and set_fit is True and main_fit is True
                else None
            ),
            training_recommendation=training_advice(
                echo_id=result.echo_id,
                level=result.level,
                score=result.score,
                passing=passing,
                set_fit=set_fit,
                main_fit=main_fit,
                cost=result.cost,
                guide=guide,
                profile=profile,
            ),
        )

    def _score_piece(
        self,
        detail: CharacterDetail,
        slot: EchoSlotSnapshot,
        group: EchoDuplicateGroup | None,
        observations: dict[str, EchoObservationRecord],
        snapshot: ScoringProfileSnapshot | None,
        profile: CharacterScoringProfile | None,
        context: ScoringContext,
    ) -> EquippedEchoScore:
        # Keep the selected physical record's effective/manual evidence authoritative.
        observation = observations.get(slot.echo_id or "")
        result = EquippedEchoScore(
            echo_id=slot.echo_id,
            character_id=detail.summary.id,
            character_name=detail.summary.name,
            slot_index=slot.slot_index,
            name=slot.name,
            set_name=slot.set_name,
            level=slot.level,
            cost=slot.cost,
            score=None,
            opened_slot_quality_score=None,
            profile_id=None if snapshot is None else snapshot.id,
            profile_version=None if snapshot is None else snapshot.version,
            observation_id=None if observation is None else observation.id,
            payload_version=None if observation is None else observation.payload_version,
            status="missing" if slot.echo_id is None else "unscored",
            notes=(),
            set_is_valid=_set_validity(slot, detail.cultivation_goal),
            main_stat_is_valid=_main_stat_validity(slot, detail.cultivation_goal),
            rescan_count=0 if group is None else group.rescan_count,
            main_stats=slot.main_stats,
        )
        if slot.echo_id is None:
            return result
        if profile is None:
            return replace(result, status="profile_unavailable", notes=("評分設定檔或版本不可用",))
        if observation is None:
            return replace(result, status="missing_observation", notes=("缺少可追溯的詞條觀察",))
        payload = observation.effective_payload_json
        level = payload.get("level")
        if type(level) is not int or not 0 <= level <= 25:
            return replace(result, status="incomplete", notes=("聲骸等級尚未確認",))
        if level != slot.level:
            return replace(result, status="incomplete", notes=("目前聲骸與最新觀察的等級不一致",))
        completeness = _substat_completeness_note(payload, level)
        if completeness is not None:
            return replace(
                result,
                status="incomplete",
                notes=(f"+{level} 應有 {level // 5} 個已開啟副詞條, 目前資料不完整",),
            )
        if level < 5:
            return replace(result, status="unrolled", notes=("尚未開啟副詞條",))
        breakdown = self._scorer.score(
            profile,
            observed_stats_from_payload(payload, requires_review=observation.requires_review),
            context,
        )
        breakdown = hold_reviewed_score(
            breakdown, payload, requires_review=observation.requires_review
        )
        reliable = breakdown.score is not None and not breakdown.requires_manual_confirmation
        return replace(
            result,
            score=breakdown.score if reliable else None,
            opened_slot_quality_score=breakdown.opened_slot_quality_score if reliable else None,
            status="scored" if reliable else "needs_review",
            breakdown=breakdown,
            is_passing=(
                False
                if result.set_is_valid is False
                or result.main_stat_is_valid is False
                or (
                    reliable
                    and breakdown.score is not None
                    and breakdown.score < profile.thresholds.passing
                )
                else True
                if reliable and result.set_is_valid is True and result.main_stat_is_valid is True
                else None
            ),
            notes=tuple(
                dict.fromkeys(
                    (
                        *breakdown.notes,
                        *breakdown.unknown_stats,
                        *breakdown.low_confidence_stats,
                        *breakdown.invalid_rolls,
                        *breakdown.missing_context,
                    )
                )
            ),
        )


def _slot_snapshot(
    slot_index: int, record: EchoRecord | None, group: EchoDuplicateGroup | None
) -> EchoSlotSnapshot:
    return EchoSlotSnapshot(
        slot_index=slot_index,
        echo_id=None if record is None else record.id,
        name=None if record is None else record.name,
        set_name=None if group is None else group.set_name,
        cost=None if record is None else record.cost,
        level=None if record is None else record.level,
        score=None,
        final_status=None if record is None else record.final_status,
        main_stats=() if record is None else tuple(record.main_stats_json),
        sub_stats=() if record is None else tuple(record.sub_stats_json),
    )


def _latest_observations(session: Session, data_set_id: str) -> dict[str, EchoObservationRecord]:
    # Rank only identifiers: historical JSON payloads remain in SQLite. The
    # descending triple exactly matches the previous ascending/last-wins order.
    ranked = (
        select(
            EchoSessionRecord.echo_id.label("echo_id"),
            EchoObservationRecord.id.label("observation_id"),
            func.row_number()
            .over(
                partition_by=EchoSessionRecord.echo_id,
                order_by=(
                    EchoObservationRecord.observed_at.desc(),
                    EchoObservationRecord.sequence_no.desc(),
                    EchoObservationRecord.id.desc(),
                ),
            )
            .label("position"),
        )
        .join(EchoSessionRecord, EchoObservationRecord.echo_session_id == EchoSessionRecord.id)
        .join(CaptureSessionRecord, EchoSessionRecord.capture_session_id == CaptureSessionRecord.id)
        .join(EchoRecord, EchoSessionRecord.echo_id == EchoRecord.id)
        .where(
            EchoRecord.data_set_id == data_set_id,
            CaptureSessionRecord.data_set_id == data_set_id,
        )
        .subquery()
    )
    # Both joins require a real echo/observation; ranking returns one pair per
    # echo. Explicit tuple typing works with SQLAlchemy 2.0 and 2.1 alike.
    rows = cast(
        Iterable[tuple[str, EchoObservationRecord]],
        session.execute(
            select(ranked.c.echo_id, EchoObservationRecord)
            .join(EchoObservationRecord, EchoObservationRecord.id == ranked.c.observation_id)
            .where(ranked.c.position == 1)
        ),
    )
    return {echo_id: observation for echo_id, observation in rows}


__all__ = [
    "CharacterEquipmentOverview",
    "CultivationCategoryProgress",
    "EquipmentOverview",
    "EquipmentOverviewService",
    "EquippedEchoScore",
]
