from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from wuwa_assistant.planning import (
    DailyActionStatus,
    DailyPlanRequest,
    InventoryCount,
    MaterialDefinition,
    MaterialPlanningOverride,
    MaterialRequirement,
    PlanNoticeCode,
    ResourcePlanningTarget,
    WeeklyMaterialUsage,
    build_daily_plan,
    calculate_material_gaps,
    calculate_target_material_gaps,
)

MONDAY = date(2026, 8, 3)


def _material(
    material_id: str,
    *,
    name: str | None = None,
    cost: int | None = 40,
    average_yield: str | None = "2",
    weekdays: tuple[int, ...] | None = (1, 2, 3, 4, 5, 6, 7),
    weekly_limit: int | None = None,
) -> MaterialDefinition:
    return MaterialDefinition(
        material_id=material_id,
        name=name or material_id,
        category="培養素材",
        stamina_cost_per_run=cost,
        average_yield=None if average_yield is None else Decimal(average_yield),
        open_weekdays=weekdays,
        weekly_limit=weekly_limit,
        source_version="manual-v1",
    )


def _target(
    target_id: str,
    rank: int,
    *,
    character_id: str | None = None,
    cultivation_target_id: str | None = None,
    due_date: date | None = None,
) -> ResourcePlanningTarget:
    return ResourcePlanningTarget(
        target_id,
        f"目標 {target_id}",
        rank,
        f"{target_id} 是目前 breadth-first 順位",
        character_id=character_id,
        cultivation_target_id=cultivation_target_id,
        due_date=due_date,
    )


def _request(
    *,
    waveplates: int = 120,
    targets: tuple[ResourcePlanningTarget, ...],
    materials: tuple[MaterialDefinition, ...],
    requirements: tuple[MaterialRequirement, ...],
    inventory: tuple[InventoryCount, ...] | None = None,
    weekly_usage: tuple[WeeklyMaterialUsage, ...] = (),
    overrides: tuple[MaterialPlanningOverride, ...] = (),
    max_actions: int = 2,
) -> DailyPlanRequest:
    return DailyPlanRequest(
        plan_date=MONDAY,
        daily_waveplates=waveplates,
        targets=targets,
        materials=materials,
        requirements=requirements,
        inventory=(
            tuple(InventoryCount(item.material_id, Decimal("0")) for item in materials)
            if inventory is None
            else inventory
        ),
        weekly_usage=weekly_usage,
        overrides=overrides,
        max_actions=max_actions,
    )


def test_material_gaps_do_not_double_count_shared_inventory() -> None:
    requirements = (
        MaterialRequirement("first", "shell", Decimal("7")),
        MaterialRequirement("second", "shell", Decimal("5")),
    )
    inventory = (InventoryCount("shell", Decimal("5")),)

    aggregate = calculate_material_gaps(requirements, inventory)
    allocated = calculate_target_material_gaps(
        requirements,
        inventory,
        target_order=("second", "first"),
    )

    assert aggregate[0].required_quantity == Decimal("12")
    assert aggregate[0].inventory_quantity == Decimal("5")
    assert aggregate[0].gap_quantity == Decimal("7")
    assert aggregate[0].inventory_is_known
    assert [(item.target_id, item.inventory_applied, item.gap_quantity) for item in allocated] == [
        ("second", Decimal("5"), Decimal("0")),
        ("first", Decimal("0"), Decimal("7")),
    ]


def test_daily_plan_allocates_primary_then_secondary_with_linkage() -> None:
    targets = (
        _target(
            "first",
            1,
            character_id="char-1",
            cultivation_target_id="cultivation-1",
        ),
        _target("second", 2, character_id="char-2"),
    )
    materials = (
        _material("boss", name="首領素材", cost=40, average_yield="3"),
        _material("credits", name="貝幣", cost=20, average_yield="2"),
    )
    requirements = (
        MaterialRequirement("first", "boss", Decimal("5")),
        MaterialRequirement("second", "credits", Decimal("4")),
    )

    result = build_daily_plan(
        _request(
            waveplates=100,
            targets=targets,
            materials=materials,
            requirements=requirements,
        )
    )

    assert [
        (item.target_id, item.material_id, item.recommended_runs) for item in result.actions
    ] == [
        ("first", "boss", 2),
        ("second", "credits", 1),
    ]
    assert result.actions[0].character_id == "char-1"
    assert result.actions[0].cultivation_target_id == "cultivation-1"
    assert result.actions[0].stamina_cost == 80
    assert result.actions[0].gap_after == Decimal("0")
    assert result.actions[1].estimated_completion_date == date(2026, 8, 4)
    assert result.remaining_waveplates == 0
    assert result.weekly_shortages == ()
    assert all(action.reason for action in result.actions)


