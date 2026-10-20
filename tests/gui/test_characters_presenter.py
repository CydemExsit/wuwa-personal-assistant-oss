from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from wuwa_assistant.application.characters import CharacterCultivationService, CreateCharacter
from wuwa_assistant.application.scoring_profiles import ScoringProfileService
from wuwa_assistant.scoring import load_template_seed_definitions
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import DataSetRecord, EchoRecord
from wuwa_assistant.ui.controllers.characters import CharactersPresenter
from wuwa_assistant.ui.dialogs.character_catalog import CharacterCatalogDialog
from wuwa_assistant.ui.pages.characters import (
    CharacterEditRequest,
    CharactersPage,
    SkillProgressView,
    WeaponProgressView,
)


@pytest.fixture
def character_workspace(
    tmp_path: Path,
) -> Iterator[
    tuple[Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage]
]:
    database = create_database(AppPaths.from_root(tmp_path / "character-presenter"))
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        data_set_id = data_set.id
    profiles = ScoringProfileService(database.session_factory)
    profiles.ensure_system_templates(load_template_seed_definitions())
    page = CharactersPage()
    try:
        yield (
            database,
            CharacterCultivationService(database.session_factory),
            profiles,
            data_set_id,
            page,
        )
    finally:
        page.close()
        database.dispose()


def _request(
    identifier: str | None = None,
    *,
    name: str = "測試角色",
    echo_validations: tuple[tuple[bool | None, bool | None], ...] = ((None, None),) * 5,
) -> CharacterEditRequest:
    return CharacterEditRequest(
        identifier=identifier,
        name=name,
        element="冷凝",
        role="主輸出",
        is_candidate=True,
        priority=1,
        level=80,
        resonance_chain=0,
        scoring_profile_id=None,
        weapon=WeaponProgressView("測試武器", 70, 1),
        team_id=None,
        skills=(
            SkillProgressView("normal_attack", "常態攻擊", 6, 6, False),
            SkillProgressView("resonance_skill", "共鳴技能", 8, 10, True),
            SkillProgressView("forte_circuit", "共鳴迴路", 8, 10, True),
            SkillProgressView("resonance_liberation", "共鳴解放", 8, 10, True),
            SkillProgressView("intro_skill", "變奏技能", 6, 6, False),
        ),
        attribute_nodes=(True,) * 8 + (False, False),
        cultivation_goal="先補齊核心技能與永久聲骸",
        notes="本機測試",
        echo_validations=echo_validations,
    )


def test_scoring_panel_and_target_survive_editor_round_trip(character_workspace) -> None:
    _, service, profiles, data_set_id, page = character_workspace
    presenter = CharactersPresenter(
        service=service, scoring_profiles=profiles, page=page, data_set_id=data_set_id
    )
    request = replace(_request(name="達妮婭"), panel_energy_regen=120, energy_target_override=130)
    workspace, identifier = presenter._save_and_reload(request, None)
    presenter._apply_workspace(workspace, selected_id=identifier)
    restored = page.build_edit_request()
    assert restored.panel_energy_regen == 120 and restored.energy_target_override == 130
    entry = next(entry for entry in workspace.entries if entry.record.summary.id == identifier)
    cleared = replace(restored, panel_energy_regen=None, energy_target_override=None)
    presenter._save_and_reload(cleared, entry)
    goal = service.get_character(identifier, data_set_id=data_set_id).cultivation_goal
    assert "scoring_panel_energy_regen" not in goal and "scoring_energy_target" not in goal
    assert goal["description"] == request.cultivation_goal
    assert presenter.shutdown()


def test_calculated_panel_inputs_survive_reload_without_overwriting_measured_context(
    character_workspace,
) -> None:
    _, service, profiles, data_set_id, page = character_workspace
    presenter = CharactersPresenter(
        service=service, scoring_profiles=profiles, page=page, data_set_id=data_set_id
    )
    try:
        request = replace(
            _request(name="珂萊塔"),
            level=20,
            character_ascension=1,
            weapon_ascension=0,
            weapon=WeaponProgressView("死與舞", 20, 1),
            panel_energy_regen=123.4,
            attribute_nodes=(True,) * 10,
        )
        workspace, identifier = presenter._save_and_reload(request, None)
        presenter._apply_workspace(workspace, selected_id=identifier)
        saved = service.get_character(identifier, data_set_id=data_set_id)
        assert saved.cultivation_goal["panel_config"] == {
            "character_ascension": 1,
            "weapon_ascension": 0,
            "node_layout": 1,
        }
        assert saved.cultivation_goal["scoring_panel_energy_regen"] == 123.4
        restored = page.build_edit_request()
        assert restored.character_ascension == 1
        assert restored.weapon_ascension == 0
        assert page.panel_stat_labels["crit_rate"].text() == "暴擊　待補資料"
        assert "暴擊" in page.attribute_node_checks[0].text()
        assert "死與舞" in page.panel_details_button._details_text
        assert "位置1" in page.panel_details_button._details_text
        # A background recognition reload recomputes the saved panel but retains editor drafts.
        page.character_ascension_combo.setCurrentIndex(0)
        page.weapon_name_edit.setText("未儲存武器")
        page.apply_external_characters(workspace.rows, tuple(e.view for e in workspace.entries))
        assert page.character_ascension_combo.currentData() is None
        assert page.weapon_name_edit.text() == "未儲存武器"
        assert "死與舞" in page.panel_details_button._details_text
    finally:
        assert presenter.shutdown()


