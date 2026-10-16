from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

import wuwa_assistant.application.planning as planning_module
from wuwa_assistant.application.planning import (
    CultivationPlanningService,
    CultivationTargetDefinition,
    DailyPlanItemDefinition,
    MaterialDefinition,
    MaterialRequirement,
)
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import (
    CharacterRecord,
    CultivationTargetMaterialRecord,
    CultivationTargetRecord,
    DataSetRecord,
    MaterialRecord,
)


@pytest.fixture
def storage(
    tmp_path: Path,
) -> Iterator[tuple[Database, CultivationPlanningService, str, str, str, str]]:
    database = create_database(AppPaths.from_root(tmp_path / "planning"))
    with database.session_factory.begin() as session:
        primary = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        demo = DataSetRecord(name="示範資料", kind="demo", is_active=False)
        session.add_all([primary, demo])
        session.flush()
        primary_character = CharacterRecord(
            data_set_id=primary.id,
            name="主要角色",
            level=90,
            cultivation_goal_json={},
        )
        demo_character = CharacterRecord(
            data_set_id=demo.id,
            name="示範角色",
            level=1,
            cultivation_goal_json={},
        )
        session.add_all([primary_character, demo_character])
        session.flush()
        ids = (primary.id, demo.id, primary_character.id, demo_character.id)
    try:
        yield (
            database,
            CultivationPlanningService(database.session_factory),
            *ids,
        )
    finally:
        database.dispose()


def _material(
    code: str,
    *,
    name: str | None = None,
    category: str = "角色突破",
) -> MaterialDefinition:
    return MaterialDefinition(
        code=code,
        name=name or code,
        category=category,
        stamina_cost_per_run=40,
        average_yield=4.5,
        open_days=(1, 3, 5),
        weekly_limit=3,
        notes="手動維護",
    )


def _package(*codes: str, source_version: str = "tw-v1") -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_version": source_version,
        "materials": [
            {
                "code": code,
                "name": f"素材 {code}",
                "category": "技能素材",
                "stamina_cost_per_run": 40,
                "average_yield": 3.25,
                "open_days": [2, 4, 6],
                "weekly_limit": None,
                "notes": "本機資料包",
            }
            for code in codes
        ],
    }


def test_material_and_inventory_crud_are_dataset_scoped_and_json_safe(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
) -> None:
    _database, service, primary_id, demo_id, *_characters = storage
    created = service.create_material(
        data_set_id=primary_id,
        definition=_material("shell-credit", name="貝幣"),
    )

    assert created.open_days == (1, 3, 5)
    assert created.average_yield == 4.5
    assert service.get_material(created.id, data_set_id=primary_id) == created
    assert [item.id for item in service.list_materials(data_set_id=primary_id)] == [created.id]
    assert service.list_materials(data_set_id=demo_id) == ()
    json.dumps(created.to_json_dict(), ensure_ascii=False, allow_nan=False)

    updated = service.update_material(
        created.id,
        data_set_id=primary_id,
        definition=MaterialDefinition(
            code="shell-credit",
            name="貝幣 (更新)",
            category="通用",
            stamina_cost_per_run=40,
            average_yield=5,
            open_days=(),
            source_status="manual",
            source_version="manual-2",
        ),
    )
    assert updated.id == created.id
    assert updated.name == "貝幣 (更新)"
    assert (
        service.upsert_material(
            data_set_id=primary_id,
            definition=_material("shell-credit", name="貝幣 (再次更新)"),
        ).id
        == created.id
    )

    first_count = service.set_inventory_count(created.id, 12.34567, data_set_id=primary_id)
    assert first_count.quantity == 12.3457
    assert service.set_inventory_count(created.id, 20, data_set_id=primary_id).quantity == 20
    assert service.get_inventory_count(created.id, data_set_id=primary_id).quantity == 20  # type: ignore[union-attr]
    assert service.list_inventory_counts(data_set_id=primary_id)[0].material_code == "shell-credit"
    assert service.list_inventory_counts(data_set_id=demo_id) == ()

    with pytest.raises(ValueError, match="requested data set"):
        service.get_material(created.id, data_set_id=demo_id)
    with pytest.raises(ValueError, match="requested data set"):
        service.set_inventory_count(created.id, 1, data_set_id=demo_id)
    demo_material = service.create_material(
        data_set_id=demo_id,
        definition=_material("shell-credit", name="示範貝幣"),
    )
    assert demo_material.id != created.id
    with pytest.raises(ValueError, match="this data set"):
        service.create_material(
            data_set_id=primary_id,
            definition=_material("shell-credit", name="重複貝幣"),
        )
    assert service.delete_material(created.id, data_set_id=primary_id)
    assert service.list_inventory_counts(data_set_id=primary_id) == ()


