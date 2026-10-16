from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from wuwa_assistant.ui.pages.echo_live import (
    CaptureState,
    CaptureStatusView,
    ChoiceOption,
    CorrectionRequest,
    EchoLivePage,
    EchoObservationView,
    EchoStatView,
    RecommendationView,
    TimelineEntryView,
)


def test_review_highlights_exact_cells_and_prioritizes_correction(qapp: QApplication) -> None:
    from wuwa_assistant.application.observation_review import FieldReviewIssue
    from wuwa_assistant.ui.pages.echo_live import CorrectionDialog

    page = _page()
    issue = FieldReviewIssue("/sub_stats/1/value", "副詞條 2數值", "辨識信心 40%, 低於 85%")
    page.apply_observation(replace(_observation(), review_issues=(issue,), needs_confirmation=True))
    model = page.sub_stats_table.model()
    assert model.index(1, 1).data(Qt.ItemDataRole.BackgroundRole) is not None
    assert model.index(1, 0).data(Qt.ItemDataRole.BackgroundRole) is None
    assert model.index(0, 1).data(Qt.ItemDataRole.BackgroundRole) is None
    assert issue.label in page.confirmation_label.text()
    assert "40%" not in page.confirmation_label.text()
    assert "40%" in page.confirmation_label.toolTip()
    dialog = CorrectionDialog(page._field_values, issues=(issue,))
    assert dialog.field_path == issue.field_path
    assert "40%" in dialog.issue_label.text()
    assert dialog.corrected_value == "9.4"
    dialog.close()
    page.apply_observation(_observation())
    assert model.index(1, 1).data(Qt.ItemDataRole.BackgroundRole) is None
    assert not page.confirmation_label.toolTip()
    page.close()


def test_many_review_reasons_keep_summary_short_and_all_field_markers(qapp: QApplication) -> None:
    from wuwa_assistant.application.observation_review import FieldReviewIssue
    from wuwa_assistant.ui.pages.echo_live import CorrectionDialog

    issues = tuple(
        FieldReviewIssue(f"/sub_stats/{i}/value", f"副詞條 {i + 1} 數值", "待核對原因 " * 100)
        for i in range(3)
    )
    page = _page()
    page.apply_observation(replace(_observation(), review_issues=issues, needs_confirmation=True))
    assert "等 3 項" in page.confirmation_label.text()
    assert len(page.confirmation_label.text()) < 60
    assert all(issue.message in page.confirmation_label.toolTip() for issue in issues)
    model = page.sub_stats_table.model()
    assert all(model.index(i, 1).data(Qt.ItemDataRole.BackgroundRole) is not None for i in range(3))
    dialog = CorrectionDialog(page._field_values, issues=issues)
    assert issues[0].reason in dialog.issue_label.text()
    dialog.close()
    page.close()


def _page() -> EchoLivePage:
    return EchoLivePage(
        monitors=(ChoiceOption("2", "顯示器 2 · 2560x1440"),),
        characters=(ChoiceOption("character-1", "測試角色"),),
        scoring_profiles=(ChoiceOption("profile-1", "通用輸出型"),),
    )


def _observation(*, needs_confirmation: bool = False) -> EchoObservationView:
    return EchoObservationView(
        session_id="echo-session-0001",
        observation_id="observation-0004",
        name="不熄獵手",
        set_name="羽落空塵之歌",
        cost=1,
        level=15,
        main_stats=(
            EchoStatView("生命", 15.5, "%", 0.98),
            EchoStatView("生命", 1550, None, 0.99),
        ),
        sub_stats=(
            EchoStatView("暴擊", 7.5, "%", 0.97, "高 Roll", "核心"),
            EchoStatView("共鳴解放傷害加成", 9.4, "%", 0.95, "中 Roll", "有效"),
            EchoStatView("攻擊", 10.1, "%", 0.96, "高 Roll", "核心"),
        ),
        locked_substat_slots=2,
        confidence=0.95,
        needs_confirmation=needs_confirmation,
        is_equipped=True,
        equipped_slot_index=3,
    )


