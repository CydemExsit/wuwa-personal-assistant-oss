from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.application.scoring_workflow import ScoringEvaluation
from wuwa_assistant.scoring import (
    EchoScoreBreakdown,
    ProjectionSummary,
    RollTier,
    ScoreBand,
    StatKey,
    StatScoreLine,
    UpgradeAction,
    UpgradeDecision,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import DataSetRecord
from wuwa_assistant.ui.controllers.scoring_profiles import (
    ScoringProfilesPresenter,
    evaluation_to_echo_recommendation,
    evaluation_to_live_substats,
    evaluation_to_profile_preview,
)
from wuwa_assistant.ui.pages.echo_live import ChoiceOption, EchoStatView
from wuwa_assistant.ui.pages.scoring_profiles import ScoringProfilesPage


@pytest.fixture
def profile_workspace(
    tmp_path: Path,
) -> Iterator[tuple[Database, ScoringProfileService, str, ScoringProfilesPage]]:
    database = create_database(AppPaths.from_root(tmp_path / "profile-presenter"))
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        data_set_id = data_set.id
    page = ScoringProfilesPage()
    try:
        yield database, ScoringProfileService(database.session_factory), data_set_id, page
    finally:
        page.close()
        database.dispose()


def test_initialize_seeds_four_templates_and_publishes_default_choices(
    qapp: QApplication,
    qtbot: QtBot,
    profile_workspace: tuple[Database, ScoringProfileService, str, ScoringProfilesPage],
) -> None:
    del qapp
    _database, service, data_set_id, page = profile_workspace
    choices_updates: list[tuple[ChoiceOption, ...]] = []
    default_updates: list[str] = []
    presenter = ScoringProfilesPresenter(
        service=service,
        page=page,
        data_set_id=data_set_id,
    )
    presenter.profile_choices_changed.connect(choices_updates.append)
    presenter.default_profile_changed.connect(default_updates.append)

    presenter.initialize()
    qtbot.waitUntil(lambda: page.profile_selector.count() == 4)
    qtbot.waitUntil(lambda: page.profile_selector.isEnabled())
    expected_default = service.get_default(data_set_id=data_set_id)
    assert expected_default is not None
    qtbot.waitUntil(lambda: page.current_profile_id() == expected_default.id)

    stored = service.list_profiles(data_set_id=data_set_id)
    assert len(stored) == 4
    assert all(profile.is_system_template for profile in stored)
    assert {profile.template_kind for profile in stored} == {
        "generic_dps",
        "healing_hp",
        "defense",
        "energy_requirement",
    }
    assert page.name_edit.text() == expected_default.name
    assert not page.system_template_banner.isHidden()
    assert not page.save_button.isEnabled()
    assert choices_updates
    assert {choice.label for choice in choices_updates[-1]} == {profile.name for profile in stored}
    assert default_updates[-1] == expected_default.id
    assert presenter.shutdown()


def test_clone_save_and_set_default_are_persisted_asynchronously_before_shutdown(
    qapp: QApplication,
    qtbot: QtBot,
    profile_workspace: tuple[Database, ScoringProfileService, str, ScoringProfilesPage],
) -> None:
    del qapp
    _database, service, data_set_id, page = profile_workspace
    default_updates: list[str] = []
    presenter = ScoringProfilesPresenter(
        service=service,
        page=page,
        data_set_id=data_set_id,
    )
    presenter.default_profile_changed.connect(default_updates.append)
    presenter.initialize()
    qtbot.waitUntil(lambda: page.profile_selector.count() == 4)
    qtbot.waitUntil(lambda: page.profile_selector.isEnabled())
    source = service.get_default(data_set_id=data_set_id)
    assert source is not None

    source_index = page.profile_selector.findData(source.id)
    page.profile_selector.setCurrentIndex(source_index)
    qtbot.waitUntil(lambda: page.current_profile_id() == source.id)
    qtbot.waitUntil(lambda: page.clone_button.isEnabled())
    QTest.mouseClick(page.clone_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: len(service.list_profiles(data_set_id=data_set_id)) == 5)
    qtbot.waitUntil(lambda: page.save_button.isEnabled())
    cloned_id = page.current_profile_id()
    assert cloned_id is not None and cloned_id != source.id
    cloned = service.get_profile(cloned_id, data_set_id=data_set_id)
    assert not cloned.is_system_template
    assert cloned.name.endswith("副本")

    page.name_edit.setText("每日使用設定")
    page.passing_threshold_spin.setValue(72.0)
    first_weight = page.weight_model.index(0, 2)
    assert page.weight_model.setData(first_weight, 1.25)
    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)

    def saved_profile_is_visible() -> bool:
        saved = service.get_profile(cloned_id, data_set_id=data_set_id)
        return saved.name == "每日使用設定" and saved.version == 2

    qtbot.waitUntil(saved_profile_is_visible)
    qtbot.waitUntil(lambda: page.profile_selector.isEnabled())
    saved = service.get_profile(cloned_id, data_set_id=data_set_id)
    assert saved.thresholds.passing == 72.0
    assert saved.weights[0].weight == 1.25

    QTest.mouseClick(page.default_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: service.get_default(data_set_id=data_set_id).id == cloned_id)
    qtbot.waitUntil(lambda: cloned_id in default_updates)
    qtbot.waitUntil(lambda: not page.default_button.isEnabled())
    assert page.status_label.text() == "預設評分設定檔已更新。"

    profile_count = len(service.list_profiles(data_set_id=data_set_id))
    assert presenter.shutdown()
    assert presenter.shutdown()
    page.create_requested.emit()
    qtbot.wait(20)
    assert len(service.list_profiles(data_set_id=data_set_id)) == profile_count