def test_material_and_inventory_aggregate_save_is_atomic(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
) -> None:
    _database, service, primary_id, _demo_id, *_characters = storage
    material, inventory = service.save_material_with_inventory(
        data_set_id=primary_id,
        material_id=None,
        definition=_material("aggregate-save", name="聚合儲存素材"),
        inventory_quantity=12,
    )
    assert inventory is not None
    assert inventory.material_id == material.id
    assert inventory.quantity == 12

    unknown_material, unknown_inventory = service.save_material_with_inventory(
        data_set_id=primary_id,
        material_id=None,
        definition=_material("unknown-inventory", name="尚未盤點素材"),
        inventory_quantity=None,
    )
    assert unknown_inventory is None
    assert service.get_inventory_count(unknown_material.id, data_set_id=primary_id) is None
    renamed, still_unknown = service.save_material_with_inventory(
        data_set_id=primary_id,
        material_id=unknown_material.id,
        definition=_material("unknown-inventory", name="改名後仍未盤點"),
        inventory_quantity=None,
    )
    assert renamed.name == "改名後仍未盤點"
    assert still_unknown is None
    assert service.get_inventory_count(unknown_material.id, data_set_id=primary_id) is None

    cleared_material, cleared_inventory = service.save_material_with_inventory(
        data_set_id=primary_id,
        material_id=material.id,
        definition=_material("aggregate-save", name="聚合儲存素材"),
        inventory_quantity=None,
        clear_inventory=True,
    )
    assert cleared_material.id == material.id
    assert cleared_inventory is None
    assert service.get_inventory_count(material.id, data_set_id=primary_id) is None

    with pytest.raises(ValueError, match="inventory quantity"):
        service.save_material_with_inventory(
            data_set_id=primary_id,
            material_id=None,
            definition=_material("must-roll-back", name="不可殘留"),
            inventory_quantity=-1,
        )
    assert all(
        item.code != "must-roll-back" for item in service.list_materials(data_set_id=primary_id)
    )


def test_cultivation_target_crud_validates_all_foreign_keys_and_round_trips(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
) -> None:
    database, service, primary_id, demo_id, primary_character_id, demo_character_id = storage
    shell = service.create_material(
        data_set_id=primary_id,
        definition=_material("target-shell", name="低頻潮蝕帆核"),
    )
    weekly = service.create_material(
        data_set_id=primary_id,
        definition=_material("target-weekly", category="每週素材"),
    )
    demo_material = service.create_material(
        data_set_id=demo_id,
        definition=_material("demo-target-shell"),
    )
    mutable_target = {"level": 10, "skills": [10, 10, 6]}
    created = service.create_cultivation_target(
        data_set_id=primary_id,
        definition=CultivationTargetDefinition(
            target_type="character_skill",
            target_key="resonance_skill",
            character_id=primary_character_id,
            current_value={"level": 7},
            target_value=mutable_target,
            priority=5,
            due_date=date(2026, 8, 8),
            notes="先補核心技能",
            required_materials=(
                MaterialRequirement(shell.id, 12.25),
                MaterialRequirement(weekly.id, 3),
            ),
        ),
    )
    with pytest.raises(ValueError, match="in use"):
        service.delete_material(shell.id, data_set_id=primary_id)
    mutable_target["level"] = 1

    assert created.character_name == "主要角色"
    assert created.target_value["level"] == 10
    assert {item.material_id for item in created.required_materials} == {shell.id, weekly.id}
    assert created.due_date == date(2026, 8, 8)
    json.dumps(created.to_json_dict(), ensure_ascii=False, allow_nan=False)
    assert service.get_cultivation_target(created.id, data_set_id=primary_id) == created
    assert [
        item.id
        for item in service.list_cultivation_targets(
            data_set_id=primary_id,
            character_id=primary_character_id,
            include_completed=False,
        )
    ] == [created.id]

    updated = service.update_cultivation_target(
        created.id,
        data_set_id=primary_id,
        definition=CultivationTargetDefinition(
            target_type="character_skill",
            target_key="resonance_skill",
            character_id=primary_character_id,
            current_value=8,
            target_value=10,
            priority=3,
            is_completed=True,
            required_materials=(MaterialRequirement(weekly.id, 2.5),),
        ),
    )
    assert updated.current_value == 8
    assert [item.material_id for item in updated.required_materials] == [weekly.id]
    assert service.list_cultivation_targets(data_set_id=primary_id, include_completed=False) == ()
    assert not service.set_cultivation_target_completed(
        created.id, False, data_set_id=primary_id
    ).is_completed

    before_count = _row_count(database, CultivationTargetRecord)
    with pytest.raises(ValueError, match="character does not belong"):
        service.create_cultivation_target(
            data_set_id=primary_id,
            definition=CultivationTargetDefinition(
                target_type="level",
                target_value=90,
                character_id=demo_character_id,
            ),
        )
    with pytest.raises(ValueError, match="material does not belong"):
        service.create_cultivation_target(
            data_set_id=primary_id,
            definition=CultivationTargetDefinition(
                target_type="level",
                target_value=90,
                character_id=primary_character_id,
                required_materials=(MaterialRequirement(demo_material.id, 1),),
            ),
        )
    with pytest.raises(ValueError, match="material does not exist"):
        service.create_cultivation_target(
            data_set_id=primary_id,
            definition=CultivationTargetDefinition(
                target_type="level",
                target_value=90,
                required_materials=(MaterialRequirement("missing", 1),),
            ),
        )
    assert _row_count(database, CultivationTargetRecord) == before_count
    with pytest.raises(ValueError, match="requested data set"):
        service.get_cultivation_target(created.id, data_set_id=demo_id)
    assert service.delete_cultivation_target(created.id, data_set_id=primary_id)