def test_compact_workspace_keeps_target_five_stats_and_scores_visible(qapp: QApplication) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QScrollArea

    from wuwa_assistant.ui.theme import apply_dark_theme

    original_style = qapp.styleSheet()
    apply_dark_theme(qapp)
    page = _page()
    try:
        page.resize(940, 740)  # 1200px app minus sidebar, title bar and status bar
        page.set_training_target(
            "穗穗 · 位置 1 · 現有聲骸 45 分\n套裝: 羽落空塵之歌\n副詞條優先: 效率、生命、雙暴"
        )
        stat = EchoStatView("共鳴解放傷害加成", 10.1, "%", 0.99, "高 Roll", "權重 .6", 12.34)
        page.apply_observation(replace(_observation(), sub_stats=(stat,) * 5))
        page.apply_recommendation(
            RecommendationView(
                conclusion="先開至 +15 再判斷",
                score=30.0,
                expected_score=62.1,
                estimated_score_range=(30.0, 87.0),
                target_score=70.68,
                target_probability=0.15556,
                score_delta=-40.68,
                reason="完整計算政策與尚需經驗的詳細說明。" * 40,
                breakdown=("機率模型與資料來源。" * 30,),
            )
        )
        page.show()
        qapp.processEvents()
        area = page.findChild(QScrollArea, "EchoLiveScrollArea")
        assert area is not None
        table = page.sub_stats_table
        last = table.visualRect(table.model().index(4, 3))
        assert table.viewport().rect().contains(last)
        point = table.viewport().mapTo(area.viewport(), last.bottomRight())
        assert area.viewport().rect().contains(point)
        assert table.model().index(0, 3).data() == "+12.34"
        assert "權重 .6" in table.model().index(0, 3).data(Qt.ItemDataRole.ToolTipRole)
        assert not page.advanced_widget.isVisible()
        assert page.training_summary_label.isVisible()
        assert page.rect().contains(page.training_summary_label.mapTo(page, QPoint(0, 0)))
        assert not page.reason_label.isVisible()
        assert not page.breakdown_label.isVisible()
        for label in (
            page.conclusion_label,
            page.cumulative_score_label,
            page.score_range_label,
            page.probability_label,
            page.score_delta_label,
        ):
            assert label.isVisible()
            assert (
                area.viewport()
                .rect()
                .contains(label.mapTo(area.viewport(), label.rect().bottomRight()))
            )
    finally:
        page.close()
        qapp.setStyleSheet(original_style)


def test_live_page_emits_array_free_capture_actions_and_updates_status(
    qapp: QApplication,
) -> None:
    page = _page()
    starts: list[tuple[str, str, str]] = []
    stops: list[bool] = []
    rechecks: list[bool] = []
    page.start_requested.connect(
        lambda monitor, character, profile: starts.append((monitor, character, profile))
    )
    page.stop_requested.connect(lambda: stops.append(True))
    page.recheck_requested.connect(lambda: rechecks.append(True))
    page.show()
    qapp.processEvents()
    assert not page.recheck_button.isEnabled()

    QTest.mouseClick(page.start_button, Qt.MouseButton.LeftButton)
    assert starts == [("2", "character-1", "character_guides")]
    assert not page.start_button.isEnabled()
    assert page.stop_button.isEnabled()

    page.apply_capture_status(
        CaptureStatusView(CaptureState.STABLE, "ROI 已連續穩定 0.60 秒", 0.94)
    )
    assert page.capture_status_label.text() == "已穩定"
    assert page.capture_status_label.property("captureState") == "stable"
    assert page.confidence_label.text() == "OCR 信心: 94%"
    assert page.recheck_button.isEnabled()
    QTest.mouseClick(page.recheck_button, Qt.MouseButton.LeftButton)
    assert rechecks == [True] and starts == [("2", "character-1", "character_guides")]

    QTest.mouseClick(page.stop_button, Qt.MouseButton.LeftButton)
    assert stops == [True]
    page.apply_capture_status(CaptureStatusView(CaptureState.STOPPED))
    assert page.start_button.isEnabled()
    assert not page.stop_button.isEnabled()
    assert not page.recheck_button.isEnabled()
    page.close()


