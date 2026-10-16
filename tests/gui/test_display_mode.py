from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest

from tests.gui.helpers import MemorySettings
from tests.gui.test_echo_workspace_presenter import _FakeLiveController, _recognition
from wuwa_assistant.app.main import DatabaseSettingsStore
from wuwa_assistant.application import (
    EchoHistoryService,
    ScoringProfileService,
    ScoringWorkflowService,
)
from wuwa_assistant.display import (
    DISPLAY_MODE_KEY,
    PANEL_POSITION_KEY,
    PANEL_PRESET_KEY,
    RESOLUTION_KEY,
)
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.ui.controllers.display_mode import DisplayModeController
from wuwa_assistant.ui.controllers.echo_workspace import EchoWorkspacePresenter
from wuwa_assistant.ui.echo_panel import EchoPanel, clamped_position, panel_position
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.navigation import PageId
from wuwa_assistant.ui.pages.echo_live import (
    CaptureState,
    CaptureStatusView,
    CorrectionRequest,
    EchoLivePage,
    EchoObservationView,
    RecommendationView,
)
from wuwa_assistant.ui.pages.history import EchoHistoryPage
from wuwa_assistant.ui.pages.onboarding import MonitorChoice, OnboardingWizard
from wuwa_assistant.ui.pages.settings import SettingsPage
from wuwa_assistant.ui.panel_model import retention_advice


def test_first_run_choices_and_resolution_change_invalidate_capture_test(qtbot, tmp_path) -> None:
    wizard = OnboardingWizard(data_path=tmp_path, monitors=(MonitorChoice("monitor-1", "螢幕"),))
    qtbot.addWidget(wizard)
    wizard.display_mode_combo.setCurrentIndex(wizard.display_mode_combo.findData("single"))
    wizard.monitor_page.resolution_combo.setCurrentIndex(0)
    wizard.set_capture_test_result("monitor-1", True, "通過", "1920x1080")
    request = wizard.request()
    assert request.display_mode == "single" and request.resolution == "1920x1080"
    wizard.monitor_page.resolution_combo.setCurrentIndex(1)
    wizard.set_capture_test_result("monitor-1", True, "舊結果", "1920x1080")
    assert not wizard.monitor_page.isComplete()
    with pytest.raises(ValueError, match="測試"):
        wizard.request()


def test_display_preferences_survive_database_reopen_and_modes_share_data(
    tmp_path: Path, qtbot
) -> None:
    paths = AppPaths.from_root(tmp_path)
    with create_database(paths) as database:
        settings = DatabaseSettingsStore(database.settings)
        data_set = EchoHistoryService(database.session_factory).ensure_active_primary_dataset()
        page = SettingsPage(settings)
        qtbot.addWidget(page)
        page.display_mode_combo.setCurrentIndex(page.display_mode_combo.findData("single"))
        page.resolution_combo.setCurrentIndex(0)
        page.panel_preset_combo.setCurrentIndex(page.panel_preset_combo.findData("bottom_right"))
        page.save()
        page.display_mode_combo.setCurrentIndex(page.display_mode_combo.findData("dual"))
        page.save()
    with create_database(paths) as database:
        assert database.settings.get(DISPLAY_MODE_KEY) == "dual"
        assert database.settings.get(RESOLUTION_KEY) == "1920x1080"
        assert database.settings.get(PANEL_PRESET_KEY) == "bottom_right"
        assert (
            EchoHistoryService(database.session_factory).ensure_active_primary_dataset() == data_set
        )
    assert AppPaths.default().root.name == "WuwaDisplayAssistant"


def test_panel_reuses_scores_marks_stale_and_withholds_missing_data(qtbot) -> None:
    page = EchoLivePage()
    qtbot.addWidget(page)
    observation = EchoObservationView("session", "observation", "測試聲骸", "套裝", 3, 15)
    recommendation = RecommendationView(
        score=61.2, target_score=72.5, save_advice="暫留", upgrade_advice="可到 +20"
    )
    page.apply_capture_status(CaptureStatusView(CaptureState.STABLE))
    page.apply_analysis_result(observation, recommendation)
    snapshot = page.panel_snapshot()
    assert (snapshot.score, snapshot.target_score) == (61.2, 72.5)
    assert (snapshot.save_advice, snapshot.upgrade_advice) == ("暫留", "可到 +20")
    assert not snapshot.stale
    page.apply_capture_status(CaptureStatusView(CaptureState.RECOGNIZING))
    assert page.panel_snapshot().stale
    # A stable capture event alone must not make the previous analysis current.
    page.apply_capture_status(CaptureStatusView(CaptureState.STABLE))
    assert page.panel_snapshot().stale
    page.apply_analysis_result(replace(observation, needs_confirmation=True), recommendation)
    snapshot = page.panel_snapshot()
    assert snapshot.missing_fields and snapshot.score is None
    assert "資料不足" in snapshot.upgrade_advice
    page.apply_analysis_result(observation, recommendation)
    assert page.panel_snapshot().score == 61.2
    assert not page.panel_snapshot().missing_fields


