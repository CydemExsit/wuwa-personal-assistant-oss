"""Dataset-scoped character cultivation persistence.

The service in this module is the application boundary for character, weapon,
skill, team, and permanent Echo-slot editing.  It returns detached immutable
snapshots, keeps every operation in a short transaction, and treats archive as
the non-destructive form of character deletion.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from .equipment_overview import EquipmentOverview

from wuwa_assistant.models.names import canonical_character_name, character_name_key
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    CharacterRecord,
    CharacterSkillRecord,
    DataSetRecord,
    EchoRecord,
    EchoSessionRecord,
    ScoringProfileRecord,
    TeamMemberRecord,
    TeamRecord,
    WeaponRecord,
    utc_now,
)

type JsonObject = dict[str, Any]

SMALL_STAT_NODE_COUNT = 10
TEAM_SIZE = 3
CORE_SKILL_TARGET = 10
SECONDARY_SKILL_TARGET = 6


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    """Definition used while creating a character's five skill rows."""

    skill_key: str
    name: str
    skill_type: str
    is_core: bool
    target_level: int | None = None


DEFAULT_SKILLS: tuple[SkillDefinition, ...] = (
    SkillDefinition("normal_attack", "常態攻擊", "normal_attack", False),
    SkillDefinition("resonance_skill", "共鳴技能", "resonance_skill", True),
    SkillDefinition("forte_circuit", "共鳴迴路", "forte_circuit", True),
    SkillDefinition("resonance_liberation", "共鳴解放", "resonance_liberation", True),
    SkillDefinition("intro_skill", "變奏技能", "intro_skill", False),
)


@dataclass(frozen=True, slots=True)
class CreateCharacter:
    name: str
    element: str | None = None
    role: str | None = None
    is_candidate: bool = True
    priority: int = 100
    level: int = 1
    resonance_chain: int = 0
    scoring_profile_id: str | None = None
    cultivation_goal: JsonObject = field(default_factory=dict)
    small_stat_nodes: tuple[bool, ...] = (False,) * SMALL_STAT_NODE_COUNT
    notes: str | None = None
    skills: tuple[SkillDefinition, ...] | None = None


@dataclass(frozen=True, slots=True)
class UpdateCharacter:
    """Full editable character fields; omitted goal/node values are preserved."""

    name: str
    element: str | None
    role: str | None
    is_candidate: bool
    priority: int
    level: int
    resonance_chain: int
    scoring_profile_id: str | None
    notes: str | None
    cultivation_goal: JsonObject | None = None
    small_stat_nodes: tuple[bool, ...] | None = None


@dataclass(frozen=True, slots=True)
class UpsertWeapon:
    name: str
    weapon_type: str | None = None
    level: int = 1
    resonance_rank: int = 1
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateSkill:
    skill_key: str
    name: str
    skill_type: str
    current_level: int
    target_level: int
    is_core: bool
    display_order: int | None = None


@dataclass(frozen=True, slots=True)
class CreateTeam:
    name: str
    priority: int = 100
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateTeam:
    name: str
    priority: int
    notes: str | None


@dataclass(frozen=True, slots=True)
class TeamMemberInput:
    character_id: str
    slot_index: int
    role: str | None = None


@dataclass(frozen=True, slots=True)
class SaveTeam:
    """One atomic create/update of a team and its ordered member list."""

    team: CreateTeam | UpdateTeam
    members: tuple[TeamMemberInput, ...] = ()


@dataclass(frozen=True, slots=True)
class SaveCharacter:
    """One atomic edit of a character and its directly owned records.

    ``character`` is a :class:`CreateCharacter` when ``character_id`` is null
    and an :class:`UpdateCharacter` otherwise.  Supplying ``skills`` replaces
    the editable values of all five existing skill rows.  A null ``weapon``
    preserves an existing weapon unless ``remove_weapon`` is true.
    """

    character: CreateCharacter | UpdateCharacter
    weapon: UpsertWeapon | None = None
    skills: tuple[UpdateSkill, ...] | None = None
    remove_weapon: bool = False


@dataclass(frozen=True, slots=True)
class WeaponSnapshot:
    id: str
    name: str
    weapon_type: str | None
    level: int
    resonance_rank: int
    notes: str | None


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    id: str
    skill_key: str
    name: str
    skill_type: str
    current_level: int
    target_level: int
    is_core: bool
    is_completed: bool
    display_order: int


@dataclass(frozen=True, slots=True)
class EchoSlotSnapshot:
    slot_index: int
    echo_id: str | None
    name: str | None
    set_name: str | None
    cost: int | None
    level: int | None
    score: float | None
    final_status: str | None
    main_stats: tuple[JsonObject, ...]
    sub_stats: tuple[JsonObject, ...]
    score_profile_id: str | None = None
    score_profile_version: int | None = None
    score_observation_id: str | None = None


@dataclass(frozen=True, slots=True)
class EchoAssignmentCandidate:
    """Echo option shown by the permanent-slot picker without exposing ORM rows."""

    id: str
    name: str | None
    set_name: str | None
    cost: int | None
    level: int | None
    score: float | None
    current_character_id: str | None
    current_character_name: str | None
    current_slot_index: int | None
    is_assigned_to_requested_character: bool


@dataclass(frozen=True, slots=True)
class CharacterSummary:
    id: str
    data_set_id: str
    name: str
    element: str | None
    role: str | None
    is_candidate: bool
    priority: int
    level: int
    resonance_chain: int
    scoring_profile_id: str | None
    scoring_profile_name: str | None
    completed_skill_count: int
    skill_count: int
    completed_small_stat_nodes: int
    equipped_echo_count: int
    archived_at: str | None


@dataclass(frozen=True, slots=True)
class TeamMembershipSnapshot:
    team_id: str
    team_name: str
    team_priority: int
    slot_index: int
    role: str | None