def test_presenter_creates_updates_and_archives_without_blocking_qt(
    qapp: QApplication,
    qtbot: QtBot,
    character_workspace: tuple[
        Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage
    ],
) -> None:
    del qapp
    _database, service, profiles, data_set_id, page = character_workspace
    choices: list[object] = []
    dashboards: list[object] = []
    scoring_changes: list[bool] = []
    presenter = CharactersPresenter(
        service=service,
        scoring_profiles=profiles,
        page=page,
        data_set_id=data_set_id,
    )
    presenter.character_choices_changed.connect(choices.append)
    presenter.dashboard_snapshot_changed.connect(dashboards.append)
    presenter.scoring_context_changed.connect(lambda: scoring_changes.append(True))

    presenter.initialize()
    qtbot.waitUntil(lambda: page.character_model.rowCount() == 0 and page.add_button.isEnabled())
    assert not scoring_changes

    page.edit_requested.emit(_request())
    qtbot.waitUntil(lambda: page.character_model.rowCount() == 1)
    stored = service.list_characters(data_set_id=data_set_id)[0]
    qtbot.waitUntil(lambda: page.current_character_id() == stored.id)
    assert page.name_edit.text() == "測試角色"
    assert "角色 Lv." in page.progress_label.text()
    assert choices and dashboards
    assert len(scoring_changes) == 1

    page.edit_requested.emit(
        _request(
            stored.id,
            name="更新角色",
            echo_validations=((True, True),) + ((None, None),) * 4,
        )
    )
    qtbot.waitUntil(
        lambda: service.get_character(stored.id, data_set_id=data_set_id).summary.name == "更新角色"
    )
    qtbot.waitUntil(lambda: page.name_edit.text() == "更新角色")
    assert len(scoring_changes) == 2
    presenter.refresh_external_changes()
    qtbot.waitUntil(lambda: not presenter._futures)
    assert len(scoring_changes) == 2  # Scan refresh must not cause a scoring feedback loop.
    assert service.get_character(
        stored.id,
        data_set_id=data_set_id,
    ).cultivation_goal["echo_slot_confirmations"]["1"] == {
        "echo_id": None,
        "set": True,
        "main_stat": True,
    }

    page.archive_requested.emit(stored.id)
    qtbot.waitUntil(
        lambda: (
            service.get_character(stored.id, data_set_id=data_set_id).summary.archived_at
            is not None
        )
    )
    qtbot.waitUntil(lambda: page.character_model.rowCount() == 0 and page.add_button.isEnabled())
    assert page.current_character_id() is None
    page.show_archived_check.setChecked(True)
    assert page.character_model.rowCount() == 1
    qtbot.waitUntil(lambda: page.archive_button.text() == "取消封存")
    assert len(scoring_changes) == 3
    assert presenter.shutdown()
    assert presenter.shutdown()


def test_presenter_batch_adds_catalog_characters_and_marks_them_existing(
    qapp: QApplication,
    qtbot: QtBot,
    character_workspace: tuple[
        Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage
    ],
) -> None:
    del qapp
    _database, service, profiles, data_set_id, page = character_workspace
    presenter = CharactersPresenter(
        service=service,
        scoring_profiles=profiles,
        page=page,
        data_set_id=data_set_id,
    )
    presenter.initialize()
    qtbot.waitUntil(page.catalog_button.isEnabled)

    presenter.open_character_catalog()
    dialog = page.findChild(CharacterCatalogDialog)
    assert dialog is not None
    for row in range(2):
        index = dialog.model.index(row, 0)
        assert dialog.model.setData(
            index,
            Qt.CheckState.Checked,
            int(Qt.ItemDataRole.CheckStateRole),
        )
    selected_names = tuple(dialog.model._rows[row].name for row in range(2))
    dialog._request_import()

    qtbot.waitUntil(lambda: len(service.list_characters(data_set_id=data_set_id)) == 2)
    assert tuple(item.name for item in service.list_characters(data_set_id=data_set_id)) == tuple(
        sorted(selected_names)
    )
    qtbot.waitUntil(lambda: "新增 2 名" in page.status_label.text())
    assert all(dialog.model._rows[row].already_added for row in range(2))
    assert presenter.shutdown()


