"""Dataset-scoped persistence for cultivation materials and daily plans.

This module is the application boundary for the manual, offline-first planner.
It deliberately accepts in-memory mappings rather than URLs or network clients:
versioned static material packages must be supplied explicitly by the caller.
Every write uses a short SQLAlchemy transaction and every returned value is a
detached immutable snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from wuwa_assistant.storage.engine import SessionFactory
from wuwa_assistant.storage.orm import (
    CharacterRecord,
    CultivationTargetMaterialRecord,
    CultivationTargetRecord,
    DailyPlanRecord,
    DataSetRecord,
    InventoryCountRecord,
    MaterialRecord,
    new_id,
    utc_now,
)

type JsonObject = dict[str, Any]
type MaterialPackageLike = StaticMaterialPackage | Mapping[str, object]

MATERIAL_PACKAGE_SCHEMA_VERSION = 1
_QUANTITY_QUANTUM = Decimal("0.0001")
_MAX_QUANTITY = Decimal("99999999999999.9999")
_ACTIVE_PLAN_STATUSES = frozenset({"planned", "completed", "deferred", "skipped"})
_PACKAGE_KEYS = frozenset({"schema_version", "source_version", "materials"})
_PACKAGE_MATERIAL_KEYS = frozenset(
    {
        "code",
        "name",
        "category",
        "stamina_cost_per_run",
        "average_yield",
        "open_days",
        "weekly_limit",
        "notes",
    }
)


@dataclass(frozen=True, slots=True)
class MaterialDefinition:
    """Editable fields for one manually maintained material."""

    code: str
    name: str
    category: str
    stamina_cost_per_run: int | None = None
    average_yield: float | None = None
    open_days: tuple[int, ...] = ()
    weekly_limit: int | None = None
    source_status: str = "manual"
    source_version: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class MaterialSnapshot:
    id: str
    data_set_id: str
    code: str
    name: str
    category: str
    stamina_cost_per_run: int | None
    average_yield: float | None
    open_days: tuple[int, ...]
    weekly_limit: int | None
    source_status: str
    source_version: str | None
    notes: str | None
    created_at: str
    updated_at: str

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "code": self.code,
            "name": self.name,
            "category": self.category,
            "stamina_cost_per_run": self.stamina_cost_per_run,
            "average_yield": self.average_yield,
            "open_days": list(self.open_days),
            "weekly_limit": self.weekly_limit,
            "source_status": self.source_status,
            "source_version": self.source_version,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class InventoryCountSnapshot:
    id: str
    data_set_id: str
    material_id: str
    material_code: str
    material_name: str
    quantity: float
    updated_at: str

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "material_id": self.material_id,
            "material_code": self.material_code,
            "material_name": self.material_name,
            "quantity": self.quantity,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MaterialRequirement:
    material_id: str
    required_quantity: float


@dataclass(frozen=True, slots=True)
class MaterialRequirementSnapshot:
    material_id: str
    material_code: str
    material_name: str
    required_quantity: float


@dataclass(frozen=True, slots=True)
class CultivationTargetDefinition:
    """Full editable target aggregate, including its material requirements."""

    target_type: str
    target_value: Any
    character_id: str | None = None
    target_key: str | None = None
    current_value: Any = None
    priority: int = 100
    is_completed: bool = False
    due_date: date | None = None
    notes: str | None = None
    required_materials: tuple[MaterialRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class CultivationTargetSnapshot:
    id: str
    data_set_id: str
    character_id: str | None
    character_name: str | None
    target_type: str
    target_key: str | None
    current_value: Any
    target_value: Any
    priority: int
    is_completed: bool
    due_date: date | None
    notes: str | None
    required_materials: tuple[MaterialRequirementSnapshot, ...]
    created_at: str
    updated_at: str

    def to_json_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "data_set_id": self.data_set_id,
            "character_id": self.character_id,
            "character_name": self.character_name,
            "target_type": self.target_type,
            "target_key": self.target_key,
            "current_value": deepcopy(self.current_value),
            "target_value": deepcopy(self.target_value),
            "priority": self.priority,
            "is_completed": self.is_completed,
            "due_date": None if self.due_date is None else self.due_date.isoformat(),
            "notes": self.notes,
            "required_materials": [
                {
                    "material_id": item.material_id,
                    "material_code": item.material_code,
                    "material_name": item.material_name,
                    "required_quantity": item.required_quantity,
                }
                for item in self.required_materials
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class DailyPlanItemDefinition:
    reason: str
    cultivation_target_id: str | None = None
    material_id: str | None = None
    recommended_runs: int = 0
    stamina_cost: int = 0
    status: str = "planned"
    estimated_completion_date: date | None = None
    assumptions: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class DailyPlanItemSnapshot:
    id: str
    rank: int
    cultivation_target_id: str | None
    material_id: str | None
    material_code: str | None
    recommended_runs: int
    stamina_cost: int
    reason: str
    status: str
    estimated_completion_date: date | None
    assumptions: JsonObject


@dataclass(frozen=True, slots=True)
class DailyPlanSnapshot:
    data_set_id: str
    plan_date: date
    generation_id: str
    items: tuple[DailyPlanItemSnapshot, ...]
    is_archived: bool
    created_at: str
    updated_at: str

    def to_json_dict(self) -> JsonObject:
        return {
            "data_set_id": self.data_set_id,
            "plan_date": self.plan_date.isoformat(),
            "generation_id": self.generation_id,
            "is_archived": self.is_archived,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "items": [
                {
                    "id": item.id,
                    "rank": item.rank,
                    "cultivation_target_id": item.cultivation_target_id,
                    "material_id": item.material_id,
                    "material_code": item.material_code,
                    "recommended_runs": item.recommended_runs,
                    "stamina_cost": item.stamina_cost,
                    "reason": item.reason,
                    "status": item.status,
                    "estimated_completion_date": (
                        None
                        if item.estimated_completion_date is None
                        else item.estimated_completion_date.isoformat()
                    ),
                    "assumptions": deepcopy(item.assumptions),
                }
                for item in self.items
            ],
        }


@dataclass(frozen=True, slots=True)
class StaticMaterialPackage:
    """Validated schema-v1 payload supplied by a user-selected local file."""

    schema_version: int
    source_version: str
    materials: tuple[MaterialDefinition, ...]

    def to_json_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "source_version": self.source_version,
            "materials": [
                {
                    "code": item.code,
                    "name": item.name,
                    "category": item.category,
                    "stamina_cost_per_run": item.stamina_cost_per_run,
                    "average_yield": item.average_yield,
                    "open_days": list(item.open_days),
                    "weekly_limit": item.weekly_limit,
                    "notes": item.notes,
                }
                for item in self.materials
            ],
        }


@dataclass(frozen=True, slots=True)
class MaterialImportPreview:
    schema_version: int
    source_version: str
    creates: tuple[str, ...]
    updates: tuple[str, ...]
    unchanged: tuple[str, ...]
    conflicts: tuple[str, ...]
    applied: bool = False

    @property
    def is_valid(self) -> bool:
        return not self.conflicts


class CultivationPlanningService:
    """Transactional persistence service for the V1 cultivation planner."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create_material(
        self,
        *,
        data_set_id: str,
        definition: MaterialDefinition,
    ) -> MaterialSnapshot:
        safe = _normalize_material(definition)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            _ensure_material_code_available(session, data_set_id, safe.code)
            record = MaterialRecord(data_set_id=data_set_id, code=safe.code, name=safe.name)
            session.add(record)
            _apply_material_definition(record, safe)
            session.flush()
            return _material_snapshot(record)

    def update_material(
        self,
        material_id: str,
        *,
        data_set_id: str,
        definition: MaterialDefinition,
    ) -> MaterialSnapshot:
        safe = _normalize_material(definition)
        with self._session_factory.begin() as session:
            record = _require_material(session, material_id, data_set_id)
            _ensure_material_code_available(session, data_set_id, safe.code, excluding_id=record.id)
            _apply_material_definition(record, safe)
            session.flush()
            return _material_snapshot(record)

    def upsert_material(
        self,
        *,
        data_set_id: str,
        definition: MaterialDefinition,
    ) -> MaterialSnapshot:
        safe = _normalize_material(definition)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            record = session.scalar(
                select(MaterialRecord).where(
                    MaterialRecord.data_set_id == data_set_id,
                    MaterialRecord.code == safe.code,
                )
            )
            if record is None:
                record = MaterialRecord(data_set_id=data_set_id, code=safe.code, name=safe.name)
                session.add(record)
            _apply_material_definition(record, safe)
            session.flush()
            return _material_snapshot(record)

    def save_material_with_inventory(
        self,
        *,
        data_set_id: str,
        material_id: str | None,
        definition: MaterialDefinition,
        inventory_quantity: object | None,
        clear_inventory: bool = False,
    ) -> tuple[MaterialSnapshot, InventoryCountSnapshot | None]:
        """Atomically save material facts and an explicitly confirmed inventory count.

        A null quantity preserves an existing count unless ``clear_inventory``
        is true.  Clearing deletes the local count so unknown remains distinct
        from a confirmed numeric zero.
        """

        safe = _normalize_material(definition)
        if type(clear_inventory) is not bool:
            raise ValueError("clear_inventory must be bool")
        if clear_inventory and inventory_quantity is not None:
            raise ValueError("clear_inventory cannot include an inventory quantity")
        safe_quantity = (
            None
            if inventory_quantity is None
            else _quantity(inventory_quantity, "inventory quantity")
        )
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            if material_id is None:
                _ensure_material_code_available(session, data_set_id, safe.code)
                material = MaterialRecord(
                    data_set_id=data_set_id,
                    code=safe.code,
                    name=safe.name,
                )
                session.add(material)
            else:
                material = _require_material(session, material_id, data_set_id)
                _ensure_material_code_available(
                    session,
                    data_set_id,
                    safe.code,
                    excluding_id=material.id,
                )
            _apply_material_definition(material, safe)
            session.flush()
            inventory = session.scalar(
                select(InventoryCountRecord).where(
                    InventoryCountRecord.data_set_id == data_set_id,
                    InventoryCountRecord.material_id == material.id,
                )
            )
            if inventory is not None and clear_inventory:
                session.delete(inventory)
                inventory = None
            if inventory is None and safe_quantity is not None:
                inventory = InventoryCountRecord(
                    data_set_id=data_set_id,
                    material_id=material.id,
                )
                session.add(inventory)
            if inventory is not None and safe_quantity is not None:
                inventory.quantity = safe_quantity
                inventory.updated_at = utc_now()
            session.flush()
            return (
                _material_snapshot(material),
                None if inventory is None else _inventory_snapshot(inventory, material),
            )

    def get_material(self, material_id: str, *, data_set_id: str) -> MaterialSnapshot:
        with self._session_factory() as session:
            return _material_snapshot(_require_material(session, material_id, data_set_id))

    def list_materials(
        self,
        *,
        data_set_id: str,
        category: str | None = None,
    ) -> tuple[MaterialSnapshot, ...]:
        safe_category = None if category is None else _required_text(category, "category", 80)
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            statement = select(MaterialRecord).where(MaterialRecord.data_set_id == data_set_id)
            if safe_category is not None:
                statement = statement.where(MaterialRecord.category == safe_category)
            records = session.scalars(
                statement.order_by(MaterialRecord.category, MaterialRecord.name)
            )
            return tuple(_material_snapshot(record) for record in records)

    def delete_material(self, material_id: str, *, data_set_id: str) -> bool:
        """Delete only an unused material so planner history is never silently damaged."""

        with self._session_factory.begin() as session:
            record = _require_material(session, material_id, data_set_id)
            if _material_is_referenced(session, material_id):
                raise ValueError("material is in use and cannot be deleted")
            session.delete(record)
            session.flush()
            return True

    def set_inventory_count(
        self,
        material_id: str,
        quantity: object,
        *,
        data_set_id: str,
    ) -> InventoryCountSnapshot:
        safe_quantity = _quantity(quantity, "inventory quantity")
        with self._session_factory.begin() as session:
            material = _require_material(session, material_id, data_set_id)
            record = session.scalar(
                select(InventoryCountRecord).where(
                    InventoryCountRecord.data_set_id == data_set_id,
                    InventoryCountRecord.material_id == material_id,
                )
            )
            if record is None:
                record = InventoryCountRecord(
                    data_set_id=data_set_id,
                    material_id=material_id,
                )
                session.add(record)
            record.quantity = safe_quantity
            record.updated_at = utc_now()
            session.flush()
            return _inventory_snapshot(record, material)

    def get_inventory_count(
        self,
        material_id: str,
        *,
        data_set_id: str,
    ) -> InventoryCountSnapshot | None:
        with self._session_factory() as session:
            material = _require_material(session, material_id, data_set_id)
            record = session.scalar(
                select(InventoryCountRecord).where(
                    InventoryCountRecord.data_set_id == data_set_id,
                    InventoryCountRecord.material_id == material_id,
                )
            )
            return None if record is None else _inventory_snapshot(record, material)

    def list_inventory_counts(
        self,
        *,
        data_set_id: str,
    ) -> tuple[InventoryCountSnapshot, ...]:
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            rows = session.execute(
                select(InventoryCountRecord, MaterialRecord)
                .join(MaterialRecord, MaterialRecord.id == InventoryCountRecord.material_id)
                .where(
                    InventoryCountRecord.data_set_id == data_set_id,
                    MaterialRecord.data_set_id == data_set_id,
                )
                .order_by(MaterialRecord.category, MaterialRecord.name)
            ).all()
            return tuple(_inventory_snapshot(count, material) for count, material in rows)

    def delete_inventory_count(self, material_id: str, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            _require_material(session, material_id, data_set_id)
            record = session.scalar(
                select(InventoryCountRecord).where(
                    InventoryCountRecord.data_set_id == data_set_id,
                    InventoryCountRecord.material_id == material_id,
                )
            )
            if record is None:
                return False
            session.delete(record)
            session.flush()
            return True

    def create_cultivation_target(
        self,
        *,
        data_set_id: str,
        definition: CultivationTargetDefinition,
    ) -> CultivationTargetSnapshot:
        safe = _normalize_target(definition)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            _validate_target_links(session, data_set_id, safe)
            record = CultivationTargetRecord(
                data_set_id=data_set_id,
                target_type=safe.target_type,
                target_value_json=deepcopy(safe.target_value),
            )
            session.add(record)
            _apply_target_definition(record, safe)
            session.flush()
            _replace_target_materials(session, record.id, data_set_id, safe.required_materials)
            session.flush()
            return _target_snapshot(session, record)

    def update_cultivation_target(
        self,
        target_id: str,
        *,
        data_set_id: str,
        definition: CultivationTargetDefinition,
    ) -> CultivationTargetSnapshot:
        safe = _normalize_target(definition)
        with self._session_factory.begin() as session:
            record = _require_target(session, target_id, data_set_id)
            _validate_target_links(session, data_set_id, safe)
            _apply_target_definition(record, safe)
            _replace_target_materials(session, record.id, data_set_id, safe.required_materials)
            session.flush()
            return _target_snapshot(session, record)

    def get_cultivation_target(
        self,
        target_id: str,
        *,
        data_set_id: str,
    ) -> CultivationTargetSnapshot:
        with self._session_factory() as session:
            return _target_snapshot(session, _require_target(session, target_id, data_set_id))

    def list_cultivation_targets(
        self,
        *,
        data_set_id: str,
        character_id: str | None = None,
        include_completed: bool = True,
    ) -> tuple[CultivationTargetSnapshot, ...]:
        safe_character_id = _optional_id(character_id, "character_id")
        if not isinstance(include_completed, bool):
            raise ValueError("include_completed must be a boolean")
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            if safe_character_id is not None:
                _require_character(session, safe_character_id, data_set_id)
            statement = select(CultivationTargetRecord).where(
                CultivationTargetRecord.data_set_id == data_set_id
            )
            if safe_character_id is not None:
                statement = statement.where(
                    CultivationTargetRecord.character_id == safe_character_id
                )
            if not include_completed:
                statement = statement.where(CultivationTargetRecord.is_completed.is_(False))
            records = session.scalars(
                statement.order_by(
                    CultivationTargetRecord.is_completed,
                    CultivationTargetRecord.priority,
                    CultivationTargetRecord.due_date.is_(None),
                    CultivationTargetRecord.due_date,
                    CultivationTargetRecord.created_at,
                )
            )
            return tuple(_target_snapshot(session, record) for record in records)

    def set_cultivation_target_completed(
        self,
        target_id: str,
        completed: bool,
        *,
        data_set_id: str,
    ) -> CultivationTargetSnapshot:
        if not isinstance(completed, bool):
            raise ValueError("completed must be a boolean")
        with self._session_factory.begin() as session:
            record = _require_target(session, target_id, data_set_id)
            record.is_completed = completed
            record.updated_at = utc_now()
            session.flush()
            return _target_snapshot(session, record)

    def delete_cultivation_target(self, target_id: str, *, data_set_id: str) -> bool:
        with self._session_factory.begin() as session:
            record = _require_target(session, target_id, data_set_id)
            used_in_plan = session.scalar(
                select(exists().where(DailyPlanRecord.cultivation_target_id == target_id))
            )
            if used_in_plan:
                raise ValueError("cultivation target is referenced by plan history")
            session.delete(record)
            session.flush()
            return True

    def replace_daily_plan(
        self,
        *,
        data_set_id: str,
        plan_date: date,
        items: Sequence[DailyPlanItemDefinition],
        generation_id: str | None = None,
    ) -> DailyPlanSnapshot:
        safe_date = _date_value(plan_date, "plan date")
        safe_items = _normalize_daily_plan_items(items)
        safe_generation = (
            new_id()
            if generation_id is None
            else _required_text(generation_id, "generation_id", 36)
        )
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            duplicate_generation = session.scalar(
                select(exists().where(DailyPlanRecord.generation_id == safe_generation))
            )
            if duplicate_generation:
                raise ValueError("generation_id already exists")
            _validate_daily_plan_links(session, data_set_id, safe_items)
            active_rows = session.scalars(
                select(DailyPlanRecord).where(
                    DailyPlanRecord.data_set_id == data_set_id,
                    DailyPlanRecord.plan_date == safe_date,
                    DailyPlanRecord.status != "archived",
                )
            ).all()
            now = utc_now()
            for row in active_rows:
                assumptions = deepcopy(row.assumptions_json)
                assumptions["archived_from_status"] = row.status
                row.assumptions_json = assumptions
                row.status = "archived"
                row.updated_at = now
            records = [
                DailyPlanRecord(
                    data_set_id=data_set_id,
                    plan_date=safe_date,
                    generation_id=safe_generation,
                    rank=rank,
                    cultivation_target_id=item.cultivation_target_id,
                    material_id=item.material_id,
                    recommended_runs=item.recommended_runs,
                    stamina_cost=item.stamina_cost,
                    reason=item.reason,
                    status=item.status,
                    estimated_completion_date=item.estimated_completion_date,
                    assumptions_json=deepcopy(item.assumptions or {}),
                )
                for rank, item in enumerate(safe_items, start=1)
            ]
            session.add_all(records)
            session.flush()
            return _daily_plan_snapshot(session, data_set_id, safe_generation)

    def get_daily_plan(
        self,
        generation_id: str,
        *,
        data_set_id: str,
    ) -> DailyPlanSnapshot:
        safe_generation = _required_text(generation_id, "generation_id", 36)
        with self._session_factory() as session:
            return _daily_plan_snapshot(session, data_set_id, safe_generation)

    def get_current_daily_plan(
        self,
        plan_date: date,
        *,
        data_set_id: str,
    ) -> DailyPlanSnapshot | None:
        safe_date = _date_value(plan_date, "plan date")
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            generation = session.scalar(
                select(DailyPlanRecord.generation_id)
                .where(
                    DailyPlanRecord.data_set_id == data_set_id,
                    DailyPlanRecord.plan_date == safe_date,
                    DailyPlanRecord.status != "archived",
                )
                .order_by(DailyPlanRecord.created_at.desc(), DailyPlanRecord.id.desc())
                .limit(1)
            )
            if generation is None:
                return None
            return _daily_plan_snapshot(session, data_set_id, generation)

    def list_daily_plans(
        self,
        *,
        data_set_id: str,
        plan_date: date | None = None,
        include_archived: bool = True,
    ) -> tuple[DailyPlanSnapshot, ...]:
        safe_date = None if plan_date is None else _date_value(plan_date, "plan date")
        if not isinstance(include_archived, bool):
            raise ValueError("include_archived must be a boolean")
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            statement = select(DailyPlanRecord).where(DailyPlanRecord.data_set_id == data_set_id)
            if safe_date is not None:
                statement = statement.where(DailyPlanRecord.plan_date == safe_date)
            if not include_archived:
                statement = statement.where(DailyPlanRecord.status != "archived")
            records = session.scalars(
                statement.order_by(
                    DailyPlanRecord.created_at.desc(),
                    DailyPlanRecord.generation_id,
                    DailyPlanRecord.rank,
                )
            ).all()
            generation_ids: list[str] = []
            for record in records:
                if record.generation_id not in generation_ids:
                    generation_ids.append(record.generation_id)
            return tuple(
                _daily_plan_snapshot(session, data_set_id, generation_id)
                for generation_id in generation_ids
            )

    def archive_daily_plan(
        self,
        generation_id: str,
        *,
        data_set_id: str,
    ) -> DailyPlanSnapshot:
        safe_generation = _required_text(generation_id, "generation_id", 36)
        with self._session_factory.begin() as session:
            rows = _daily_plan_rows(session, data_set_id, safe_generation)
            now = utc_now()
            for row in rows:
                if row.status == "archived":
                    continue
                assumptions = deepcopy(row.assumptions_json)
                assumptions.setdefault("archived_from_status", row.status)
                row.assumptions_json = assumptions
                row.status = "archived"
                row.updated_at = now
            session.flush()
            return _daily_plan_snapshot_from_rows(session, rows)

    def set_daily_plan_status(
        self,
        generation_id: str,
        status: str,
        *,
        data_set_id: str,
    ) -> DailyPlanSnapshot:
        """Set every item in one active generation in a single transaction."""

        safe_generation = _required_text(generation_id, "generation_id", 36)
        safe_status = _plan_status(status, allow_archived=False)
        with self._session_factory.begin() as session:
            rows = _daily_plan_rows(session, data_set_id, safe_generation)
            if any(row.status == "archived" for row in rows):
                raise ValueError("an archived daily plan cannot be edited")
            now = utc_now()
            for row in rows:
                row.status = safe_status
                row.updated_at = now
            session.flush()
            return _daily_plan_snapshot_from_rows(session, rows)

    def set_daily_plan_item_status(
        self,
        item_id: str,
        status: str,
        *,
        data_set_id: str,
    ) -> DailyPlanSnapshot:
        safe_id = _required_text(item_id, "daily plan item id", 36)
        safe_status = _plan_status(status, allow_archived=False)
        with self._session_factory.begin() as session:
            record = session.get(DailyPlanRecord, safe_id)
            if record is None:
                raise ValueError("daily plan item does not exist")
            if record.data_set_id != data_set_id:
                raise ValueError("daily plan item does not belong to the requested data set")
            if record.status == "archived":
                raise ValueError("an archived daily plan cannot be edited")
            record.status = safe_status
            record.updated_at = utc_now()
            session.flush()
            return _daily_plan_snapshot(session, data_set_id, record.generation_id)

    def validate_material_package(self, payload: MaterialPackageLike) -> StaticMaterialPackage:
        return _normalize_material_package(payload)

    def preview_material_package(
        self,
        *,
        data_set_id: str,
        payload: MaterialPackageLike,
    ) -> MaterialImportPreview:
        package = _normalize_material_package(payload)
        with self._session_factory() as session:
            _require_data_set(session, data_set_id)
            return _preview_material_package(session, data_set_id, package)

    def apply_material_package(
        self,
        *,
        data_set_id: str,
        payload: MaterialPackageLike,
        dry_run: bool = False,
    ) -> MaterialImportPreview:
        """Apply a user-supplied local package atomically; this method never downloads."""

        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        package = _normalize_material_package(payload)
        with self._session_factory.begin() as session:
            _require_data_set(session, data_set_id)
            preview = _preview_material_package(session, data_set_id, package)
            if not preview.is_valid:
                raise ValueError("material package conflicts: " + "; ".join(preview.conflicts))
            if dry_run:
                return preview
            for material in package.materials:
                _apply_import_material(session, data_set_id, material)
            session.flush()
            return MaterialImportPreview(
                schema_version=preview.schema_version,
                source_version=preview.source_version,
                creates=preview.creates,
                updates=preview.updates,
                unchanged=preview.unchanged,
                conflicts=preview.conflicts,
                applied=True,
            )


def _normalize_material(definition: MaterialDefinition) -> MaterialDefinition:
    if not isinstance(definition, MaterialDefinition):
        raise ValueError("definition must be MaterialDefinition")
    return MaterialDefinition(
        code=_required_text(definition.code, "material code", 120),
        name=_required_text(definition.name, "material name", 160),
        category=_required_text(definition.category, "material category", 80),
        stamina_cost_per_run=_optional_nonnegative_int(
            definition.stamina_cost_per_run, "stamina cost per run"
        ),
        average_yield=_optional_nonnegative_number(definition.average_yield, "average yield"),
        open_days=_open_days(definition.open_days),
        weekly_limit=_optional_nonnegative_int(definition.weekly_limit, "weekly limit"),
        source_status=_required_text(definition.source_status, "source status", 40),
        source_version=_optional_text(definition.source_version, "source version", 80),
        notes=_optional_text(definition.notes, "notes", None),
    )


def _apply_material_definition(record: MaterialRecord, definition: MaterialDefinition) -> None:
    record.code = definition.code
    record.name = definition.name
    record.category = definition.category
    record.stamina_cost_per_run = definition.stamina_cost_per_run
    record.average_yield = definition.average_yield
    record.open_days_json = list(definition.open_days)
    record.weekly_limit = definition.weekly_limit
    record.source_status = definition.source_status
    record.source_version = definition.source_version
    record.notes = definition.notes
    record.updated_at = utc_now()


def _material_snapshot(record: MaterialRecord) -> MaterialSnapshot:
    if record.data_set_id is None:
        raise RuntimeError("dataset-scoped service cannot return a global material")
    return MaterialSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        code=record.code,
        name=record.name,
        category=record.category,
        stamina_cost_per_run=record.stamina_cost_per_run,
        average_yield=record.average_yield,
        open_days=tuple(record.open_days_json),
        weekly_limit=record.weekly_limit,
        source_status=record.source_status,
        source_version=record.source_version,
        notes=record.notes,
        created_at=_iso(record.created_at),
        updated_at=_iso(record.updated_at),
    )


