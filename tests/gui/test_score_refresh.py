from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from threading import Event

import pytest
from sqlalchemy import select

from wuwa_assistant.application import (
    CharacterCultivationService,
    EchoDisposition,
    EchoHistory,
    EchoHistoryService,
    ScoringProfileService,
    ScoringWorkflowService,
)
from wuwa_assistant.application.characters import CreateCharacter
from wuwa_assistant.application.equipment_overview import EquippedEchoScore
from wuwa_assistant.application.replacement_training import (
    ReplacementComparison,
    ReplacementTarget,
    ReplacementTrainingService,
)
from wuwa_assistant.scoring import (
    DecisionInputs,
    EchoScoringEngine,
    ScoringContext,
    load_template_seed_definitions,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import UpgradeDecisionRecord
from wuwa_assistant.ui.controllers.echo_workspace import EchoWorkspacePresenter
from wuwa_assistant.ui.pages.echo_live import ChoiceOption, EchoLivePage
from wuwa_assistant.ui.pages.history import EchoHistoryPage

from .test_echo_workspace_presenter import _FakeLiveController, _recognition


@dataclass
class _Workspace:
    database: Database
    history: EchoHistoryService
    scoring: ScoringWorkflowService
    presenter: EchoWorkspacePresenter
    page: EchoLivePage
    snapshot: EchoHistory
    payload: dict

    def apply(self, snapshot: EchoHistory | None = None, *, persist: bool = False) -> None:
        self.presenter._apply_history(
            snapshot or self.snapshot,
            update_live=True,
            evaluate_score=True,
            persist_score=persist,
        )

    def score_text(self) -> str:
        model = self.page.sub_stats_table.model()
        assert model is not None
        return str(model.data(model.index(0, 3)))


@pytest.fixture
def workspace(tmp_path, qtbot):
    database = create_database(AppPaths.from_root(tmp_path / "workspace"))
    history = EchoHistoryService(database.session_factory)
    data_set_id = history.ensure_active_primary_dataset()
    profiles = ScoringProfileService(database.session_factory)
    profiles.ensure_system_templates(load_template_seed_definitions())
    profile = profiles.get_default(data_set_id=data_set_id)
    assert profile is not None
    capture = history.start_capture(
        data_set_id=data_set_id,
        backend_kind="test",
        monitor_index=1,
        sampling_hz=5,
        stable_wait_ms=600,
        app_version="test",
        scoring_profile_id=profile.id,
    )
    payload = deepcopy(_recognition().observation)
    payload["level"] = 20
    payload["locked_substat_slots"] = 1
    payload["events"] = []
    stat = payload["sub_stats"][0]
    payload["sub_stats"] = [
        {**stat, "name": name, "value": value, "slot_index": index}
        for index, (name, value) in enumerate(
            (("暴擊", 7.5), ("暴擊傷害", 15.0), ("攻擊", 7.9), ("共鳴效率", 8.4)), 1
        )
    ]
    receipt = history.ingest_live_result(capture.id, payload, source_sequence=1)
    assert receipt.echo_session_id is not None
    snapshot = history.get_history(receipt.echo_session_id)
    scoring = ScoringWorkflowService(database.session_factory, profiles)
    page = EchoLivePage()
    history_page = EchoHistoryPage()
    qtbot.addWidget(page)
    qtbot.addWidget(history_page)
    presenter = EchoWorkspacePresenter(
        live_controller=_FakeLiveController(),
        history_service=history,
        echo_page=page,
        history_page=history_page,
        scoring_workflow=scoring,
        data_set_id=data_set_id,
        app_version="test",
    )
    try:
        yield _Workspace(database, history, scoring, presenter, page, snapshot, payload)
    finally:
        presenter.shutdown()
        database.dispose()


def _changed_observation(snapshot, **changes):
    observation = snapshot.observations[-1]
    payload = deepcopy(observation.effective_payload)
    payload.update(changes)
    return replace(
        snapshot,
        observations=(replace(observation, id=observation.id + "-new", effective_payload=payload),),
    )


def test_frequent_persisted_rescans_do_not_starve_or_clear_finished_scores(
    workspace, qtbot, monkeypatch
):
    first_started, first_release = Event(), Event()
    second_started, second_release = Event(), Event()
    calls = []
    original = workspace.scoring.evaluate

    def slow_score(*args, **kwargs):
        calls.append(kwargs["observation_id"])
        result = original(*args, **kwargs)
        started, release = (
            (first_started, first_release) if len(calls) == 1 else (second_started, second_release)
        )
        started.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(workspace.scoring, "evaluate", slow_score)
    try:
        workspace.apply(persist=True)
        qtbot.waitUntil(first_started.is_set, timeout=10000)
        latest = workspace.snapshot
        for sequence in range(2, 22):
            payload = deepcopy(workspace.payload)
            payload["source_timestamp"] = f"2026-09-27T00:00:{sequence:02d}+00:00"
            payload["semantic_fingerprint"] = f"animated-{sequence}"
            for stat in payload["sub_stats"]:
                for field in ("confidence", "name_confidence", "value_confidence"):
                    stat[field] = 0.96 + sequence / 1000
            receipt = workspace.history.ingest_live_result(
                latest.capture.id, payload, source_sequence=sequence
            )
            assert receipt.observation_id is not None
            latest = workspace.history.get_history(latest.session.id)
            workspace.apply(latest, persist=True)
        assert len(calls) == 1
        first_release.set()
        qtbot.waitUntil(second_started.is_set, timeout=10000)
        qtbot.waitUntil(lambda: workspace.score_text() != "—")
        shown = workspace.score_text()
        assert calls == [workspace.snapshot.observations[-1].id, latest.observations[-1].id]
        # A fresh observation updates confidence and identity while keeping the
        # just-completed equivalent score visible during the next DB refresh.
        workspace.apply(latest, persist=True)
        assert workspace.score_text() == shown
        assert workspace.page._observation.observation_id == latest.observations[-1].id
        assert workspace.page._observation.sub_stats[0].confidence == pytest.approx(0.981)
        second_release.set()
        qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
        persisted = workspace.history.get_history(latest.session.id)
        assert len(persisted.observations) == 21
        with workspace.database.session_factory() as session:
            decisions = tuple(session.scalars(select(UpgradeDecisionRecord)))
            assert {row.observation_id for row in decisions} == {
                workspace.snapshot.observations[-1].id,
                latest.observations[-1].id,
            }
    finally:
        first_release.set()
        second_release.set()


@pytest.mark.parametrize(
    "change", ["value", "level", "session", "owner", "profile", "review", "low_confidence"]
)
def test_changed_scoring_inputs_publish_complete_result_without_clearing_previous(
    workspace, qtbot, monkeypatch, change
):
    workspace.apply()
    qtbot.waitUntil(lambda: workspace.score_text() != "—", timeout=10000)
    previous = workspace.page._observation
    previous_score = workspace.score_text()
    previous_conclusion = workspace.page.conclusion_label.text()
    previous_range = workspace.page.score_range_label.text()
    started, release = Event(), Event()
    original = workspace.scoring.evaluate

    def slow_score(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace.scoring, "evaluate", slow_score)
    snapshot = workspace.snapshot
    if change in {"value", "low_confidence"}:
        stats = deepcopy(workspace.payload["sub_stats"])
        stats[0]["value" if change == "value" else "confidence"] = 8.1 if change == "value" else 0.4
        snapshot = _changed_observation(snapshot, sub_stats=stats)
    elif change == "level":
        snapshot = _changed_observation(snapshot, level=22)
    elif change == "session":
        snapshot = replace(snapshot, session=replace(snapshot.session, id="other-session"))
    elif change == "owner":
        snapshot = replace(snapshot, session=replace(snapshot.session, character_id="other-owner"))
    elif change == "profile":
        profiles = ScoringProfileService(workspace.database.session_factory)
        other = next(
            p
            for p in profiles.list_profiles(data_set_id=snapshot.capture.data_set_id)
            if p.id != snapshot.capture.scoring_profile_id
        )
        snapshot = replace(snapshot, capture=replace(snapshot.capture, scoring_profile_id=other.id))
    elif change == "review":
        snapshot = replace(
            snapshot,
            observations=(replace(snapshot.observations[-1], requires_review=True),),
        )
    try:
        workspace.apply(snapshot)
        assert workspace.page._observation == previous
        assert workspace.score_text() == previous_score
        qtbot.waitUntil(started.is_set)
        assert workspace.score_text() == previous_score
        assert workspace.page.conclusion_label.text() == previous_conclusion
        assert workspace.page.score_range_label.text() == previous_range
        assert not workspace.page.analysis_refresh_label.isHidden()
        assert "上次完成" in workspace.page.analysis_refresh_label.text()
        assert not workspace.page.correction_button.isEnabled()
        assert not workspace.page.keep_button.isEnabled()
    finally:
        release.set()
        qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
    assert workspace.page._observation.observation_id == snapshot.observations[-1].id
    assert workspace.page._observation.level == snapshot.observations[-1].effective_payload["level"]
    assert workspace.page.analysis_refresh_label.isHidden()
    assert workspace.page.correction_button.isEnabled()
    if change == "low_confidence":
        assert workspace.score_text() == "—"
        assert workspace.page._observation.sub_stats[0].confidence == 0.4


def test_context_change_rejects_running_result_and_reads_fresh_again(workspace, qtbot, monkeypatch):
    first_started, first_release = Event(), Event()
    second_started, second_release = Event(), Event()
    original = workspace.scoring.evaluate
    calls = []

    def slow_score(*args, **kwargs):
        calls.append(kwargs)
        result = original(*args, **kwargs)
        started, release = (
            (first_started, first_release) if len(calls) == 1 else (second_started, second_release)
        )
        started.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(workspace.scoring, "evaluate", slow_score)
    try:
        workspace.apply()
        qtbot.waitUntil(first_started.is_set, timeout=10000)
        workspace.presenter.refresh_scoring_context()
        first_release.set()
        qtbot.waitUntil(second_started.is_set, timeout=10000)
        assert workspace.score_text() == "—"
        second_release.set()
        qtbot.waitUntil(lambda: workspace.score_text() != "—", timeout=10000)
        # A completed evaluation is presentation state, not a permanent cache:
        # another reading still checks current profile/context in the backend.
        workspace.apply()
        qtbot.waitUntil(lambda: len(calls) == 3, timeout=10000)
        qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
    finally:
        first_release.set()
        second_release.set()


def test_confidence_acceptance_key_uses_actual_scorer_boundary(workspace, qtbot):
    profiles = ScoringProfileService(workspace.database.session_factory)
    custom = ScoringWorkflowService(
        workspace.database.session_factory,
        profiles,
        scoring_engine=EchoScoringEngine(minimum_confidence=0.99),
    )
    workspace.presenter._scoring = custom
    stats = deepcopy(workspace.payload["sub_stats"])
    for stat in stats:
        for field in ("confidence", "name_confidence", "value_confidence"):
            stat[field] = 1.0
    accepted = _changed_observation(workspace.snapshot, sub_stats=stats)
    workspace.apply(accepted)
    qtbot.waitUntil(lambda: workspace.score_text() != "—", timeout=10000)
    previous_score = workspace.score_text()
    stats[0]["confidence"] = 0.98  # Above the default .85, below this scorer's .99.
    workspace.apply(_changed_observation(accepted, sub_stats=stats))
    assert workspace.score_text() == previous_score
    assert workspace.page._observation.sub_stats[0].confidence == 1.0
    qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
    assert workspace.score_text() == "—"


def test_new_piece_and_corrected_reading_reject_delayed_old_analysis(workspace, qtbot, monkeypatch):
    workspace.apply()
    qtbot.waitUntil(lambda: workspace.score_text() != "—", timeout=10000)
    previous = workspace.page._observation
    previous_score = workspace.score_text()
    starts, releases = [Event(), Event()], [Event(), Event()]
    calls = []
    published = []
    original = workspace.scoring.evaluate
    original_publish = workspace.page.apply_analysis_result

    def slow_score(*args, **kwargs):
        index = len(calls)
        calls.append(kwargs["observation_id"])
        result = original(*args, **kwargs)
        starts[index].set()
        assert releases[index].wait(10)
        return result

    def record_publication(observation, recommendation, *, current=True):
        published.append((observation, recommendation))
        original_publish(observation, recommendation, current=current)

    monkeypatch.setattr(workspace.scoring, "evaluate", slow_score)
    monkeypatch.setattr(workspace.page, "apply_analysis_result", record_publication)
    low_stats = deepcopy(workspace.payload["sub_stats"])
    low_stats[0]["confidence"] = 0.4
    candidate = _changed_observation(workspace.snapshot, name="新聲骸", sub_stats=low_stats)
    candidate = replace(
        candidate,
        session=replace(candidate.session, id="candidate-session", local_session_id="candidate"),
        observations=(replace(candidate.observations[-1], requires_review=True),),
    )
    corrected = _changed_observation(candidate, sub_stats=deepcopy(workspace.payload["sub_stats"]))
    corrected = replace(
        corrected,
        observations=(replace(corrected.observations[-1], requires_review=False),),
    )
    try:
        workspace.apply(candidate)
        qtbot.waitUntil(starts[0].is_set, timeout=10000)
        workspace.apply(corrected)
        releases[0].set()
        qtbot.waitUntil(starts[1].is_set, timeout=10000)
        assert workspace.page._observation == previous
        assert workspace.score_text() == previous_score
        assert published == []
        releases[1].set()
        qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
        assert calls == [candidate.observations[-1].id, corrected.observations[-1].id]
        assert len(published) == 1
        observation, recommendation = published[0]
        assert observation.name == "新聲骸"
        assert observation.observation_id == corrected.observations[-1].id
        assert not observation.needs_confirmation
        assert observation.sub_stats[0].score_contribution is not None
        assert recommendation.score is not None
        assert workspace.page.name_label.text() == "新聲骸"
        assert workspace.page.analysis_refresh_label.isHidden()
    finally:
        for release in releases:
            release.set()


def test_failed_new_analysis_publishes_latest_unscored_fields_with_error(
    workspace, qtbot, monkeypatch
):
    workspace.apply()
    qtbot.waitUntil(lambda: workspace.score_text() != "—", timeout=10000)
    previous = workspace.page._observation
    started, release = Event(), Event()

    def failed_score(*args, **kwargs):
        started.set()
        assert release.wait(10)
        raise ValueError("測試評分失敗")

    monkeypatch.setattr(workspace.scoring, "evaluate", failed_score)
    latest = _changed_observation(workspace.snapshot, name="另一顆聲骸")
    try:
        workspace.apply(latest)
        qtbot.waitUntil(started.is_set)
        assert workspace.page._observation == previous
        release.set()
        qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
        assert workspace.page.name_label.text() == "另一顆聲骸"
        assert workspace.score_text() == "—"
        assert "測試評分失敗" in workspace.page.reason_label.text()
        assert workspace.page.analysis_refresh_label.isHidden()
        assert workspace.page.correction_button.isEnabled()
    finally:
        release.set()


def test_save_during_new_analysis_targets_displayed_echo_and_survives_reopen(
    workspace, qtbot, monkeypatch
):
    characters = CharacterCultivationService(workspace.database.session_factory)
    owner = characters.create_character(
        data_set_id=workspace.snapshot.capture.data_set_id, command=CreateCharacter(name="測試角色")
    ).summary.id
    workspace.page.set_characters((ChoiceOption(owner, "測試角色"),))
    workspace.apply()
    qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
    workspace.page.select_result_character(owner)
    workspace.page.result_slot_combo.setCurrentIndex(5)
    displayed = workspace.page._observation.session_id
    started, release = Event(), Event()
    original = workspace.scoring.evaluate

    def slow_score(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(workspace.scoring, "evaluate", slow_score)
    payload = deepcopy(workspace.payload)
    payload["current_session"]["local_session_id"] = "echo-session-0002"
    receipt = workspace.history.ingest_live_result(
        workspace.snapshot.capture.id, payload, source_sequence=2
    )
    newer = workspace.history.get_history(receipt.echo_session_id)
    try:
        workspace.apply(newer)
        qtbot.waitUntil(started.is_set)
        assert workspace.page._observation.session_id == displayed
        assert workspace.page.link_character_button.isEnabled()
        workspace.page.link_character_button.click()
    finally:
        release.set()
    qtbot.waitUntil(
        lambda: (
            workspace.history.get_history(workspace.snapshot.session.id).session.slot_index == 5
        ),
        timeout=10000,
    )
    qtbot.waitUntil(lambda: not workspace.presenter._futures, timeout=10000)
    assert workspace.history.get_history(newer.session.id).session.slot_index is None
    workspace.database.engine.dispose()
    saved = workspace.history.get_history(workspace.snapshot.session.id).session
    assert saved.character_id == owner and saved.final_status == EchoDisposition.EQUIPPED
    assert (
        characters.get_character(owner, data_set_id=workspace.snapshot.capture.data_set_id)
        .echo_slots[4]
        .echo_id
        == saved.echo_id
    )


def test_profile_update_keeps_automatic_character_selection(workspace, qtbot, monkeypatch):
    result = workspace.scoring.evaluate(
        workspace.payload,
        data_set_id=workspace.snapshot.capture.data_set_id,
        profile_id=workspace.snapshot.capture.scoring_profile_id,
        persist=False,
    )
    workspace.presenter._current_history = replace(
        workspace.snapshot,
        capture=replace(workspace.snapshot.capture, scoring_profile_id=None),
    )
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(workspace.scoring, "evaluate", evaluate)
    workspace.presenter.rescore_current("edited-unrelated-profile")
    qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
    assert len(calls) == 1
    assert calls[0]["profile_id"] is None
    assert calls[0]["character_mode"] is True


def test_saving_candidate_switches_to_self_cultivation_and_target_change_restores_comparison(
    workspace, qtbot
):
    from wuwa_assistant.scoring.character_guides import load_character_seed_definitions

    ds = workspace.snapshot.capture.data_set_id
    profiles = ScoringProfileService(workspace.database.session_factory)
    profiles.ensure_system_templates(load_character_seed_definitions())
    owner = (
        CharacterCultivationService(workspace.database.session_factory)
        .create_character(data_set_id=ds, command=CreateCharacter(name="洛瑟菈"))
        .summary.id
    )
    payload = deepcopy(workspace.payload)
    payload["set_name"] = "雪落無聲之願"
    payload["main_stats"][0].update(name="攻擊", value=18.0)
    payload["main_stats"][1].update(name="攻擊", value=84, unit="")
    receipt = workspace.history.ingest_live_result(
        workspace.snapshot.capture.id, payload, source_sequence=2
    )
    snapshot = workspace.history.get_history(receipt.echo_session_id)
    other_capture = workspace.history.start_capture(
        data_set_id=ds,
        backend_kind="test",
        monitor_index=1,
        sampling_hz=5,
        stable_wait_ms=600,
        app_version="test",
    )
    other_receipt = workspace.history.ingest_live_result(
        other_capture.id, payload, source_sequence=1
    )
    other = workspace.history.set_disposition(
        other_receipt.echo_session_id, EchoDisposition.EQUIPPED, character_id=owner, slot_index=1
    )
    assert other.echo_id != snapshot.session.echo_id
    workspace.presenter._replacement = ReplacementTrainingService(
        workspace.database.session_factory
    )
    workspace.presenter._training_target = (owner, 1)
    workspace.apply(snapshot)
    qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=30000)
    assert workspace.page.training_panel.title() == "替換目標"
    assert not workspace.page.probability_label.isHidden()
    assert not workspace.page.score_delta_label.isHidden()
    assert "+0.0" in workspace.page.score_delta_label.text()
    workspace.presenter.link_character(snapshot.session.local_session_id, owner, 1)
    qtbot.waitUntil(
        lambda: workspace.page.conclusion_label.text() == "建議升至 +25 並保留", timeout=30000
    )
    assert workspace.page.training_panel.title() == "現有聲骸強化"
    assert workspace.page.probability_label.isHidden()
    assert workspace.page.score_delta_label.isHidden()
    assert workspace.presenter._scored_evaluation.projection.improvement_target is None
    workspace.database.engine.dispose()
    assert workspace.history.get_history(snapshot.session.id).session.slot_index == 1
    # A new target must not keep the same-piece heading or hide valid metrics.
    workspace.history.set_disposition(
        other_receipt.echo_session_id, EchoDisposition.EQUIPPED, character_id=owner, slot_index=2
    )
    workspace.presenter.set_training_target(owner, 2)
    qtbot.waitUntil(
        lambda: (
            workspace.presenter._training_target == (owner, 2)
            and workspace.presenter._score_in_flight is None
        ),
        timeout=30000,
    )
    assert workspace.page.training_panel.title() == "替換目標"
    assert not workspace.page.probability_label.isHidden()
    assert not workspace.page.score_delta_label.isHidden()
    assert workspace.presenter._scored_evaluation.projection.improvement_target is not None
    workspace.presenter.clear_training_target()
    qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=30000)
    assert workspace.page.training_panel.isHidden()
    assert not workspace.page.probability_label.isHidden()


