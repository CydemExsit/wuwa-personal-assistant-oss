from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
)
from wuwa_assistant.application.planning import (
    CultivationPlanningService,
    CultivationTargetDefinition,
    MaterialDefinition,
    MaterialRequirement,
)
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord
from wuwa_assistant.ui.controllers.today_plan import (
    DAILY_WAVEPLATES_KEY,
    TodayPlanPresenter,
)
from wuwa_assistant.ui.dialogs.planning_data import (
    MaterialSaveRequest,
    PlanningDataDialog,
)
from wuwa_assistant.ui.pages.today_plan import (
    InventoryEditRequest,
    TodayPlanPage,
    TodayPlanRequest,
)


def test_presenter_generates_persists_and_edits_local_planning_data(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "today-presenter"))
    page = TodayPlanPage()
    try:
        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
            session.add(data_set)
            session.flush()
            data_set_id = data_set.id
        characters = CharacterCultivationService(database.session_factory)
        character = characters.create_character(
            data_set_id=data_set_id,
            command=CreateCharacter("測試角色", priority=1),
        )
        planning = CultivationPlanningService(database.session_factory)
        material = planning.create_material(
            data_set_id=data_set_id,
            definition=MaterialDefinition(
                code="skill-drop",
                name="技能素材",
                category="技能",
                stamina_cost_per_run=40,
                average_yield=2,
                open_days=(1, 2, 3, 4, 5, 6, 7),
                weekly_limit=3,
                source_version="1",
            ),
        )
        target = planning.create_cultivation_target(
            data_set_id=data_set_id,
            definition=CultivationTargetDefinition(
                character_id=character.summary.id,
                target_type="echo_slot",
                target_key="slot-1",
                current_value=0,
                target_value=1,
                priority=1,
                required_materials=(MaterialRequirement(material.id, 6),),
            ),
        )
        profiles = ScoringProfileService(database.session_factory)
        profiles.ensure_system_templates(load_template_seed_definitions())
        dashboard_updates: list[str] = []
        presenter = TodayPlanPresenter(
            planning_service=planning,
            character_service=characters,
            scoring_profiles=profiles,
            settings=database.settings,
            page=page,
            data_set_id=data_set_id,
            today=lambda: date(2026, 8, 3),
            legacy_resource_planning=True,
        )
        presenter.dashboard_priority_changed.connect(dashboard_updates.append)

        presenter.initialize()
        qtbot.waitUntil(lambda: "技能素材" in page.first_priority_card.title_label.text())
        assert not page.complete_plan_button.isEnabled()
        assert page.first_priority_card.meta_label.text().startswith("需要人工確認")
        assert "建議次數資料不足" in page.first_priority_card.meta_label.text()
        assert planning.get_current_daily_plan(date(2026, 8, 3), data_set_id=data_set_id) is None
        assert dashboard_updates

        page.inventory_edit_requested.emit(InventoryEditRequest(material.id, 0))

        def inventory_zero_is_confirmed() -> bool:
            stored = planning.get_inventory_count(material.id, data_set_id=data_set_id)
            return stored is not None and stored.quantity == 0

        qtbot.waitUntil(inventory_zero_is_confirmed)
        qtbot.waitUntil(lambda: "3 次" in page.first_priority_card.meta_label.text())

        page.plan_requested.emit(TodayPlanRequest(date(2026, 8, 3), 80))
        qtbot.waitUntil(
            lambda: (
                planning.get_current_daily_plan(date(2026, 8, 3), data_set_id=data_set_id)
                is not None
            )
        )
        qtbot.waitUntil(lambda: page.waveplates_spin.value() == 80)
        qtbot.waitUntil(lambda: page.complete_plan_button.isEnabled())
        assert database.settings.get(DAILY_WAVEPLATES_KEY) == 80

        page.inventory_edit_requested.emit(InventoryEditRequest(material.id, 4))

        def inventory_is_four() -> bool:
            stored = planning.get_inventory_count(material.id, data_set_id=data_set_id)
            return stored is not None and stored.quantity == 4

        qtbot.waitUntil(inventory_is_four)
        qtbot.waitUntil(lambda: "1 次" in page.first_priority_card.meta_label.text())
        assert page.complete_plan_button.isEnabled()
        current_generation = planning.get_current_daily_plan(
            date(2026, 8, 3), data_set_id=data_set_id
        )
        assert current_generation is not None
        generation_id = current_generation.generation_id
        assert presenter.shutdown()
        page.close()

        page = TodayPlanPage()
        presenter = TodayPlanPresenter(
            planning_service=planning,
            character_service=characters,
            scoring_profiles=profiles,
            settings=database.settings,
            page=page,
            data_set_id=data_set_id,
            today=lambda: date(2026, 8, 3),
            legacy_resource_planning=True,
        )
        presenter.initialize()
        qtbot.waitUntil(lambda: page.complete_plan_button.isEnabled())
        assert "1 次" in page.first_priority_card.meta_label.text()
        assert "已儲存" in page.status_label.text()
        restarted_generation = planning.get_current_daily_plan(
            date(2026, 8, 3), data_set_id=data_set_id
        )
        assert restarted_generation is not None
        assert restarted_generation.generation_id == generation_id

        page.complete_plan_requested.emit(current_generation.generation_id)

        def current_plan_is_completed() -> bool:
            current = planning.get_current_daily_plan(date(2026, 8, 3), data_set_id=data_set_id)
            return current is not None and all(item.status == "completed" for item in current.items)

        qtbot.waitUntil(current_plan_is_completed)
        qtbot.waitUntil(lambda: not page.complete_plan_button.isEnabled())

        page.manage_data_requested.emit()
        qtbot.waitUntil(lambda: page.findChild(PlanningDataDialog) is not None)
        dialog = page.findChild(PlanningDataDialog)
        assert dialog is not None
        qtbot.waitUntil(lambda: dialog.material_model.rowCount() == 1)
        assert dialog.target_model.rowCount() == 1

        dialog.material_save_requested.emit(
            MaterialSaveRequest(
                identifier=None,
                code="weapon-drop",
                name="武器素材",
                category="武器",
                waveplate_cost_per_run=40,
                average_output=3,
                open_weekdays=(1, 2, 3, 4, 5, 6, 7),
                weekly_limit=None,
                source_status="manual_unverified",
                version="1",
                inventory_count=9,
            )
        )
        qtbot.waitUntil(lambda: dialog.material_model.rowCount() == 2)
        added = next(
            item
            for item in planning.list_materials(data_set_id=data_set_id)
            if item.code == "weapon-drop"
        )
        stored_inventory = planning.get_inventory_count(added.id, data_set_id=data_set_id)
        assert stored_inventory is not None
        assert stored_inventory.quantity == 9

        dialog.material_archive_requested.emit(added.id)
        qtbot.waitUntil(
            lambda: all(
                item.id != added.id for item in planning.list_materials(data_set_id=data_set_id)
            )
        )
        qtbot.waitUntil(lambda: dialog.material_model.rowCount() == 1)

        dialog.target_archive_requested.emit(target.id)
        qtbot.waitUntil(
            lambda: planning.get_cultivation_target(target.id, data_set_id=data_set_id).is_completed
        )
        assert presenter.shutdown()
    finally:
        page.close()
        database.dispose()