def test_target_requirement_replacement_rolls_back_as_one_transaction(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, service, primary_id, _demo_id, character_id, _demo_character_id = storage
    first = service.create_material(
        data_set_id=primary_id,
        definition=_material("rollback-first"),
    )
    second = service.create_material(
        data_set_id=primary_id,
        definition=_material("rollback-second"),
    )
    target = service.create_cultivation_target(
        data_set_id=primary_id,
        definition=CultivationTargetDefinition(
            target_type="weapon_level",
            target_value=90,
            character_id=character_id,
            priority=7,
            required_materials=(MaterialRequirement(first.id, 10),),
        ),
    )

    def fail_after_delete(
        session: Any,
        target_id: str,
        data_set_id: str,
        requirements: Any,
    ) -> None:
        del data_set_id, requirements
        rows = session.scalars(
            select(CultivationTargetMaterialRecord).where(
                CultivationTargetMaterialRecord.cultivation_target_id == target_id
            )
        ).all()
        for row in rows:
            session.delete(row)
        session.flush()
        raise RuntimeError("simulated target material failure")

    monkeypatch.setattr(planning_module, "_replace_target_materials", fail_after_delete)
    with pytest.raises(RuntimeError, match="simulated target material failure"):
        service.update_cultivation_target(
            target.id,
            data_set_id=primary_id,
            definition=CultivationTargetDefinition(
                target_type="weapon_level",
                target_value=90,
                character_id=character_id,
                priority=1,
                required_materials=(MaterialRequirement(second.id, 20),),
            ),
        )

    restored = service.get_cultivation_target(target.id, data_set_id=primary_id)
    assert restored.priority == 7
    assert [(item.material_id, item.required_quantity) for item in restored.required_materials] == [
        (first.id, 10)
    ]


def test_daily_plan_replacement_preserves_generations_and_is_atomic(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
) -> None:
    _database, service, primary_id, demo_id, character_id, _demo_character_id = storage
    material = service.create_material(
        data_set_id=primary_id,
        definition=_material("daily-material"),
    )
    demo_material = service.create_material(
        data_set_id=demo_id,
        definition=_material("demo-daily-material"),
    )
    target = service.create_cultivation_target(
        data_set_id=primary_id,
        definition=CultivationTargetDefinition(
            target_type="skill",
            target_value=10,
            character_id=character_id,
            required_materials=(MaterialRequirement(material.id, 12),),
        ),
    )
    plan_date = date(2026, 8, 3)
    first = service.replace_daily_plan(
        data_set_id=primary_id,
        plan_date=plan_date,
        generation_id="generation-1",
        items=(
            DailyPlanItemDefinition(
                reason="第一優先補核心技能",
                cultivation_target_id=target.id,
                material_id=material.id,
                recommended_runs=3,
                stamina_cost=120,
                estimated_completion_date=date(2026, 8, 5),
                assumptions={"daily_stamina": 240},
            ),
            DailyPlanItemDefinition(reason="保留波片", status="deferred"),
        ),
    )
    assert [item.rank for item in first.items] == [1, 2]
    assert first.items[0].material_code == "daily-material"
    assert first.items[0].assumptions == {"daily_stamina": 240}
    json.dumps(first.to_json_dict(), ensure_ascii=False, allow_nan=False)

    second = service.replace_daily_plan(
        data_set_id=primary_id,
        plan_date=plan_date,
        generation_id="generation-2",
        items=(DailyPlanItemDefinition(reason="新的優先項目", recommended_runs=2),),
    )
    assert service.get_daily_plan(first.generation_id, data_set_id=primary_id).is_archived
    assert service.get_current_daily_plan(plan_date, data_set_id=primary_id) == second
    assert len(service.list_daily_plans(data_set_id=primary_id, plan_date=plan_date)) == 2
    assert [
        item.generation_id
        for item in service.list_daily_plans(
            data_set_id=primary_id,
            plan_date=plan_date,
            include_archived=False,
        )
    ] == [second.generation_id]

    with pytest.raises(ValueError, match="cultivation target does not exist"):
        service.replace_daily_plan(
            data_set_id=primary_id,
            plan_date=plan_date,
            items=(
                DailyPlanItemDefinition(
                    reason="錯誤 FK",
                    cultivation_target_id="missing-target",
                ),
            ),
        )
    with pytest.raises(ValueError, match="material does not belong"):
        service.replace_daily_plan(
            data_set_id=primary_id,
            plan_date=plan_date,
            items=(
                DailyPlanItemDefinition(
                    reason="跨資料集素材",
                    material_id=demo_material.id,
                ),
            ),
        )
    assert service.get_current_daily_plan(plan_date, data_set_id=primary_id) == second
    with pytest.raises(ValueError, match="already exists"):
        service.replace_daily_plan(
            data_set_id=primary_id,
            plan_date=plan_date,
            generation_id="generation-2",
            items=(DailyPlanItemDefinition(reason="重複 generation"),),
        )
    with pytest.raises(ValueError, match="requested data set"):
        service.get_daily_plan(second.generation_id, data_set_id=demo_id)
    with pytest.raises(ValueError, match="must be a date"):
        service.get_current_daily_plan(datetime(2026, 8, 3), data_set_id=primary_id)  # type: ignore[arg-type]

    completed = service.set_daily_plan_status(
        second.generation_id,
        "completed",
        data_set_id=primary_id,
    )
    assert completed.items[0].status == "completed"
    archived = service.archive_daily_plan(second.generation_id, data_set_id=primary_id)
    assert archived.is_archived
    assert archived.items[0].assumptions["archived_from_status"] == "completed"
    archived_again = service.archive_daily_plan(second.generation_id, data_set_id=primary_id)
    assert archived_again.items[0].assumptions["archived_from_status"] == "completed"
    assert service.get_current_daily_plan(plan_date, data_set_id=primary_id) is None
    with pytest.raises(ValueError, match="archived"):
        service.set_daily_plan_item_status(
            second.items[0].id,
            "planned",
            data_set_id=primary_id,
        )
    with pytest.raises(ValueError, match="plan history"):
        service.delete_cultivation_target(target.id, data_set_id=primary_id)


def test_versioned_manual_material_package_supports_preview_dry_run_and_apply(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
) -> None:
    database, service, primary_id, demo_id, *_characters = storage
    payload = _package("package-a", "package-b")
    validated = service.validate_material_package(payload)
    assert service.validate_material_package(validated.to_json_dict()) == validated

    preview = service.preview_material_package(data_set_id=primary_id, payload=payload)
    assert preview.is_valid
    assert preview.creates == ("package-a", "package-b")
    assert not preview.applied
    dry_run = service.apply_material_package(
        data_set_id=primary_id,
        payload=payload,
        dry_run=True,
    )
    assert dry_run == preview
    assert _row_count(database, MaterialRecord) == 0

    applied = service.apply_material_package(data_set_id=primary_id, payload=payload)
    assert applied.applied
    assert _row_count(database, MaterialRecord) == 2
    repeated = service.preview_material_package(data_set_id=primary_id, payload=payload)
    assert repeated.unchanged == ("package-a", "package-b")

    changed = _package("package-a", "package-b")
    materials = changed["materials"]
    assert isinstance(materials, list)
    assert isinstance(materials[0], dict)
    materials[0]["average_yield"] = 9.5
    assert service.preview_material_package(data_set_id=primary_id, payload=changed).updates == (
        "package-a",
    )
    service.apply_material_package(data_set_id=primary_id, payload=changed)
    assert service.list_materials(data_set_id=primary_id)[0].average_yield == 9.5

    demo_preview = service.preview_material_package(data_set_id=demo_id, payload=payload)
    assert demo_preview.is_valid
    assert demo_preview.creates == ("package-a", "package-b")
    service.apply_material_package(data_set_id=demo_id, payload=payload)
    assert {item.code for item in service.list_materials(data_set_id=demo_id)} == {
        "package-a",
        "package-b",
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 2, "source_version": "v2", "materials": []}, "unsupported"),
        (
            {
                "schema_version": 1,
                "source_version": "v1",
                "materials": [
                    {"code": "duplicate", "name": "A", "category": "X"},
                    {"code": "duplicate", "name": "B", "category": "X"},
                ],
            },
            "codes must be unique",
        ),
        (
            {
                "schema_version": 1,
                "source_version": "v1",
                "materials": [
                    {
                        "code": "invalid-day",
                        "name": "A",
                        "category": "X",
                        "open_days": [8],
                    }
                ],
            },
            "within \\[1, 7\\]",
        ),
    ],
)
def test_invalid_material_packages_are_rejected_before_database_access(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
    payload: dict[str, object],
    message: str,
) -> None:
    database, service, primary_id, *_rest = storage
    with pytest.raises(ValueError, match=message):
        service.apply_material_package(data_set_id=primary_id, payload=payload)
    assert _row_count(database, MaterialRecord) == 0


