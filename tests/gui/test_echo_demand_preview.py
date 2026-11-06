from dataclasses import replace

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox, QPushButton
from sqlalchemy import func, select

from tests.gui.test_echo_workspace_presenter import _FakeLiveController, _recognition
from wuwa_assistant.application import (
    EchoHistoryService,
    ScoringProfileService,
    ScoringWorkflowService,
)
from wuwa_assistant.application.characters import CharacterCultivationService, CreateCharacter
from wuwa_assistant.application.echo_demand import EchoDemandService
from wuwa_assistant.application.echo_sources import load_echo_sources
from wuwa_assistant.application.planning import CultivationPlanningService
from wuwa_assistant.planning.farming import FarmCharacter, FarmEcho, FarmRole, recommend_zones
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import (
    CaptureSessionRecord,
    EchoObservationRecord,
    EchoRecord,
    UpgradeDecisionRecord,
)
from wuwa_assistant.ui.controllers.echo_workspace import EchoWorkspacePresenter
from wuwa_assistant.ui.controllers.today_plan import TodayPlanPresenter
from wuwa_assistant.ui.pages.echo_live import ChoiceOption, CorrectionRequest, EchoLivePage
from wuwa_assistant.ui.pages.equipment_detail import (
    EquipmentDetailPage,
    GearCharacterDetailView,
    GearPieceDetailView,
)
from wuwa_assistant.ui.pages.history import EchoHistoryPage
from wuwa_assistant.ui.pages.today_plan import TodayPlanPage


def test_default_capture_only_persists_explicitly_applied_equipment(qtbot, tmp_path):
    database = create_database(AppPaths.from_root(tmp_path))
    history = EchoHistoryService(database.session_factory)
    dataset = history.ensure_active_primary_dataset()
    profiles = ScoringProfileService(database.session_factory)
    profiles.ensure_system_templates(load_template_seed_definitions())
    characters = CharacterCultivationService(database.session_factory)
    owner = characters.create_character(data_set_id=dataset, command=CreateCharacter("折枝"))
    live = _FakeLiveController()
    page = EchoLivePage()
    history_page = EchoHistoryPage()
    qtbot.addWidget(page)
    qtbot.addWidget(history_page)
    page.set_characters((ChoiceOption(owner.summary.id, "折枝"),))
    presenter = EchoWorkspacePresenter(
        live_controller=live,
        history_service=history,
        echo_page=page,
        history_page=history_page,
        scoring_workflow=ScoringWorkflowService(database.session_factory, profiles),
        data_set_id=dataset,
        app_version="test",
    )

    def records():
        with database.session_factory() as session:
            return tuple(
                session.scalar(select(func.count()).select_from(model))
                for model in (
                    CaptureSessionRecord,
                    EchoObservationRecord,
                    EchoRecord,
                    UpgradeDecisionRecord,
                )
            )

    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: page.monitor_combo.currentData() == "monitor-1")
        page.start_button.click()
        qtbot.waitUntil(lambda: live.start_count == 1)
        assert page.keep_button.isHidden() and page.discard_button.isHidden()
        packet = _recognition()
        raw = {**packet.observation, "set_name": "凝夜白霜"}
        live.recognition_ready.emit(replace(packet, observation=raw))
        qtbot.waitUntil(lambda: page.name_label.text() == "不熄獵手")
        qtbot.waitUntil(lambda: presenter._has_completed_analysis)
        assert records() == (0, 0, 0, 0)
        latest = presenter._current_history.observations[-1]
        presenter.correct_observation(
            CorrectionRequest("echo-session-0001", latest.id, "/name", "不熄獵手", "核對聲骸")
        )
        qtbot.waitUntil(lambda: page.name_label.text() == "核對聲骸")
        assert records() == (0, 0, 0, 0)
        page.organize_toggle.click()
        page.select_result_character(owner.summary.id)
        page.result_slot_combo.setCurrentIndex(page.result_slot_combo.findData(3))
        assert page.link_character_button.isEnabled()
        with qtbot.waitSignal(presenter.characters_changed):
            page.link_character_button.click()
        page.link_character_button.click()
        assert not presenter._saving_equipment
        assert records() == (1, 1, 1, 0)
        detail = characters.get_character(owner.summary.id, data_set_id=dataset)
        assert detail.echo_slots[2].name == "核對聲骸"
        presenter.stop_capture()
        live.recognition_ready.emit(replace(packet, source_sequence=11))
        assert records() == (1, 1, 1, 0)
    finally:
        presenter.shutdown()
        database.dispose()