def test_live_page_starts_with_simple_guide_and_keeps_scoring_optional(
    qapp: QApplication,
) -> None:
    page = _page()
    page.show()
    qapp.processEvents()
    try:
        assert page.idle_panel.isVisible()
        assert page.results_container.isHidden()
        assert page.advanced_widget.isHidden()
        assert page.start_button.property("role") == "primary"
        assert "1." in page.idle_steps_label.text()
        assert "不含畫面" in page.idle_steps_label.text()

        QTest.mouseClick(page.advanced_toggle, Qt.MouseButton.LeftButton)
        assert page.advanced_widget.isVisible()
        assert "收合" in page.advanced_toggle.text()
    finally:
        page.close()


def test_manual_mode_reads_only_on_button_and_keeps_completed_result(qapp: QApplication) -> None:
    page = _page()
    starts: list[tuple[str, str, str]] = []
    rechecks: list[bool] = []
    modes: list[str] = []
    page.start_requested.connect(lambda *args: starts.append(args))
    page.recheck_requested.connect(lambda: rechecks.append(True))
    page.capture_mode_changed.connect(modes.append)
    page.show()
    try:
        page.capture_mode_combo.setCurrentIndex(page.capture_mode_combo.findData("manual"))
        assert modes == ["manual"] and starts == [] and rechecks == []
        assert page.start_button.text() == "辨識目前畫面"
        assert page.recheck_button.isHidden()
        assert "再按一次" in page.idle_steps_label.text()

        QTest.mouseClick(page.start_button, Qt.MouseButton.LeftButton)
        assert len(starts) == 1 and rechecks == []
        assert not page.start_button.isEnabled() and page.stop_button.isEnabled()
        page.apply_capture_status(CaptureStatusView(CaptureState.RECOGNIZING))
        QTest.mouseClick(page.start_button, Qt.MouseButton.LeftButton)
        assert len(starts) == 1 and rechecks == []

        observation = _observation()
        page.apply_observation(observation)
        page.apply_capture_status(CaptureStatusView(CaptureState.WAITING_FOR_REQUEST))
        assert page.start_button.isEnabled() and page.stop_button.isEnabled()
        assert not page.monitor_combo.isEnabled()
        assert page.capture_status_label.text() == "等待手動辨識"
        assert page.name_label.text() == observation.name
        QTest.mouseClick(page.start_button, Qt.MouseButton.LeftButton)
        assert len(starts) == 1 and rechecks == [True]
        assert not page.start_button.isEnabled()
        assert page.name_label.text() == observation.name
    finally:
        page.close()


def test_manual_timeout_and_page_guidance_never_promise_automatic_reading(
    qapp: QApplication,
) -> None:
    page = _page()
    try:
        page.select_capture_mode("manual")
        page.apply_page_detection("echo_selection", 0.99, True)
        assert "自動" not in page.page_detection_label.text()
        assert "辨識目前畫面" in page.page_detection_label.text()
        page.apply_capture_status(
            CaptureStatusView(CaptureState.WAITING_FOR_REQUEST, "本次未讀取成功, 請再按一次。")
        )
        assert page.start_button.isEnabled()
        assert "本次未讀取成功" in page.idle_steps_label.text()
        for kind in ("echo_enhancement", "enhancement_material_selection", "unknown"):
            page.apply_page_detection(kind, None, False)
            assert "自動" not in page.page_detection_label.text()
            assert "辨識目前畫面" in page.page_detection_label.text()
    finally:
        page.close()


def test_restoring_capture_mode_has_no_intent_and_auto_remains_default(qapp: QApplication) -> None:
    page = _page()
    intents: list[str] = []
    page.capture_mode_changed.connect(intents.append)
    page.start_requested.connect(lambda *_: intents.append("start"))
    page.recheck_requested.connect(lambda: intents.append("recheck"))
    try:
        assert not page.manual_mode
        page.select_capture_mode("manual")
        assert page.manual_mode and intents == []
        page.select_capture_mode("auto")
        assert not page.manual_mode and intents == []
        assert page.start_button.text() == "開始辨識"
        with pytest.raises(ValueError):
            page.select_capture_mode("unsupported")
    finally:
        page.close()