@dataclass(frozen=True, slots=True)
class CharacterDetail:
    summary: CharacterSummary
    cultivation_goal: JsonObject
    small_stat_nodes: tuple[bool, ...]
    notes: str | None
    weapon: WeaponSnapshot | None
    skills: tuple[SkillSnapshot, ...]
    echo_slots: tuple[EchoSlotSnapshot, ...]
    teams: tuple[TeamMembershipSnapshot, ...]
    created_at: str
    updated_at: str
    unpositioned_echoes: tuple[EchoAssignmentCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class TeamMemberSnapshot:
    character_id: str
    character_name: str
    slot_index: int
    role: str | None


@dataclass(frozen=True, slots=True)
class TeamSnapshot:
    id: str
    data_set_id: str
    name: str
    priority: int
    notes: str | None
    members: tuple[TeamMemberSnapshot, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class BatchCreateCharactersResult:
    """Outcome of one atomic catalog import.

    Existing names, including archived characters, are reported as skipped so
    the import never duplicates or silently restores personal records.
    """

    created: tuple[CharacterDetail, ...]
    skipped_names: tuple[str, ...]


class CharacterCultivationService:
    """Transactional application service for cultivation records."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def equipment_overview(
        self,
        *,
        data_set_id: str,
        profile_id: str | None = None,
        character_details: tuple[CharacterDetail, ...] | None = None,
    ) -> EquipmentOverview:
        from .equipment_overview import EquipmentOverviewService

        return EquipmentOverviewService(self._session_factory).get_overview(
            data_set_id=data_set_id,
            profile_id=profile_id,
            character_details=character_details,
        )

    def create_character(
        self,
        *,
        data_set_id: str,
        command: CreateCharacter,
    ) -> CharacterDetail:
        return self.save_character(
            data_set_id=data_set_id,
            command=SaveCharacter(character=command),
        )

    def batch_create_characters(
        self,
        *,
        data_set_id: str,
        commands: Sequence[CreateCharacter],
    ) -> BatchCreateCharactersResult:
        """Create multiple initial character records in one short transaction.

        Commands preserve the user's selection order.  Names already present
        in the requested data set are skipped rather than treated as an error.
        Duplicate names inside the incoming batch are rejected before any
        database write.
        """

        if isinstance(commands, (str, bytes)) or not isinstance(commands, Sequence):
            raise ValueError("commands must be a sequence of CreateCharacter")
        normalized = tuple(_normalize_create_character(command) for command in commands)
        names = tuple(command.name for command in normalized)
        if len(names) != len(set(names)):
            raise ValueError("batch character names must be unique")
        if not normalized:
            return BatchCreateCharactersResult((), ())

        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            existing_names = set(
                session.scalars(
                    select(CharacterRecord.name).where(
                        CharacterRecord.data_set_id == data_set_id,
                        CharacterRecord.name.in_(names),
                    )
                ).all()
            )
            created_records = [
                _create_character_record(session, data_set_id, command)
                for command in normalized
                if command.name not in existing_names
            ]
            session.flush()
            return BatchCreateCharactersResult(
                created=_character_details(session, created_records),
                skipped_names=tuple(name for name in names if name in existing_names),
            )

    def update_character(
        self,
        character_id: str,
        *,
        data_set_id: str,
        command: UpdateCharacter,
    ) -> CharacterDetail:
        return self.save_character(
            character_id=character_id,
            data_set_id=data_set_id,
            command=SaveCharacter(character=command),
        )

    def save_character(
        self,
        *,
        data_set_id: str,
        command: SaveCharacter,
        character_id: str | None = None,
    ) -> CharacterDetail:
        """Create or update the complete editor aggregate in one transaction."""

        if not isinstance(command, SaveCharacter):
            raise ValueError("command must be SaveCharacter")
        if not isinstance(command.remove_weapon, bool):
            raise ValueError("remove_weapon must be a boolean")
        if command.weapon is not None and command.remove_weapon:
            raise ValueError("weapon and remove_weapon cannot both be supplied")
        weapon = None if command.weapon is None else _normalize_weapon(command.weapon)
        skills = (
            None
            if command.skills is None
            else _normalize_skill_updates(command.skills, require_all_five=True)
        )

        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            if character_id is None:
                if not isinstance(command.character, CreateCharacter):
                    raise ValueError("creating a character requires CreateCharacter")
                create = _normalize_create_character(command.character)
                record = _create_character_record(session, data_set_id, create)
            else:
                if not isinstance(command.character, UpdateCharacter):
                    raise ValueError("updating a character requires UpdateCharacter")
                update = _normalize_update_character(command.character)
                record = _require_character(session, character_id, data_set_id)
                _apply_character_update(session, record, data_set_id, update)

            if command.remove_weapon:
                existing_weapon = session.scalar(
                    select(WeaponRecord).where(WeaponRecord.character_id == record.id)
                )
                if existing_weapon is not None:
                    session.delete(existing_weapon)
                    session.flush()
            elif weapon is not None:
                _upsert_weapon_record(session, record, data_set_id, weapon)

            if skills is not None:
                _apply_skill_updates(session, record.id, skills)
            session.flush()
            return _character_detail(session, record)

    def get_character(self, character_id: str, *, data_set_id: str) -> CharacterDetail:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            return _character_detail(
                session,
                _require_character(session, character_id, data_set_id),
            )

    def list_characters(
        self,
        *,
        data_set_id: str,
        include_archived: bool = False,
        candidates_only: bool = False,
    ) -> tuple[CharacterSummary, ...]:
        return tuple(
            detail.summary
            for detail in self.list_character_details(
                data_set_id=data_set_id,
                include_archived=include_archived,
                candidates_only=candidates_only,
            )
        )

    def list_character_details(
        self,
        *,
        data_set_id: str,
        include_archived: bool = False,
        candidates_only: bool = False,
    ) -> tuple[CharacterDetail, ...]:
        """Load one detached roster snapshot with bounded related-record queries."""
        with self._session_factory() as session:
            # pysqlite's legacy mode does not begin a transaction for SELECT.
            # Explicit BEGIN keeps the roster and its related rows at one revision.
            session.connection().exec_driver_sql("BEGIN")
            _require_data_set(session, data_set_id)
            statement = select(CharacterRecord).where(CharacterRecord.data_set_id == data_set_id)
            if not include_archived:
                statement = statement.where(CharacterRecord.archived_at.is_(None))
            if candidates_only:
                statement = statement.where(CharacterRecord.is_candidate.is_(True))
            records = session.scalars(
                statement.order_by(
                    CharacterRecord.priority,
                    CharacterRecord.name,
                    CharacterRecord.id,
                )
            ).all()
            return _character_details(session, records)

    def set_candidate(
        self,
        character_id: str,
        is_candidate: bool,
        *,
        data_set_id: str,
    ) -> CharacterDetail:
        if not isinstance(is_candidate, bool):
            raise ValueError("is_candidate must be a boolean")
        with self._session_factory.begin() as session:
            record = _require_character(session, character_id, data_set_id)
            if record.archived_at is not None and is_candidate:
                raise ValueError("an archived character cannot be marked as a candidate")
            record.is_candidate = is_candidate
            record.updated_at = utc_now()
            session.flush()
            return _character_detail(session, record)

    def archive_character(self, character_id: str, *, data_set_id: str) -> CharacterDetail:
        with self._session_factory.begin() as session:
            record = _require_character(session, character_id, data_set_id)
            if record.archived_at is None:
                record.archived_at = utc_now()
                record.is_candidate = False
                record.updated_at = utc_now()
            session.flush()
            return _character_detail(session, record)

    def delete_character(self, character_id: str, *, data_set_id: str) -> CharacterDetail:
        """Archive instead of physically deleting cultivation history."""

        return self.archive_character(character_id, data_set_id=data_set_id)

    def restore_character(
        self,
        character_id: str,
        *,
        data_set_id: str,
        as_candidate: bool = True,
    ) -> CharacterDetail:
        if not isinstance(as_candidate, bool):
            raise ValueError("as_candidate must be a boolean")
        with self._session_factory.begin() as session:
            record = _require_character(session, character_id, data_set_id)
            record.archived_at = None
            record.is_candidate = as_candidate
            record.updated_at = utc_now()
            session.flush()
            return _character_detail(session, record)

    def upsert_weapon(
        self,
        character_id: str,
        *,
        data_set_id: str,
        command: UpsertWeapon,
    ) -> CharacterDetail:
        normalized = _normalize_weapon(command)
        with self._session_factory.begin() as session:
            character = _require_character(session, character_id, data_set_id)
            weapon = session.scalar(
                select(WeaponRecord).where(WeaponRecord.character_id == character.id)
            )
            if weapon is None:
                weapon = WeaponRecord(
                    data_set_id=data_set_id,
                    character_id=character.id,
                    name=normalized.name,
                )
                session.add(weapon)
            weapon.data_set_id = data_set_id
            weapon.name = normalized.name
            weapon.weapon_type = normalized.weapon_type
            weapon.level = normalized.level
            weapon.resonance_rank = normalized.resonance_rank
            weapon.notes = normalized.notes
            weapon.updated_at = utc_now()
            session.flush()
            return _character_detail(session, character)

    def remove_weapon(self, character_id: str, *, data_set_id: str) -> CharacterDetail:
        with self._session_factory.begin() as session:
            character = _require_character(session, character_id, data_set_id)
            weapon = session.scalar(
                select(WeaponRecord).where(WeaponRecord.character_id == character.id)
            )
            if weapon is not None:
                session.delete(weapon)
                session.flush()
            return _character_detail(session, character)

    def update_skill(
        self,
        character_id: str,
        *,
        data_set_id: str,
        command: UpdateSkill,
    ) -> CharacterDetail:
        normalized = _normalize_skill_update(command)
        with self._session_factory.begin() as session:
            character = _require_character(session, character_id, data_set_id)
            skill = session.scalar(
                select(CharacterSkillRecord).where(
                    CharacterSkillRecord.character_id == character.id,
                    CharacterSkillRecord.skill_key == normalized.skill_key,
                )
            )
            if skill is None:
                raise ValueError(f"skill does not exist: {normalized.skill_key}")
            skill.name = normalized.name
            skill.skill_type = normalized.skill_type
            skill.current_level = normalized.current_level
            skill.target_level = normalized.target_level
            skill.is_core = normalized.is_core
            if normalized.display_order is not None:
                skill.display_order = normalized.display_order
            skill.is_completed = normalized.current_level >= normalized.target_level
            skill.updated_at = utc_now()
            session.flush()
            return _character_detail(session, character)

    def set_small_stat_node(
        self,
        character_id: str,
        node_index: int,
        completed: bool,
        *,
        data_set_id: str,
    ) -> CharacterDetail:
        if isinstance(node_index, bool) or not 1 <= node_index <= SMALL_STAT_NODE_COUNT:
            raise ValueError(f"node_index must be within [1, {SMALL_STAT_NODE_COUNT}]")
        if not isinstance(completed, bool):
            raise ValueError("completed must be a boolean")
        with self._session_factory.begin() as session:
            character = _require_character(session, character_id, data_set_id)
            goal = _json_object(character.cultivation_goal_json, "cultivation goal")
            nodes = list(_nodes_from_goal(goal))
            nodes[node_index - 1] = completed
            character.cultivation_goal_json = _goal_with_nodes(goal, tuple(nodes))
            character.updated_at = utc_now()
            session.flush()
            return _character_detail(session, character)

    def list_echoes_for_assignment(
        self,
        *,
        data_set_id: str,
        character_id: str | None = None,
    ) -> tuple[EchoAssignmentCandidate, ...]:
        """Return all non-archived Echoes, including explicitly replaceable assignments."""

        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            if character_id is not None:
                _require_character(session, character_id, data_set_id)
            records = session.scalars(
                select(EchoRecord)
                .where(
                    EchoRecord.data_set_id == data_set_id,
                    EchoRecord.archived_at.is_(None),
                    ~exists(
                        select(EchoSessionRecord.id).where(
                            EchoSessionRecord.echo_id == EchoRecord.id,
                            EchoSessionRecord.was_consumed.is_(True),
                        )
                    ),
                )
                .order_by(EchoRecord.name, EchoRecord.level.desc(), EchoRecord.id)
            ).all()
            candidates: list[EchoAssignmentCandidate] = []
            for record in records:
                current_character = (
                    None
                    if record.character_id is None
                    else session.get(CharacterRecord, record.character_id)
                )
                candidates.append(
                    EchoAssignmentCandidate(
                        id=record.id,
                        name=record.name,
                        set_name=record.set_name,
                        cost=record.cost,
                        level=record.level,
                        score=record.score,
                        current_character_id=record.character_id,
                        current_character_name=(
                            None if current_character is None else current_character.name
                        ),
                        current_slot_index=record.slot_index,
                        is_assigned_to_requested_character=(
                            character_id is not None and record.character_id == character_id
                        ),
                    )
                )
            return tuple(candidates)

    def assign_echo_slot(
        self,
        character_id: str,
        slot_index: int,
        echo_id: str,
        *,
        data_set_id: str,
        replace: bool = False,
    ) -> CharacterDetail:
        with self._session_factory.begin() as session:
            character = assign_echo_record(
                session,
                character_id,
                slot_index,
                echo_id,
                data_set_id=data_set_id,
                replace=replace,
            )
            return _character_detail(session, character)

    def unassign_echo_slot(
        self,
        character_id: str,
        slot_index: int,
        *,
        data_set_id: str,
    ) -> CharacterDetail:
        _validate_echo_slot(slot_index)
        with self._session_factory.begin() as session:
            character = _require_character(session, character_id, data_set_id)
            echo = session.scalar(
                select(EchoRecord).where(
                    EchoRecord.character_id == character.id,
                    EchoRecord.slot_index == slot_index,
                    EchoRecord.archived_at.is_(None),
                )
            )
            if echo is not None:
                _clear_echo_assignment(echo)
                session.flush()
            return _character_detail(session, character)

    def create_team(
        self,
        *,
        data_set_id: str,
        command: CreateTeam,
        members: Sequence[TeamMemberInput] = (),
    ) -> TeamSnapshot:
        return self.save_team(
            data_set_id=data_set_id,
            command=SaveTeam(team=command, members=tuple(members)),
        )

    def save_team(
        self,
        *,
        data_set_id: str,
        command: SaveTeam,
        team_id: str | None = None,
    ) -> TeamSnapshot:
        """Persist team fields and members together so failed membership rolls back fields."""

        if not isinstance(command, SaveTeam):
            raise ValueError("command must be SaveTeam")
        safe_members = _normalize_team_members(command.members)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            _validate_member_characters(session, data_set_id, safe_members)
            if team_id is None:
                if not isinstance(command.team, CreateTeam):
                    raise ValueError("creating a team requires CreateTeam")
                create = _normalize_create_team(command.team)
                _ensure_team_name_available(session, data_set_id, create.name)
                team = TeamRecord(
                    data_set_id=data_set_id,
                    name=create.name,
                    priority=create.priority,
                    notes=create.notes,
                )
                session.add(team)
                session.flush()
            else:
                if not isinstance(command.team, UpdateTeam):
                    raise ValueError("updating a team requires UpdateTeam")
                update = _normalize_update_team(command.team)
                team = _require_team(session, team_id, data_set_id)
                _ensure_team_name_available(
                    session,
                    data_set_id,
                    update.name,
                    excluding_team_id=team.id,
                )
                team.name = update.name
                team.priority = update.priority
                team.notes = update.notes
                existing = session.scalars(
                    select(TeamMemberRecord).where(TeamMemberRecord.team_id == team.id)
                ).all()
                for member in existing:
                    session.delete(member)
                session.flush()
            _add_team_members(session, team.id, safe_members)
            team.updated_at = utc_now()
            session.flush()
            return _team_snapshot(session, team)

    def update_team(
        self,
        team_id: str,
        *,
        data_set_id: str,
        command: UpdateTeam,
    ) -> TeamSnapshot:
        normalized = _normalize_update_team(command)
        with self._session_factory.begin() as session:
            team = _require_team(session, team_id, data_set_id)
            _ensure_team_name_available(
                session,
                data_set_id,
                normalized.name,
                excluding_team_id=team.id,
            )
            team.name = normalized.name
            team.priority = normalized.priority
            team.notes = normalized.notes
            team.updated_at = utc_now()
            session.flush()
            return _team_snapshot(session, team)

    def get_team(self, team_id: str, *, data_set_id: str) -> TeamSnapshot:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            return _team_snapshot(session, _require_team(session, team_id, data_set_id))

    def list_teams(self, *, data_set_id: str) -> tuple[TeamSnapshot, ...]:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            teams = session.scalars(
                select(TeamRecord)
                .where(TeamRecord.data_set_id == data_set_id)
                .order_by(TeamRecord.priority, TeamRecord.name, TeamRecord.id)
            ).all()
            return tuple(_team_snapshot(session, team) for team in teams)

    def replace_team_members(
        self,
        team_id: str,
        members: Sequence[TeamMemberInput],
        *,
        data_set_id: str,
    ) -> TeamSnapshot:
        safe_members = _normalize_team_members(members)
        with self._session_factory.begin() as session:
            team = _require_team(session, team_id, data_set_id)
            _validate_member_characters(session, data_set_id, safe_members)
            existing = session.scalars(
                select(TeamMemberRecord).where(TeamMemberRecord.team_id == team.id)
            ).all()
            for member in existing:
                session.delete(member)
            session.flush()
            _add_team_members(session, team.id, safe_members)
            team.updated_at = utc_now()
            session.flush()
            return _team_snapshot(session, team)

    def delete_team(self, team_id: str, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            team = _require_team(session, team_id, data_set_id)
            session.delete(team)
            session.flush()
            return True


def ensure_recognized_character(
    session: Session,
    *,
    data_set_id: str,
    name: str,
) -> tuple[CharacterRecord | None, bool]:
    """Resolve a recognized owner or add its initial record in this transaction.

    The caller validates recognition evidence. Existing personal fields are
    preserved, and an archived name is never duplicated or restored. New records
    use the same editable defaults and five skills as manual character creation;
    no element, role, or permanent Echo assignment is inferred from the name.
    """

    command = _normalize_create_character(CreateCharacter(name=canonical_character_name(name)))
    _require_data_set(session, data_set_id)
    normalized_name = character_name_key(command.name)
    matches = [
        record
        for record in session.scalars(
            select(CharacterRecord)
            .where(CharacterRecord.data_set_id == data_set_id)
            .order_by(CharacterRecord.name, CharacterRecord.id)
        )
        if character_name_key(record.name) == normalized_name
    ]
    if matches:
        active = next((record for record in matches if record.archived_at is None), None)
        return active, False
    return _create_character_record(session, data_set_id, command), True


def assign_echo_record(
    session: Session,
    character_id: str,
    slot_index: int,
    echo_id: str,
    *,
    data_set_id: str,
    replace: bool = False,
    recognized: bool = False,
) -> CharacterRecord:
    """Assign a permanent slot within the caller's transaction without committing.

    Live-history edits and cultivation editing share the same ownership,
    availability, and explicit-replacement rules. Flushes release occupied slot
    keys before replacement, while any later caller failure can roll back all
    assignment changes together with its other work.
    """

    _validate_echo_slot(slot_index)
    if not isinstance(replace, bool):
        raise ValueError("replace must be a boolean")
    character = _require_character(session, character_id, data_set_id)
    if character.archived_at is not None:
        raise ValueError("cannot equip an Echo to an archived character")
    echo = _require_echo(session, echo_id, data_set_id)
    if echo.archived_at is not None:
        raise ValueError("cannot equip an archived Echo")
    if session.scalar(
        select(
            exists().where(
                EchoSessionRecord.echo_id == echo.id,
                EchoSessionRecord.was_consumed.is_(True),
            )
        )
    ):
        raise ValueError("cannot equip a consumed Echo")

    occupying = session.scalar(
        select(EchoRecord).where(
            EchoRecord.character_id == character.id,
            EchoRecord.slot_index == slot_index,
            EchoRecord.archived_at.is_(None),
        )
    )
    same_assignment = echo.character_id == character.id and echo.slot_index == slot_index
    echo_assigned_elsewhere = echo.character_id is not None and (
        echo.character_id != character.id or (echo.slot_index is not None and not same_assignment)
    )
    occupied_by_other = occupying is not None and occupying.id != echo.id
    if (echo_assigned_elsewhere or occupied_by_other) and not replace:
        raise ValueError("Echo or target slot is already assigned; pass replace=True")

    if occupied_by_other and occupying is not None:
        _clear_echo_assignment(occupying)
    if echo_assigned_elsewhere:
        _clear_echo_assignment(echo)
    if occupied_by_other or echo_assigned_elsewhere:
        # Release partial-unique slot keys before writing the new key.
        session.flush()
    echo.character_id = character.id
    echo.slot_index = slot_index
    echo.final_status = "equipped"
    echo.equipped_at = utc_now()
    echo.updated_at = utc_now()
    for echo_session in session.scalars(
        select(EchoSessionRecord).where(EchoSessionRecord.echo_id == echo.id)
    ):
        echo_session.metadata_json = {
            **echo_session.metadata_json,
            "equipment_assignment_source": "recognition" if recognized else "manual",
        }
    session.flush()
    return character


def _create_character_record(
    session: Session,
    data_set_id: str,
    command: CreateCharacter,
) -> CharacterRecord:
    _ensure_character_name_available(session, data_set_id, command.name)
    if command.scoring_profile_id is not None:
        _require_visible_profile(session, command.scoring_profile_id, data_set_id)
    record = CharacterRecord(
        data_set_id=data_set_id,
        name=command.name,
        element=command.element,
        role=command.role,
        is_candidate=command.is_candidate,
        priority=command.priority,
        level=command.level,
        resonance_chain=command.resonance_chain,
        scoring_profile_id=command.scoring_profile_id,
        cultivation_goal_json=_goal_with_nodes(
            command.cultivation_goal,
            command.small_stat_nodes,
        ),
        notes=command.notes,
    )
    session.add(record)
    session.flush()
    session.add_all(
        [
            CharacterSkillRecord(
                character_id=record.id,
                skill_key=definition.skill_key,
                name=definition.name,
                skill_type=definition.skill_type,
                current_level=1,
                target_level=_skill_target(definition),
                is_core=definition.is_core,
                is_completed=_skill_target(definition) <= 1,
                display_order=index,
            )
            for index, definition in enumerate(command.skills or DEFAULT_SKILLS)
        ]
    )
    session.flush()
    return record


def _apply_character_update(
    session: Session,
    record: CharacterRecord,
    data_set_id: str,
    command: UpdateCharacter,
) -> None:
    _ensure_character_name_available(
        session,
        data_set_id,
        command.name,
        excluding_character_id=record.id,
    )
    if command.scoring_profile_id is not None:
        _require_visible_profile(session, command.scoring_profile_id, data_set_id)
    if record.archived_at is not None and command.is_candidate:
        raise ValueError("an archived character cannot be marked as a candidate")
    record.name = command.name
    record.element = command.element
    record.role = command.role
    record.is_candidate = command.is_candidate
    record.priority = command.priority
    record.level = command.level
    record.resonance_chain = command.resonance_chain
    record.scoring_profile_id = command.scoring_profile_id
    record.notes = command.notes
    if command.cultivation_goal is not None or command.small_stat_nodes is not None:
        current_goal = _json_object(record.cultivation_goal_json, "cultivation goal")
        goal = current_goal if command.cultivation_goal is None else command.cultivation_goal
        nodes = (
            _nodes_from_goal(current_goal)
            if command.small_stat_nodes is None
            else command.small_stat_nodes
        )
        record.cultivation_goal_json = _goal_with_nodes(goal, nodes)
    record.updated_at = utc_now()


def _upsert_weapon_record(
    session: Session,
    character: CharacterRecord,
    data_set_id: str,
    command: UpsertWeapon,
) -> WeaponRecord:
    weapon = session.scalar(select(WeaponRecord).where(WeaponRecord.character_id == character.id))
    if weapon is None:
        weapon = WeaponRecord(
            data_set_id=data_set_id,
            character_id=character.id,
            name=command.name,
        )
        session.add(weapon)
    weapon.data_set_id = data_set_id
    weapon.name = command.name
    weapon.weapon_type = command.weapon_type
    weapon.level = command.level
    weapon.resonance_rank = command.resonance_rank
    weapon.notes = command.notes
    weapon.updated_at = utc_now()
    session.flush()
    return weapon


def _apply_skill_updates(
    session: Session,
    character_id: str,
    commands: Sequence[UpdateSkill],
) -> None:
    records = session.scalars(
        select(CharacterSkillRecord).where(CharacterSkillRecord.character_id == character_id)
    ).all()
    by_key = {record.skill_key: record for record in records}
    unknown = [command.skill_key for command in commands if command.skill_key not in by_key]
    if unknown:
        raise ValueError(f"skill does not exist: {unknown[0]}")
    for command in commands:
        record = by_key[command.skill_key]
        record.name = command.name
        record.skill_type = command.skill_type
        record.current_level = command.current_level
        record.target_level = command.target_level
        record.is_core = command.is_core
        if command.display_order is not None:
            record.display_order = command.display_order
        record.is_completed = command.current_level >= command.target_level
        record.updated_at = utc_now()


def _normalize_create_character(command: CreateCharacter) -> CreateCharacter:
    if not isinstance(command, CreateCharacter):
        raise ValueError("command must be CreateCharacter")
    skills = DEFAULT_SKILLS if command.skills is None else command.skills
    normalized_skills = _normalize_skill_definitions(skills)
    return CreateCharacter(
        name=_required_text(command.name, "character name", 120),
        element=_optional_text(command.element, "element", 60),
        role=_optional_text(command.role, "role", 100),
        is_candidate=_strict_bool(command.is_candidate, "is_candidate"),
        priority=_nonnegative_int(command.priority, "priority"),
        level=_bounded_int(command.level, "level", minimum=0, maximum=90),
        resonance_chain=_bounded_int(
            command.resonance_chain,
            "resonance_chain",
            minimum=0,
            maximum=6,
        ),
        scoring_profile_id=_optional_id(command.scoring_profile_id, "scoring_profile_id"),
        cultivation_goal=_json_object(command.cultivation_goal, "cultivation goal"),
        small_stat_nodes=_normalize_nodes(command.small_stat_nodes),
        notes=_optional_text(command.notes, "notes", None),
        skills=normalized_skills,
    )


def _normalize_update_character(command: UpdateCharacter) -> UpdateCharacter:
    if not isinstance(command, UpdateCharacter):
        raise ValueError("command must be UpdateCharacter")
    return UpdateCharacter(
        name=_required_text(command.name, "character name", 120),
        element=_optional_text(command.element, "element", 60),
        role=_optional_text(command.role, "role", 100),
        is_candidate=_strict_bool(command.is_candidate, "is_candidate"),
        priority=_nonnegative_int(command.priority, "priority"),
        level=_bounded_int(command.level, "level", minimum=0, maximum=90),
        resonance_chain=_bounded_int(
            command.resonance_chain,
            "resonance_chain",
            minimum=0,
            maximum=6,
        ),
        scoring_profile_id=_optional_id(command.scoring_profile_id, "scoring_profile_id"),
        notes=_optional_text(command.notes, "notes", None),
        cultivation_goal=(
            None
            if command.cultivation_goal is None
            else _json_object(command.cultivation_goal, "cultivation goal")
        ),
        small_stat_nodes=(
            None if command.small_stat_nodes is None else _normalize_nodes(command.small_stat_nodes)
        ),
    )


def _normalize_weapon(command: UpsertWeapon) -> UpsertWeapon:
    if not isinstance(command, UpsertWeapon):
        raise ValueError("command must be UpsertWeapon")
    return UpsertWeapon(
        name=_required_text(command.name, "weapon name", 160),
        weapon_type=_optional_text(command.weapon_type, "weapon type", 60),
        level=_bounded_int(command.level, "weapon level", minimum=0, maximum=90),
        resonance_rank=_bounded_int(
            command.resonance_rank,
            "resonance rank",
            minimum=1,
            maximum=5,
        ),
        notes=_optional_text(command.notes, "notes", None),
    )


def _normalize_skill_update(command: UpdateSkill) -> UpdateSkill:
    if not isinstance(command, UpdateSkill):
        raise ValueError("command must be UpdateSkill")
    return UpdateSkill(
        skill_key=_required_text(command.skill_key, "skill key", 100),
        name=_required_text(command.name, "skill name", 160),
        skill_type=_required_text(command.skill_type, "skill type", 40),
        current_level=_bounded_int(
            command.current_level,
            "current skill level",
            minimum=0,
            maximum=10,
        ),
        target_level=_bounded_int(
            command.target_level,
            "target skill level",
            minimum=1,
            maximum=10,
        ),
        is_core=_strict_bool(command.is_core, "is_core"),
        display_order=None
        if command.display_order is None
        else _bounded_int(command.display_order, "skill priority", minimum=0, maximum=4),
    )


def _normalize_skill_updates(
    commands: Sequence[UpdateSkill],
    *,
    require_all_five: bool,
) -> tuple[UpdateSkill, ...]:
    if isinstance(commands, (str, bytes)):
        raise ValueError("skills must be a sequence of UpdateSkill values")
    if require_all_five and len(commands) != 5:
        raise ValueError("an aggregate character edit must contain exactly five skills")
    normalized = tuple(_normalize_skill_update(command) for command in commands)
    keys = [command.skill_key for command in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("skill keys must be unique")
    return normalized


def _normalize_skill_definitions(
    definitions: Sequence[SkillDefinition],
) -> tuple[SkillDefinition, ...]:
    if isinstance(definitions, (str, bytes)) or len(definitions) != 5:
        raise ValueError("a character must have exactly five skill definitions")
    normalized: list[SkillDefinition] = []
    for definition in definitions:
        if not isinstance(definition, SkillDefinition):
            raise ValueError("skills must contain SkillDefinition values")
        target = (
            None
            if definition.target_level is None
            else _bounded_int(
                definition.target_level,
                "target skill level",
                minimum=1,
                maximum=10,
            )
        )
        normalized.append(
            SkillDefinition(
                skill_key=_required_text(definition.skill_key, "skill key", 100),
                name=_required_text(definition.name, "skill name", 160),
                skill_type=_required_text(definition.skill_type, "skill type", 40),
                is_core=_strict_bool(definition.is_core, "is_core"),
                target_level=target,
            )
        )
    keys = [item.skill_key for item in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("skill keys must be unique")
    return tuple(normalized)


def _skill_target(definition: SkillDefinition) -> int:
    if definition.target_level is not None:
        return definition.target_level
    return CORE_SKILL_TARGET if definition.is_core else SECONDARY_SKILL_TARGET


def _normalize_create_team(command: CreateTeam) -> CreateTeam:
    if not isinstance(command, CreateTeam):
        raise ValueError("command must be CreateTeam")
    return CreateTeam(
        name=_required_text(command.name, "team name", 160),
        priority=_nonnegative_int(command.priority, "priority"),
        notes=_optional_text(command.notes, "notes", None),
    )


def _normalize_update_team(command: UpdateTeam) -> UpdateTeam:
    if not isinstance(command, UpdateTeam):
        raise ValueError("command must be UpdateTeam")
    return UpdateTeam(
        name=_required_text(command.name, "team name", 160),
        priority=_nonnegative_int(command.priority, "priority"),
        notes=_optional_text(command.notes, "notes", None),
    )


def _normalize_team_members(
    members: Sequence[TeamMemberInput],
) -> tuple[TeamMemberInput, ...]:
    if isinstance(members, (str, bytes)) or len(members) > TEAM_SIZE:
        raise ValueError(f"a team can contain at most {TEAM_SIZE} members")
    normalized: list[TeamMemberInput] = []
    for member in members:
        if not isinstance(member, TeamMemberInput):
            raise ValueError("members must contain TeamMemberInput values")
        slot = _nonnegative_int(member.slot_index, "team slot")
        if not 1 <= slot <= TEAM_SIZE:
            raise ValueError(f"team slot must be within [1, {TEAM_SIZE}]")
        normalized.append(
            TeamMemberInput(
                character_id=_required_text(member.character_id, "character id", 36),
                slot_index=slot,
                role=_optional_text(member.role, "team role", 100),
            )
        )
    character_ids = [item.character_id for item in normalized]
    slots = [item.slot_index for item in normalized]
    if len(character_ids) != len(set(character_ids)):
        raise ValueError("a character cannot occupy multiple team slots")
    if len(slots) != len(set(slots)):
        raise ValueError("team slots must be unique")
    return tuple(sorted(normalized, key=lambda item: item.slot_index))


def _validate_member_characters(
    session: Session,
    data_set_id: str,
    members: Sequence[TeamMemberInput],
) -> None:
    for member in members:
        character = _require_character(session, member.character_id, data_set_id)
        if character.archived_at is not None:
            raise ValueError("an archived character cannot be added to a team")


def _add_team_members(
    session: Session,
    team_id: str,
    members: Sequence[TeamMemberInput],
) -> None:
    session.add_all(
        [
            TeamMemberRecord(
                team_id=team_id,
                character_id=member.character_id,
                slot_index=member.slot_index,
                role=member.role,
            )
            for member in members
        ]
    )


def _require_data_set(session: Session, data_set_id: str) -> DataSetRecord:
    record = session.get(DataSetRecord, data_set_id)
    if record is None:
        raise ValueError("data set does not exist")
    return record


def _require_character(
    session: Session,
    character_id: str,
    data_set_id: str,
) -> CharacterRecord:
    record = session.get(CharacterRecord, character_id)
    if record is None:
        raise ValueError("character does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("character does not belong to the requested data set")
    return record


def _require_team(session: Session, team_id: str, data_set_id: str) -> TeamRecord:
    record = session.get(TeamRecord, team_id)
    if record is None:
        raise ValueError("team does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("team does not belong to the requested data set")
    return record


def _require_echo(session: Session, echo_id: str, data_set_id: str) -> EchoRecord:
    record = session.get(EchoRecord, echo_id)
    if record is None:
        raise ValueError("Echo does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("Echo does not belong to the requested data set")
    return record


def _require_visible_profile(
    session: Session,
    profile_id: str,
    data_set_id: str,
) -> ScoringProfileRecord:
    record = session.get(ScoringProfileRecord, profile_id)
    if record is None:
        raise ValueError("scoring profile does not exist")
    is_visible_template = record.data_set_id is None and record.is_system_template
    if record.data_set_id != data_set_id and not is_visible_template:
        raise ValueError("scoring profile does not belong to the requested data set")
    return record


def _ensure_character_name_available(
    session: Session,
    data_set_id: str,
    name: str,
    *,
    excluding_character_id: str | None = None,
) -> None:
    owner = session.scalar(
        select(CharacterRecord).where(
            CharacterRecord.data_set_id == data_set_id,
            CharacterRecord.name == name,
        )
    )
    if owner is not None and owner.id != excluding_character_id:
        raise ValueError("character name already exists in this data set")


def _ensure_team_name_available(
    session: Session,
    data_set_id: str,
    name: str,
    *,
    excluding_team_id: str | None = None,
) -> None:
    owner = session.scalar(
        select(TeamRecord).where(
            TeamRecord.data_set_id == data_set_id,
            TeamRecord.name == name,
        )
    )
    if owner is not None and owner.id != excluding_team_id:
        raise ValueError("team name already exists in this data set")


@dataclass(slots=True)
class _CharacterRows:
    skills: dict[str, list[CharacterSkillRecord]] = field(default_factory=lambda: defaultdict(list))
    echoes: dict[str, list[EchoRecord]] = field(default_factory=lambda: defaultdict(list))
    weapons: dict[str, WeaponRecord] = field(default_factory=dict)
    teams: dict[str, list[TeamMembershipSnapshot]] = field(
        default_factory=lambda: defaultdict(list)
    )
    profile_names: dict[str, str] = field(default_factory=dict)


def _character_details(
    session: Session, records: Sequence[CharacterRecord]
) -> tuple[CharacterDetail, ...]:
    rows = _CharacterRows()
    # Stay below SQLite's legacy 999-variable limit, including large imports.
    for offset in range(0, len(records), 400):
        batch = records[offset : offset + 400]
        ids = tuple(record.id for record in batch)
        for skill in session.scalars(
            select(CharacterSkillRecord)
            .where(CharacterSkillRecord.character_id.in_(ids))
            .order_by(CharacterSkillRecord.display_order, CharacterSkillRecord.id)
        ):
            rows.skills[skill.character_id].append(skill)
        for echo in session.scalars(
            select(EchoRecord)
            .join(CharacterRecord, EchoRecord.character_id == CharacterRecord.id)
            .where(
                EchoRecord.character_id.in_(ids),
                EchoRecord.data_set_id == CharacterRecord.data_set_id,
                EchoRecord.archived_at.is_(None),
            )
            .order_by(EchoRecord.created_at, EchoRecord.id)
        ):
            assert echo.character_id is not None
            rows.echoes[echo.character_id].append(echo)
        for weapon in session.scalars(
            select(WeaponRecord)
            .join(CharacterRecord, WeaponRecord.character_id == CharacterRecord.id)
            .where(
                WeaponRecord.character_id.in_(ids),
                WeaponRecord.data_set_id == CharacterRecord.data_set_id,
            )
        ):
            assert weapon.character_id is not None
            rows.weapons[weapon.character_id] = weapon
        for member, team in session.execute(
            select(TeamMemberRecord, TeamRecord)
            .join(TeamRecord, TeamRecord.id == TeamMemberRecord.team_id)
            .join(CharacterRecord, TeamMemberRecord.character_id == CharacterRecord.id)
            .where(
                TeamMemberRecord.character_id.in_(ids),
                TeamRecord.data_set_id == CharacterRecord.data_set_id,
            )
            .order_by(TeamRecord.priority, TeamRecord.name, TeamMemberRecord.slot_index)
        ):
            rows.teams[member.character_id].append(
                TeamMembershipSnapshot(
                    team_id=team.id,
                    team_name=team.name,
                    team_priority=team.priority,
                    slot_index=member.slot_index,
                    role=member.role,
                )
            )
        profile_ids = {record.scoring_profile_id for record in batch} - {None}
        if profile_ids:
            rows.profile_names.update(
                (profile_id, name)
                for profile_id, name in session.execute(
                    select(ScoringProfileRecord.id, ScoringProfileRecord.name).where(
                        ScoringProfileRecord.id.in_(profile_ids)
                    )
                )
            )
    return tuple(_character_detail_from_rows(record, rows) for record in records)


def _character_detail(session: Session, record: CharacterRecord) -> CharacterDetail:
    return _character_details(session, (record,))[0]


def _character_detail_from_rows(record: CharacterRecord, rows: _CharacterRows) -> CharacterDetail:
    goal = _json_object(record.cultivation_goal_json, "cultivation goal")
    skills = tuple(
        SkillSnapshot(
            id=row.id,
            skill_key=row.skill_key,
            name=row.name,
            skill_type=row.skill_type,
            current_level=row.current_level,
            target_level=row.target_level,
            is_core=row.is_core,
            is_completed=row.is_completed,
            display_order=row.display_order,
        )
        for row in rows.skills[record.id]
    )
    echoes = rows.echoes[record.id]
    weapon = rows.weapons.get(record.id)
    return CharacterDetail(
        summary=CharacterSummary(
            id=record.id,
            data_set_id=record.data_set_id,
            name=record.name,
            element=record.element,
            role=record.role,
            is_candidate=record.is_candidate,
            priority=record.priority,
            level=record.level,
            resonance_chain=record.resonance_chain,
            scoring_profile_id=record.scoring_profile_id,
            scoring_profile_name=rows.profile_names.get(record.scoring_profile_id or ""),
            completed_skill_count=sum(item.is_completed for item in skills),
            skill_count=len(skills),
            completed_small_stat_nodes=sum(_nodes_from_goal(goal)),
            equipped_echo_count=sum(echo.slot_index is not None for echo in echoes),
            archived_at=_optional_iso(record.archived_at),
        ),
        cultivation_goal=goal,
        small_stat_nodes=_nodes_from_goal(goal),
        notes=record.notes,
        weapon=None
        if weapon is None
        else WeaponSnapshot(
            id=weapon.id,
            name=weapon.name,
            weapon_type=weapon.weapon_type,
            level=weapon.level,
            resonance_rank=weapon.resonance_rank,
            notes=weapon.notes,
        ),
        skills=skills,
        echo_slots=_echo_slots(echoes),
        teams=tuple(rows.teams[record.id]),
        created_at=_iso(record.created_at),
        updated_at=_iso(record.updated_at),
        unpositioned_echoes=tuple(
            EchoAssignmentCandidate(
                id=echo.id,
                name=echo.name,
                set_name=echo.set_name,
                cost=echo.cost,
                level=echo.level,
                score=echo.score,
                current_character_id=record.id,
                current_character_name=record.name,
                current_slot_index=None,
                is_assigned_to_requested_character=True,
            )
            for echo in echoes
            if echo.slot_index is None
        ),
    )


def _echo_slots(records: Sequence[EchoRecord]) -> tuple[EchoSlotSnapshot, ...]:
    by_slot = {record.slot_index: record for record in records if record.slot_index is not None}
    snapshots: list[EchoSlotSnapshot] = []
    for slot_index in range(1, 6):
        record = by_slot.get(slot_index)
        snapshots.append(
            EchoSlotSnapshot(
                slot_index=slot_index,
                echo_id=None if record is None else record.id,
                name=None if record is None else record.name,
                set_name=None if record is None else record.set_name,
                cost=None if record is None else record.cost,
                level=None if record is None else record.level,
                score=None if record is None else record.score,
                final_status=None if record is None else record.final_status,
                main_stats=()
                if record is None
                else tuple(_json_object(item, "main stat") for item in record.main_stats_json),
                sub_stats=()
                if record is None
                else tuple(_json_object(item, "sub stat") for item in record.sub_stats_json),
                score_profile_id=None if record is None else record.score_profile_id,
                score_profile_version=None if record is None else record.score_profile_version,
                score_observation_id=None if record is None else record.score_observation_id,
            )
        )
    return tuple(snapshots)


def _team_snapshot(session: Session, team: TeamRecord) -> TeamSnapshot:
    rows = session.execute(
        select(TeamMemberRecord, CharacterRecord)
        .join(CharacterRecord, CharacterRecord.id == TeamMemberRecord.character_id)
        .where(TeamMemberRecord.team_id == team.id)
        .order_by(TeamMemberRecord.slot_index, CharacterRecord.name)
    ).all()
    return TeamSnapshot(
        id=team.id,
        data_set_id=team.data_set_id,
        name=team.name,
        priority=team.priority,
        notes=team.notes,
        members=tuple(
            TeamMemberSnapshot(
                character_id=character.id,
                character_name=character.name,
                slot_index=member.slot_index,
                role=member.role,
            )
            for member, character in rows
        ),
        created_at=_iso(team.created_at),
        updated_at=_iso(team.updated_at),
    )


def _goal_with_nodes(goal: Mapping[str, object], nodes: Sequence[bool]) -> JsonObject:
    normalized_goal = _json_object(goal, "cultivation goal")
    normalized_nodes = _normalize_nodes(nodes)
    normalized_goal["small_stat_nodes"] = list(normalized_nodes)
    defaults = normalized_goal.get("skill_target_defaults")
    if defaults is None:
        normalized_goal["skill_target_defaults"] = {
            "core": CORE_SKILL_TARGET,
            "secondary": SECONDARY_SKILL_TARGET,
        }
    elif not isinstance(defaults, Mapping):
        raise ValueError("skill_target_defaults must be a JSON object")
    return normalized_goal


def _nodes_from_goal(goal: Mapping[str, object]) -> tuple[bool, ...]:
    value = goal.get("small_stat_nodes", (False,) * SMALL_STAT_NODE_COUNT)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("small_stat_nodes must be a JSON array")
    return _normalize_nodes(value)


def _normalize_nodes(values: Sequence[object]) -> tuple[bool, ...]:
    if isinstance(values, (str, bytes)) or len(values) != SMALL_STAT_NODE_COUNT:
        raise ValueError(f"small_stat_nodes must contain {SMALL_STAT_NODE_COUNT} booleans")
    if any(not isinstance(value, bool) for value in values):
        raise ValueError("small_stat_nodes must contain only booleans")
    return tuple(bool(value) for value in values)


def _clear_echo_assignment(echo: EchoRecord) -> None:
    echo.character_id = None
    echo.slot_index = None
    echo.equipped_at = None
    if echo.final_status == "equipped":
        echo.final_status = "kept"
    echo.updated_at = utc_now()


def _validate_echo_slot(slot_index: int) -> None:
    if isinstance(slot_index, bool) or not isinstance(slot_index, int) or not 1 <= slot_index <= 5:
        raise ValueError("slot_index must be within [1, 5]")


def _json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    candidate = dict(value)
    try:
        encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON-safe values") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return deepcopy(decoded)


def _required_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return normalized


def _optional_text(value: object, label: str, maximum: int | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text or null")
    normalized = value.strip()
    if not normalized:
        return None
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return normalized


def _optional_id(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, 36)


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _bounded_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer within [{minimum}, {maximum}]")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _iso(value: datetime) -> str:
    return value.isoformat()


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


__all__ = [
    "CORE_SKILL_TARGET",
    "DEFAULT_SKILLS",
    "SECONDARY_SKILL_TARGET",
    "SMALL_STAT_NODE_COUNT",
    "TEAM_SIZE",
    "CharacterCultivationService",
    "CharacterDetail",
    "CharacterSummary",
    "CreateCharacter",
    "CreateTeam",
    "EchoAssignmentCandidate",
    "EchoSlotSnapshot",
    "SaveCharacter",
    "SkillDefinition",
    "SkillSnapshot",
    "TeamMemberInput",
    "TeamMemberSnapshot",
    "TeamMembershipSnapshot",
    "TeamSnapshot",
    "UpdateCharacter",
    "UpdateSkill",
    "UpdateTeam",
    "UpsertWeapon",
    "WeaponSnapshot",
]
