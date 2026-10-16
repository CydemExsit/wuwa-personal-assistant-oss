from __future__ import annotations

from pathlib import Path

import pytest

from wuwa_assistant.application.characters import (
    CharacterCultivationService,
    CreateCharacter,
    CreateTeam,
    SaveTeam,
    TeamMemberInput,
    UpdateTeam,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.orm import DataSetRecord


def test_team_fields_and_members_save_atomically(tmp_path: Path) -> None:
    with create_database(AppPaths.from_root(tmp_path / "team-aggregate")) as database:
        with database.session_factory.begin() as session:
            data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
            session.add(data_set)
            session.flush()
            data_set_id = data_set.id
        service = CharacterCultivationService(database.session_factory)
        first = service.create_character(
            data_set_id=data_set_id,
            command=CreateCharacter("角色一"),
        )
        second = service.create_character(
            data_set_id=data_set_id,
            command=CreateCharacter("角色二"),
        )
        team = service.save_team(
            data_set_id=data_set_id,
            command=SaveTeam(
                CreateTeam("第一隊", 1),
                (TeamMemberInput(first.summary.id, 1),),
            ),
        )

        with pytest.raises(ValueError, match="character does not exist"):
            service.save_team(
                data_set_id=data_set_id,
                team_id=team.id,
                command=SaveTeam(
                    UpdateTeam("不應保存", 99, None),
                    (TeamMemberInput("missing-character", 1),),
                ),
            )

        unchanged = service.get_team(team.id, data_set_id=data_set_id)
        assert unchanged.name == "第一隊"
        assert [member.character_id for member in unchanged.members] == [first.summary.id]

        updated = service.save_team(
            data_set_id=data_set_id,
            team_id=team.id,
            command=SaveTeam(
                UpdateTeam("主力隊", 2, "手動設定"),
                (
                    TeamMemberInput(second.summary.id, 1),
                    TeamMemberInput(first.summary.id, 2),
                ),
            ),
        )
        assert updated.name == "主力隊"
        assert [member.character_id for member in updated.members] == [
            second.summary.id,
            first.summary.id,
        ]