def test_default_presenter_updates_both_sections_without_material_inventory_queries(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qapp
    database = create_database(AppPaths.from_root(tmp_path / "simple-today"))
    page = TodayPlanPage()
    presenter: TodayPlanPresenter | None = None
    try:
        with database.session_factory.begin() as session:
            dataset = DataSetRecord(name="主要", kind="primary", is_active=True)
            session.add(dataset)
            session.flush()
            dataset_id = dataset.id
        characters = CharacterCultivationService(database.session_factory)
        characters.create_character(data_set_id=dataset_id, command=CreateCharacter("維里奈"))
        profiles = ScoringProfileService(database.session_factory)
        profiles.ensure_system_templates(load_template_seed_definitions())
        planning = CultivationPlanningService(database.session_factory)

        def obsolete_query(**kwargs: object) -> object:
            raise AssertionError(f"today guidance must not require material accounting: {kwargs}")

        for method in (
            "list_materials",
            "list_inventory_counts",
            "list_cultivation_targets",
            "get_current_daily_plan",
            "replace_daily_plan",
        ):
            monkeypatch.setattr(planning, method, obsolete_query)
        presenter = TodayPlanPresenter(
            planning_service=planning,
            character_service=characters,
            scoring_profiles=profiles,
            settings=database.settings,
            page=page,
            data_set_id=dataset_id,
            today=lambda: date(2026, 10, 8),
        )
        page.set_activity_notices(("已同步的無音區雙倍",))
        presenter.initialize()
        qtbot.waitUntil(lambda: page.skill_model.rowCount() > 0)
        assert page.zone_model.rowCount() > 0
        assert page.activity_label.text() == "已同步的無音區雙倍"
        assert "素材" not in page.status_label.text()
        page.plan_requested.emit(TodayPlanRequest(date(2026, 10, 9), 240))
        qtbot.waitUntil(lambda: "建議已更新" in page.status_label.text())
        assert page.skill_model.rowCount() > 0
        assert page.zone_model.rowCount() > 0
        assert planning.list_daily_plans(data_set_id=dataset_id) == ()
        assert presenter.shutdown()
    finally:
        if presenter is not None:
            presenter.shutdown()
        page.close()
        database.dispose()