def test_external_refresh_preserves_edit_and_saves_it_with_updated_equipment(
    qapp: QApplication,
    qtbot: QtBot,
    character_workspace: tuple[
        Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage
    ],
) -> None:
    del qapp
    database, service, profiles, data_set_id, page = character_workspace
    existing = service.create_character(
        data_set_id=data_set_id, command=CreateCharacter(name="原角色")
    )
    presenter = CharactersPresenter(
        service=service, scoring_profiles=profiles, page=page, data_set_id=data_set_id
    )
    choices: list[object] = []
    dashboards: list[object] = []
    presenter.character_choices_changed.connect(choices.append)
    presenter.dashboard_snapshot_changed.connect(dashboards.append)
    try:
        presenter.initialize()
        qtbot.waitUntil(lambda: page.name_edit.text() == "原角色")
        page.name_edit.setText("尚未儲存名稱")
        page.level_spin.setValue(70)
        page.notes_edit.setPlainText("尚未儲存備註")
        page.weapon_name_edit.setText("尚未儲存武器")
        page.attribute_node_checks[0].setChecked(True)
        page.skill_model.setData(page.skill_model.index(1, 1), 8, int(Qt.ItemDataRole.EditRole))
        card = page.echo_cards[0]
        card.set_validity_combo.setCurrentIndex(card.set_validity_combo.findData(True))
        draft = page.build_edit_request()
        service.create_character(
            data_set_id=data_set_id, command=CreateCharacter(name="辨識新角色")
        )
        with database.session_factory.begin() as session:
            echo = EchoRecord(data_set_id=data_set_id, name="新裝備聲骸", cost=1, level=25)
            session.add(echo)
            session.flush()
            echo_id = echo.id
        service.assign_echo_slot(existing.summary.id, 2, echo_id, data_set_id=data_set_id)
        presenter.refresh_external_changes()
        qtbot.waitUntil(lambda: page.echo_cards[1].name_label.text() == "新裝備聲骸")
        assert page.character_model.rowCount() == 2
        assert page.current_character_id() == existing.summary.id
        assert page.build_edit_request() == draft
        assert len(choices) >= 2 and len(dashboards) >= 2
        page.edit_requested.emit(page.build_edit_request())
        qtbot.waitUntil(
            lambda: (
                service.get_character(existing.summary.id, data_set_id=data_set_id).summary.name
                == "尚未儲存名稱"
            )
        )
        saved = service.get_character(existing.summary.id, data_set_id=data_set_id)
        assert saved.summary.level == 70 and saved.notes == "尚未儲存備註"
        assert saved.weapon is not None and saved.weapon.name == "尚未儲存武器"
        assert saved.skills[1].current_level == 8 and saved.small_stat_nodes[0]
        assert saved.echo_slots[1].echo_id == echo_id
    finally:
        assert presenter.shutdown()


@pytest.mark.parametrize("draft_open", [False, True])
def test_external_refresh_first_owner_preserves_an_intentional_new_character_draft(
    qapp: QApplication,
    qtbot: QtBot,
    character_workspace: tuple[
        Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage
    ],
    draft_open: bool,
) -> None:
    del qapp
    _database, service, profiles, data_set_id, page = character_workspace
    presenter = CharactersPresenter(
        service=service, scoring_profiles=profiles, page=page, data_set_id=data_set_id
    )
    try:
        presenter.initialize()
        qtbot.waitUntil(page.add_button.isEnabled)
        if draft_open:
            page.add_button.click()
            page.name_edit.setText("我的新角色草稿")
            page.notes_edit.setPlainText("不要遺失")
        recognized = service.create_character(
            data_set_id=data_set_id, command=CreateCharacter(name="辨識角色")
        )
        presenter.refresh_external_changes()
        qtbot.waitUntil(lambda: page.character_model.rowCount() == 1)
        if draft_open:
            assert page.name_edit.text() == "我的新角色草稿"
            assert page.notes_edit.toPlainText() == "不要遺失"
            assert page.build_edit_request().identifier is None
            page.edit_requested.emit(page.build_edit_request())
            qtbot.waitUntil(lambda: len(service.list_characters(data_set_id=data_set_id)) == 2)
            assert {item.name for item in service.list_characters(data_set_id=data_set_id)} == {
                "我的新角色草稿",
                "辨識角色",
            }
        else:
            assert page.current_character_id() == recognized.summary.id
            assert page.name_edit.text() == "辨識角色"
    finally:
        assert presenter.shutdown()


