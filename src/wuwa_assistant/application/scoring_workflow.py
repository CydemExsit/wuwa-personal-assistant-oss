"""Application boundary for reproducible scoring and persisted upgrade decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wuwa_assistant.scoring import (
    CharacterScoringProfile,
    ConditionalWeight,
    CritBalanceRule,
    DecisionInputs,
    EchoScoreBreakdown,
    EchoScoringEngine,
    ObservedStat,
    ProjectionSummary,
    ScoreThresholds,
    ScoringContext,
    StatKey,
    StatWeight,
    ThresholdRule,
    UpgradeDecision,
    UpgradeDecisionEngine,
)
from wuwa_assistant.scoring.character_guides import apply_guide_context
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    CharacterRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    ScoringProfileRecord,
    UpgradeDecisionRecord,
)

from .character_scoring import character_context, character_profile
from .observation_review import confirmed_fields, hold_reviewed_score, scoring_stat_confidence
from .scoring_profiles import ScoringProfileService, ScoringProfileSnapshot

_SUPPORTED_LEVELS = frozenset(range(26))
_DECISION_LEVELS = frozenset(range(5, 26))
_TOTAL_SUBSTAT_SLOTS = 5


@dataclass(frozen=True, slots=True)
class ScoringEvaluation:
    profile_id: str
    profile_name: str
    breakdown: EchoScoreBreakdown
    projection: ProjectionSummary
    decision: UpgradeDecision
    persisted_decision_id: str | None = None
    character_label: str | None = None


@dataclass(frozen=True, slots=True)
class _PersistenceGuard:
    data_set_id: str
    capture_session_id: str
    echo_session_id: str
    echo_id: str
    observation_id: str
    observation_payload_version: int
    observation_requires_review: bool
    effective_observation: dict[str, Any]
    profile_id: str
    profile_version: int
    character_state: dict[str, Any] | None = None


class ScoringWorkflowService:
    """Evaluate one effective observation with a stored, versioned profile."""

    def __init__(
        self,
        session_factory: SessionFactory,
        profile_service: ScoringProfileService,
        *,
        scoring_engine: EchoScoringEngine | None = None,
        decision_engine: UpgradeDecisionEngine | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._profiles = profile_service
        self._scorer = scoring_engine or EchoScoringEngine()
        self._decisions = decision_engine or UpgradeDecisionEngine()

    @property
    def minimum_confidence(self) -> float:
        return self._scorer.minimum_confidence

    def evaluate(
        self,
        effective_observation: Mapping[str, object],
        *,
        data_set_id: str,
        profile_id: str | None = None,
        echo_session_id: str | None = None,
        observation_id: str | None = None,
        requires_review: bool = False,
        context: ScoringContext | None = None,
        decision_inputs: DecisionInputs | None = None,
        persist: bool = True,
        character_mode: bool = False,
    ) -> ScoringEvaluation:
        safe_observation = _json_object(effective_observation, "effective observation")
        snapshot = (
            self._profiles.get_profile(profile_id, data_set_id=data_set_id)
            if profile_id is not None
            else self._profiles.get_default(data_set_id=data_set_id)
        )
        character_label = None
        character_state = None
        if character_mode and profile_id is None:
            with self._session_factory() as session:
                chain = (
                    None
                    if echo_session_id is None
                    else session.get(EchoSessionRecord, echo_session_id)
                )
                capture = (
                    None
                    if chain is None
                    else session.get(CaptureSessionRecord, chain.capture_session_id)
                )
                echo = (
                    None
                    if chain is None or chain.echo_id is None
                    else session.get(EchoRecord, chain.echo_id)
                )
                owner_id = None if echo is None else echo.character_id
                if owner_id is None and capture is not None:
                    owner_id = capture.assigned_character_id
                owner = None if owner_id is None else session.get(CharacterRecord, owner_id)
                if (
                    capture is None
                    or capture.data_set_id != data_set_id
                    or owner is None
                    or owner.data_set_id != data_set_id
                    or owner.archived_at is not None
                ):
                    raise LookupError("尚未確認聲骸所屬角色, 請先選擇角色或明確指定評分設定檔")
                character_label = owner.name
                if echo is not None:
                    character_state = _character_scoring_state(session, echo.id, capture.id)
                snapshot = character_profile(
                    self._profiles,
                    data_set_id=data_set_id,
                    name=owner.name,
                    explicit_profile_id=owner.scoring_profile_id,
                )
                context = context or character_context(owner.cultivation_goal_json)
        if snapshot is None:
            raise LookupError("尚無已查核的角色評分設定; 請於角色頁指定自訂設定檔")
        if not self._scorer.supports_version(snapshot.roll_data_version):
            raise ValueError(
                "scoring profile roll data version does not match the bundled roll data: "
                f"{snapshot.roll_data_version!r} != {self._scorer.assumptions_version!r}"
            )
        if persist and (echo_session_id is None or observation_id is None):
            raise ValueError(
                "echo_session_id and observation_id are required for persistence; "
                "use persist=False for a preview"
            )
        guard = None
        if persist and echo_session_id is not None and observation_id is not None:
            guard = self._load_persistence_guard(
                data_set_id=data_set_id,
                echo_session_id=echo_session_id,
                observation_id=observation_id,
                profile=snapshot,
                effective_observation=safe_observation,
            )
            guard = replace(guard, character_state=character_state)
        profile = profile_from_snapshot(snapshot)
        review_required = requires_review or (
            guard.observation_requires_review if guard is not None else False
        )
        scoring_context = context or ScoringContext()
        guide_notes: tuple[str, ...] = ()
        if snapshot.is_system_template:
            profile, guide_notes = apply_guide_context(profile, scoring_context)
        context_json = _scoring_context_json(scoring_context)
        level = _optional_int(safe_observation.get("level"))
        inputs = decision_inputs or DecisionInputs(level=level if level is not None else -1)
        expected_input_level = level if level is not None else -1
        if inputs.level != expected_input_level:
            raise ValueError("decision input level must match the effective observation level")

        completeness_note = _substat_completeness_note(safe_observation, level)
        if completeness_note is not None:
            breakdown = _unscored_manual_breakdown(profile, completeness_note)
            projection = _manual_projection(
                snapshot.roll_data_version or self._scorer.assumptions_version,
                remaining_slots=_remaining_slots_for_level(level),
                notes=(
                    completeness_note,
                    "unobserved unlocked substats are not treated as future rolls",
                ),
            )
        else:
            stats = observed_stats_from_payload(
                safe_observation,
                requires_review=review_required,
            )
            breakdown = self._scorer.score(profile, stats, scoring_context)
            breakdown = hold_reviewed_score(
                breakdown, safe_observation, requires_review=review_required
            )
            if review_required:
                projection = _manual_projection(
                    snapshot.roll_data_version or self._scorer.assumptions_version,
                    remaining_slots=_remaining_slots_for_level(level),
                    notes=breakdown.notes,
                )
            elif level in _DECISION_LEVELS:
                if inputs.existing_gear_score is None:
                    projection = self._scorer.enumerate_remaining(profile, stats, scoring_context)
                else:
                    projection = self._scorer.enumerate_remaining(
                        profile,
                        stats,
                        scoring_context,
                        improvement_target=inputs.existing_gear_score + inputs.replacement_margin,
                    )
            else:
                projection = _manual_projection(
                    snapshot.roll_data_version or self._scorer.assumptions_version,
                    remaining_slots=max(0, _TOTAL_SUBSTAT_SLOTS - len(stats)),
                    notes=(
                        "no tuned substats before +5; remaining-roll enumeration skipped"
                        if level in _SUPPORTED_LEVELS
                        else "unsupported upgrade level; remaining-roll enumeration skipped",
                    ),
                )
        if guide_notes:
            breakdown = replace(breakdown, notes=(*guide_notes, *breakdown.notes))
        inputs_json = _decision_inputs_json(inputs)
        decision = self._decisions.decide(profile, breakdown, projection, inputs)
        decision_id = None
        if guard is not None:
            decision_id = self._persist_decision(
                guard,
                safe_observation,
                breakdown,
                projection,
                decision,
                decision_inputs_json=inputs_json,
                scoring_context_json=context_json,
            )
        return ScoringEvaluation(
            profile_id=snapshot.id,
            profile_name=snapshot.name,
            breakdown=breakdown,
            projection=projection,
            decision=decision,
            persisted_decision_id=decision_id,
            character_label=character_label,
        )

    def _load_persistence_guard(
        self,
        *,
        data_set_id: str,
        echo_session_id: str,
        observation_id: str,
        profile: ScoringProfileSnapshot,
        effective_observation: dict[str, Any],
    ) -> _PersistenceGuard:
        with self._session_factory() as session:
            return _validate_persistence_chain(
                session,
                data_set_id=data_set_id,
                echo_session_id=echo_session_id,
                observation_id=observation_id,
                profile_id=profile.id,
                profile_version=profile.version,
                effective_observation=effective_observation,
            )

    def _persist_decision(
        self,
        guard: _PersistenceGuard,
        effective_observation: dict[str, Any],
        breakdown: EchoScoreBreakdown,
        projection: ProjectionSummary,
        decision: UpgradeDecision,
        *,
        decision_inputs_json: dict[str, Any],
        scoring_context_json: dict[str, Any],
    ) -> str:
        breakdown_json = breakdown.to_dict()
        probabilities = dict(decision.probabilities)
        fingerprint = _evaluation_fingerprint(
            guard,
            effective_observation,
            breakdown_json,
            projection.to_dict(),
            decision.to_dict(),
            decision_inputs_json,
            scoring_context_json,
        )
        try:
            with self._session_factory.begin() as session:
                current = _validate_persistence_chain(
                    session,
                    data_set_id=guard.data_set_id,
                    echo_session_id=guard.echo_session_id,
                    observation_id=guard.observation_id,
                    profile_id=guard.profile_id,
                    profile_version=guard.profile_version,
                    effective_observation=effective_observation,
                )
                if guard.character_state is not None:
                    current = replace(
                        current,
                        character_state=_character_scoring_state(
                            session,
                            guard.echo_id,
                            guard.capture_session_id,
                        ),
                    )
                if current != guard:
                    raise RuntimeError("scoring inputs changed before persistence")
                existing = session.scalar(
                    select(UpgradeDecisionRecord).where(
                        UpgradeDecisionRecord.evaluation_fingerprint == fingerprint
                    )
                )
                _update_confirmed_echo_score(
                    session,
                    guard=current,
                    breakdown=breakdown,
                    decision=decision,
                )
                if existing is not None:
                    return existing.id
                record = UpgradeDecisionRecord(
                    echo_session_id=guard.echo_session_id,
                    observation_id=guard.observation_id,
                    scoring_profile_id=guard.profile_id,
                    evaluation_fingerprint=fingerprint,
                    profile_version=guard.profile_version,
                    observation_payload_version=guard.observation_payload_version,
                    requires_manual_confirmation=decision.requires_manual_confirmation,
                    decision_inputs_json=deepcopy(decision_inputs_json),
                    scoring_context_json=deepcopy(scoring_context_json),
                    decision=decision.action.value,
                    current_score=decision.current_score,
                    predicted_min_score=decision.predicted_min_score,
                    predicted_max_score=decision.predicted_max_score,
                    probabilities_json=probabilities,
                    breakdown_json=breakdown_json,
                    assumptions_version=decision.assumptions_version,
                    reason=decision.reason,
                )
                session.add(record)
                session.flush()
                return record.id
        except IntegrityError:
            with self._session_factory() as session:
                existing = session.scalar(
                    select(UpgradeDecisionRecord).where(
                        UpgradeDecisionRecord.evaluation_fingerprint == fingerprint
                    )
                )
                if existing is not None:
                    return existing.id
            raise


def _character_scoring_state(session: Session, echo_id: str, capture_id: str) -> dict[str, Any]:
    echo = session.get(EchoRecord, echo_id)
    capture = session.get(CaptureSessionRecord, capture_id)
    if echo is None or capture is None:
        return {}
    owner_id = echo.character_id or capture.assigned_character_id
    owner = None if owner_id is None else session.get(CharacterRecord, owner_id)
    if owner is None:
        return {}
    return {
        "owner_id": owner.id,
        "owner_name": owner.name,
        "owner_profile_id": owner.scoring_profile_id,
        "goal": deepcopy(owner.cultivation_goal_json),
        "active": owner.archived_at is None,
        "echo_owner_id": echo.character_id,
        "capture_character_id": capture.assigned_character_id,
    }


def _validate_persistence_chain(
    session: Session,
    *,
    data_set_id: str,
    echo_session_id: str,
    observation_id: str,
    profile_id: str,
    profile_version: int,
    effective_observation: dict[str, Any],
) -> _PersistenceGuard:
    echo_session = session.get(EchoSessionRecord, echo_session_id)
    if echo_session is None:
        raise LookupError(f"echo session not found: {echo_session_id}")
    capture = session.get(CaptureSessionRecord, echo_session.capture_session_id)
    if capture is None:
        raise LookupError(f"capture session not found: {echo_session.capture_session_id}")
    echo = session.get(EchoRecord, echo_session.echo_id)
    if echo is None:
        raise LookupError(f"echo not found for session: {echo_session_id}")
    observation = session.get(EchoObservationRecord, observation_id)
    if observation is None:
        raise LookupError(f"echo observation not found: {observation_id}")
    profile = session.get(ScoringProfileRecord, profile_id)
    if profile is None:
        raise LookupError(f"scoring profile not found: {profile_id}")

    if observation.echo_session_id != echo_session.id:
        raise ValueError("observation does not belong to the echo session")
    if capture.data_set_id != data_set_id or echo.data_set_id != data_set_id:
        raise ValueError("capture session and echo must belong to the scoring data set")
    if not (
        profile.data_set_id == data_set_id
        or (profile.data_set_id is None and profile.is_system_template)
    ):
        raise ValueError("scoring profile does not belong to the scoring data set")
    if capture.scoring_profile_id is not None and capture.scoring_profile_id != profile.id:
        raise ValueError("scoring profile does not match the capture session")
    if profile.version != profile_version:
        raise RuntimeError("scoring profile changed before persistence")

    stored_payload = _json_object(
        observation.effective_payload_json,
        "stored effective observation",
    )
    if stored_payload != effective_observation:
        raise RuntimeError("effective observation changed before persistence")
    return _PersistenceGuard(
        data_set_id=data_set_id,
        capture_session_id=capture.id,
        echo_session_id=echo_session.id,
        echo_id=echo.id,
        observation_id=observation.id,
        observation_payload_version=observation.payload_version,
        observation_requires_review=observation.requires_review,
        effective_observation=stored_payload,
        profile_id=profile.id,
        profile_version=profile.version,
    )


def _update_confirmed_echo_score(
    session: Session,
    *,
    guard: _PersistenceGuard,
    breakdown: EchoScoreBreakdown,
    decision: UpgradeDecision,
) -> None:
    if (
        breakdown.score is None
        or breakdown.requires_manual_confirmation
        or decision.requires_manual_confirmation
    ):
        return
    observation = session.get(EchoObservationRecord, guard.observation_id)
    echo = session.get(EchoRecord, guard.echo_id)
    if observation is None or echo is None:
        raise RuntimeError("confirmed score records disappeared during persistence")
    latest_sequence = session.scalar(
        select(func.max(EchoObservationRecord.sequence_no)).where(
            EchoObservationRecord.echo_session_id == guard.echo_session_id
        )
    )
    if latest_sequence != observation.sequence_no:
        return
    echo.score = breakdown.score
    echo.score_profile_id = guard.profile_id
    echo.score_profile_version = guard.profile_version
    echo.score_observation_id = guard.observation_id


def _decision_inputs_json(inputs: DecisionInputs) -> dict[str, Any]:
    return _json_object(
        {
            "level": inputs.level,
            "existing_gear_score": inputs.existing_gear_score,
            "character_already_qualified": inputs.character_already_qualified,
            "breadth_first_progression": inputs.breadth_first_progression,
            "embryo_supply": inputs.embryo_supply.value,
            "same_type_candidates": inputs.same_type_candidates,
            "replacement_margin": inputs.replacement_margin,
        },
        "decision inputs",
    )


def _scoring_context_json(context: ScoringContext) -> dict[str, Any]:
    return _json_object(
        {
            "current_totals": {
                str(key): value
                for key, value in sorted(
                    context.current_totals.items(),
                    key=lambda item: str(item[0]),
                )
            },
            "conditions": {str(key): value for key, value in sorted(context.conditions.items())},
            "panel_crit_rate": context.panel_crit_rate,
            "panel_crit_damage": context.panel_crit_damage,
        },
        "scoring context",
    )


def _evaluation_fingerprint(
    guard: _PersistenceGuard,
    effective_observation: dict[str, Any],
    breakdown_json: dict[str, Any],
    projection_json: dict[str, Any],
    decision_json: dict[str, Any],
    decision_inputs_json: dict[str, Any],
    scoring_context_json: dict[str, Any],
) -> str:
    payload = {
        "data_set_id": guard.data_set_id,
        "echo_session_id": guard.echo_session_id,
        "observation_id": guard.observation_id,
        "observation_payload_version": guard.observation_payload_version,
        "effective_observation": effective_observation,
        "profile_id": guard.profile_id,
        "profile_version": guard.profile_version,
        "breakdown": breakdown_json,
        "projection": projection_json,
        "decision": decision_json,
        "decision_inputs": decision_inputs_json,
        "scoring_context": scoring_context_json,
    }
    if guard.character_state is not None:
        payload["character_reference"] = guard.character_state
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_object(value: object, label: str) -> dict[str, Any]:
    try:
        safe = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON data") from exc
    if not isinstance(safe, dict):
        raise ValueError(f"{label} must be a JSON object")
    return safe


def profile_from_snapshot(snapshot: ScoringProfileSnapshot) -> CharacterScoringProfile:
    """Convert persisted base/conditional rows into the pure scoring model."""

    base_rows = tuple(item for item in snapshot.weights if item.condition_key == "always")
    base_by_stat = {item.stat_name: item for item in base_rows}
    weights = tuple(
        StatWeight(
            stat=_stat_key(item.stat_name),
            weight=item.weight,
            cap_value=item.cap_value,
        )
        for item in base_rows
    )
    conditional: list[ConditionalWeight] = []
    for item in snapshot.weights:
        if item.condition_key == "always":
            continue
        base = base_by_stat.get(item.stat_name)
        if base is None or base.weight <= 0:
            raise ValueError(f"conditional stat lacks a positive base weight: {item.stat_name}")
        condition = item.condition or {}
        expected = condition.get("value", True)
        if not isinstance(expected, (str, int, float, bool)):
            raise ValueError(f"conditional expected value is invalid: {item.stat_name}")
        multiplier = item.weight / base.weight
        conditional.append(
            ConditionalWeight(
                stat=_stat_key(item.stat_name),
                condition_key=item.condition_key,
                expected_value=expected,
                multiplier=multiplier,
            )
        )
    threshold_rules = tuple(_threshold_rule(item) for item in snapshot.threshold_rules)
    crit_rule = _crit_rule(snapshot.crit_balance_rule)
    return CharacterScoringProfile(
        profile_id=snapshot.id,
        name=snapshot.name,
        template_kind=snapshot.template_kind,
        version=snapshot.version,
        description=snapshot.description or "",
        thresholds=ScoreThresholds(
            usable=snapshot.thresholds.usable,
            passing=snapshot.thresholds.passing,
            perfect=snapshot.thresholds.perfect,
            exceptional=snapshot.thresholds.exceptional,
        ),
        weights=weights,
        conditional_weights=tuple(conditional),
        threshold_rules=threshold_rules,
        crit_balance_rule=crit_rule,
        roll_data_version=snapshot.roll_data_version or "",
        is_default=snapshot.is_default,
    )


def _substat_completeness_note(
    payload: Mapping[str, object],
    level: int | None,
) -> str | None:
    """Reject partial unlocked-slot observations before score/projection work.

    A missing OCR row at +25 is already unlocked; it is not another possible
    future roll.  Keeping this invariant at the application boundary also
    protects older imports and manually assembled payloads that did not pass
    through the current parser completeness gate.
    """

    if level not in _SUPPORTED_LEVELS:
        return None
    expected = level // 5
    raw_stats = payload.get("sub_stats")
    actual = len(raw_stats) if isinstance(raw_stats, list) else None
    if actual == expected:
        return None
    actual_label = "missing list" if actual is None else str(actual)
    return (
        f"incomplete substat observation for +{level}: "
        f"expected {expected} unlocked rows, received {actual_label}"
    )


def _remaining_slots_for_level(level: int | None) -> int:
    if level not in _SUPPORTED_LEVELS:
        return 0
    return _TOTAL_SUBSTAT_SLOTS - level // 5


def _unscored_manual_breakdown(
    profile: CharacterScoringProfile,
    note: str,
) -> EchoScoreBreakdown:
    maximum = sum(
        sorted((weight.weight for weight in profile.weights), reverse=True)[:_TOTAL_SUBSTAT_SLOTS]
    )
    return EchoScoreBreakdown(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        score=None,
        band=None,
        lines=(),
        earned_weight_points=0.0,
        maximum_weight_points=round(maximum, 6),
        requires_manual_confirmation=True,
        notes=(note,),
    )


def _manual_projection(
    assumptions_version: str,
    *,
    remaining_slots: int,
    notes: tuple[str, ...],
) -> ProjectionSummary:
    return ProjectionSummary(
        assumptions_version=assumptions_version,
        remaining_slots=remaining_slots,
        outcome_count=0,
        minimum_score=None,
        maximum_score=None,
        expected_score=None,
        probabilities={},
        total_probability=0.0,
        requires_manual_confirmation=True,
        notes=notes,
    )


def observed_stats_from_payload(
    payload: Mapping[str, object],
    *,
    requires_review: bool = False,
) -> tuple[ObservedStat, ...]:
    # Review status gates the overall result separately. It must not falsify
    # every correctly recognized substat's OCR probability.
    del requires_review
    raw_stats = payload.get("sub_stats")
    if not isinstance(raw_stats, list):
        return ()
    observations: list[ObservedStat] = []
    for item in raw_stats:
        if not isinstance(item, Mapping):
            observations.append(ObservedStat(None, None, None, None))
            continue
        confidence = scoring_stat_confidence(
            item, f"/sub_stats/{len(observations)}", confirmed_fields(payload)
        )
        observations.append(
            ObservedStat(
                name=_optional_text(item.get("name")),
                value=_optional_number(item.get("value")),
                unit=_optional_text(item.get("unit")) or "",
                confidence=confidence,
                slot_index=_optional_int(item.get("slot_index")),
            )
        )
    return tuple(observations)


def _threshold_rule(value: Mapping[str, Any]) -> ThresholdRule:
    return ThresholdRule(
        rule_id=_required_text(value.get("rule_id"), "threshold rule id"),
        stat=_stat_key(_required_text(value.get("stat"), "threshold stat")),
        minimum=_optional_float(value.get("minimum")),
        maximum=_optional_float(value.get("maximum")),
        below_multiplier=_float(value.get("below_multiplier"), 1.0),
        within_multiplier=_float(value.get("within_multiplier"), 1.0),
        above_multiplier=_float(value.get("above_multiplier"), 1.0),
        required_context=bool(value.get("required_context", False)),
    )


def _crit_rule(value: Mapping[str, Any] | None) -> CritBalanceRule | None:
    if value is None:
        return None
    return CritBalanceRule(
        target_excess_damage_per_rate=_float(value.get("target_excess_damage_per_rate"), 2.0),
        tolerance=_float(value.get("tolerance"), 0.25),
        max_adjustment=_float(value.get("max_adjustment"), 0.20),
        required_context=bool(value.get("required_context", False)),
    )


def _stat_key(value: str) -> StatKey:
    try:
        return StatKey(value)
    except ValueError as exc:
        raise ValueError(f"unknown stat key in scoring profile: {value}") from exc


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _optional_int(value: object) -> int | None:
    return value if type(value) is int else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number or null")
    return float(value)


def _float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    return float(value)


__all__ = [
    "ScoringEvaluation",
    "ScoringWorkflowService",
    "observed_stats_from_payload",
    "profile_from_snapshot",
]