def _comparison(workspace, *, slot=1, baseline=50.0, comparable=True):
    evaluation = workspace.scoring.evaluate(
        workspace.payload,
        data_set_id=workspace.snapshot.capture.data_set_id,
        profile_id=workspace.snapshot.capture.scoring_profile_id,
        decision_inputs=DecisionInputs(level=20, existing_gear_score=baseline),
        persist=False,
    )
    assert evaluation.breakdown.score is not None
    piece = EquippedEchoScore(
        echo_id=f"target-echo-{slot}",
        character_id="target-character",
        character_name="替換角色",
        slot_index=slot,
        name="現有聲骸",
        set_name="測試套裝",
        level=25,
        cost=1,
        score=baseline,
        opened_slot_quality_score=baseline,
        profile_id=evaluation.profile_id,
        profile_version=1,
        observation_id=f"target-observation-{slot}",
        payload_version=1,
        status="scored",
        notes=(),
        set_is_valid=True,
        main_stat_is_valid=True,
    )
    target = ReplacementTarget(
        piece.character_id,
        piece.character_name,
        slot,
        piece,
        evaluation.profile_id,
        evaluation.profile_name,
        ScoringContext(),
        f"替換角色 · 位置 {slot} · 現有 {baseline:.2f} 分",
    )
    return ReplacementComparison(
        target=target,
        evaluation=evaluation,
        conclusion="繼續比較" if comparable else "COST 不同",
        reason="比較同位置的聲骸" if comparable else "此 COST 不能直接替換目前位置",
        delta=round(evaluation.breakdown.score - baseline, 2),
        target_comparable=comparable,
    )