def test_live_page_detection_gives_actionable_chinese_guidance(
    qapp: QApplication,
) -> None:
    page = _page()
    page.show()
    qapp.processEvents()
    try:
        page.apply_page_detection("character_echo_overview", 0.91, False)
        assert "角色聲骸總覽" in page.page_detection_label.text()
        assert "選擇一顆聲骸" in page.page_detection_label.text()
        assert page.confidence_label.text() == "OCR 信心: 91%"

        page.apply_page_detection("echo_selection", 0.93, False)
        assert "聲骸選擇" in page.page_detection_label.text()
        assert "自動讀取所選聲骸" in page.page_detection_label.text()

        page.apply_page_detection("echo_enhancement", 0.97, True)
        assert "聲骸強化" in page.page_detection_label.text()
        assert "返回聲骸清單後自動恢復" in page.page_detection_label.text()
        assert page.page_detection_label.property("pageType") == "echo_enhancement"

        page.apply_page_detection("unknown", 0.22, False)
        assert "未看到支援的聲骸頁" in page.page_detection_label.text()
        assert "立即重新辨識" in page.page_detection_label.text()
        assert "預覽診斷包" in page.page_detection_label.text()
        assert "不含畫面" in page.page_detection_label.text()
        assert page.confidence_label.text() == "OCR 信心: 22%"
        assert page.confidence_label.property("lowConfidence") is True
    finally:
        page.close()


def test_live_page_renders_observation_recommendation_and_timeline(
    qapp: QApplication,
) -> None:
    page = _page()
    page.show()
    qapp.processEvents()
    page.begin_capture_run("本次辨識 · 2026-08-24 14:30 開始")
    page.apply_observation(_observation(needs_confirmation=True))
    page.select_result_character("character-1")
    page.apply_recommendation(
        RecommendationView(
            conclusion="繼續",
            opened_slot_quality_score=78.5,
            score=47.1,
            expected_score=86.4,
            estimated_score_range=(82.0, 96.0),
            pass_probability=0.91,
            perfect_probability=0.44,
            exceptional_probability=0.08,
            reason="已有兩條高價值詞條, 仍有兩個欄位可成長。",
            breakdown=("暴擊高 Roll: +18.0", "攻擊% 高 Roll: +14.0"),
            scoring_profile_name="通用輸出型",
        )
    )
    page.set_timeline([TimelineEntryView("12:00:00", "升級", 10, "新增共鳴解放傷害加成", 0.95)])
    page.append_timeline_entry(TimelineEntryView("12:00:05", "升級", 15, "新增攻擊%", 0.96))

    assert page.session_label.text() == "第 1 顆"
    assert page.capture_run_label.text() == "本次辨識 · 2026-08-24 14:30 開始 · 本次第 1 顆"
    assert page.idle_panel.isHidden()
    assert page.results_container.isVisible()
    assert page.name_label.text() == "不熄獵手"
    assert page.level_label.text() == "+15"
    assert page.game_equipped_label.property("equippedState") == "equipped"
    assert page.result_slot_combo.currentData() == 3
    assert page.result_character_combo.currentData() == "character-1"
    assert "已自動保存" in page.result_status_label.text()
    assert page.main_stats_table.model().rowCount() == 2
    assert page.sub_stats_table.model().rowCount() == 3
    assert page.confirmation_label.property("needsConfirmation") is True
    assert page.conclusion_label.text() == "繼續"
    assert page.scoring_basis_label.text() == "評分: 通用參考"
    assert "通用輸出型" in page.scoring_basis_label.toolTip()
    assert page.score_label.text() == "已開詞條品質 78.5/100"
    assert page.cumulative_score_label.text() == "累積分 47.1/100"
    assert "+25" in page.cumulative_score_label.toolTip()
    from PySide6.QtGui import QTextDocument

    document = QTextDocument()
    document.setHtml(page.score_range_label.text())
    assert document.toPlainText() == "期望 86.4 · 範圍 82.0\u201396.0"
    assert page.probability_label.text() == "超越現有機率 —"
    assert "通用門檻參考: 及格 91%" in page.breakdown_label.text()
    assert not page.reason_label.isVisible()
    page.breakdown_button.click()
    assert page.reason_label.isVisible()
    assert "兩條高價值" in page.reason_label.text()
    assert page.timeline_model.rowCount() == 2
    assert page.timeline_model.data(page.timeline_model.index(1, 2)) == "+15"
    page.clear_observation()
    assert page.idle_panel.isVisible()
    assert page.results_container.isHidden()
    assert page.result_slot_combo.currentData() == 0
    page.close()