def _inventory_snapshot(
    record: InventoryCountRecord,
    material: MaterialRecord,
) -> InventoryCountSnapshot:
    return InventoryCountSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        material_id=record.material_id,
        material_code=material.code,
        material_name=material.name,
        quantity=float(record.quantity),
        updated_at=_iso(record.updated_at),
    )


def _normalize_target(definition: CultivationTargetDefinition) -> CultivationTargetDefinition:
    if not isinstance(definition, CultivationTargetDefinition):
        raise ValueError("definition must be CultivationTargetDefinition")
    if definition.target_value is None:
        raise ValueError("target value cannot be null")
    requirements = _normalize_requirements(definition.required_materials)
    return CultivationTargetDefinition(
        target_type=_required_text(definition.target_type, "target type", 80),
        target_value=_json_value(definition.target_value, "target value"),
        character_id=_optional_id(definition.character_id, "character_id"),
        target_key=_optional_text(definition.target_key, "target key", 160),
        current_value=_json_value(definition.current_value, "current value"),
        priority=_nonnegative_int(definition.priority, "priority"),
        is_completed=_strict_bool(definition.is_completed, "is_completed"),
        due_date=(
            None if definition.due_date is None else _date_value(definition.due_date, "due date")
        ),
        notes=_optional_text(definition.notes, "notes", None),
        required_materials=requirements,
    )