@pytest.mark.parametrize("comparable", [True, False])
def test_replacement_result_exposes_target_metrics_only_when_comparable(
    workspace, qtbot, monkeypatch, comparable
):
    comparison = _comparison(workspace, comparable=comparable)
    assert comparison.evaluation.projection.improvement_target == 50
    assert comparison.evaluation.projection.improvement_probability is not None
    service = ReplacementTrainingService(workspace.database.session_factory)
    monkeypatch.setattr(service, "evaluate", lambda *args, **kwargs: comparison)
    workspace.presenter._replacement = service
    workspace.presenter._training_target = (comparison.target.character_id, 1)
    recommendations = []
    apply = workspace.page.apply_recommendation

    def record(view):
        recommendations.append(view)
        apply(view)

    monkeypatch.setattr(workspace.page, "apply_recommendation", record)
    workspace.apply()
    qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
    shown = recommendations[-1]
    assert shown.conclusion == comparison.conclusion
    if comparable:
        assert shown.target_score == 50
        assert shown.score_delta == comparison.delta
        assert shown.target_probability == comparison.evaluation.projection.improvement_probability
        from PySide6.QtGui import QTextDocument

        document = QTextDocument()
        document.setHtml(workspace.page.score_delta_label.text())
        assert "現有 50.0" in document.toPlainText()
        assert not workspace.page.score_delta_label.isHidden()
        assert workspace.page.probability_label.text() != "超越現有機率 —"
    else:
        # The adapter can infer a target from this projection; the presenter
        # must explicitly remove it when fit/COST makes the comparison invalid.
        assert shown.target_score is None
        assert shown.score_delta is None
        assert shown.target_probability is None
        assert workspace.page.score_delta_label.isHidden()
        assert workspace.page.probability_label.text() == "超越現有機率 —"