def test_closed_higher_priority_target_is_deferred_for_an_open_target() -> None:
    targets = (_target("closed", 1), _target("open", 2))
    materials = (
        _material("tuesday", weekdays=(2,)),
        _material("monday", weekdays=(1,)),
    )
    requirements = (
        MaterialRequirement("closed", "tuesday", Decimal("2")),
        MaterialRequirement("open", "monday", Decimal("2")),
    )

    result = build_daily_plan(
        _request(targets=targets, materials=materials, requirements=requirements)
    )

    assert len(result.actions) == 1
    assert result.actions[0].target_id == "open"
    assert any(notice.code is PlanNoticeCode.CLOSED_TODAY for notice in result.notices)
    closed_estimate = next(item for item in result.target_estimates if item.target_id == "closed")
    assert closed_estimate.estimated_date == date(2026, 8, 4)


def test_same_target_prioritizes_rare_open_day_before_daily_material() -> None:
    target = _target("target", 1)
    materials = (
        _material("a-daily", cost=40, average_yield="1"),
        _material("z-monday", cost=40, average_yield="1", weekdays=(1,)),
    )
    requirements = (
        MaterialRequirement("target", "a-daily", Decimal("1")),
        MaterialRequirement("target", "z-monday", Decimal("1")),
    )

    result = build_daily_plan(
        _request(
            waveplates=40,
            targets=(target,),
            materials=materials,
            requirements=requirements,
        )
    )

    assert len(result.actions) == 1
    assert result.actions[0].material_id == "z-monday"
    assert result.actions[0].recommended_runs == 1
    assert result.actions[0].estimated_completion_date == date(2026, 8, 4)


def test_one_run_surplus_is_shared_with_later_targets_for_same_material() -> None:
    material = _material(
        "shared",
        cost=40,
        average_yield="3",
        weekly_limit=1,
    )
    result = build_daily_plan(
        _request(
            waveplates=40,
            targets=(_target("first", 1), _target("second", 2)),
            materials=(material,),
            requirements=(
                MaterialRequirement("first", "shared", Decimal("1")),
                MaterialRequirement("second", "shared", Decimal("2")),
            ),
        )
    )

    assert [(item.target_id, item.recommended_runs) for item in result.actions] == [("first", 1)]
    assert [item.estimated_date for item in result.target_estimates] == [MONDAY, MONDAY]
    assert result.weekly_shortages == ()


def test_material_scoped_max_runs_today_is_not_reused_per_target() -> None:
    material = _material("shared", cost=10, average_yield="1")
    result = build_daily_plan(
        _request(
            waveplates=20,
            targets=(_target("first", 1), _target("second", 2)),
            materials=(material,),
            requirements=(
                MaterialRequirement("first", "shared", Decimal("1")),
                MaterialRequirement("second", "shared", Decimal("1")),
            ),
            overrides=(MaterialPlanningOverride("shared", max_runs_today=1),),
        )
    )

    assert [(item.target_id, item.recommended_runs) for item in result.actions] == [("first", 1)]
    assert [item.estimated_date for item in result.target_estimates] == [
        MONDAY,
        date(2026, 8, 4),
    ]


def test_completion_simulation_respects_daily_action_limit() -> None:
    target = _target("target", 1)
    materials = tuple(_material(item, cost=40, average_yield="1") for item in ("a", "b", "c"))
    requirements = tuple(
        MaterialRequirement("target", item.material_id, Decimal("1")) for item in materials
    )

    result = build_daily_plan(
        _request(
            waveplates=120,
            targets=(target,),
            materials=materials,
            requirements=requirements,
            max_actions=2,
        )
    )

    assert len(result.actions) == 2
    assert result.remaining_waveplates == 40
    assert result.target_estimates[0].estimated_date == date(2026, 8, 4)


def test_earlier_due_target_wins_when_queue_ranks_are_equal() -> None:
    result = build_daily_plan(
        _request(
            waveplates=40,
            targets=(
                _target("undated", 1),
                _target("due", 1, due_date=MONDAY),
            ),
            materials=(_material("undated"), _material("due")),
            requirements=(
                MaterialRequirement("undated", "undated", Decimal("2")),
                MaterialRequirement("due", "due", Decimal("2")),
            ),
        )
    )

    assert result.actions[0].target_id == "due"