def test_panel_stays_visible_waiting_and_only_modifier_drag_moves_it(qtbot) -> None:
    modifiers = [Qt.KeyboardModifier.NoModifier]
    panel = EchoPanel(protect=lambda: None, modifiers=lambda: modifiers[0])
    qtbot.addWidget(panel)
    bounds = QRect(-1200, 0, 1200, 700)
    panel.start(bounds, "top_left")
    original = panel.pos()
    QTest.mousePress(panel, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    QTest.mouseMove(panel, QPoint(70, 60))
    QTest.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=QPoint(70, 60))
    assert panel.pos() == original
    modifiers[0] = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    panel.poll_modifiers()
    assert panel.interactive
    QTest.mousePress(panel, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    QTest.mouseMove(panel, QPoint(70, 60))
    QTest.mouseRelease(panel, Qt.MouseButton.LeftButton, pos=QPoint(70, 60))
    assert panel.pos() != original
    modifiers[0] = Qt.KeyboardModifier.NoModifier
    panel.poll_modifiers()
    assert not panel.interactive
    panel.close()
    assert panel.isVisible()
    panel.end()
    assert not panel.isVisible()
    assert clamped_position(QPoint(9000, -8000), bounds, (330, 300)) == QPoint(-330, 0)
    assert bounds.contains(QRect(panel_position(bounds, (330, 300), "bottom_right"), panel.size()))


def test_mode_switch_stops_one_session_and_keeps_sqlite_records(qtbot, tmp_path, request) -> None:
    database = create_database(AppPaths.from_root(tmp_path))
    request.addfinalizer(database.dispose)
    history = EchoHistoryService(database.session_factory)
    data_set = history.ensure_active_primary_dataset()
    settings = MemorySettings({DISPLAY_MODE_KEY: "single"})
    page, settings_page, live = EchoLivePage(), SettingsPage(settings), _FakeLiveController()
    qtbot.addWidget(page)
    qtbot.addWidget(settings_page)
    presenter = EchoWorkspacePresenter(
        live_controller=live,
        history_service=history,
        echo_page=page,
        history_page=EchoHistoryPage(),
        settings_page=settings_page,
        data_set_id=data_set,
        app_version="test",
    )
    request.addfinalizer(presenter.shutdown)
    controller = DisplayModeController(
        presenter=presenter,
        page=page,
        settings_page=settings_page,
        settings=settings,
        panel_factory=lambda: EchoPanel(protect=lambda: None),
    )
    request.addfinalizer(controller.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: bool(page.monitor_combo.currentData()))
    page.start_button.click()
    qtbot.waitUntil(lambda: live.start_count == 1)
    assert controller.panel is not None and controller.panel.isVisible()
    page.apply_capture_status(CaptureStatusView(CaptureState.WAITING_FOR_PAGE))
    qtbot.wait(20)
    assert controller.panel.isVisible()
    controller._save_position(QPoint(30, 40))
    assert settings.get(PANEL_POSITION_KEY) is not None
    controller.apply_settings({DISPLAY_MODE_KEY: "dual"})
    qtbot.waitUntil(lambda: page.start_button.isEnabled())
    assert live.stop_count == 1
    assert not controller.panel.isVisible()
    assert history.ensure_active_primary_dataset() == data_set
    page.start_button.click()
    qtbot.waitUntil(lambda: live.start_count == 2)
    assert not controller.panel.isVisible()
    page.stop_button.click()
    qtbot.waitUntil(lambda: live.stop_count == 2)


def test_manual_confirmation_recomputes_real_score_in_both_views(
    qtbot, tmp_path, request, monkeypatch
) -> None:
    database = create_database(AppPaths.from_root(tmp_path))
    request.addfinalizer(database.dispose)
    history = EchoHistoryService(database.session_factory)
    data_set = history.ensure_active_primary_dataset()
    profiles = ScoringProfileService(database.session_factory)
    profiles.ensure_system_templates(load_template_seed_definitions())
    profile = profiles.get_default(data_set_id=data_set)
    assert profile is not None
    settings = MemorySettings({DISPLAY_MODE_KEY: "single"})
    page, settings_page, live = EchoLivePage(), SettingsPage(settings), _FakeLiveController()
    qtbot.addWidget(page)
    qtbot.addWidget(settings_page)
    presenter = EchoWorkspacePresenter(
        live_controller=live,
        history_service=history,
        echo_page=page,
        history_page=EchoHistoryPage(),
        settings_page=settings_page,
        scoring_workflow=ScoringWorkflowService(database.session_factory, profiles),
        data_set_id=data_set,
        app_version="test",
    )
    request.addfinalizer(presenter.shutdown)
    controller = DisplayModeController(
        presenter=presenter,
        page=page,
        settings_page=settings_page,
        settings=settings,
        panel_factory=lambda: EchoPanel(protect=lambda: None),
    )
    request.addfinalizer(controller.shutdown)
    presenter.initialize()
    qtbot.waitUntil(lambda: page.start_button.isEnabled())
    page.start_requested.emit("monitor-1", "", profile.id)
    qtbot.waitUntil(lambda: live.start_count == 1)
    recognition = _recognition()
    payload = deepcopy(recognition.observation)
    payload["sub_stats"][0]["confidence"] = 0.60
    payload["sub_stats"][0]["value_confidence"] = 0.60
    live.recognition_ready.emit(replace(recognition, observation=payload))
    qtbot.waitUntil(lambda: bool(page.panel_snapshot().missing_fields), timeout=30_000)
    assert page.panel_snapshot().score is None
    assert controller.panel is not None
    qtbot.waitUntil(lambda: "修正 OCR" in controller.panel.detail_label.text())
    entry = history.list_history()[0]
    observation = history.get_history(entry.id).observations[-1]
    presenter.correct_observation(
        CorrectionRequest(
            session_id="echo-session-0001",
            observation_id=observation.id,
            field_path="/sub_stats/0/value",
            previous_value="7.5",
            corrected_value="7.5",
        )
    )
    qtbot.waitUntil(lambda: page.panel_snapshot().score is not None, timeout=30_000)
    qtbot.waitUntil(lambda: controller.panel.score_label.text() != "—", timeout=30_000)
    snapshot = page.panel_snapshot()
    assert not snapshot.missing_fields
    assert float(controller.panel.score_label.text()) == pytest.approx(snapshot.score, abs=0.05)
    saved = history.get_history(entry.id).observations[-1]
    assert saved.raw_payload["sub_stats"][0]["value_confidence"] == 0.60
    assert "/sub_stats/0/value" in saved.effective_payload["manual_confirmed_fields"]

    # Leaving and returning to the same echo schedules a semantic duplicate,
    # which must refresh the presentation without another observation or score.
    page.apply_capture_status(CaptureStatusView(CaptureState.STABLE))
    assert not page.panel_snapshot().stale
    page.apply_capture_status(CaptureStatusView(CaptureState.WAITING_FOR_PAGE))
    page.apply_capture_status(CaptureStatusView(CaptureState.STABLE))
    assert page.panel_snapshot().stale
    other_echo = deepcopy(payload)
    other_echo["current_session"]["local_session_id"] = "echo-session-0002"
    presenter._handle_recognition(
        replace(recognition, source_sequence=11, unchanged=True, observation=other_echo)
    )
    assert page.panel_snapshot().stale
    live.recognition_ready.emit(
        replace(recognition, source_sequence=12, unchanged=True, observation=payload)
    )
    qtbot.waitUntil(lambda: not page.panel_snapshot().stale)
    assert page.panel_snapshot().score == snapshot.score
    assert len(history.get_history(entry.id).observations) == 1

    # Hold the next database ingest: a completed old score or a same-session
    # duplicate must not relabel the old level as the newly observed level.
    with monkeypatch.context() as patch:
        patch.setattr(presenter, "_submit", lambda *args, **kwargs: None)
        upgraded = deepcopy(payload)
        upgraded["level"] = 10
        presenter._handle_recognition(
            replace(recognition, source_sequence=13, observation=upgraded)
        )
        presenter._publish_analysis(page._last_recommendation)
        assert page.panel_snapshot().stale
        presenter._handle_recognition(
            replace(recognition, source_sequence=14, unchanged=True, observation=upgraded)
        )
        assert page.panel_snapshot().stale


@pytest.mark.parametrize("action", ["continue", "stop", "max_level", "cannot_surpass_existing"])
def test_retention_advice_handles_terminal_decisions_without_automatic_discard(action) -> None:
    assert retention_advice(action, qualified=False) != "等待評分"
    assert "保留" in retention_advice(action, qualified=True)


def test_small_logical_desktop_keeps_settings_and_capture_controls_reachable(qtbot) -> None:
    window = MainWindow(settings=MemorySettings())
    qtbot.addWidget(window)
    window.resize(920, 480)
    window.show()
    for page, control in (
        (PageId.SETTINGS, window.settings_page.save_button),
        (PageId.ECHO_ENHANCEMENT, window.echo_page.start_button),
    ):
        window.navigate_to(page)
        qtbot.wait(20)
        window.workspace_scroll.ensureWidgetVisible(control)
        qtbot.wait(20)
        viewport = window.workspace_scroll.viewport()
        assert viewport.rect().contains(control.mapTo(viewport, control.rect().center()))
    assert window.width() == 920 and window.height() == 480


def test_failed_drag_affinity_restores_passthrough_and_can_always_end(qtbot) -> None:
    protections = []

    def protect():
        protections.append(True)
        if len(protections) > 1:
            raise RuntimeError("display changed")

    panel = EchoPanel(
        protect=protect,
        modifiers=lambda: Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    qtbot.addWidget(panel)
    panel.start(QRect(0, 0, 1280, 700), "top_left")
    with qtbot.waitSignal(panel.failed):
        panel.poll_modifiers()
    assert not panel.interactive
    assert panel.windowHandle().flags() & Qt.WindowType.WindowTransparentForInput
    panel.end()
    assert not panel.isVisible()