def test_changing_target_during_compare_rejects_old_score_and_metrics(
    workspace, qtbot, monkeypatch
):
    comparisons = {
        1: _comparison(workspace, slot=1, baseline=50),
        2: _comparison(workspace, slot=2, baseline=80),
    }
    started = {1: Event(), 2: Event()}
    release = {1: Event(), 2: Event()}
    calls = []
    recommendations = []
    service = ReplacementTrainingService(workspace.database.session_factory)

    def target(_data_set_id, _character_id, slot_index):
        return comparisons[slot_index].target

    def evaluate(*args, **kwargs):
        slot = kwargs["slot_index"]
        calls.append(slot)
        started[slot].set()
        assert release[slot].wait(10)
        return comparisons[slot]

    apply = workspace.page.apply_recommendation

    def record(view):
        recommendations.append(view)
        apply(view)

    monkeypatch.setattr(service, "target", target)
    monkeypatch.setattr(service, "evaluate", evaluate)
    monkeypatch.setattr(workspace.page, "apply_recommendation", record)
    workspace.presenter._replacement = service
    try:
        with qtbot.waitSignal(workspace.presenter.training_ready):
            workspace.presenter.set_training_target("target-character", 1)
        workspace.apply()
        qtbot.waitUntil(started[1].is_set)
        workspace.presenter.set_training_target("target-character", 2)
        release[1].set()
        qtbot.waitUntil(started[2].is_set)
        assert workspace.score_text() == "—"
        assert "位置 2" in workspace.page.training_label.text()
        assert all(view.target_score != 50 for view in recommendations)
        release[2].set()
        qtbot.waitUntil(lambda: workspace.presenter._score_in_flight is None, timeout=10000)
        assert calls == [1, 2]
        assert recommendations[-1].target_score == 80
        assert recommendations[-1].score_delta == comparisons[2].delta
        assert workspace.score_text() != "—"
    finally:
        release[1].set()
        release[2].set()