def test_unknown_yield_and_schedule_require_confirmation_without_estimate() -> None:
    targets = (_target("unknown", 1), _target("known", 2))
    materials = (
        _material("mystery", average_yield=None, weekdays=None),
        _material("known", cost=40, average_yield="2"),
    )
    requirements = (
        MaterialRequirement("unknown", "mystery", Decimal("3")),
        MaterialRequirement("known", "known", Decimal("2")),
    )

    result = build_daily_plan(
        _request(targets=targets, materials=materials, requirements=requirements)
    )

    first = result.actions[0]
    assert first.target_id == "unknown"
    assert first.status is DailyActionStatus.NEEDS_CONFIRMATION
    assert first.requires_manual_confirmation
    assert first.recommended_runs is None
    assert first.expected_quantity is None
    assert first.gap_after is None
    assert first.estimated_completion_date is None
    assert "需要人工確認" in first.reason
    assert result.actions[1].status is DailyActionStatus.PLANNED
    assert not result.actions[1].requires_manual_confirmation
    estimate = next(item for item in result.target_estimates if item.target_id == "unknown")
    assert estimate.estimated_date is None
    assert estimate.requires_manual_confirmation
    assert {notice.code for notice in result.notices} >= {
        PlanNoticeCode.UNKNOWN_SCHEDULE,
        PlanNoticeCode.UNKNOWN_YIELD,
    }
    assert result.weekly_shortages[0].requires_manual_confirmation


def test_missing_inventory_is_unknown_but_explicit_zero_can_be_planned() -> None:
    target = _target("target", 1)
    material = _material("shell", cost=40, average_yield="2")
    requirement = MaterialRequirement("target", "shell", Decimal("4"))

    unknown = build_daily_plan(
        _request(
            targets=(target,),
            materials=(material,),
            requirements=(requirement,),
            inventory=(),
        )
    )
    known_zero = build_daily_plan(
        _request(
            targets=(target,),
            materials=(material,),
            requirements=(requirement,),
            inventory=(InventoryCount("shell", Decimal("0")),),
        )
    )

    unknown_action = unknown.actions[0]
    assert unknown_action.status is DailyActionStatus.NEEDS_CONFIRMATION
    assert unknown_action.requires_manual_confirmation
    assert unknown_action.recommended_runs is None
    assert unknown_action.stamina_cost is None
    assert unknown_action.expected_quantity is None
    assert unknown_action.gap_before is None
    assert unknown_action.estimated_completion_date is None
    assert "庫存未登錄" in unknown_action.reason
    assert any(notice.code is PlanNoticeCode.UNKNOWN_INVENTORY for notice in unknown.notices)
    assert unknown.target_estimates[0].requires_manual_confirmation
    assert unknown.target_estimates[0].estimated_date is None
    assert unknown.weekly_shortages[0].requires_manual_confirmation
    assert unknown.weekly_shortages[0].remaining_quantity is None
    assert not unknown.material_gaps[0].inventory_is_known
    assert not unknown.target_gaps[0].inventory_is_known

    known_action = known_zero.actions[0]
    assert known_action.status is DailyActionStatus.PLANNED
    assert not known_action.requires_manual_confirmation
    assert known_action.recommended_runs == 2
    assert known_action.stamina_cost == 80
    assert known_action.expected_quantity == Decimal("4")
    assert known_action.gap_before == Decimal("4")
    assert known_action.estimated_completion_date == MONDAY
    assert known_zero.material_gaps[0].inventory_is_known


def test_weekly_limit_is_respected_and_reported_as_shortage() -> None:
    material = _material(
        "weekly",
        average_yield="2",
        weekdays=(1,),
        weekly_limit=1,
    )
    request = _request(
        targets=(_target("target", 1),),
        materials=(material,),
        requirements=(MaterialRequirement("target", "weekly", Decimal("10")),),
    )

    result = build_daily_plan(request)

    assert result.actions[0].recommended_runs == 1
    assert result.weekly_shortages[0].remaining_quantity == Decimal("8")
    assert not result.weekly_shortages[0].requires_manual_confirmation
    estimate = result.target_estimates[0]
    assert estimate.estimated_date == date(2026, 8, 31)


def test_existing_weekly_usage_can_exhaust_the_limit() -> None:
    material = _material("weekly", weekdays=(1,), weekly_limit=2)
    result = build_daily_plan(
        _request(
            targets=(_target("target", 1),),
            materials=(material,),
            requirements=(MaterialRequirement("target", "weekly", Decimal("2")),),
            weekly_usage=(WeeklyMaterialUsage("weekly", 2),),
        )
    )

    assert result.actions == ()
    assert any(notice.code is PlanNoticeCode.WEEKLY_LIMIT_REACHED for notice in result.notices)
    assert result.target_estimates[0].estimated_date == date(2026, 8, 10)