def _apply_target_definition(
    record: CultivationTargetRecord,
    definition: CultivationTargetDefinition,
) -> None:
    record.character_id = definition.character_id
    record.target_type = definition.target_type
    record.target_key = definition.target_key
    record.current_value_json = deepcopy(definition.current_value)
    record.target_value_json = deepcopy(definition.target_value)
    record.priority = definition.priority
    record.is_completed = definition.is_completed
    record.due_date = definition.due_date
    record.notes = definition.notes
    record.updated_at = utc_now()


def _normalize_requirements(
    requirements: Sequence[MaterialRequirement],
) -> tuple[MaterialRequirement, ...]:
    if isinstance(requirements, (str, bytes)):
        raise ValueError("required materials must be a sequence")
    normalized: list[MaterialRequirement] = []
    for requirement in requirements:
        if not isinstance(requirement, MaterialRequirement):
            raise ValueError("required materials must contain MaterialRequirement values")
        normalized.append(
            MaterialRequirement(
                material_id=_required_text(requirement.material_id, "material_id", 36),
                required_quantity=_quantity(
                    requirement.required_quantity, "required material quantity"
                ),
            )
        )
    ids = [item.material_id for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("required material ids must be unique")
    return tuple(normalized)


def _validate_target_links(
    session: Session,
    data_set_id: str,
    definition: CultivationTargetDefinition,
) -> None:
    if definition.character_id is not None:
        _require_character(session, definition.character_id, data_set_id)
    for requirement in definition.required_materials:
        _require_material(session, requirement.material_id, data_set_id)


def _replace_target_materials(
    session: Session,
    target_id: str,
    data_set_id: str,
    requirements: Sequence[MaterialRequirement],
) -> None:
    for requirement in requirements:
        _require_material(session, requirement.material_id, data_set_id)
    existing = session.scalars(
        select(CultivationTargetMaterialRecord).where(
            CultivationTargetMaterialRecord.cultivation_target_id == target_id
        )
    ).all()
    for record in existing:
        session.delete(record)
    session.flush()
    session.add_all(
        [
            CultivationTargetMaterialRecord(
                cultivation_target_id=target_id,
                material_id=requirement.material_id,
                required_quantity=Decimal(str(requirement.required_quantity)),
            )
            for requirement in requirements
        ]
    )


def _target_snapshot(
    session: Session,
    record: CultivationTargetRecord,
) -> CultivationTargetSnapshot:
    character_name: str | None = None
    if record.character_id is not None:
        character = session.get(CharacterRecord, record.character_id)
        character_name = None if character is None else character.name
    rows = session.execute(
        select(CultivationTargetMaterialRecord, MaterialRecord)
        .join(
            MaterialRecord,
            MaterialRecord.id == CultivationTargetMaterialRecord.material_id,
        )
        .where(CultivationTargetMaterialRecord.cultivation_target_id == record.id)
        .order_by(MaterialRecord.category, MaterialRecord.name)
    ).all()
    return CultivationTargetSnapshot(
        id=record.id,
        data_set_id=record.data_set_id,
        character_id=record.character_id,
        character_name=character_name,
        target_type=record.target_type,
        target_key=record.target_key,
        current_value=deepcopy(record.current_value_json),
        target_value=deepcopy(record.target_value_json),
        priority=record.priority,
        is_completed=record.is_completed,
        due_date=record.due_date,
        notes=record.notes,
        required_materials=tuple(
            MaterialRequirementSnapshot(
                material_id=requirement.material_id,
                material_code=material.code,
                material_name=material.name,
                required_quantity=float(requirement.required_quantity),
            )
            for requirement, material in rows
        ),
        created_at=_iso(record.created_at),
        updated_at=_iso(record.updated_at),
    )


def _normalize_daily_plan_items(
    items: Sequence[DailyPlanItemDefinition],
) -> tuple[DailyPlanItemDefinition, ...]:
    if isinstance(items, (str, bytes)) or not items:
        raise ValueError("a daily plan must contain at least one item")
    normalized: list[DailyPlanItemDefinition] = []
    for item in items:
        if not isinstance(item, DailyPlanItemDefinition):
            raise ValueError("daily plan items must be DailyPlanItemDefinition values")
        normalized.append(
            DailyPlanItemDefinition(
                reason=_required_text(item.reason, "plan reason", 10_000),
                cultivation_target_id=_optional_id(
                    item.cultivation_target_id, "cultivation_target_id"
                ),
                material_id=_optional_id(item.material_id, "material_id"),
                recommended_runs=_nonnegative_int(item.recommended_runs, "recommended runs"),
                stamina_cost=_nonnegative_int(item.stamina_cost, "stamina cost"),
                status=_plan_status(item.status, allow_archived=False),
                estimated_completion_date=(
                    None
                    if item.estimated_completion_date is None
                    else _date_value(item.estimated_completion_date, "estimated completion date")
                ),
                assumptions=(
                    {}
                    if item.assumptions is None
                    else _json_object(item.assumptions, "plan assumptions")
                ),
            )
        )
    return tuple(normalized)


def _validate_daily_plan_links(
    session: Session,
    data_set_id: str,
    items: Sequence[DailyPlanItemDefinition],
) -> None:
    for item in items:
        if item.cultivation_target_id is not None:
            _require_target(session, item.cultivation_target_id, data_set_id)
        if item.material_id is not None:
            _require_material(session, item.material_id, data_set_id)


def _daily_plan_rows(
    session: Session,
    data_set_id: str,
    generation_id: str,
) -> list[DailyPlanRecord]:
    _require_data_set(session, data_set_id)
    rows = list(
        session.scalars(
            select(DailyPlanRecord)
            .where(
                DailyPlanRecord.data_set_id == data_set_id,
                DailyPlanRecord.generation_id == generation_id,
            )
            .order_by(DailyPlanRecord.rank)
        ).all()
    )
    if not rows:
        foreign_rows = session.scalar(
            select(exists().where(DailyPlanRecord.generation_id == generation_id))
        )
        if foreign_rows:
            raise ValueError("daily plan does not belong to the requested data set")
        raise ValueError("daily plan does not exist")
    return rows


def _daily_plan_snapshot(
    session: Session,
    data_set_id: str,
    generation_id: str,
) -> DailyPlanSnapshot:
    return _daily_plan_snapshot_from_rows(
        session,
        _daily_plan_rows(session, data_set_id, generation_id),
    )


def _daily_plan_snapshot_from_rows(
    session: Session,
    rows: Sequence[DailyPlanRecord],
) -> DailyPlanSnapshot:
    if not rows:
        raise ValueError("daily plan does not exist")
    ordered = sorted(rows, key=lambda item: item.rank)
    material_ids = {item.material_id for item in ordered if item.material_id is not None}
    materials = {
        material.id: material
        for material in session.scalars(
            select(MaterialRecord).where(MaterialRecord.id.in_(material_ids))
        ).all()
    }
    return DailyPlanSnapshot(
        data_set_id=ordered[0].data_set_id,
        plan_date=ordered[0].plan_date,
        generation_id=ordered[0].generation_id,
        items=tuple(
            DailyPlanItemSnapshot(
                id=item.id,
                rank=item.rank,
                cultivation_target_id=item.cultivation_target_id,
                material_id=item.material_id,
                material_code=(
                    None
                    if item.material_id is None or item.material_id not in materials
                    else materials[item.material_id].code
                ),
                recommended_runs=item.recommended_runs,
                stamina_cost=item.stamina_cost,
                reason=item.reason,
                status=item.status,
                estimated_completion_date=item.estimated_completion_date,
                assumptions=_json_object(item.assumptions_json, "stored plan assumptions"),
            )
            for item in ordered
        ),
        is_archived=all(item.status == "archived" for item in ordered),
        created_at=_iso(min(item.created_at for item in ordered)),
        updated_at=_iso(max(item.updated_at for item in ordered)),
    )


def _normalize_material_package(payload: MaterialPackageLike) -> StaticMaterialPackage:
    if isinstance(payload, StaticMaterialPackage):
        if payload.schema_version != MATERIAL_PACKAGE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported material package schema_version: {payload.schema_version}"
            )
        source_version = _required_text(payload.source_version, "source version", 80)
        if not payload.materials:
            raise ValueError("material package must contain at least one material")
        materials = tuple(
            _normalize_material(
                MaterialDefinition(
                    code=item.code,
                    name=item.name,
                    category=item.category,
                    stamina_cost_per_run=item.stamina_cost_per_run,
                    average_yield=item.average_yield,
                    open_days=item.open_days,
                    weekly_limit=item.weekly_limit,
                    source_status="manual_package",
                    source_version=source_version,
                    notes=item.notes,
                )
            )
            for item in payload.materials
        )
    else:
        if not isinstance(payload, Mapping):
            raise ValueError("material package must be a JSON object")
        unknown_keys = set(payload) - _PACKAGE_KEYS
        if unknown_keys:
            raise ValueError(f"unknown material package field: {sorted(unknown_keys)[0]}")
        schema_version = payload.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError("material package schema_version must be an integer")
        if schema_version != MATERIAL_PACKAGE_SCHEMA_VERSION:
            raise ValueError(f"unsupported material package schema_version: {schema_version}")
        source_version = _required_text(payload.get("source_version"), "source version", 80)
        raw_materials = payload.get("materials")
        if (
            not isinstance(raw_materials, Sequence)
            or isinstance(raw_materials, (str, bytes))
            or not raw_materials
        ):
            raise ValueError("material package must contain at least one material")
        parsed: list[MaterialDefinition] = []
        for index, raw in enumerate(raw_materials):
            if not isinstance(raw, Mapping):
                raise ValueError(f"material package item {index} must be a JSON object")
            unknown_item_keys = set(raw) - _PACKAGE_MATERIAL_KEYS
            if unknown_item_keys:
                raise ValueError(
                    f"unknown material package item field: {sorted(unknown_item_keys)[0]}"
                )
            parsed.append(
                MaterialDefinition(
                    code=_required_text(raw.get("code"), "material code", 120),
                    name=_required_text(raw.get("name"), "material name", 160),
                    category=_required_text(raw.get("category"), "material category", 80),
                    stamina_cost_per_run=_optional_nonnegative_int(
                        raw.get("stamina_cost_per_run"), "stamina cost per run"
                    ),
                    average_yield=_optional_nonnegative_number(
                        raw.get("average_yield"), "average yield"
                    ),
                    open_days=_open_days(raw.get("open_days", ())),
                    weekly_limit=_optional_nonnegative_int(raw.get("weekly_limit"), "weekly limit"),
                    source_status="manual_package",
                    source_version=source_version,
                    notes=_optional_text(raw.get("notes"), "notes", None),
                )
            )
        materials = tuple(parsed)
    codes = [item.code for item in materials]
    if len(codes) != len(set(codes)):
        raise ValueError("material package codes must be unique")
    return StaticMaterialPackage(
        schema_version=MATERIAL_PACKAGE_SCHEMA_VERSION,
        source_version=source_version,
        materials=materials,
    )


