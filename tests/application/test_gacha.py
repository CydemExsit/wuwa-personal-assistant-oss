from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import pytest

from wuwa_assistant.application.gacha import (
    GachaIncomeDefinition,
    GachaPlanningService,
    GachaStatusDefinition,
    GachaTargetDefinition,
    parse_manual_pull_requirements,
)
from wuwa_assistant.gacha.models import ForecastScenario
from wuwa_assistant.storage import AppPaths, create_database
from wuwa_assistant.storage.database import Database
from wuwa_assistant.storage.orm import DataSetRecord


@pytest.fixture
def storage(
    tmp_path: Path,
) -> Iterator[tuple[Database, GachaPlanningService, str, str]]:
    database = create_database(AppPaths.from_root(tmp_path / "gacha"))
    with database.session_factory.begin() as session:
        primary = DataSetRecord(name="Primary", kind="primary", is_active=True)
        demo = DataSetRecord(name="Demo", kind="demo", is_active=False)
        session.add_all([primary, demo])
        session.flush()
        ids = (primary.id, demo.id)
    try:
        yield database, GachaPlanningService(database.session_factory), *ids
    finally:
        database.dispose()


def _target(
    name: str = "Target Resonator",
    *,
    target_type: str = "character",
    scenario_json: dict[str, object] | None = None,
    status: str = "planned",
) -> GachaTargetDefinition:
    return GachaTargetDefinition(
        target_type=target_type,
        name=name,
        target_copies=1,
        target_version="3.1",
        target_date=date(2026, 9, 10),
        priority=10,
        status=status,
        scenario_json=scenario_json
        or {
            "required_pulls": {
                "conservative": 12,
                "typical": 10,
                "optimistic": 8,
            }
        },
        notes="manual target",
    )


def _income(
    label: str = "Event income",
    *,
    pull_type: str = "character",
    scenario: str = "general",
    received: bool = False,
    astrite: int = 320,
    pulls: int = 3,
) -> GachaIncomeDefinition:
    return GachaIncomeDefinition(
        label=label,
        expected_date=date(2026, 8, 20),
        astrite_amount=astrite,
        pull_amount=pulls,
        pull_type=pull_type,
        scenario=scenario,
        recurrence_rule_json={"kind": "once", "metadata": {"manual": True}},
        is_received=received,
        notes="manual income",
    )


def test_status_upsert_is_unique_and_dataset_scoped(
    storage: tuple[Database, GachaPlanningService, str, str],
) -> None:
    _database, service, primary_id, demo_id = storage
    first = service.save_status(
        data_set_id=primary_id,
        definition=GachaStatusDefinition(
            astrite=12345,
            limited_character_pulls=7,
            limited_weapon_pulls=4,
            character_pity=60,
            weapon_pity=10,
            character_guaranteed=True,
        ),
    )
    updated = service.save_status(
        data_set_id=primary_id,
        definition=GachaStatusDefinition(astrite=160),
    )
    demo = service.save_status(
        data_set_id=demo_id,
        definition=GachaStatusDefinition(astrite=999),
    )

    assert updated.id == first.id
    assert service.get_status(data_set_id=primary_id) == updated
    assert service.get_status(data_set_id=demo_id) == demo
    assert updated.astrite == 160
    assert demo.astrite == 999
    json.dumps(updated.to_json_dict(), allow_nan=False)
    assert service.delete_status(data_set_id=primary_id)
    assert not service.delete_status(data_set_id=primary_id)
    assert service.get_status(data_set_id=primary_id) is None


def test_target_crud_returns_detached_json_and_enforces_dataset(
    storage: tuple[Database, GachaPlanningService, str, str],
) -> None:
    _database, service, primary_id, demo_id = storage
    source = {
        "required_pulls": {"conservative": 160, "typical": 120, "optimistic": 80},
        "user_note": {"confidence": "manual"},
    }
    created = service.create_target(
        data_set_id=primary_id,
        definition=_target(scenario_json=source),
    )
    source["user_note"] = {"confidence": "changed-after-save"}
    created.scenario_json["user_note"] = {"confidence": "changed-snapshot"}

    stored = service.get_target(created.id, data_set_id=primary_id)
    assert stored.scenario_json["user_note"] == {"confidence": "manual"}
    assert service.list_targets(data_set_id=demo_id) == ()
    json.dumps(stored.to_json_dict(), ensure_ascii=False, allow_nan=False)

    updated = service.update_target(
        created.id,
        data_set_id=primary_id,
        definition=_target("Updated target", target_type="weapon", status="active"),
    )
    assert updated.id == created.id
    assert updated.name == "Updated target"
    assert updated.target_type == "weapon"
    assert service.list_targets(data_set_id=primary_id, include_inactive=False) == (updated,)

    with pytest.raises(ValueError, match="requested data set"):
        service.get_target(created.id, data_set_id=demo_id)
    with pytest.raises(ValueError, match="requested data set"):
        service.delete_target(created.id, data_set_id=demo_id)
    assert service.delete_target(created.id, data_set_id=primary_id)
    assert service.list_targets(data_set_id=primary_id) == ()


