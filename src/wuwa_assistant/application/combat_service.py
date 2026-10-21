"""Dataset-scoped combat settings and read-only available-inventory boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import exists, select

from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import EchoObservationRecord, EchoRecord, EchoSessionRecord, utc_now

from .characters import (
    CharacterCultivationService,
    CharacterDetail,
    EchoSlotSnapshot,
    _require_character,
)
from .combat_build import (
    MODEL_VERSION,
    CombatTeam,
    load_combat_catalog,
    team_to_json,
    teams_from_goal,
)


@dataclass(frozen=True, slots=True)
class CombatSettings:
    character_id: str
    teams: tuple[CombatTeam, ...]
    primary_id: str | None


class CombatBuildService:
    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory
        self._characters = CharacterCultivationService(factory)

    def roster(self, data_set_id: str) -> dict[str, CharacterDetail]:
        return {
            d.summary.id: d
            for d in self._characters.list_character_details(
                data_set_id=data_set_id, include_archived=False
            )
        }

    def save(self, data_set_id: str, settings: CombatSettings) -> None:
        rows = [team_to_json(t) for t in settings.teams]
        teams_from_goal({"combat_teams": rows})
        if settings.teams and settings.primary_id not in {t.identifier for t in settings.teams}:
            raise ValueError("請指定主要隊伍")
        if not settings.teams and settings.primary_id is not None:
            raise ValueError("沒有隊伍時不可指定主隊")
        with self._factory.begin() as session:
            record = _require_character(session, settings.character_id, data_set_id)
            for team in settings.teams:
                if settings.character_id in team.member_ids:
                    raise ValueError("隊友不可重複選擇目前角色")
                if team.outro_id is not None and team.outro_id not in team.member_ids:
                    raise ValueError("直接延奏隊友不在此隊伍中")
                for identifier in team.member_ids:
                    actor = _require_character(session, identifier, data_set_id)
                    if actor.archived_at is not None:
                        raise ValueError("封存角色不可作為隊友")
                for identifier, mode in team.modes:
                    if identifier not in {settings.character_id, *team.member_ids}:
                        raise ValueError("模態設定的角色不在此隊伍中")
                    actor = _require_character(session, identifier, data_set_id)
                    if mode not in load_combat_catalog()["modes"].get(actor.name, []):
                        raise ValueError("角色模態不適用")
            goal = deepcopy(record.cultivation_goal_json)
            goal["combat_teams"] = rows
            goal["combat_primary_team"] = settings.primary_id
            goal["combat_model"] = MODEL_VERSION
            record.cultivation_goal_json = goal
            record.updated_at = utc_now()

    def free_inventory(self, data_set_id: str, character_id: str) -> tuple[EchoSlotSnapshot, ...]:
        """Never steal equipped Echoes or propose consumed/archived equipment."""
        with self._factory() as session:
            _require_character(session, character_id, data_set_id)
            records = session.scalars(
                select(EchoRecord).where(
                    EchoRecord.data_set_id == data_set_id,
                    EchoRecord.archived_at.is_(None),
                    EchoRecord.slot_index.is_(None),
                    (EchoRecord.character_id.is_(None) | (EchoRecord.character_id == character_id)),
                    ~exists(
                        select(EchoSessionRecord.id).where(
                            EchoSessionRecord.echo_id == EchoRecord.id,
                            EchoSessionRecord.was_consumed.is_(True),
                        )
                    ),
                )
            ).all()
            observation_rows = session.execute(
                select(EchoSessionRecord.echo_id, EchoObservationRecord.requires_review)
                .join(
                    EchoObservationRecord,
                    EchoObservationRecord.echo_session_id == EchoSessionRecord.id,
                )
                .where(EchoSessionRecord.echo_id.in_([e.id for e in records]))
                .order_by(EchoObservationRecord.observed_at, EchoObservationRecord.id)
            ).all()
            review_by_echo = {identifier: review for identifier, review in observation_rows}
            return tuple(
                EchoSlotSnapshot(
                    0,
                    e.id,
                    e.name,
                    e.set_name,
                    e.cost,
                    e.level,
                    e.score,
                    e.final_status,
                    tuple(deepcopy(e.main_stats_json)),
                    tuple(deepcopy(e.sub_stats_json)),
                )
                for e in records
                if review_by_echo.get(e.id) is False
            )
