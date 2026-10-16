from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from wuwa_assistant.application.echo_history import EchoHistoryService
from wuwa_assistant.application.scoring_profiles import (
    ProfileInUseError,
    ScoringProfileService,
)
from wuwa_assistant.application.scoring_workflow import ScoringWorkflowService
from wuwa_assistant.scoring import (
    DecisionInputs,
    EchoScoringEngine,
    EmbryoSupply,
    UpgradeAction,
    load_template_seed_definitions,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    DataSetRecord,
    EchoObservationRecord,
    EchoRecord,
    EchoSessionRecord,
    UpgradeDecisionRecord,
)

_CRIT_RATE = "\u66b4\u64ca"
_CRIT_DAMAGE = "\u66b4\u64ca\u50b7\u5bb3"
_ATTACK = "\u653b\u64ca"
_ENERGY_REGEN = "\u5171\u9cf4\u6548\u7387"
_BASIC_ATTACK_DAMAGE = "\u666e\u653b\u50b7\u5bb3\u52a0\u6210"
_UNKNOWN_STAT = "\u672a\u77e5\u8a5e\u689d"


@dataclass(frozen=True, slots=True)
class WorkflowFixture:
    database: Database
    history: EchoHistoryService
    profiles: ScoringProfileService
    workflow: ScoringWorkflowService
    data_set_id: str
    system_profile_id: str
    profile_id: str
    echo_session_id: str
    observation_id: str
    effective_observation: dict[str, Any]


@pytest.fixture
def scoring_workflow(tmp_path: Path) -> Iterator[WorkflowFixture]:
    database = create_database(AppPaths.from_root(tmp_path / "scoring-workflow"))
    history = EchoHistoryService(database.session_factory)
    data_set_id = history.ensure_active_primary_dataset(name="Primary")
    profiles = ScoringProfileService(database.session_factory)
    seeded = profiles.ensure_system_templates(load_template_seed_definitions())
    system_profile = next(item for item in seeded if item.template_kind == "generic_dps")
    profile = profiles.clone_template(
        system_profile.id,
        data_set_id=data_set_id,
        name="Workflow profile",
    )
    capture = history.start_capture(
        data_set_id=data_set_id,
        backend_kind="fake",
        monitor_index=1,
        sampling_hz=5.0,
        stable_wait_ms=600,
        app_version="0.5.0",
        scoring_profile_id=profile.id,
    )
    receipt = history.ingest_live_result(
        capture.id,
        _observation_payload(level=20, sub_stats=_known_stats()),
        source_sequence=1,
    )
    assert receipt.echo_session_id is not None
    assert receipt.observation_id is not None
    effective = history.get_history(receipt.echo_session_id).observations[0].effective_payload
    try:
        yield WorkflowFixture(
            database=database,
            history=history,
            profiles=profiles,
            workflow=ScoringWorkflowService(database.session_factory, profiles),
            data_set_id=data_set_id,
            system_profile_id=system_profile.id,
            profile_id=profile.id,
            echo_session_id=receipt.echo_session_id,
            observation_id=receipt.observation_id,
            effective_observation=effective,
        )
    finally:
        database.dispose()


def _stat(
    name: str,
    value: float,
    *,
    unit: str = "%",
    confidence: float = 0.98,
    slot_index: int = 1,
) -> dict[str, Any]:
    return {
        "stat_kind": "substat",
        "name": name,
        "value": value,
        "unit": unit,
        "confidence": confidence,
        "name_confidence": confidence,
        "value_confidence": confidence,
        "raw_name": name,
        "raw_value": f"{value}{unit}",
        "slot_index": slot_index,
    }


def _known_stats() -> list[dict[str, Any]]:
    return [
        _stat(_CRIT_RATE, 7.5, slot_index=1),
        _stat(_CRIT_DAMAGE, 15.0, slot_index=2),
        _stat(_ATTACK, 10.1, slot_index=3),
        _stat(_ENERGY_REGEN, 10.0, slot_index=4),
    ]