def test_live_page_auto_selects_only_unambiguous_character_and_preserves_choice(
    qapp: QApplication,
) -> None:
    del qapp
    page = EchoLivePage(
        monitors=(ChoiceOption("1", "顯示器 1"),),
        scoring_profiles=(ChoiceOption("profile-1", "通用輸出型"),),
    )
    try:
        page.set_characters(
            (
                ChoiceOption("character-1", "愛彌斯"),
                ChoiceOption("character-2", "秧秧"),
            )
        )
        assert page.character_combo.currentData() == ""
        assert "等待角色與評分結果" in page.scoring_basis_label.text()

        page.set_characters((ChoiceOption("character-1", "愛彌斯"),))
        assert page.character_combo.currentData() == "character-1"
        assert page.scoring_basis_label.text() == "評分: 等待角色與評分結果"
        assert "依所屬角色需求(自動)" in page.scoring_basis_label.toolTip()

        page.set_characters(
            (
                ChoiceOption("character-1", "愛彌斯"),
                ChoiceOption("character-2", "秧秧"),
            )
        )
        assert page.character_combo.currentData() == "character-1"
        assert page.scoring_basis_label.text() == "評分: 等待角色與評分結果"
    finally:
        page.close()


def test_target_probability_and_delta_remain_primary_while_reason_is_folded(
    qapp: QApplication,
) -> None:
    page = _page()
    try:
        page.apply_observation(_observation())
        page.apply_recommendation(
            RecommendationView(
                conclusion="建議停止投入",
                score=38.79,
                expected_score=47.42,
                estimated_score_range=(38.79, 76.83),
                pass_probability=0.02,
                perfect_probability=0.0,
                exceptional_probability=0.0,
                target_score=70.68,
                target_probability=0.015556,
                score_delta=-31.89,
                reason="下一節點需要 63500 經驗。",
                breakdown=("評分依角色需求計算。",),
            )
        )
        page.show()
        qapp.processEvents()
        assert page.probability_label.text() == "超越現有機率 1.6%"
        assert "同分不算超越" in page.probability_label.toolTip()
        from PySide6.QtGui import QTextDocument

        document = QTextDocument()
        document.setHtml(page.score_delta_label.text())
        assert document.toPlainText() == "與現有差距 -31.9 分 · 現有 70.7"
        assert page.score_delta_label.isVisible()
        assert not page.reason_label.isVisible()
        assert not page.breakdown_label.isVisible()
        page.breakdown_button.click()
        assert page.reason_label.isVisible()
        assert "63500" in page.reason_label.text()
        assert "通用門檻參考: 及格 2%" in page.breakdown_label.text()
        page.breakdown_button.click()
        page.apply_recommendation(RecommendationView())
        assert not page.score_delta_label.isVisible()
        assert "70.68" not in page.probability_label.toolTip()
        assert not page.reason_label.isVisible()
    finally:
        page.close()


@pytest.mark.parametrize(
    ("probability", "display"),
    [(0.00001, "<0.1%"), (0.99999, ">99.9%"), (0.0, "0%"), (1.0, "100%")],
)
def test_target_probability_preserves_tiny_nonzero_chances(
    qapp: QApplication, probability: float, display: str
) -> None:
    del qapp
    page = _page()
    try:
        page.apply_recommendation(
            RecommendationView(target_score=75.0, target_probability=probability)
        )
        assert page.probability_label.text() == f"超越現有機率 {display}"
    finally:
        page.close()


