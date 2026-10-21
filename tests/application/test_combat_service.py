from __future__ import annotations

from dataclasses import replace

import pytest

from wuwa_assistant.application.characters import CharacterCultivationService, CreateCharacter
from wuwa_assistant.application.combat_build import CombatTeam, teams_from_goal
from wuwa_assistant.application.combat_service import CombatBuildService, CombatSettings
from wuwa_assistant.storage.orm import EchoRecord

from .test_equipment_overview import _echo
from .test_equipment_overview import equipment as equipment_fixture


@pytest.fixture
def equipment(tmp_path):
    yield from equipment_fixture.__wrapped__(tmp_path)


def test_settings_are_independent_reusable_and_survive_reopen_without_losing_goals(equipment):
    characters = CharacterCultivationService(equipment.database.session_factory)
    a = characters.create_character(
        data_set_id=equipment.data_set_id,
        command=CreateCharacter("愛彌斯", cultivation_goal={"description": "保留內容"}),
    )
    b = characters.create_character(
        data_set_id=equipment.data_set_id, command=CreateCharacter("千咲")
    )
    c = characters.create_character(
        data_set_id=equipment.data_set_id, command=CreateCharacter("琳奈")
    )
    service = CombatBuildService(equipment.database.session_factory)
    first = CombatTeam("a1", "隊伍1", (b.summary.id, c.summary.id), 120, confirmed=True)
    second = replace(first, identifier="a2", name="隊伍2", energy_target=130)
    service.save(equipment.data_set_id, CombatSettings(a.summary.id, (first, second), "a2"))
    # The same core teammate remains available to another independent owner.
    other = CombatTeam("c1", "另一角色的隊伍", (b.summary.id,), 140, confirmed=True)
    service.save(equipment.data_set_id, CombatSettings(c.summary.id, (other,), "c1"))
    reloaded = characters.get_character(a.summary.id, data_set_id=equipment.data_set_id)
    assert teams_from_goal(reloaded.cultivation_goal) == (first, second)
    assert reloaded.cultivation_goal["description"] == "保留內容"
    assert reloaded.cultivation_goal["combat_primary_team"] == "a2"
    assert (
        len(
            characters.get_character(
                c.summary.id, data_set_id=equipment.data_set_id
            ).cultivation_goal["combat_teams"]
        )
        == 1
    )


def test_invalid_self_other_dataset_archived_and_primary_fail_atomically(equipment):
    service = CombatBuildService(equipment.database.session_factory)
    owner = equipment.character("愛彌斯")
    teammate = equipment.character("千咲")
    CharacterCultivationService(equipment.database.session_factory).archive_character(
        teammate, data_set_id=equipment.data_set_id
    )
    for members, primary in [
        ((owner,), "t"),
        (("unknown",), "t"),
        ((teammate,), "t"),
        ((), "invalid"),
    ]:
        team = CombatTeam("t", "隊伍", members, 120)
        with pytest.raises((ValueError, LookupError)):
            service.save(equipment.data_set_id, CombatSettings(owner, (team,), primary))
    assert "combat_teams" not in service.roster(equipment.data_set_id)[owner].cultivation_goal


def test_inventory_excludes_equipped_foreign_consumed_archived_and_unreviewed(equipment):
    owner = equipment.character("珂萊塔")
    other = equipment.character("另一角色")
    free, _ = _echo(equipment, owner, None)
    _echo(equipment, owner, 1)
    _echo(equipment, other, None)
    _echo(equipment, owner, None, consumed=True)
    _echo(equipment, owner, None, archived=True)
    _echo(equipment, owner, None, review=True)
    _echo(equipment, owner, None, observation=False)
    records = CombatBuildService(equipment.database.session_factory).free_inventory(
        equipment.data_set_id, owner
    )
    assert [r.echo_id for r in records] == [free]
    with equipment.database.session_factory() as session:
        assert session.get(EchoRecord, free).character_id == owner