def test_evaluation_mapping_explains_early_quality_and_normalizes_contributions() -> None:
    high_roll = RollTier("high", "高 Roll", 0.75, 1.0)
    lines = (
        StatScoreLine(
            stat=StatKey.CRIT_RATE,
            value=8.1,
            unit="%",
            confidence=0.99,
            roll_ratio=0.9,
            roll_tier=high_roll,
            base_weight=1.5,
            roll_credit=1.0,
            conditional_multiplier=1.0,
            threshold_multiplier=1.0,
            crit_balance_multiplier=1.0,
            weighted_points=1.5,
        ),
        StatScoreLine(
            stat=StatKey.ATTACK_PCT,
            value=10.1,
            unit="%",
            confidence=0.98,
            roll_ratio=0.85,
            roll_tier=high_roll,
            base_weight=1.0,
            roll_credit=1.0,
            conditional_multiplier=1.0,
            threshold_multiplier=1.0,
            crit_balance_multiplier=1.0,
            weighted_points=1.0,
        ),
    )
    breakdown = EchoScoreBreakdown(
        profile_id="profile-1",
        profile_version=1,
        score=50.0,
        band=ScoreBand.TRANSITIONAL,
        lines=lines,
        earned_weight_points=2.5,
        maximum_weight_points=5.0,
        opened_slot_quality_score=83.3,
    )
    projection = ProjectionSummary(
        assumptions_version="uniform-v1",
        remaining_slots=3,
        outcome_count=8,
        minimum_score=50.0,
        maximum_score=95.0,
        expected_score=72.4,
        probabilities={"passing": 0.6, "perfect": 0.2, "exceptional": 0.05},
        total_probability=1.0,
    )
    decision = UpgradeDecision(
        action=UpgradeAction.CONTINUE,
        current_score=50.0,
        predicted_min_score=50.0,
        predicted_max_score=95.0,
        probabilities=projection.probabilities,
        assumptions_version="uniform-v1",
        reason="已開詞條品質良好。",
        reason_codes=("quality_good",),
    )
    evaluation = ScoringEvaluation(
        profile_id="profile-1",
        profile_name="愛彌斯輸出",
        breakdown=breakdown,
        projection=projection,
        decision=decision,
    )

    preview = evaluation_to_profile_preview(evaluation)
    assert [line.contribution for line in preview.score.breakdown] == [30.0, 20.0]
    assert sum(line.contribution for line in preview.score.breakdown) == preview.score.score

    generic = evaluation_to_echo_recommendation(evaluation)
    recommendation = evaluation_to_echo_recommendation(evaluation, character_label="愛彌斯")
    assert generic.scoring_basis == "指定設定檔參考"
    assert recommendation.scoring_basis == "愛彌斯"
    assert recommendation.scoring_profile_name == "愛彌斯輸出"
    assert recommendation.opened_slot_quality_score == 83.3
    assert recommendation.score == 50.0
    assert recommendation.expected_score == 72.4
    assert "+30.00" in recommendation.breakdown[0]
    assert "+20.00" in recommendation.breakdown[1]
    assert recommendation.target_probability is None
    assert recommendation.target_score is None
    assert recommendation.score_delta is None
    assert "Roll 資料版本: uniform-v1" in recommendation.breakdown

    targeted = replace(
        evaluation,
        projection=replace(
            projection,
            improvement_target=70.68,
            improvement_probability=0.015556,
        ),
    )
    targeted_view = evaluation_to_echo_recommendation(
        targeted, target_score=70.68, score_delta=-20.68
    )
    assert targeted_view.target_score == 70.68
    assert targeted_view.target_probability == 0.015556
    assert targeted_view.target_probability != targeted_view.pass_probability
    assert targeted_view.score_delta == -20.68
    # A stale target, extra replacement margin, or uncertain OCR cannot be
    # described as the chance of strictly beating the displayed equipped item.
    stale = evaluation_to_echo_recommendation(targeted, target_score=72.0)
    assert stale.target_probability is None
    assert stale.score_delta == -22.0
    uncertain = evaluation_to_echo_recommendation(
        replace(
            targeted, projection=replace(targeted.projection, requires_manual_confirmation=True)
        )
    )
    assert uncertain.target_probability is None
    derived = evaluation_to_echo_recommendation(targeted)
    assert derived.target_score == 70.68
    assert derived.target_probability == 0.015556
    assert derived.score_delta == -20.68
    checkpoint = evaluation_to_echo_recommendation(
        replace(evaluation, decision=replace(decision, suggested_level=15))
    )
    assert checkpoint.conclusion == "可到 +15 再評估"

    live_stats = evaluation_to_live_substats(
        evaluation,
        (
            EchoStatView("暴擊", 8.1, "%", 0.99),
            EchoStatView("攻擊", 10.1, "%", 0.98),
        ),
    )
    assert live_stats[0].character_value == "權重 1.50 · +30.00 分"
    assert live_stats[1].character_value == "權重 1.00 · +20.00 分"

    capped_lines = (
        replace(lines[0], weighted_points=3.0),
        replace(lines[1], weighted_points=2.7),
    )
    capped_evaluation = replace(
        evaluation,
        breakdown=replace(
            breakdown,
            score=100.0,
            lines=capped_lines,
            earned_weight_points=5.7,
        ),
        decision=replace(decision, current_score=100.0),
    )
    capped_preview = evaluation_to_profile_preview(capped_evaluation)
    assert sum(line.contribution for line in capped_preview.score.breakdown) == pytest.approx(100.0)