def test_presenter_surfaces_atomic_validation_error_without_partial_update(
    qapp: QApplication,
    qtbot: QtBot,
    character_workspace: tuple[
        Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage
    ],
) -> None:
    del qapp
    _database, service, profiles, data_set_id, page = character_workspace
    presenter = CharactersPresenter(
        service=service,
        scoring_profiles=profiles,
        page=page,
        data_set_id=data_set_id,
    )
    presenter.initialize()
    qtbot.waitUntil(page.add_button.isEnabled)

    invalid = _request()
    invalid = CharacterEditRequest(
        identifier=None,
        name=invalid.name,
        element=invalid.element,
        role=invalid.role,
        is_candidate=invalid.is_candidate,
        priority=invalid.priority,
        level=invalid.level,
        resonance_chain=invalid.resonance_chain,
        scoring_profile_id="missing-profile",
        weapon=invalid.weapon,
        team_id=None,
        skills=invalid.skills,
        attribute_nodes=invalid.attribute_nodes,
        cultivation_goal=invalid.cultivation_goal,
        notes=invalid.notes,
    )
    page.edit_requested.emit(invalid)

    qtbot.waitUntil(lambda: page.status_label.property("error") is True)
    assert service.list_characters(data_set_id=data_set_id) == ()
    assert presenter.shutdown()


def test_profile_change_after_stale_refresh_releases_loading_and_preserves_draft(
    qapp: QApplication,
    character_workspace: tuple[
        Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage
    ],
) -> None:
    del qapp
    _database, service, profiles, data_set_id, page = character_workspace
    service.create_character(data_set_id=data_set_id, command=CreateCharacter(name="原角色"))
    presenter = CharactersPresenter(
        service=service, scoring_profiles=profiles, page=page, data_set_id=data_set_id
    )
    try:
        initial = presenter._load_workspace()
        presenter._apply_workspace(initial, None)
        assert initial.overview is not None
        page.name_edit.setText("尚未儲存的名稱")
        page.notes_edit.setPlainText("切換比較基準時保留")
        page.scoring_profile_combo.setCurrentIndex(1)
        draft = page.build_edit_request()

        # Deliver the completion order from Refresh A -> comparison B while A runs.
        page.set_loading(True, "重新整理中")
        stale = presenter._load_workspace()
        comparison = next(
            profile.identifier
            for profile in initial.profiles
            if profile.identifier != initial.overview.profile_id
        )
        presenter._comparison_profile_id = comparison
        presenter._apply_workspace(stale, None)
        assert not page.refresh_button.isEnabled()
        current = presenter._load_workspace()
        presenter._apply_workspace(current, None, preserve_editor=True)

        assert page.overview_page.profile_combo.currentData() == comparison
        assert page.refresh_button.isEnabled()
        assert page.add_button.isEnabled()
        assert page.catalog_button.isEnabled()
        assert page.character_table.isEnabled()
        assert page.build_edit_request() == draft
    finally:
        assert presenter.shutdown()


def test_profile_change_does_not_discard_successful_new_character_save(
    qapp: QApplication,
    character_workspace: tuple[
        Database, CharacterCultivationService, ScoringProfileService, str, CharactersPage
    ],
) -> None:
    from wuwa_assistant.ui.controllers.characters import _JobResult, _Operation

    del qapp
    _database, service, profiles, data_set_id, page = character_workspace
    presenter = CharactersPresenter(
        service=service, scoring_profiles=profiles, page=page, data_set_id=data_set_id
    )
    try:
        initial = presenter._load_workspace()
        presenter._apply_workspace(initial, None)
        assert initial.overview is not None
        page._request_add()
        page.name_edit.setText("已儲存的新角色")
        page.notes_edit.setPlainText("儲存時切換比較基準")
        request = page.build_edit_request()
        page.set_loading(True, "儲存中")
        stale, saved_id = presenter._save_and_reload(request, None)
        comparison = next(
            profile.identifier
            for profile in initial.profiles
            if profile.identifier != initial.overview.profile_id
        )
        presenter._comparison_profile_id = comparison

        # Mutation completion must survive an obsolete comparison snapshot.
        presenter._handle_job_result(
            _JobResult(_Operation.SAVE, workspace=stale, selected_id=saved_id)
        )
        assert not page._creating
        assert page.build_edit_request().identifier == saved_id
        assert page.current_character_id() == saved_id
        current = presenter._load_workspace()
        presenter._apply_workspace(current, None, preserve_editor=True)
        saved_request = page.build_edit_request()
        assert saved_request.identifier == saved_id
        assert saved_request.notes == request.notes
        assert page.overview_page.profile_combo.currentData() == comparison

        # A second Save updates that identity instead of attempting a duplicate insert.
        presenter._save_and_reload(saved_request, presenter._entries[saved_id])
        assert len(service.list_characters(data_set_id=data_set_id)) == 1
    finally:
        assert presenter.shutdown()
