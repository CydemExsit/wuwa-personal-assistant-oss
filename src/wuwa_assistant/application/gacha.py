"""Dataset-scoped application boundary for manual gacha planning.

All values are entered or imported locally.  This service never reads an
account API or log and never derives pull odds.  The raw scenario JSON is kept
for forward compatibility, while V1 planning consumes only explicit values in
``required_pulls``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from wuwa_assistant.gacha.models import (
    GachaIncome,
    GachaPlan,
    GachaResources,
    GachaStatus,
    GachaTarget,
    IncomeScenario,
    ManualPullRequirements,
    PullType,
)
from wuwa_assistant.gacha.planner import plan_gacha_resources
from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    DataSetRecord,
    GachaIncomeItemRecord,
    GachaStatusRecord,
    GachaTargetRecord,
    utc_now,
)

type JsonObject = dict[str, Any]

_TARGET_TYPES = frozenset({PullType.CHARACTER.value, PullType.WEAPON.value})
_TARGET_STATUSES = frozenset(
    {"planned", "active", "obtained", "acquired", "completed", "cancelled", "skipped"}
)
_INCOME_SCENARIOS = frozenset(item.value for item in IncomeScenario)
_PULL_TYPES = frozenset(item.value for item in PullType)
_TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class GachaStatusDefinition:
    astrite: int = 0
    limited_character_pulls: int = 0
    limited_weapon_pulls: int = 0
    character_pity: int = 0
    weapon_pity: int = 0
    character_guaranteed: bool = False
    weapon_guaranteed: bool = False


@dataclass(frozen=True, slots=True)
class GachaTargetDefinition:
    target_type: str
    name: str
    target_copies: int = 1
    target_version: str | None = None
    target_date: date | None = None
    priority: int = 100
    status: str = "planned"
    scenario_json: JsonObject = field(default_factory=dict)
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class GachaIncomeDefinition:
    label: str
    expected_date: date | None = None
    astrite_amount: int = 0
    pull_amount: int = 0
    pull_type: str = PullType.UNKNOWN.value
    scenario: str = IncomeScenario.GENERAL.value
    recurrence_rule_json: JsonObject | None = None
    is_received: bool = False
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class GachaStatusSnapshot:
    id: str
    data_set_id: str
    astrite: int
    limited_character_pulls: int
    limited_weapon_pulls: int
    character_pity: int
    weapon_pity: int
    character_guaranteed: bool
    weapon_guaranteed: bool
    updated_at: str

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "astrite": self.astrite,
            "limited_character_pulls": self.limited_character_pulls,
            "limited_weapon_pulls": self.limited_weapon_pulls,
            "character_pity": self.character_pity,
            "weapon_pity": self.weapon_pity,
            "character_guaranteed": self.character_guaranteed,
            "weapon_guaranteed": self.weapon_guaranteed,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class GachaTargetSnapshot:
    id: str
    data_set_id: str
    target_type: str
    name: str
    target_copies: int
    target_version: str | None
    target_date: date | None
    priority: int
    status: str
    scenario_json: JsonObject
    notes: str | None
    created_at: str
    updated_at: str

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "target_type": self.target_type,
            "name": self.name,
            "target_copies": self.target_copies,
            "target_version": self.target_version,
            "target_date": None if self.target_date is None else self.target_date.isoformat(),
            "priority": self.priority,
            "status": self.status,
            "scenario_json": deepcopy(self.scenario_json),
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class GachaIncomeSnapshot:
    id: str
    data_set_id: str
    label: str
    expected_date: date | None
    astrite_amount: int
    pull_amount: int
    pull_type: str
    scenario: str
    recurrence_rule_json: JsonObject | None
    is_received: bool
    notes: str | None
    created_at: str
    updated_at: str

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "label": self.label,
            "expected_date": (
                None if self.expected_date is None else self.expected_date.isoformat()
            ),
            "astrite_amount": self.astrite_amount,
            "pull_amount": self.pull_amount,
            "pull_type": self.pull_type,
            "scenario": self.scenario,
            "recurrence_rule_json": deepcopy(self.recurrence_rule_json),
            "is_received": self.is_received,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class GachaWorkspaceSnapshot:
    status: GachaStatusSnapshot | None
    targets: tuple[GachaTargetSnapshot, ...]
    incomes: tuple[GachaIncomeSnapshot, ...]
    plan: GachaPlan


class GachaPlanningService:
    """Short-transaction CRUD plus one consistent planning snapshot."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def save_status(
        self, *, data_set_id: str, definition: GachaStatusDefinition
    ) -> GachaStatusSnapshot:
        safe = _normalize_status(definition)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            record = session.scalar(
                select(GachaStatusRecord).where(GachaStatusRecord.data_set_id == data_set_id)
            )
            if record is None:
                record = GachaStatusRecord(data_set_id=data_set_id)
                session.add(record)
            record.astrite = safe.astrite
            record.limited_character_pulls = safe.limited_character_pulls
            record.limited_weapon_pulls = safe.limited_weapon_pulls
            record.character_pity = safe.character_pity
            record.weapon_pity = safe.weapon_pity
            record.character_guaranteed = safe.character_guaranteed
            record.weapon_guaranteed = safe.weapon_guaranteed
            record.updated_at = utc_now()
            session.flush()
            return _status_snapshot(record)

    def get_status(self, *, data_set_id: str) -> GachaStatusSnapshot | None:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            record = session.scalar(
                select(GachaStatusRecord).where(GachaStatusRecord.data_set_id == data_set_id)
            )
            return None if record is None else _status_snapshot(record)

    def delete_status(self, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            record = session.scalar(
                select(GachaStatusRecord).where(GachaStatusRecord.data_set_id == data_set_id)
            )
            if record is None:
                return False
            session.delete(record)
            session.flush()
            return True

    def create_target(
        self, *, data_set_id: str, definition: GachaTargetDefinition
    ) -> GachaTargetSnapshot:
        safe = _normalize_target(definition)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            record = GachaTargetRecord(
                data_set_id=data_set_id,
                target_type=safe.target_type,
                name=safe.name,
                scenario_json=deepcopy(safe.scenario_json),
            )
            _apply_target(record, safe)
            session.add(record)
            session.flush()
            return _target_snapshot(record)

    def update_target(
        self,
        target_id: str,
        *,
        data_set_id: str,
        definition: GachaTargetDefinition,
    ) -> GachaTargetSnapshot:
        safe = _normalize_target(definition)
        with self._session_factory.begin() as session:
            record = _require_target(session, target_id, data_set_id)
            _apply_target(record, safe)
            session.flush()
            return _target_snapshot(record)

    def get_target(self, target_id: str, *, data_set_id: str) -> GachaTargetSnapshot:
        with self._session_factory() as session:
            return _target_snapshot(_require_target(session, target_id, data_set_id))

    def list_targets(
        self, *, data_set_id: str, include_inactive: bool = True
    ) -> tuple[GachaTargetSnapshot, ...]:
        if type(include_inactive) is not bool:
            raise ValueError("include_inactive must be a boolean")
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            statement = select(GachaTargetRecord).where(
                GachaTargetRecord.data_set_id == data_set_id
            )
            if not include_inactive:
                statement = statement.where(GachaTargetRecord.status.in_(("planned", "active")))
            rows = session.scalars(
                statement.order_by(
                    GachaTargetRecord.priority,
                    GachaTargetRecord.target_date.is_(None),
                    GachaTargetRecord.target_date,
                    GachaTargetRecord.id,
                )
            )
            return tuple(_target_snapshot(record) for record in rows)

    def delete_target(self, target_id: str, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            record = _require_target(session, target_id, data_set_id)
            session.delete(record)
            session.flush()
            return True

    def create_income(
        self, *, data_set_id: str, definition: GachaIncomeDefinition
    ) -> GachaIncomeSnapshot:
        safe = _normalize_income(definition)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            record = GachaIncomeItemRecord(
                data_set_id=data_set_id,
                label=safe.label,
                pull_type=safe.pull_type,
            )
            _apply_income(record, safe)
            session.add(record)
            session.flush()
            return _income_snapshot(record)

    def update_income(
        self,
        income_id: str,
        *,
        data_set_id: str,
        definition: GachaIncomeDefinition,
    ) -> GachaIncomeSnapshot:
        safe = _normalize_income(definition)
        with self._session_factory.begin() as session:
            record = _require_income(session, income_id, data_set_id)
            _apply_income(record, safe)
            session.flush()
            return _income_snapshot(record)

    def get_income(self, income_id: str, *, data_set_id: str) -> GachaIncomeSnapshot:
        with self._session_factory() as session:
            return _income_snapshot(_require_income(session, income_id, data_set_id))

    def list_incomes(
        self, *, data_set_id: str, include_received: bool = True
    ) -> tuple[GachaIncomeSnapshot, ...]:
        if type(include_received) is not bool:
            raise ValueError("include_received must be a boolean")
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            statement = select(GachaIncomeItemRecord).where(
                GachaIncomeItemRecord.data_set_id == data_set_id
            )
            if not include_received:
                statement = statement.where(GachaIncomeItemRecord.is_received.is_(False))
            rows = session.scalars(
                statement.order_by(
                    GachaIncomeItemRecord.expected_date.is_(None),
                    GachaIncomeItemRecord.expected_date,
                    GachaIncomeItemRecord.id,
                )
            )
            return tuple(_income_snapshot(record) for record in rows)

    def delete_income(self, income_id: str, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            record = _require_income(session, income_id, data_set_id)
            session.delete(record)
            session.flush()
            return True

    def build_plan(self, *, data_set_id: str, as_of: date | None = None) -> GachaPlan:
        """Read status, targets, and income in one consistent local snapshot."""

        return self.load_workspace(data_set_id=data_set_id, as_of=as_of).plan

    def load_workspace(
        self,
        *,
        data_set_id: str,
        as_of: date | None = None,
    ) -> GachaWorkspaceSnapshot:
        """Return detached CRUD rows and their plan from one database snapshot."""

        planning_date = datetime.now(_TAIPEI).date() if as_of is None else as_of
        if type(planning_date) is not date:
            raise ValueError("as_of must be a date")

        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            status_record = session.scalar(
                select(GachaStatusRecord).where(GachaStatusRecord.data_set_id == data_set_id)
            )
            target_records = tuple(
                session.scalars(
                    select(GachaTargetRecord)
                    .where(GachaTargetRecord.data_set_id == data_set_id)
                    .order_by(
                        GachaTargetRecord.priority,
                        GachaTargetRecord.target_date.is_(None),
                        GachaTargetRecord.target_date,
                        GachaTargetRecord.id,
                    )
                )
            )
            income_records = tuple(
                session.scalars(
                    select(GachaIncomeItemRecord)
                    .where(GachaIncomeItemRecord.data_set_id == data_set_id)
                    .order_by(
                        GachaIncomeItemRecord.expected_date.is_(None),
                        GachaIncomeItemRecord.expected_date,
                        GachaIncomeItemRecord.id,
                    )
                )
            )
            status = _domain_status(status_record)
            targets = tuple(_domain_target(record) for record in target_records)
            incomes = tuple(_domain_income(record) for record in income_records)
            return GachaWorkspaceSnapshot(
                status=None if status_record is None else _status_snapshot(status_record),
                targets=tuple(_target_snapshot(record) for record in target_records),
                incomes=tuple(_income_snapshot(record) for record in income_records),
                plan=plan_gacha_resources(
                    status=status,
                    targets=targets,
                    incomes=incomes,
                    as_of=planning_date,
                ),
            )


def parse_manual_pull_requirements(scenario_json: Mapping[str, object]) -> ManualPullRequirements:
    """Return only explicit manual values; invalid or missing fields stay null."""

    raw = scenario_json.get("required_pulls")
    requirements = raw if isinstance(raw, Mapping) else {}
    return ManualPullRequirements(
        conservative=_optional_requirement(requirements.get("conservative")),
        typical=_optional_requirement(requirements.get("typical")),
        optimistic=_optional_requirement(requirements.get("optimistic")),
    )


def _normalize_status(definition: GachaStatusDefinition) -> GachaStatusDefinition:
    return GachaStatusDefinition(
        astrite=_nonnegative_int(definition.astrite, "astrite"),
        limited_character_pulls=_nonnegative_int(
            definition.limited_character_pulls, "limited character pulls"
        ),
        limited_weapon_pulls=_nonnegative_int(
            definition.limited_weapon_pulls, "limited weapon pulls"
        ),
        character_pity=_nonnegative_int(definition.character_pity, "character pity"),
        weapon_pity=_nonnegative_int(definition.weapon_pity, "weapon pity"),
        character_guaranteed=_boolean(definition.character_guaranteed, "character guaranteed"),
        weapon_guaranteed=_boolean(definition.weapon_guaranteed, "weapon guaranteed"),
    )


def _normalize_target(definition: GachaTargetDefinition) -> GachaTargetDefinition:
    target_type = _choice(definition.target_type, "target type", _TARGET_TYPES)
    status = _choice(definition.status, "target status", _TARGET_STATUSES)
    return GachaTargetDefinition(
        target_type=target_type,
        name=_required_text(definition.name, "target name", 160),
        target_copies=_positive_int(definition.target_copies, "target copies"),
        target_version=_optional_text(definition.target_version, "target version", 80),
        target_date=_optional_date(definition.target_date, "target date"),
        priority=_nonnegative_int(definition.priority, "target priority"),
        status=status,
        scenario_json=_json_object(definition.scenario_json, "target scenario"),
        notes=_optional_text(definition.notes, "target notes", None),
    )


def _normalize_income(definition: GachaIncomeDefinition) -> GachaIncomeDefinition:
    return GachaIncomeDefinition(
        label=_required_text(definition.label, "income label", 200),
        expected_date=_optional_date(definition.expected_date, "income date"),
        astrite_amount=_nonnegative_int(definition.astrite_amount, "income astrite"),
        pull_amount=_nonnegative_int(definition.pull_amount, "income pulls"),
        pull_type=_choice(definition.pull_type, "income pull type", _PULL_TYPES),
        scenario=_choice(definition.scenario, "income scenario", _INCOME_SCENARIOS),
        recurrence_rule_json=(
            None
            if definition.recurrence_rule_json is None
            else _json_object(definition.recurrence_rule_json, "income recurrence")
        ),
        is_received=_boolean(definition.is_received, "income received"),
        notes=_optional_text(definition.notes, "income notes", None),
    )


def _apply_target(record: GachaTargetRecord, definition: GachaTargetDefinition) -> None:
    record.target_type = definition.target_type
    record.name = definition.name
    record.target_copies = definition.target_copies
    record.target_version = definition.target_version
    record.target_date = definition.target_date
    record.priority = definition.priority
    record.status = definition.status
    record.scenario_json = deepcopy(definition.scenario_json)
    record.notes = definition.notes
    record.updated_at = utc_now()


def _apply_income(record: GachaIncomeItemRecord, definition: GachaIncomeDefinition) -> None:
    record.label = definition.label
    record.expected_date = definition.expected_date
    record.astrite_amount = definition.astrite_amount
    record.pull_amount = definition.pull_amount
    record.pull_type = definition.pull_type
    record.scenario = definition.scenario
    record.recurrence_rule_json = deepcopy(definition.recurrence_rule_json)
    record.is_received = definition.is_received
    record.notes = definition.notes
    record.updated_at = utc_now()


def _status_snapshot(record: GachaStatusRecord) -> GachaStatusSnapshot:
    return GachaStatusSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        astrite=record.astrite,
        limited_character_pulls=record.limited_character_pulls,
        limited_weapon_pulls=record.limited_weapon_pulls,
        character_pity=record.character_pity,
        weapon_pity=record.weapon_pity,
        character_guaranteed=record.character_guaranteed,
        weapon_guaranteed=record.weapon_guaranteed,
        updated_at=_timestamp(record.updated_at),
    )


def _target_snapshot(record: GachaTargetRecord) -> GachaTargetSnapshot:
    return GachaTargetSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        target_type=record.target_type,
        name=record.name,
        target_copies=record.target_copies,
        target_version=record.target_version,
        target_date=record.target_date,
        priority=record.priority,
        status=record.status,
        scenario_json=_json_object(record.scenario_json, "stored target scenario"),
        notes=record.notes,
        created_at=_timestamp(record.created_at),
        updated_at=_timestamp(record.updated_at),
    )


def _income_snapshot(record: GachaIncomeItemRecord) -> GachaIncomeSnapshot:
    recurrence = (
        None
        if record.recurrence_rule_json is None
        else _json_object(record.recurrence_rule_json, "stored income recurrence")
    )
    return GachaIncomeSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        label=record.label,
        expected_date=record.expected_date,
        astrite_amount=record.astrite_amount,
        pull_amount=record.pull_amount,
        pull_type=record.pull_type,
        scenario=record.scenario,
        recurrence_rule_json=recurrence,
        is_received=record.is_received,
        notes=record.notes,
        created_at=_timestamp(record.created_at),
        updated_at=_timestamp(record.updated_at),
    )