def test_live_page_emits_audit_ready_correction_and_disposition(
    qapp: QApplication,
) -> None:
    page = _page()
    corrections: list[CorrectionRequest] = []
    dispositions: list[tuple[str, str]] = []
    character_links: list[tuple[str, str, int]] = []
    navigation: list[int] = []
    page.correction_requested.connect(corrections.append)
    page.disposition_requested.connect(
        lambda session_id, disposition: dispositions.append((session_id, disposition))
    )
    page.character_link_requested.connect(
        lambda session_id, character_id, slot_index: character_links.append(
            (session_id, character_id, slot_index)
        )
    )
    page.session_navigation_requested.connect(navigation.append)
    page.show()
    qapp.processEvents()
    page.apply_observation(_observation())

    page.select_result_character("character-1")
    page.submit_correction("/sub_stats/0/value", "8.1")
    qapp.processEvents()
    assert corrections == [
        CorrectionRequest(
            "echo-session-0001", "observation-0004", "/sub_stats/0/value", "7.5", "8.1"
        )
    ]

    page.disposition_combo.setCurrentIndex(page.disposition_combo.findData("kept"))
    QTest.mouseClick(page.disposition_button, Qt.MouseButton.LeftButton)
    assert dispositions == [("echo-session-0001", "kept")]
    QTest.mouseClick(page.discard_button, Qt.MouseButton.LeftButton)
    assert dispositions[-1] == ("echo-session-0001", "discarded")
    QTest.mouseClick(page.link_character_button, Qt.MouseButton.LeftButton)
    assert character_links == [("echo-session-0001", "character-1", 3)]
    page.apply_disposition("equipped")
    assert page.result_status_label.property("disposition") == "equipped"
    assert "已處理" in page.result_status_label.text()

    previous = page.findChild(type(page.start_button), "EchoPreviousButton")
    next_button = page.findChild(type(page.start_button), "EchoNextButton")
    assert previous is not None and next_button is not None
    QTest.mouseClick(previous, Qt.MouseButton.LeftButton)
    QTest.mouseClick(next_button, Qt.MouseButton.LeftButton)
    assert navigation == [-1, 1]
    page.close()


def test_live_page_explains_strict_and_possible_revisits(qapp: QApplication) -> None:
    del qapp
    page = _page()
    page.apply_observation(_observation())

    page.apply_revisit_notice("reused")
    assert "接回原紀錄" in page.result_status_label.text()
    assert "沒有重複建立" in page.result_status_label.text()

    page.apply_revisit_notice("possible")
    assert "證據不足" in page.result_status_label.text()
    assert "分成新的紀錄" in page.result_status_label.text()
    page.close()


def test_live_page_rejects_untrusted_dto_values(qapp: QApplication) -> None:
    del qapp
    with pytest.raises(ValueError, match="confidence"):
        CaptureStatusView(CaptureState.STABLE, confidence=1.01)
    with pytest.raises(ValueError, match="level"):
        EchoObservationView("session", "observation", None, None, None, 30)


def test_live_page_without_monitor_cannot_start(qapp: QApplication) -> None:
    page = EchoLivePage()
    page.show()
    qapp.processEvents()
    assert not page.start_button.isEnabled()
    assert page.monitor_combo.currentText() == "尚未偵測顯示器"
    page.close()


def test_owner_suggestion_waits_for_roster_and_preserves_manual_choice(qapp: QApplication) -> None:
    page = EchoLivePage()
    try:
        observation = replace(
            _observation(), equipped_character_name="愛彌斯", equipped_slot_index=None
        )
        page.apply_observation(observation)
        assert "愛彌斯" in page.game_equipped_label.text()
        assert page.result_slot_combo.currentData() == 0
        page.set_characters((ChoiceOption("new", "愛彌斯"), ChoiceOption("other", "秧秧")))
        assert page.result_character_combo.currentData() == "new"
        assert not page.link_character_button.isEnabled()
        page.select_result_character("other")
        page.result_slot_combo.setCurrentIndex(2)
        page.apply_observation(observation)
        page.set_characters((ChoiceOption("new", "愛彌斯"), ChoiceOption("other", "秧秧")))
        assert page.result_character_combo.currentData() == "other"
        assert page.result_slot_combo.currentData() == 2
        page.apply_observation(
            replace(observation, session_id="echo-session-0002", equipped_character_name=None)
        )
        assert page.result_character_combo.currentData() == ""
        assert page.result_slot_combo.currentData() == 0
    finally:
        page.close()


