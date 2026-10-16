from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from wuwa_assistant.ui.pages.scoring_profiles import (
    EchoScoreView,
    ProfileSummaryView,
    ScoreBreakdownItemView,
    ScoreDecisionPresentationView,
    ScoreThresholdsView,
    ScoringProfileEditRequest,
    ScoringProfilesPage,
    ScoringProfileView,
    StatWeightView,
    UpgradeDecisionView,
)


def _summary(
    identifier: str = "profile-1",
    name: str = "通用輸出型",
    *,
    system: bool = False,
    default: bool = False,
) -> ProfileSummaryView:
    return ProfileSummaryView(identifier, name, "通用輸出", system, default)


def _profile(*, system: bool = False, default: bool = False) -> ScoringProfileView:
    return ScoringProfileView(
        identifier="profile-1",
        name="我的緋雪設定",
        template_kind="generic_dps",
        template_label="通用輸出型",
        description="重視暴擊平衡與共鳴解放。",
        thresholds=ScoreThresholdsView(55, 70, 88, 95),
        stat_weights=(
            StatWeightView("暴擊", "%", 1.5, 20.0, "crit_balance"),
            StatWeightView("攻擊", "%", 1.0, None, "always"),
        ),
        is_system_template=system,
        is_default=default,
        version=3,
    )


def test_profile_page_has_zero_dependency_constructor_and_stable_controls(
    qapp: QApplication,
) -> None:
    page = ScoringProfilesPage()
    page.show()
    qapp.processEvents()

    assert page.objectName() == "ScoringProfilesPage"
    assert page.profile_selector.objectName() == "ScoringProfileSelector"
    assert page.weight_table.objectName() == "ScoringStatWeightTable"
    assert page.breakdown_table.objectName() == "ScoringBreakdownTable"
    assert page.current_profile_id() is None
    assert not page.save_button.isEnabled()
    assert not page.delete_button.isEnabled()
    assert page.breakdown_model.rowCount() == 0
    page.close()


def test_profile_page_emits_selector_and_profile_action_signals(qapp: QApplication) -> None:
    page = ScoringProfilesPage()
    selected: list[str] = []
    created: list[bool] = []
    cloned: list[str] = []
    defaults: list[str] = []
    page.profile_selected.connect(selected.append)
    page.create_requested.connect(lambda: created.append(True))
    page.clone_requested.connect(cloned.append)
    page.default_requested.connect(defaults.append)
    page.set_profiles(
        (
            _summary(),
            _summary("profile-2", "生命治療型"),
        )
    )
    page.apply_profile(_profile())
    page.show()
    qapp.processEvents()

    assert selected == ["profile-1"]
    page.profile_selector.setCurrentIndex(1)
    QTest.mouseClick(page.create_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.clone_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.default_button, Qt.MouseButton.LeftButton)

    assert selected == ["profile-1", "profile-2"]
    assert created == [True]
    assert cloned == ["profile-2"]
    assert defaults == ["profile-2"]
    page.close()


def test_user_profile_is_editable_and_emits_an_immutable_save_request(
    qapp: QApplication,
) -> None:
    page = ScoringProfilesPage()
    saves: list[ScoringProfileEditRequest] = []
    deletes: list[str] = []
    page.save_requested.connect(saves.append)
    page.delete_requested.connect(deletes.append)
    page.set_profiles((_summary(),))
    page.apply_profile(_profile())
    page.show()
    qapp.processEvents()

    assert page.name_edit.isEnabled()
    assert not page.system_template_banner.isVisible()
    assert page.weight_model.flags(page.weight_model.index(0, 2)) & Qt.ItemFlag.ItemIsEditable

    page.name_edit.setText("緋雪最終版")
    page.passing_threshold_spin.setValue(72)
    assert page.weight_model.setData(page.weight_model.index(0, 2), 1.75)
    assert page.weight_model.setData(page.weight_model.index(0, 3), "18.5")
    assert page.weight_model.setData(page.weight_model.index(0, 4), "crit_below_target")
    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.delete_button, Qt.MouseButton.LeftButton)

    assert len(saves) == 1
    request = saves[0]
    assert request.identifier == "profile-1"
    assert request.name == "緋雪最終版"
    assert request.thresholds.passing == 72
    assert request.stat_weights[0] == StatWeightView("暴擊", "%", 1.75, 18.5, "crit_below_target")
    assert request.expected_version == 3
    assert deletes == ["profile-1"]
    page.close()