def _known_main_stats() -> list[dict[str, Any]]:
    return [
        {
            **_stat(_ATTACK, 18.0, slot_index=0),
            "stat_kind": "primary",
        },
        {
            **_stat(_ATTACK, 150, unit="", slot_index=1),
            "stat_kind": "fixed",
        },
    ]


def _observation_payload(
    *,
    level: int,
    sub_stats: list[dict[str, Any]],
    local_session_id: str = "echo-session-workflow-0001",
) -> dict[str, Any]:
    return {
        "page_type": "echo_enhancement",
        "name": "Test Echo",
        "level": level,
        "projected_level": None,
        "cost": 1,
        "main_stats": _known_main_stats(),
        "projected_main_stats": [],
        "sub_stats": sub_stats,
        "locked_substat_slots": max(0, 5 - len(sub_stats)),
        "confidence": {
            "page_type": 0.99,
            "name": 0.99,
            "level": 0.99,
        },
        "source_timestamp": "2026-08-01T12:00:00+00:00",
        "source_file": None,
        "is_discarded": False,
        "material_source_selected": None,
        "material_source_level": None,
        "raw_ocr": {},
        "debug_roi_paths": [],
        "debug_ocr_path": None,
        "semantic_fingerprint": f"{local_session_id}:{level}:{len(sub_stats)}",
        "current_session": {
            "local_session_id": local_session_id,
            "current_state": "active",
            "is_discarded": False,
            "was_consumed": False,
            "consumed_into_session_id": None,
            "latest_level": level,
        },
        "events": [],
    }


def test_evaluate_persists_explainable_decision_idempotently(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow

    first = fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
    )
    repeated = fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
    )

    assert first.breakdown.score is not None
    assert not first.breakdown.requires_manual_confirmation
    assert first.breakdown.lines
    assert first.projection.outcome_count > 0
    assert not first.projection.requires_manual_confirmation
    assert first.decision.action is not UpgradeAction.MANUAL_CONFIRMATION
    assert first.persisted_decision_id is not None
    assert repeated.persisted_decision_id == first.persisted_decision_id
    assert repeated.breakdown == first.breakdown
    assert repeated.decision == first.decision

    with fixture.database.session_factory() as session:
        records = list(session.scalars(select(UpgradeDecisionRecord)).all())
        assert len(records) == 1
        record = records[0]
        echo_session = session.get(EchoSessionRecord, fixture.echo_session_id)
        assert echo_session is not None
        echo = session.get(EchoRecord, echo_session.echo_id)
        assert echo is not None
        assert record.id == first.persisted_decision_id
        assert record.echo_session_id == fixture.echo_session_id
        assert record.observation_id == fixture.observation_id
        assert record.scoring_profile_id == fixture.profile_id
        assert record.decision == first.decision.action.value
        assert record.breakdown_json == first.breakdown.to_dict()
        assert record.assumptions_version == first.decision.assumptions_version
        assert len(record.evaluation_fingerprint) == 64
        assert record.profile_version == 1
        assert record.observation_payload_version == 1
        assert not record.requires_manual_confirmation
        assert record.decision_inputs_json["level"] == 20
        assert record.scoring_context_json["current_totals"] == {}
        assert echo.score == first.breakdown.score
        assert echo.score_profile_id == fixture.profile_id
        assert echo.score_profile_version == 1
        assert echo.score_observation_id == fixture.observation_id


