from __future__ import annotations

from dataclasses import replace
from math import fsum
from threading import Event

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QPushButton
from sqlalchemy import select

from tests.application.test_combat_replacement import candidate, registered_build
from tests.application.test_equipment_overview import equipment as equipment_fixture
from tests.gui.helpers import MemorySettings
from tests.gui.test_echo_workspace_presenter import _FakeLiveController, _recognition
from tests.gui.test_tacet_stock import stock_snapshot
from wuwa_assistant.application import EchoHistoryService
from wuwa_assistant.application.characters import CharacterCultivationService
from wuwa_assistant.application.combat_build import CombatTeam
from wuwa_assistant.application.combat_service import CombatBuildService, CombatSettings
from wuwa_assistant.application.planning import CultivationPlanningService
from wuwa_assistant.application.replacement_training import ReplacementTrainingService
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.application.tacet_catalog import load_tacet_catalog
from wuwa_assistant.application.tacet_stock import TacetStockService
from wuwa_assistant.display import DISPLAY_MODE_KEY
from wuwa_assistant.planning.farming import (
    FarmCharacter,
    FarmEcho,
    FarmRole,
    FarmZone,
    recommend_zones,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import CaptureSessionRecord
from wuwa_assistant.ui.controllers.display_mode import DisplayModeController
from wuwa_assistant.ui.controllers.echo_workspace import EchoWorkspacePresenter
from wuwa_assistant.ui.controllers.scoring_profiles import replacement_to_echo_recommendation
from wuwa_assistant.ui.controllers.tacet_stock import TacetStockPresenter
from wuwa_assistant.ui.controllers.today_plan import TodayPlanPresenter
from wuwa_assistant.ui.echo_panel import EchoPanel
from wuwa_assistant.ui.main_window import MainWindow
from wuwa_assistant.ui.navigation import PageId
from wuwa_assistant.ui.pages.echo_live import (
    CaptureState,
    CaptureStatusView,
    EchoLivePage,
    EchoObservationView,
)
from wuwa_assistant.ui.pages.today_plan import TacetStockEditRequest
from wuwa_assistant.ui.widgets.farming_reasons import reason_rows


@pytest.fixture
def equipment(tmp_path):
    yield from equipment_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize("passing", [60, 70, 80])
def test_grouped_alternatives_use_exact_existing_shares_and_leave_ranking_unchanged(passing):
    character = FarmCharacter(
        "c",
        "角色",
        FarmRole.MAIN,
        "主C",
        ("A", "B"),
        (
            FarmEcho(2, 3, 25, 40, "A", True, True, target_sets=("A", "B")),
            FarmEcho(3, 3, 25, 65, "B", True, True, target_sets=("A", "B")),
        ),
        passing_score=passing,
        check_set_combination=False,
    )
    plan = recommend_zones((character,), (FarmZone("field", "區域", "地區", ("A", "B")),))
    zone = plan.recommendations[0]
    before = repr(plan)
    rows = reason_rows(zone)
    assert len(rows) == 2
    for row in rows:
        slot = int(row[2][1:-1])
        benefits = [b for b in zone.benefits if b.slot_index == slot]
        assert len(benefits) == 2
        assert row[4] == f"{fsum(b.points for b in benefits):.3f}"
        if (40 if slot == 2 else 65) < passing:
            assert f"差 {passing - (40 if slot == 2 else 65)} 分" in row[3]
    assert float(rows[0][4]) + float(rows[1][4]) == pytest.approx(
        zone.weighted_base_points, abs=0.001
    )
    assert repr(plan) == before


def stock_button(dialog, field):
    # setCellWidget schedules the previous QObject for deferred deletion. Search
    # the actual view, not detached widgets still present in QObject.findChild.
    for row in range(dialog.table.rowCount()):
        button = dialog.table.cellWidget(row, 2)
        if isinstance(button, QPushButton) and button.objectName() == f"TacetStock_{field}":
            return button
    raise AssertionError(f"No visible stock control for {field}")


def test_shared_stock_entries_refresh_plan_dashboard_failure_bulk_and_reopen(
    qtbot, tmp_path, monkeypatch
):
    database = create_database(AppPaths.from_root(tmp_path))
    window = MainWindow(MemorySettings())
    qtbot.addWidget(window)
    service = TacetStockService(database.settings)
    editor = TacetStockPresenter(service, "primary", parent=window)
    monkeypatch.setattr(
        "wuwa_assistant.ui.controllers.today_plan.build_today_farming",
        lambda *a, stocked_field_ids, **kw: stock_snapshot(stocked_field_ids),
    )
    planner = TodayPlanPresenter(
        planning_service=CultivationPlanningService(database.session_factory),
        character_service=CharacterCultivationService(database.session_factory),
        scoring_profiles=ScoringProfileService(database.session_factory),
        settings=database.settings,
        page=window.today_plan_page,
        data_set_id="primary",
        tacet_stock=editor,
    )
    editor.changed.connect(planner.refresh)
    editor.busy_changed.connect(window.today_plan_page.set_loading)
    window.characters_page.equipment_workspace.tacet_stock_requested.connect(editor.open)
    window.echo_page.tacet_stock_requested.connect(editor.open)
    updates = []
    planner.dashboard_priority_changed.connect(updates.append)
    changed = []
    editor.changed.connect(lambda: changed.append(True))
    try:
        planner.initialize()
        qtbot.waitUntil(lambda: len(updates) == 1)
        field = stock_snapshot().farming.recommendations[0].zone.identifier
        other_dataset_before = service.stocked_field_ids("other", load_tacet_catalog())
        window.characters_page.equipment_workspace.tacet_stock_button.click()
        qtbot.waitUntil(lambda: editor.dialog is not None and not editor._busy)
        dialog = editor.dialog
        button = stock_button(dialog, field)
        assert button.text() == "還有可培養"
        button.click()
        assert not dialog.table.isEnabled()
        qtbot.waitUntil(lambda: len(changed) == 1 and not editor._busy)
        qtbot.waitUntil(lambda: len(updates) == 2)
        assert "中曲臺地" in updates[-1]
        assert not service.stocked_field_ids("primary", load_tacet_catalog()).issuperset({field})
        dialog.close()
        window.echo_page.tacet_stock_button.click()
        qtbot.waitUntil(lambda: not editor._busy)
        assert editor.dialog is dialog
        assert stock_button(dialog, field).text() == "需要的已用完"
        stock_button(dialog, field).click()
        qtbot.waitUntil(lambda: len(changed) == 2 and not editor._busy)
        dialog.close()
        window.characters_page.equipment_workspace.tacet_stock_button.click()
        qtbot.waitUntil(lambda: not editor._busy)
        assert stock_button(dialog, field).text() == "還有可培養"
        with monkeypatch.context() as patch:

            def fail(*a, **kw):
                raise OSError("synthetic storage failure")

            patch.setattr(service, "save", fail)
            stock_button(dialog, field).click()
            qtbot.waitUntil(lambda: not editor._busy)
            assert "synthetic storage failure" in dialog.status.text()
            assert stock_button(dialog, field).text() == "還有可培養"
            assert len(changed) == 2
        dialog.all_exhausted.click()
        qtbot.waitUntil(lambda: len(changed) == 3 and not editor._busy)
        assert not service.stocked_field_ids("primary", load_tacet_catalog())
        # The existing planner row/bulk entry uses the very same writer.
        planner.update_tacet_stock(TacetStockEditRequest((field,), True))
        qtbot.waitUntil(lambda: len(changed) == 4 and not editor._busy)
        assert stock_button(dialog, field).text() == "還有可培養"
        assert field in service.stocked_field_ids("primary", load_tacet_catalog())
        assert service.stocked_field_ids("other", load_tacet_catalog()) == other_dataset_before
    finally:
        editor.shutdown()
        planner.shutdown()
        window.close()
        database.dispose()
    with create_database(AppPaths.from_root(tmp_path)) as reopened:
        assert TacetStockService(reopened.settings).stocked_field_ids(
            "primary", load_tacet_catalog()
        ) == frozenset({field})


def test_stock_double_click_is_single_accepted_write(qtbot, tmp_path, monkeypatch):
    with create_database(AppPaths.from_root(tmp_path)) as database:
        service = TacetStockService(database.settings)
        editor = TacetStockPresenter(service, "primary")
        gate, entered = Event(), Event()
        original = service.save
        calls = []

        def slow(*a, **kw):
            calls.append(1)
            entered.set()
            assert gate.wait(5)
            return original(*a, **kw)

        monkeypatch.setattr(service, "save", slow)
        try:
            editor.open()
            qtbot.waitUntil(lambda: not editor._busy)
            request = TacetStockEditRequest((load_tacet_catalog().fields[0].field_id,), False)
            editor.save(request)
            qtbot.waitUntil(entered.is_set)
            editor.save(request)
            editor.open()
            gate.set()
            qtbot.waitUntil(lambda: not editor._busy)
            assert calls == [1]
        finally:
            gate.set()
            editor.shutdown()


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("route", ["sidebar", "programmatic", "back"])
def test_leaving_enhancement_stops_pending_and_active_manual_or_auto_capture(
    qtbot, tmp_path, monkeypatch, pending, manual, route
):
    database = create_database(AppPaths.from_root(tmp_path))
    history = EchoHistoryService(database.session_factory)
    window = MainWindow(MemorySettings({DISPLAY_MODE_KEY: "single"}))
    qtbot.addWidget(window)
    window.navigate_to(PageId.EQUIPMENT)
    window.navigate_to(PageId.ECHO_ENHANCEMENT)
    live = _FakeLiveController()
    presenter = EchoWorkspacePresenter(
        live_controller=live,
        history_service=history,
        echo_page=window.echo_page,
        history_page=window.history_page,
        app_version="test",
    )
    display = DisplayModeController(
        presenter=presenter,
        page=window.echo_page,
        settings_page=window.settings_page,
        settings=MemorySettings({DISPLAY_MODE_KEY: "single"}),
        panel_factory=lambda: EchoPanel(protect=lambda: None),
    )
    # Avoid requiring a physical 1440p monitor for the synthetic native panel.
    monkeypatch.setattr(display, "prepare_capture", lambda monitor: True)
    presenter.capture_guard = lambda monitor: True
    window.echo_enhancement_left.connect(presenter.stop_capture)
    window.echo_enhancement_left.connect(display.end_panel)
    original = history.start_capture
    gate, entered = Event(), Event()

    def delayed(**kw):
        entered.set()
        assert gate.wait(5)
        return original(**kw)

    if pending:
        monkeypatch.setattr(history, "start_capture", delayed)
    else:
        gate.set()
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: window.echo_page.monitor_combo.currentData() == "monitor-1")
        window.echo_page.select_capture_mode("manual" if manual else "auto")
        window.echo_page.start_button.click()
        if pending:
            qtbot.waitUntil(entered.is_set)
        else:
            qtbot.waitUntil(lambda: live.start_count == 1)
            live.recognition_ready.emit(_recognition())
            qtbot.waitUntil(lambda: len(history.list_history()) == 1)
        display.panel = EchoPanel(protect=lambda: None)
        display.panel.start(QRect(0, 0, 1200, 800), "top_left")
        assert display.panel.isVisible()
        # Selecting the same assistant page is not a departure.
        window.navigate_to(PageId.ECHO_ENHANCEMENT)
        assert live.stop_count == 0
        if route == "back":
            window.go_back()
        elif route == "sidebar":
            index = window.navigation_model.index_for(PageId.DASHBOARD)
            window.navigation_view.setCurrentIndex(index)
        else:
            window.navigate_to(PageId.CHARACTERS)
        assert live.stop_count == 1
        assert not display.panel.isVisible()
        # Old queued payloads and rapid return/departure cannot resurrect capture.
        live.recognition_ready.emit(replace(_recognition(), source_sequence=999))
        for _ in range(3):
            window.navigate_to(PageId.ECHO_ENHANCEMENT)
            window.navigate_to(PageId.DASHBOARD)
        gate.set()
        qtbot.waitUntil(lambda: not presenter._futures)
        window.navigate_to(PageId.ECHO_ENHANCEMENT)
        qtbot.wait(30)
        assert live.start_count == (0 if pending else 1)
        assert presenter._active_capture_id is None
        assert len(history.list_history()) == (0 if pending else 1)
        assert window.echo_page._capture_state is CaptureState.STOPPED
        with database.session_factory() as session:
            assert all(
                row.status == "stopped" for row in session.scalars(select(CaptureSessionRecord))
            )
    finally:
        gate.set()
        display.shutdown()
        presenter.shutdown()
        window.close()
        database.dispose()