def test_explicit_override_enables_unknown_material_and_caps_today_runs() -> None:
    material = _material("manual", cost=None, average_yield=None, weekdays=None)
    override = MaterialPlanningOverride(
        "manual",
        stamina_cost_per_run=20,
        average_yield=Decimal("2"),
        open_weekdays=(1, 2, 3, 4, 5, 6, 7),
        max_runs_today=1,
    )
    result = build_daily_plan(
        _request(
            waveplates=100,
            targets=(_target("target", 1),),
            materials=(material,),
            requirements=(MaterialRequirement("target", "manual", Decimal("5")),),
            overrides=(override,),
        )
    )

    action = result.actions[0]
    assert action.status is DailyActionStatus.PLANNED
    assert action.recommended_runs == 1
    assert action.gap_after == Decimal("3")
    assert action.estimated_completion_date == date(2026, 8, 4)
    assert any(notice.code is PlanNoticeCode.MANUAL_OVERRIDE for notice in result.notices)


def test_today_only_schedule_override_does_not_invent_future_open_days() -> None:
    material = _material("unknown-days", weekdays=None)
    override = MaterialPlanningOverride("unknown-days", open_today=True, max_runs_today=1)
    result = build_daily_plan(
        _request(
            targets=(_target("target", 1),),
            materials=(material,),
            requirements=(MaterialRequirement("target", "unknown-days", Decimal("4")),),
            overrides=(override,),
        )
    )

    assert result.actions[0].recommended_runs == 1
    assert result.actions[0].estimated_completion_date is None
    assert result.target_estimates[0].requires_manual_confirmation
    assert result.weekly_shortages[0].requires_manual_confirmation


def test_zero_gap_completed_and_zero_budget_behaviors_are_explicit() -> None:
    target = _target("target", 1)
    material = _material("shell", cost=40)
    complete = build_daily_plan(
        _request(
            targets=(target,),
            materials=(material,),
            requirements=(MaterialRequirement("target", "shell", Decimal("2")),),
            inventory=(InventoryCount("shell", Decimal("2")),),
        )
    )
    no_budget = build_daily_plan(
        _request(
            waveplates=0,
            targets=(target,),
            materials=(material,),
            requirements=(MaterialRequirement("target", "shell", Decimal("2")),),
        )
    )

    assert complete.actions == ()
    assert complete.material_gaps[0].gap_quantity == 0
    assert complete.target_estimates[0].estimated_date == MONDAY
    assert no_budget.actions == ()
    assert any(
        notice.code is PlanNoticeCode.INSUFFICIENT_WAVEPLATES for notice in no_budget.notices
    )
    assert no_budget.target_estimates[0].estimated_date is None


def test_daily_plan_is_deterministic_for_reordered_input() -> None:
    targets = (_target("b", 1), _target("a", 1))
    materials = (_material("z"), _material("a"))
    requirements = (
        MaterialRequirement("b", "z", Decimal("2")),
        MaterialRequirement("a", "a", Decimal("2")),
    )
    forward = build_daily_plan(
        _request(targets=targets, materials=materials, requirements=requirements)
    )
    reverse = build_daily_plan(
        _request(
            targets=tuple(reversed(targets)),
            materials=tuple(reversed(materials)),
            requirements=tuple(reversed(requirements)),
        )
    )

    assert forward == reverse
    assert [action.target_id for action in forward.actions] == ["a", "b"]


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: InventoryCount("item", Decimal("-1")),
        lambda: MaterialRequirement("target", "item", Decimal("NaN")),
        lambda: _material("item", weekdays=()),
        lambda: _material("item", weekdays=(0,)),
        lambda: _material("item", average_yield="0"),
        lambda: MaterialPlanningOverride("item", max_runs_today=-1),
    ],
)
def test_resource_boundaries_fail_fast(constructor: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        constructor()  # type: ignore[operator]


def test_references_and_duplicates_fail_fast() -> None:
    target = _target("target", 1)
    material = _material("item")
    with pytest.raises(ValueError, match="unknown target"):
        build_daily_plan(
            _request(
                targets=(target,),
                materials=(material,),
                requirements=(MaterialRequirement("missing", "item", Decimal("1")),),
            )
        )
    with pytest.raises(ValueError, match="material ids must be unique"):
        build_daily_plan(
            _request(
                targets=(target,),
                materials=(material, _material("ITEM")),
                requirements=(),
            )
        )
    with pytest.raises(ValueError, match="inventory ids must be unique"):
        calculate_material_gaps(
            (),
            (InventoryCount("item", Decimal("1")), InventoryCount("ITEM", Decimal("2"))),
        )