def test_cross_dataset_and_wrong_session_observation_are_rejected(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow
    with fixture.database.session_factory.begin() as session:
        demo = DataSetRecord(name="Demo", kind="demo", is_active=False)
        session.add(demo)
        session.flush()
        demo_id = demo.id
    capture = fixture.history.start_capture(
        data_set_id=demo_id,
        backend_kind="fake",
        monitor_index=1,
        sampling_hz=5.0,
        stable_wait_ms=600,
        app_version="0.5.0",
        scoring_profile_id=fixture.system_profile_id,
    )
    receipt = fixture.history.ingest_live_result(
        capture.id,
        _observation_payload(level=20, sub_stats=_known_stats(), local_session_id="demo"),
        source_sequence=1,
    )
    assert receipt.echo_session_id is not None
    assert receipt.observation_id is not None
    demo_payload = (
        fixture.history.get_history(receipt.echo_session_id).observations[0].effective_payload
    )

    with pytest.raises(ValueError, match="must belong to the scoring data set"):
        fixture.workflow.evaluate(
            demo_payload,
            data_set_id=fixture.data_set_id,
            profile_id=fixture.profile_id,
            echo_session_id=receipt.echo_session_id,
            observation_id=receipt.observation_id,
        )
    with pytest.raises(ValueError, match="does not belong to the echo session"):
        fixture.workflow.evaluate(
            demo_payload,
            data_set_id=fixture.data_set_id,
            profile_id=fixture.profile_id,
            echo_session_id=fixture.echo_session_id,
            observation_id=receipt.observation_id,
        )
    with fixture.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UpgradeDecisionRecord)) == 0


def test_stale_or_unpersisted_payload_is_rejected(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow
    stale = {
        **fixture.effective_observation,
        "level": 15,
    }

    with pytest.raises(RuntimeError, match="effective observation changed"):
        fixture.workflow.evaluate(
            stale,
            data_set_id=fixture.data_set_id,
            profile_id=fixture.profile_id,
            echo_session_id=fixture.echo_session_id,
            observation_id=fixture.observation_id,
        )
    with fixture.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UpgradeDecisionRecord)) == 0


def test_persistence_requires_session_and_observation_identity(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow

    with pytest.raises(ValueError, match="required for persistence"):
        fixture.workflow.evaluate(
            fixture.effective_observation,
            data_set_id=fixture.data_set_id,
            profile_id=fixture.profile_id,
        )


def test_decision_input_level_must_match_observation(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow

    with pytest.raises(ValueError, match="must match the effective observation level"):
        fixture.workflow.evaluate(
            fixture.effective_observation,
            data_set_id=fixture.data_set_id,
            profile_id=fixture.profile_id,
            decision_inputs=DecisionInputs(level=5),
            persist=False,
        )


def test_missing_name_preserves_known_stat_points_but_holds_whole_score(scoring_workflow):
    fixture = scoring_workflow
    payload = deepcopy(fixture.effective_observation)
    payload["name"] = None
    result = fixture.workflow.evaluate(
        payload,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        requires_review=True,
        persist=False,
    )
    assert result.breakdown.score is None
    assert len(result.breakdown.lines) == 4
    assert not result.breakdown.low_confidence_stats
    assert any("聲骸名稱" in note for note in result.breakdown.notes)


def test_manual_evaluation_preserves_last_confirmed_score(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow
    confirmed = fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
    )
    manual = fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
        requires_review=True,
    )

    assert confirmed.breakdown.score is not None
    assert manual.breakdown.score is None
    assert manual.decision.action is UpgradeAction.MANUAL_CONFIRMATION
    assert manual.persisted_decision_id != confirmed.persisted_decision_id
    with fixture.database.session_factory() as session:
        echo_session = session.get(EchoSessionRecord, fixture.echo_session_id)
        assert echo_session is not None
        echo = session.get(EchoRecord, echo_session.echo_id)
        assert echo is not None
        assert echo.score == confirmed.breakdown.score
        assert echo.score_profile_id == fixture.profile_id
        assert echo.score_observation_id == fixture.observation_id
        manual_record = session.get(UpgradeDecisionRecord, manual.persisted_decision_id)
        assert manual_record is not None and manual_record.requires_manual_confirmation


def test_fingerprint_distinguishes_decision_inputs_and_delete_is_friendly(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow
    scarce = fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
        decision_inputs=DecisionInputs(level=20, embryo_supply=EmbryoSupply.SCARCE),
    )
    abundant = fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
        decision_inputs=DecisionInputs(level=20, embryo_supply=EmbryoSupply.ABUNDANT),
    )

    assert scarce.persisted_decision_id != abundant.persisted_decision_id
    with fixture.database.session_factory() as session:
        records = list(
            session.scalars(
                select(UpgradeDecisionRecord).order_by(UpgradeDecisionRecord.created_at)
            ).all()
        )
        assert len(records) == 2
        assert records[0].evaluation_fingerprint != records[1].evaluation_fingerprint
        assert {record.decision_inputs_json["embryo_supply"] for record in records} == {
            "scarce",
            "abundant",
        }
    with pytest.raises(ProfileInUseError, match="historical upgrade decisions"):
        fixture.profiles.delete_user_profile(
            fixture.profile_id,
            data_set_id=fixture.data_set_id,
        )