def test_slot_toggle_refreshes_planning_and_filters_sources_by_cost(qtbot, tmp_path, monkeypatch):
    database = create_database(AppPaths.from_root(tmp_path))
    history = EchoHistoryService(database.session_factory)
    dataset = history.ensure_active_primary_dataset()
    characters = CharacterCultivationService(database.session_factory)
    owner = characters.create_character(data_set_id=dataset, command=CreateCharacter("折枝"))
    custom = characters.create_character(data_set_id=dataset, command=CreateCharacter("資料待補"))
    characters.set_candidate(owner.summary.id, False, data_set_id=dataset)
    demands = EchoDemandService(database.settings, dataset)
    page = TodayPlanPage()
    gear = EquipmentDetailPage()
    qtbot.addWidget(page)
    qtbot.addWidget(gear)
    presenter = TodayPlanPresenter(
        planning_service=CultivationPlanningService(database.session_factory),
        character_service=characters,
        scoring_profiles=ScoringProfileService(database.session_factory),
        settings=database.settings,
        page=page,
        data_set_id=dataset,
        echo_demand=demands,
    )

    def save(character_id, slot, cost):
        demands.set(character_id, slot, cost)
        presenter.refresh()

    def sources():
        return {
            page.zone_model.index(index, column).data()
            for index in range(page.zone_model.rowCount())
            for column in (0, 3)
        }

    def slot(position, cost):
        return GearPieceDetailView(
            position, None, "聲骸", None, cost, "", (), None, (), "", "", "", ""
        )

    def button(position):
        action = gear.cards[position].findChild(QPushButton, f"EchoDemandSlot{position}")
        assert action is not None
        return action

    try:
        gear.configure_farming(demands.list, save)
        gear.apply_detail(
            GearCharacterDetailView(
                owner.summary.id, "折枝", "", "", "", "", "", (slot(1, 4), slot(3, None))
            )
        )
        presenter.initialize()
        button(1).click()
        qtbot.waitUntil(lambda: "首領" in sources())
        assert "無音區" not in sources()
        assert any("貝奧海域" in text for text in sources())
        monkeypatch.setattr(QInputDialog, "getItem", lambda *args, **kwargs: ("3", True))
        button(3).click()
        qtbot.waitUntil(lambda: "無音區" in sources())
        assert EchoDemandService(database.settings, dataset).list() == {
            (owner.summary.id, 1): 4,
            (owner.summary.id, 3): 3,
        }
        assert EchoDemandService(database.settings, "another-dataset").list() == {}
        button(1).click()
        qtbot.waitUntil(lambda: "首領" not in sources())
        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))

        def fail(*args, **kwargs):
            raise OSError("test write failed")

        with monkeypatch.context() as patch:
            patch.setattr(demands._settings, "set", fail)
            button(3).click()
        assert warnings == ["test write failed"]
        assert button(3).text() == "取消補充需求"
        assert demands.list() == {(owner.summary.id, 3): 3}
        save(custom.summary.id, 5, 4)
        qtbot.waitUntil(lambda: "資料待補 第 5 件" in page.zone_details._details_text)
        button(3).click()
        qtbot.waitUntil(lambda: page.zone_model.rowCount() == 0)
    finally:
        presenter.shutdown()
        database.dispose()


@pytest.mark.parametrize(
    ("cost", "set_name", "kind"),
    ((4, "沉日劫明", "週本"), (3, "失序彼岸之夢", "夢魘聚落"), (3, "輕雲出月", "野外吸收")),
)
def test_curated_acquisition_matches_exact_cost_and_sonata(cost, set_name, kind):
    character = FarmCharacter(
        "c",
        "角色",
        FarmRole.SECONDARY,
        "副C",
        (set_name,),
        (FarmEcho(3, None, None, None, None, None, None, equipped=False),),
    )
    plan = recommend_zones(
        (character,), load_echo_sources().routes, requested_slots={("c", 3): cost}
    )
    assert any(row.zone.kind == kind for row in plan.recommendations)
    if kind != "野外吸收":
        assert all(row.zone.kind != "野外吸收" for row in plan.recommendations)
    assert all(row.echo_count == 1 and row.benefits[0].cost == cost for row in plan.recommendations)