def _preview_material_package(
    session: Session,
    data_set_id: str,
    package: StaticMaterialPackage,
) -> MaterialImportPreview:
    codes = [item.code for item in package.materials]
    existing = {
        record.code: record
        for record in session.scalars(
            select(MaterialRecord).where(
                MaterialRecord.data_set_id == data_set_id,
                MaterialRecord.code.in_(codes),
            )
        ).all()
    }
    creates: list[str] = []
    updates: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for item in package.materials:
        record = existing.get(item.code)
        if record is None:
            creates.append(item.code)
        elif _material_matches(record, item):
            unchanged.append(item.code)
        else:
            updates.append(item.code)
    return MaterialImportPreview(
        schema_version=package.schema_version,
        source_version=package.source_version,
        creates=tuple(creates),
        updates=tuple(updates),
        unchanged=tuple(unchanged),
        conflicts=tuple(conflicts),
    )


def _apply_import_material(
    session: Session,
    data_set_id: str,
    definition: MaterialDefinition,
) -> None:
    record = session.scalar(
        select(MaterialRecord).where(
            MaterialRecord.data_set_id == data_set_id,
            MaterialRecord.code == definition.code,
        )
    )
    if record is None:
        record = MaterialRecord(
            data_set_id=data_set_id,
            code=definition.code,
            name=definition.name,
        )
        session.add(record)
    _apply_material_definition(record, definition)