@pytest.mark.parametrize("level", range(5))
def test_levels_before_first_tuning_slot_skip_expensive_projection_enumeration(
    scoring_workflow: WorkflowFixture,
    monkeypatch: pytest.MonkeyPatch,
    level: int,
) -> None:
    fixture = scoring_workflow
    engine = EchoScoringEngine()

    def fail_enumeration(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("level zero must not enumerate remaining rolls")

    monkeypatch.setattr(engine, "enumerate_remaining", fail_enumeration)
    workflow = ScoringWorkflowService(
        fixture.database.session_factory,
        fixture.profiles,
        scoring_engine=engine,
    )

    result = workflow.evaluate(
        _observation_payload(level=level, sub_stats=[]),
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        persist=False,
    )

    assert result.projection.outcome_count == 0
    assert result.projection.expected_score is None
    assert result.projection.requires_manual_confirmation
    assert result.decision.action is UpgradeAction.MANUAL_CONFIRMATION


@pytest.mark.parametrize(
    ("level", "sub_stat_count", "expected_remaining"),
    [
        (0, None, 5),
        (5, 0, 4),
        (10, 3, 3),
        (15, 2, 2),
        (17, 2, 2),
        (22, 3, 1),
        (24, 5, 1),
        (25, 4, 0),
    ],
)
def test_supported_level_requires_exact_unlocked_substat_count_before_scoring(
    scoring_workflow: WorkflowFixture,
    monkeypatch: pytest.MonkeyPatch,
    level: int,
    sub_stat_count: int | None,
    expected_remaining: int,
) -> None:
    fixture = scoring_workflow
    engine = EchoScoringEngine()

    def fail_score(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("incomplete unlocked slots must not be scored")

    def fail_enumeration(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("incomplete unlocked slots must not be projected")

    monkeypatch.setattr(engine, "score", fail_score)
    monkeypatch.setattr(engine, "enumerate_remaining", fail_enumeration)
    workflow = ScoringWorkflowService(
        fixture.database.session_factory,
        fixture.profiles,
        scoring_engine=engine,
    )
    payload = _observation_payload(
        level=level,
        sub_stats=[*_known_stats(), _stat(_BASIC_ATTACK_DAMAGE, 7.9, slot_index=5)][
            : sub_stat_count or 0
        ],
    )
    if sub_stat_count is None:
        payload.pop("sub_stats")

    result = workflow.evaluate(
        payload,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        persist=False,
    )

    assert result.breakdown.score is None
    assert result.breakdown.lines == ()
    assert result.breakdown.requires_manual_confirmation
    assert "incomplete substat observation" in result.breakdown.notes[0]
    assert result.projection.remaining_slots == expected_remaining
    assert result.projection.outcome_count == 0
    assert result.projection.minimum_score is None
    assert result.projection.maximum_score is None
    assert result.projection.requires_manual_confirmation
    assert result.decision.action is UpgradeAction.MANUAL_CONFIRMATION
    assert result.decision.current_score is None
    assert result.decision.predicted_max_score is None
    assert result.decision.requires_manual_confirmation


@pytest.mark.parametrize("level", [17, 22])
def test_corrected_non_node_level_scores_and_persists_without_changing_raw_evidence(
    scoring_workflow: WorkflowFixture,
    level: int,
) -> None:
    fixture = scoring_workflow
    with fixture.database.session_factory() as session:
        tracked = session.get(EchoSessionRecord, fixture.echo_session_id)
        assert tracked is not None
        capture_id = tracked.capture_session_id
    payload = _observation_payload(
        level=level,
        sub_stats=_known_stats()[: level // 5],
        local_session_id=f"correct-level-{level}",
    )
    payload["level"] = None
    payload["confidence"]["level"] = None
    receipt = fixture.history.ingest_live_result(capture_id, payload, source_sequence=2)
    assert receipt.echo_session_id is not None and receipt.observation_id is not None
    fixture.history.correct_observation(receipt.observation_id, "/level", level, mark_reviewed=True)
    corrected = fixture.history.get_history(receipt.echo_session_id).observations[0]
    assert corrected.effective_payload["level"] == level
    assert corrected.effective_payload["main_stats"] == payload["main_stats"]
    assert corrected.raw_payload["level"] is None
    assert not corrected.requires_review

    result = fixture.workflow.evaluate(
        corrected.effective_payload,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=receipt.echo_session_id,
        observation_id=receipt.observation_id,
    )
    reference = fixture.workflow.evaluate(
        {**corrected.effective_payload, "level": level // 5 * 5},
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        persist=False,
    )
    assert result.breakdown.score is not None
    assert result.breakdown.score == reference.breakdown.score
    assert result.projection == reference.projection
    assert result.projection.remaining_slots == 5 - level // 5
    assert not result.projection.requires_manual_confirmation
    with fixture.database.session_factory() as session:
        record = session.get(UpgradeDecisionRecord, result.persisted_decision_id)
        assert record is not None
        assert record.decision_inputs_json["level"] == level
        stored = session.get(EchoObservationRecord, receipt.observation_id)
        assert stored is not None and stored.level == level


def test_incomplete_max_level_persists_manual_without_overwriting_confirmed_score(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow
    confirmed = fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
    )
    assert confirmed.breakdown.score is not None
    with fixture.database.session_factory() as session:
        echo_session = session.get(EchoSessionRecord, fixture.echo_session_id)
        assert echo_session is not None
        capture_session_id = echo_session.capture_session_id

    receipt = fixture.history.ingest_live_result(
        capture_session_id,
        _observation_payload(level=25, sub_stats=_known_stats()),
        source_sequence=2,
    )
    assert receipt.echo_session_id == fixture.echo_session_id
    assert receipt.observation_id is not None
    incomplete_payload = (
        fixture.history.get_history(fixture.echo_session_id).observations[-1].effective_payload
    )

    incomplete = fixture.workflow.evaluate(
        incomplete_payload,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=receipt.observation_id,
    )

    assert incomplete.decision.action is UpgradeAction.MANUAL_CONFIRMATION
    assert incomplete.projection.remaining_slots == 0
    assert incomplete.persisted_decision_id is not None
    with fixture.database.session_factory() as session:
        echo_session = session.get(EchoSessionRecord, fixture.echo_session_id)
        assert echo_session is not None
        echo = session.get(EchoRecord, echo_session.echo_id)
        assert echo is not None
        assert echo.score == confirmed.breakdown.score
        assert echo.score_observation_id == fixture.observation_id
        record = session.get(UpgradeDecisionRecord, incomplete.persisted_decision_id)
        assert record is not None
        assert record.requires_manual_confirmation
        assert record.profile_version == 1


def test_system_template_v2_upgrade_preserves_user_profile_and_v1_decision(
    tmp_path: Path,
) -> None:
    database = create_database(AppPaths.from_root(tmp_path / "template-upgrade"))
    history = EchoHistoryService(database.session_factory)
    profiles = ScoringProfileService(database.session_factory)
    data_set_id = history.ensure_active_primary_dataset(name="Primary")
    v1_definitions = load_template_seed_definitions()
    for definition in v1_definitions:
        definition["version"] = 1
    seeded_v1 = profiles.ensure_system_templates(v1_definitions)
    system_v1 = next(item for item in seeded_v1 if item.template_kind == "generic_dps")
    user_profile = profiles.clone_template(
        system_v1.id,
        data_set_id=data_set_id,
        name="Personal preserved profile",
    )
    user_before = user_profile.to_json_dict()
    capture = history.start_capture(
        data_set_id=data_set_id,
        backend_kind="fake",
        monitor_index=1,
        sampling_hz=5.0,
        stable_wait_ms=600,
        app_version="1.0.3",
        scoring_profile_id=system_v1.id,
    )
    receipt = history.ingest_live_result(
        capture.id,
        _observation_payload(level=20, sub_stats=_known_stats()),
        source_sequence=1,
    )
    assert receipt.echo_session_id is not None
    assert receipt.observation_id is not None
    payload = history.get_history(receipt.echo_session_id).observations[0].effective_payload
    workflow = ScoringWorkflowService(database.session_factory, profiles)
    old_evaluation = workflow.evaluate(
        payload,
        data_set_id=data_set_id,
        profile_id=system_v1.id,
        echo_session_id=receipt.echo_session_id,
        observation_id=receipt.observation_id,
    )
    assert old_evaluation.persisted_decision_id is not None

    v2_definitions = deepcopy(v1_definitions)
    for definition in v2_definitions:
        definition["version"] = 2
    generic_v2 = next(item for item in v2_definitions if item["template_kind"] == "generic_dps")
    generic_v2["description"] = "Version 2 persisted seed"
    upgraded = profiles.ensure_system_templates(v2_definitions)
    upgraded_system = next(item for item in upgraded if item.template_kind == "generic_dps")

    same_version_changed = deepcopy(v2_definitions)
    same_generic = next(
        item for item in same_version_changed if item["template_kind"] == "generic_dps"
    )
    same_generic["description"] = "must not replace an already applied version"
    repeated = profiles.ensure_system_templates(same_version_changed)
    repeated_system = next(item for item in repeated if item.template_kind == "generic_dps")

    assert upgraded_system.id == system_v1.id
    assert upgraded_system.version == 2
    assert upgraded_system.description == "Version 2 persisted seed"
    assert repeated_system.to_json_dict() == upgraded_system.to_json_dict()
    assert (
        profiles.get_profile(
            user_profile.id,
            data_set_id=data_set_id,
        ).to_json_dict()
        == user_before
    )
    v2_evaluation = workflow.evaluate(
        payload,
        data_set_id=data_set_id,
        profile_id=system_v1.id,
        echo_session_id=receipt.echo_session_id,
        observation_id=receipt.observation_id,
    )
    assert v2_evaluation.persisted_decision_id is not None
    assert v2_evaluation.persisted_decision_id != old_evaluation.persisted_decision_id
    with database.session_factory() as session:
        historical = session.get(UpgradeDecisionRecord, old_evaluation.persisted_decision_id)
        current = session.get(UpgradeDecisionRecord, v2_evaluation.persisted_decision_id)
        assert historical is not None
        assert current is not None
        assert historical.scoring_profile_id == system_v1.id
        assert historical.profile_version == 1
        assert current.scoring_profile_id == system_v1.id
        assert current.profile_version == 2
        assert session.scalar(select(func.count()).select_from(UpgradeDecisionRecord)) == 2
    database.dispose()


def test_roll_data_version_mismatch_is_rejected(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow
    stale = fixture.profiles.create_user_profile(
        data_set_id=fixture.data_set_id,
        definition={
            "name": "Stale roll data",
            "roll_data_version": "echo-rolls-stale-v0",
        },
        weights=[{"stat_name": "crit_rate", "unit": "%", "weight": 1.0}],
    )

    with pytest.raises(ValueError, match="roll data version does not match"):
        fixture.workflow.evaluate(
            fixture.effective_observation,
            data_set_id=fixture.data_set_id,
            profile_id=stale.id,
            persist=False,
        )


def test_payload_version_change_during_evaluation_aborts_persistence(
    scoring_workflow: WorkflowFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = scoring_workflow
    engine = EchoScoringEngine()
    original_score = engine.score

    def score_then_mutate(*args: Any, **kwargs: Any) -> Any:
        result = original_score(*args, **kwargs)
        with fixture.database.session_factory.begin() as session:
            observation = session.get(EchoObservationRecord, fixture.observation_id)
            assert observation is not None
            observation.payload_version += 1
        return result

    monkeypatch.setattr(engine, "score", score_then_mutate)
    workflow = ScoringWorkflowService(
        fixture.database.session_factory,
        fixture.profiles,
        scoring_engine=engine,
    )

    with pytest.raises(RuntimeError, match="scoring inputs changed before persistence"):
        workflow.evaluate(
            fixture.effective_observation,
            data_set_id=fixture.data_set_id,
            profile_id=fixture.profile_id,
            echo_session_id=fixture.echo_session_id,
            observation_id=fixture.observation_id,
        )

    with fixture.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UpgradeDecisionRecord)) == 0


def test_only_latest_confirmed_observation_updates_echo_score(
    scoring_workflow: WorkflowFixture,
) -> None:
    fixture = scoring_workflow
    with fixture.database.session_factory() as session:
        echo_session = session.get(EchoSessionRecord, fixture.echo_session_id)
        assert echo_session is not None
        capture_session_id = echo_session.capture_session_id
    newest_stats = [
        _stat(_CRIT_RATE, 10.5, slot_index=1),
        _stat(_CRIT_DAMAGE, 21.0, slot_index=2),
        _stat(_ATTACK, 11.6, slot_index=3),
        _stat(_ENERGY_REGEN, 12.4, slot_index=4),
        _stat(_BASIC_ATTACK_DAMAGE, 11.6, slot_index=5),
    ]
    receipt = fixture.history.ingest_live_result(
        capture_session_id,
        _observation_payload(level=25, sub_stats=newest_stats),
        source_sequence=2,
    )
    assert receipt.echo_session_id == fixture.echo_session_id
    assert receipt.observation_id is not None
    newest_payload = (
        fixture.history.get_history(fixture.echo_session_id).observations[-1].effective_payload
    )

    newest = fixture.workflow.evaluate(
        newest_payload,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=receipt.observation_id,
    )
    assert newest.breakdown.score is not None
    fixture.workflow.evaluate(
        fixture.effective_observation,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        echo_session_id=fixture.echo_session_id,
        observation_id=fixture.observation_id,
    )

    with fixture.database.session_factory() as session:
        echo_session = session.get(EchoSessionRecord, fixture.echo_session_id)
        assert echo_session is not None
        echo = session.get(EchoRecord, echo_session.echo_id)
        assert echo is not None
        assert echo.score == newest.breakdown.score
        assert echo.score_observation_id == receipt.observation_id


@pytest.mark.parametrize(
    ("sub_stats", "expected_unknown", "expected_low_confidence"),
    [
        ([_stat(_UNKNOWN_STAT, 7.5)], (_UNKNOWN_STAT,), ()),
        ([_stat(_CRIT_RATE, 7.5, confidence=0.40)], (), (_CRIT_RATE,)),
    ],
)
def test_uncertain_or_unknown_stat_requires_confirmation_without_guessing(
    scoring_workflow: WorkflowFixture,
    sub_stats: list[dict[str, Any]],
    expected_unknown: tuple[str, ...],
    expected_low_confidence: tuple[str, ...],
) -> None:
    fixture = scoring_workflow
    payload = _observation_payload(level=5, sub_stats=sub_stats)

    result = fixture.workflow.evaluate(
        payload,
        data_set_id=fixture.data_set_id,
        profile_id=fixture.profile_id,
        persist=False,
    )

    assert result.breakdown.score is None
    assert result.breakdown.lines == ()
    assert result.breakdown.unknown_stats == expected_unknown
    assert result.breakdown.low_confidence_stats == expected_low_confidence
    assert result.breakdown.requires_manual_confirmation
    assert result.projection.outcome_count == 0
    assert result.projection.expected_score is None
    assert result.projection.requires_manual_confirmation
    assert result.decision.action is UpgradeAction.MANUAL_CONFIRMATION
    assert result.decision.current_score is None
    assert result.decision.requires_manual_confirmation
    assert result.persisted_decision_id is None

    with fixture.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UpgradeDecisionRecord)) == 0