def test_invalid_threshold_order_stays_in_view_and_does_not_emit_save(
    qapp: QApplication,
) -> None:
    page = ScoringProfilesPage()
    saves: list[object] = []
    page.save_requested.connect(saves.append)
    page.set_profiles((_summary(),))
    page.apply_profile(_profile())
    page.passing_threshold_spin.setValue(90)
    QTest.mouseClick(page.save_button, Qt.MouseButton.LeftButton)

    assert saves == []
    assert page.status_label.property("error") is True
    assert "可用 < 及格 < 完美 < 極品" in page.status_label.text()
    page.close()


def test_system_template_is_read_only_but_can_be_cloned_or_selected_as_default(
    qapp: QApplication,
) -> None:
    page = ScoringProfilesPage()
    clones: list[str] = []
    defaults: list[str] = []
    page.clone_requested.connect(clones.append)
    page.default_requested.connect(defaults.append)
    page.set_profiles((_summary(system=True),))
    page.apply_profile(_profile(system=True))
    page.show()
    qapp.processEvents()

    assert page.system_template_banner.isVisible()
    assert not page.name_edit.isEnabled()
    assert not page.save_button.isEnabled()
    assert not page.delete_button.isEnabled()
    assert not (page.weight_model.flags(page.weight_model.index(0, 2)) & Qt.ItemFlag.ItemIsEditable)
    assert page.clone_button.isEnabled()
    assert page.default_button.isEnabled()

    QTest.mouseClick(page.clone_button, Qt.MouseButton.LeftButton)
    QTest.mouseClick(page.default_button, Qt.MouseButton.LeftButton)
    assert clones == ["profile-1"]
    assert defaults == ["profile-1"]
    page.close()


def test_capture_active_locks_all_profile_mutations(qapp: QApplication) -> None:
    page = ScoringProfilesPage()
    page.set_profiles((_summary(),))
    page.apply_profile(_profile())
    page.show()
    qapp.processEvents()

    page.set_capture_active(True)

    assert page.profile_selector.isEnabled()
    assert not page.create_button.isEnabled()
    assert not page.clone_button.isEnabled()
    assert not page.default_button.isEnabled()
    assert not page.save_button.isEnabled()
    assert not page.delete_button.isEnabled()
    assert "停止辨識後" in page.status_label.text()

    page.set_capture_active(False)
    assert page.create_button.isEnabled()
    assert page.clone_button.isEnabled()
    assert page.save_button.isEnabled()
    page.close()


def test_score_and_decision_dtos_render_explainable_breakdown(qapp: QApplication) -> None:
    page = ScoringProfilesPage()
    page.apply_score_preview(
        ScoreDecisionPresentationView(
            score=EchoScoreView(
                score=82.5,
                grade_label="及格",
                breakdown=(
                    ScoreBreakdownItemView("暴擊", "7.5%", "高 Roll", 1.5, 18.25, "核心"),
                    ScoreBreakdownItemView("攻擊%", "10.1%", "高 Roll", 1.0, 12.0),
                ),
                is_provisional=True,
            ),
            decision=UpgradeDecisionView(
                action_label="繼續到 +20",
                reason="目前核心詞條品質良好, 還有兩個可成長欄位。",
                projected_score_range=(84, 96),
                pass_probability=0.94,
                perfect_probability=0.42,
                exceptional_probability=0.08,
                assumptions=("剩餘 Roll 採均勻分布", "尚未套用隊伍 Buff"),
            ),
        )
    )
    page.show()
    qapp.processEvents()

    assert page.score_label.text() == "82.5 / 100"
    assert page.score_label.property("provisional") is True
    assert page.grade_label.text() == "及格 (暫定)"
    assert page.decision_label.text() == "繼續到 +20"
    assert "94%" in page.probability_label.text()
    from PySide6.QtGui import QTextDocument

    from wuwa_assistant.ui.score_colors import score_color

    document = QTextDocument()
    document.setHtml(page.probability_label.text())
    assert "84.0-96.0" in document.toPlainText()
    for value in (84, 96):
        assert score_color(value).name() in page.probability_label.text()
    page.score_label.ensurePolished()
    assert page.score_label.palette().windowText().color().name() == score_color(82.5).name()
    assert "均勻分布" in page.assumptions_label.text()
    assert page.breakdown_model.rowCount() == 2
    assert page.breakdown_model.data(page.breakdown_model.index(0, 4)) == "+18.25"
    page.close()