def test_income_crud_preserves_ticket_type_and_nested_json(
    storage: tuple[Database, GachaPlanningService, str, str],
) -> None:
    _database, service, primary_id, demo_id = storage
    created = service.create_income(
        data_set_id=primary_id,
        definition=_income(pull_type="weapon", scenario="typical"),
    )
    assert created.pull_type == "weapon"
    assert created.scenario == "typical"
    assert created.recurrence_rule_json == {
        "kind": "once",
        "metadata": {"manual": True},
    }
    assert created.recurrence_rule_json is not None
    created.recurrence_rule_json["metadata"] = {}
    assert service.get_income(created.id, data_set_id=primary_id).recurrence_rule_json == {
        "kind": "once",
        "metadata": {"manual": True},
    }
    assert service.list_incomes(data_set_id=demo_id) == ()

    updated = service.update_income(
        created.id,
        data_set_id=primary_id,
        definition=_income("Received", pull_type="unknown", received=True),
    )
    assert updated.pull_type == "unknown"
    assert updated.is_received is True
    assert service.list_incomes(data_set_id=primary_id, include_received=False) == ()
    json.dumps(updated.to_json_dict(), ensure_ascii=False, allow_nan=False)

    with pytest.raises(ValueError, match="requested data set"):
        service.update_income(
            created.id,
            data_set_id=demo_id,
            definition=_income(),
        )
    assert service.delete_income(created.id, data_set_id=primary_id)


def test_build_plan_uses_one_dataset_and_only_explicit_requirements(
    storage: tuple[Database, GachaPlanningService, str, str],
) -> None:
    _database, service, primary_id, demo_id = storage
    service.save_status(
        data_set_id=primary_id,
        definition=GachaStatusDefinition(
            astrite=320,
            limited_character_pulls=2,
            character_pity=79,
            character_guaranteed=True,
        ),
    )
    first = service.create_target(
        data_set_id=primary_id,
        definition=_target(
            "Explicit target",
            scenario_json={
                "required_pulls": {
                    "conservative": 8,
                    "typical": 7,
                    "optimistic": 6,
                }
            },
        ),
    )
    missing = service.create_target(
        data_set_id=primary_id,
        definition=GachaTargetDefinition(
            target_type="weapon",
            name="Unknown requirement",
            priority=20,
            scenario_json={"pity": 79, "guaranteed": True},
        ),
    )
    service.create_income(data_set_id=primary_id, definition=_income())
    service.create_income(
        data_set_id=demo_id,
        definition=_income("Must stay in demo", astrite=99999),
    )

    plan = service.build_plan(data_set_id=primary_id, as_of=date(2026, 8, 8))
    conservative = plan.scenario(ForecastScenario.CONSERVATIVE)
    assert [allocation.target_id for allocation in conservative.allocations] == [
        first.id,
        missing.id,
    ]
    assert conservative.allocations[0].required_pulls == 8
    assert conservative.allocations[0].funded_pulls == 8
    assert conservative.remaining_resources.astrite == 160
    assert conservative.allocations[1].required_pulls is None
    assert conservative.allocations[1].funded_pulls is None
    assert any(
        warning.code == "manual_requirement_missing" and warning.target_id == missing.id
        for warning in plan.warnings
    )
    workspace = service.load_workspace(data_set_id=primary_id, as_of=date(2026, 8, 8))
    assert workspace.status is not None
    assert {target.id for target in workspace.targets} == {first.id, missing.id}
    assert len(workspace.incomes) == 1
    assert workspace.plan == plan


def test_empty_dataset_plan_starts_at_known_zero_without_future_income(
    storage: tuple[Database, GachaPlanningService, str, str],
) -> None:
    _database, service, _primary_id, demo_id = storage
    plan = service.build_plan(data_set_id=demo_id, as_of=date(2026, 8, 8))
    for scenario in ForecastScenario:
        result = plan.scenario(scenario)
        assert result.allocations == ()
        assert result.remaining_resources.astrite == 0


class _InvalidOperation(Protocol):
    def __call__(self, service: GachaPlanningService, data_set_id: str) -> Any: ...


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            lambda service, data_set_id: service.save_status(
                data_set_id=data_set_id,
                definition=GachaStatusDefinition(astrite=-1),
            ),
            "astrite",
        ),
        (
            lambda service, data_set_id: service.create_target(
                data_set_id=data_set_id,
                definition=_target(target_type="standard"),
            ),
            "target type",
        ),
        (
            lambda service, data_set_id: service.create_income(
                data_set_id=data_set_id,
                definition=_income(pull_type="intertwined"),
            ),
            "pull type",
        ),
        (
            lambda service, data_set_id: service.create_target(
                data_set_id=data_set_id,
                definition=_target(scenario_json={"bad": float("nan")}),
            ),
            "JSON-compatible",
        ),
    ],
)
def test_invalid_writes_are_rejected_without_partial_rows(
    storage: tuple[Database, GachaPlanningService, str, str],
    operation: _InvalidOperation,
    message: str,
) -> None:
    _database, service, primary_id, _demo_id = storage
    with pytest.raises(ValueError, match=message):
        operation(service, primary_id)
    assert service.get_status(data_set_id=primary_id) is None
    assert service.list_targets(data_set_id=primary_id) == ()
    assert service.list_incomes(data_set_id=primary_id) == ()


def test_manual_requirement_parser_never_infers_missing_values() -> None:
    parsed = parse_manual_pull_requirements(
        {
            "required_pulls": {
                "conservative": 160,
                "typical": "120",
                "optimistic": -1,
            },
            "character_pity": 79,
            "character_guaranteed": True,
        }
    )
    assert parsed.conservative == 160
    assert parsed.typical is None
    assert parsed.optimistic is None