def _material_matches(record: MaterialRecord, definition: MaterialDefinition) -> bool:
    return (
        record.code == definition.code
        and record.name == definition.name
        and record.category == definition.category
        and record.stamina_cost_per_run == definition.stamina_cost_per_run
        and record.average_yield == definition.average_yield
        and tuple(record.open_days_json) == definition.open_days
        and record.weekly_limit == definition.weekly_limit
        and record.source_status == definition.source_status
        and record.source_version == definition.source_version
        and record.notes == definition.notes
    )


def _require_data_set(session: Session, data_set_id: str) -> DataSetRecord:
    safe_id = _required_text(data_set_id, "data_set_id", 36)
    record = session.get(DataSetRecord, safe_id)
    if record is None:
        raise ValueError("data set does not exist")
    return record


def _require_material(
    session: Session,
    material_id: str,
    data_set_id: str,
) -> MaterialRecord:
    safe_id = _required_text(material_id, "material_id", 36)
    record = session.get(MaterialRecord, safe_id)
    if record is None:
        raise ValueError("material does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("material does not belong to the requested data set")
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


def _require_target(
    session: Session,
    target_id: str,
    data_set_id: str,
) -> CultivationTargetRecord:
    safe_id = _required_text(target_id, "cultivation target id", 36)
    record = session.get(CultivationTargetRecord, safe_id)
    if record is None:
        raise ValueError("cultivation target does not exist")
    if record.data_set_id != data_set_id:
        raise ValueError("cultivation target does not belong to the requested data set")
    return record


def _ensure_material_code_available(
    session: Session,
    data_set_id: str,
    code: str,
    *,
    excluding_id: str | None = None,
) -> None:
    record = session.scalar(
        select(MaterialRecord).where(
            MaterialRecord.data_set_id == data_set_id,
            MaterialRecord.code == code,
        )
    )
    if record is not None and record.id != excluding_id:
        raise ValueError("material code is already in use in this data set")


def _material_is_referenced(session: Session, material_id: str) -> bool:
    checks = (
        CultivationTargetMaterialRecord.material_id == material_id,
        DailyPlanRecord.material_id == material_id,
    )
    return any(bool(session.scalar(select(exists().where(condition)))) for condition in checks)


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


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, label)