@pytest.mark.parametrize("level", [0, 4, 5, 10, 15, 20, 24, 25])
@pytest.mark.parametrize("same", [False, True])
def test_unknown_combat_preserves_quality_decision_in_workspace_and_panel(
    qtbot, equipment, level, same
):
    detail = registered_build(equipment)
    CombatBuildService(equipment.database.session_factory).save(
        equipment.data_set_id,
        CombatSettings(
            detail.summary.id,
            (CombatTeam("primary", "未確認隊伍", energy_target=145, confirmed=False),),
            "primary",
        ),
    )
    payload = candidate(detail)
    payload["level"] = level
    payload["sub_stats"] = payload["sub_stats"][: level // 5]
    value = ReplacementTrainingService(equipment.database.session_factory).evaluate(
        payload,
        data_set_id=equipment.data_set_id,
        character_id=detail.summary.id,
        slot_index=5,
        candidate_echo_id=detail.echo_slots[4].echo_id if same else None,
    )
    view = replacement_to_echo_recommendation(value)
    assert value.quality_advice_available
    if value.combat is not None:
        assert value.combat.after.sufficient is None
        assert value.quality_only
        assert "依聲骸品質評估" in view.save_advice
        assert "依聲骸品質評估" in view.upgrade_advice
        assert "尚未驗證" in view.reason
        assert view.save_advice != "戰鬥資料待補"
    else:
        assert level < 5
        assert "+15" in view.conclusion
    assert view.score == value.evaluation.breakdown.score
    page = EchoLivePage()
    qtbot.addWidget(page)
    page.apply_capture_status(CaptureStatusView(CaptureState.STABLE))
    page.apply_analysis_result(EchoObservationView("s", "o", "候選", "套裝", 3, level), view)
    assert view.save_advice in page.save_advice_label.text()
    assert page.panel_snapshot().save_advice == view.save_advice
    assert page.panel_snapshot().upgrade_advice == (view.upgrade_advice or view.conclusion)


@pytest.mark.parametrize("missing", ["review", "level", "cost", "set", "main", "subs"])
def test_quality_fallback_does_not_bypass_necessary_data_or_equipment_gates(equipment, missing):
    detail = registered_build(equipment)
    CombatBuildService(equipment.database.session_factory).save(
        equipment.data_set_id,
        CombatSettings(
            detail.summary.id,
            (CombatTeam("primary", "未確認隊伍", energy_target=145, confirmed=False),),
            "primary",
        ),
    )
    payload = candidate(detail)
    if missing == "level":
        payload["level"] = None
    if missing == "cost":
        payload["cost"] = 4
    if missing == "set":
        payload["set_name"] = "不符套裝"
    if missing == "main":
        payload["main_stats"] = []
    if missing == "subs":
        payload["sub_stats"] = []
    value = ReplacementTrainingService(equipment.database.session_factory).evaluate(
        payload,
        data_set_id=equipment.data_set_id,
        character_id=detail.summary.id,
        slot_index=5,
        requires_review=missing == "review",
    )
    assert not value.quality_advice_available
    assert not value.quality_only
    assert value.combat is None
    view = replacement_to_echo_recommendation(value)
    assert "可考慮替換" not in view.conclusion
    assert "依聲骸品質評估" not in view.save_advice
    assert view.save_advice != "戰鬥資料待補"
    assert view.upgrade_advice != "戰鬥資料待補"
    if missing == "level":
        assert view.save_advice == "資料待補"
        assert view.upgrade_advice == "資料待補"


def test_committed_stock_with_failed_readback_requires_reload_before_more_edits(
    qtbot, tmp_path, monkeypatch
):
    with create_database(AppPaths.from_root(tmp_path)) as database:
        service = TacetStockService(database.settings)
        editor = TacetStockPresenter(service, "primary")
        changed = []
        editor.changed.connect(lambda: changed.append(True))
        try:
            editor.open()
            qtbot.waitUntil(lambda: not editor._busy)
            field = load_tacet_catalog().fields[0].field_id
            before = stock_button(editor.dialog, field).text()
            with monkeypatch.context() as patch:
                patch.setattr(
                    service,
                    "stocked_field_ids",
                    lambda *a: (_ for _ in ()).throw(OSError("readback failed")),
                )
                editor.save(TacetStockEditRequest((field,), False))
                qtbot.waitUntil(lambda: not editor._busy)
                assert len(changed) == 1
                assert "readback failed" in editor.dialog.status.text()
                assert not editor.dialog.table.isEnabled()
                assert stock_button(editor.dialog, field).text() == before
            editor.open()
            qtbot.waitUntil(lambda: not editor._busy)
            assert editor.dialog.table.isEnabled()
            assert stock_button(editor.dialog, field).text() == "需要的已用完"
            assert field not in service.stocked_field_ids("primary", load_tacet_catalog())
        finally:
            editor.shutdown()