def test_keep_with_slot_saves_assignment_and_saved_slot_is_restored(qapp: QApplication) -> None:
    page = _page()
    links: list[tuple[str, str, int]] = []
    dispositions: list[tuple[str, str]] = []
    page.character_link_requested.connect(lambda *args: links.append(args))
    page.disposition_requested.connect(lambda *args: dispositions.append(args))
    try:
        observation = replace(_observation(), equipped_slot_index=None)
        page.apply_observation(observation)
        page.keep_button.click()
        assert dispositions == [(observation.session_id, "kept")]
        page.select_result_character("character-1")
        page.result_slot_combo.setCurrentIndex(3)
        assert page.keep_button.text() == "保留並套用位置"
        page.keep_button.click()
        assert links == [(observation.session_id, "character-1", 3)]
        assert len(dispositions) == 1
        saved = replace(observation, assigned_character_id="character-1", assigned_slot_index=3)
        page.apply_observation(saved)
        page.apply_disposition("equipped")
        assert "測試角色 · 位置 3" in page.result_status_label.text()
        assert "不必重選" in page.result_status_label.text()
        page.result_slot_combo.setCurrentIndex(4)
        page.apply_observation(saved)
        assert page.result_slot_combo.currentData() == 4
        page.apply_observation(replace(observation, session_id="echo-session-0002"))
        page.apply_observation(saved)
        assert page.result_character_combo.currentData() == "character-1"
        assert page.result_slot_combo.currentData() == 3
    finally:
        page.close()


def test_recognized_unsaved_slot_can_be_saved_while_analysis_is_pending(qapp: QApplication) -> None:
    page = _page()
    links: list[tuple[str, str, int]] = []
    page.character_link_requested.connect(lambda *args: links.append(args))
    try:
        observation = replace(
            _observation(),
            equipped_character_name="測試角色",
            equipped_slot_index=5,
            assigned_character_id="character-1",
        )
        page.apply_observation(observation)
        page.apply_disposition("equipped")
        page.set_analysis_pending(True)
        assert page.result_slot_combo.currentData() == 5
        assert "已辨識位置 5" in page.game_equipped_label.text()
        assert "尚未保存位置" in page.result_status_label.text()
        assert page.link_character_button.isEnabled()
        page.link_character_button.click()
        assert links == [(observation.session_id, "character-1", 5)]
        # Repeated OCR must not undo an explicit unsaved slot correction.
        page.result_slot_combo.setCurrentIndex(4)
        page.apply_observation(observation)
        assert page.result_slot_combo.currentData() == 4
        page.apply_observation(replace(observation, assigned_slot_index=5))
        page.apply_disposition("equipped")
        assert "已同步位置 5" in page.game_equipped_label.text()
        assert "已保存至 測試角色 · 位置 5" in page.result_status_label.text()
    finally:
        page.close()


@pytest.mark.parametrize("saved", [False, True])
def test_archived_choice_does_not_silently_assign_another_character(
    qapp: QApplication, saved: bool
) -> None:
    page = _page()
    try:
        page.set_characters(
            (ChoiceOption("character-1", "測試角色"), ChoiceOption("other", "秧秧"))
        )
        page.apply_observation(
            replace(
                _observation(),
                assigned_character_id="character-1" if saved else None,
                assigned_slot_index=3 if saved else None,
            )
        )
        page.select_result_character("character-1")
        assert page.link_character_button.isEnabled()
        page.set_characters((ChoiceOption("other", "秧秧"),))
        assert page.result_character_combo.currentData() == ""
        assert not page.link_character_button.isEnabled()
        assert page.keep_button.text() == "保留這顆"
    finally:
        page.close()