def _domain_status(record: GachaStatusRecord | None) -> GachaStatus:
    if record is None:
        return GachaStatus(resources=GachaResources())
    return GachaStatus(
        resources=GachaResources(
            astrite=record.astrite,
            character_pulls=record.limited_character_pulls,
            weapon_pulls=record.limited_weapon_pulls,
        ),
        character_pity=record.character_pity,
        weapon_pity=record.weapon_pity,
        character_guaranteed=record.character_guaranteed,
        weapon_guaranteed=record.weapon_guaranteed,
    )


def _domain_target(record: GachaTargetRecord) -> GachaTarget:
    target_type = PullType(record.target_type)
    return GachaTarget(
        target_id=record.id,
        target_type=target_type,
        name=record.name,
        target_copies=record.target_copies,
        target_date=record.target_date,
        priority=record.priority,
        status=record.status,
        requirements=parse_manual_pull_requirements(record.scenario_json),
    )


def _domain_income(record: GachaIncomeItemRecord) -> GachaIncome:
    try:
        scenario: IncomeScenario | None = IncomeScenario(record.scenario)
    except ValueError:
        scenario = None
    try:
        pull_type = PullType(record.pull_type)
    except ValueError:
        pull_type = PullType.UNKNOWN
    return GachaIncome(
        income_id=record.id,
        label=record.label,
        expected_date=record.expected_date,
        astrite_amount=record.astrite_amount,
        pull_amount=record.pull_amount,
        pull_type=pull_type,
        scenario=scenario,
        is_received=record.is_received,
    )