def test_material_package_apply_rolls_back_every_item_on_failure(
    storage: tuple[Database, CultivationPlanningService, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, service, primary_id, *_rest = storage
    original_apply = planning_module._apply_import_material
    calls = 0

    def fail_on_second(
        session: Any,
        data_set_id: str,
        definition: MaterialDefinition,
    ) -> None:
        nonlocal calls
        calls += 1
        original_apply(session, data_set_id, definition)
        session.flush()
        if calls == 2:
            raise RuntimeError("simulated package failure")

    monkeypatch.setattr(planning_module, "_apply_import_material", fail_on_second)
    with pytest.raises(RuntimeError, match="simulated package failure"):
        service.apply_material_package(
            data_set_id=primary_id,
            payload=_package("atomic-a", "atomic-b"),
        )
    assert _row_count(database, MaterialRecord) == 0


def test_planning_snapshots_survive_database_restart(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "restart")
    database = create_database(paths)
    with database.session_factory.begin() as session:
        data_set = DataSetRecord(name="主要資料", kind="primary", is_active=True)
        session.add(data_set)
        session.flush()
        data_set_id = data_set.id
    service = CultivationPlanningService(database.session_factory)
    material = service.create_material(
        data_set_id=data_set_id,
        definition=_material("restart-material"),
    )
    service.set_inventory_count(material.id, 123.5, data_set_id=data_set_id)
    target = service.create_cultivation_target(
        data_set_id=data_set_id,
        definition=CultivationTargetDefinition(
            target_type="manual",
            current_value={"done": 1},
            target_value={"done": 2},
            required_materials=(MaterialRequirement(material.id, 7.5),),
        ),
    )
    plan = service.replace_daily_plan(
        data_set_id=data_set_id,
        plan_date=date(2026, 8, 9),
        items=(
            DailyPlanItemDefinition(
                reason="重新啟動後仍需存在",
                cultivation_target_id=target.id,
                material_id=material.id,
                recommended_runs=2,
                stamina_cost=80,
            ),
        ),
    )
    database.dispose()

    reopened = create_database(paths)
    try:
        restored = CultivationPlanningService(reopened.session_factory)
        assert (
            restored.get_material(material.id, data_set_id=data_set_id).code == "restart-material"
        )
        assert restored.get_inventory_count(material.id, data_set_id=data_set_id).quantity == 123.5  # type: ignore[union-attr]
        assert restored.get_cultivation_target(target.id, data_set_id=data_set_id).target_value == {
            "done": 2
        }
        assert (
            restored.get_daily_plan(plan.generation_id, data_set_id=data_set_id)
            .items[0]
            .recommended_runs
            == 2
        )
    finally:
        reopened.dispose()


def _row_count(database: Database, model: type[Any]) -> int:
    with database.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)