def _optional_nonnegative_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{label} must be a finite nonnegative number or null")
    return float(value)


def _quantity(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} must be a finite nonnegative number")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite nonnegative number") from exc
    if not decimal_value.is_finite() or decimal_value < 0 or decimal_value > _MAX_QUANTITY:
        raise ValueError(f"{label} must be a finite nonnegative number within storage range")
    return float(decimal_value.quantize(_QUANTITY_QUANTUM, rounding=ROUND_HALF_UP))


def _open_days(values: object) -> tuple[int, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("open days must be a sequence of ISO weekdays")
    normalized: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 7:
            raise ValueError("open days must contain ISO weekdays within [1, 7]")
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise ValueError("open days must be unique")
    return tuple(sorted(normalized))


def _strict_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _date_value(value: object, label: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{label} must be a date")
    return value


def _plan_status(value: object, *, allow_archived: bool) -> str:
    status = _required_text(value, "plan status", 40)
    allowed = _ACTIVE_PLAN_STATUSES | ({"archived"} if allow_archived else set())
    if status not in allowed:
        raise ValueError(f"plan status must be one of: {', '.join(sorted(allowed))}")
    return status


def _json_value(value: object, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return deepcopy(json.loads(encoded))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON-safe values") from exc


def _json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    decoded = _json_value(dict(value), label)
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return decoded


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


__all__ = [
    "MATERIAL_PACKAGE_SCHEMA_VERSION",
    "CultivationPlanningService",
    "CultivationTargetDefinition",
    "CultivationTargetSnapshot",
    "DailyPlanItemDefinition",
    "DailyPlanItemSnapshot",
    "DailyPlanSnapshot",
    "InventoryCountSnapshot",
    "MaterialDefinition",
    "MaterialImportPreview",
    "MaterialRequirement",
    "MaterialRequirementSnapshot",
    "MaterialSnapshot",
    "StaticMaterialPackage",
]