def _require_data_set(session: Session, data_set_id: str) -> DataSetRecord:
    safe_id = _required_text(data_set_id, "data_set_id", 36)
    record = session.get(DataSetRecord, safe_id)
    if record is None:
        raise ValueError("data set does not exist")
    return record


def _require_target(session: Session, target_id: str, data_set_id: str) -> GachaTargetRecord:
    safe_id = _required_text(target_id, "gacha target id", 36)
    record = session.get(GachaTargetRecord, safe_id)
    if record is None:
        raise ValueError("gacha target does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("gacha target does not belong to the requested data set")
    return record


def _require_income(session: Session, income_id: str, data_set_id: str) -> GachaIncomeItemRecord:
    safe_id = _required_text(income_id, "gacha income id", 36)
    record = session.get(GachaIncomeItemRecord, safe_id)
    if record is None:
        raise ValueError("gacha income does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("gacha income does not belong to the requested data set")
    return record


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


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _choice(value: object, label: str, choices: frozenset[str]) -> str:
    normalized = _required_text(value, label, 40).casefold()
    if normalized not in choices:
        raise ValueError(f"{label} is not supported")
    return normalized


def _optional_date(value: object, label: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{label} must be a date or null")
    return value


def _json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON-compatible values") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return decoded


def _optional_requirement(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
