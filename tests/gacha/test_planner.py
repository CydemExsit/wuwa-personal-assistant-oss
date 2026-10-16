from __future__ import annotations

from datetime import date

from wuwa_assistant.gacha.models import (
    ForecastScenario,
    GachaIncome,
    GachaResources,
    GachaStatus,
    GachaTarget,
    IncomeScenario,
    ManualPullRequirements,
    PullType,
)
from wuwa_assistant.gacha.planner import plan_gacha_resources

AS_OF = date(2026, 8, 8)


def _status(*, astrite: int = 0, character_pulls: int = 0, weapon_pulls: int = 0) -> GachaStatus:
    return GachaStatus(
        resources=GachaResources(
            astrite=astrite,
            character_pulls=character_pulls,
            weapon_pulls=weapon_pulls,
        )
    )


def _target(
    target_id: str,
    target_type: PullType,
    *,
    priority: int = 100,
    target_date: date | None = date(2026, 9, 1),
    conservative: int | None = 10,
    typical: int | None = 10,
    optimistic: int | None = 10,
) -> GachaTarget:
    return GachaTarget(
        target_id=target_id,
        target_type=target_type,
        name=target_id,
        target_copies=1,
        target_date=target_date,
        priority=priority,
        status="planned",
        requirements=ManualPullRequirements(
            conservative=conservative,
            typical=typical,
            optimistic=optimistic,
        ),
    )


def _income(
    income_id: str,
    *,
    scenario: IncomeScenario | None = IncomeScenario.GENERAL,
    expected_date: date | None = date(2026, 8, 15),
    astrite: int = 0,
    pulls: int = 0,
    pull_type: PullType = PullType.UNKNOWN,
    received: bool = False,
) -> GachaIncome:
    return GachaIncome(
        income_id=income_id,
        label=income_id,
        expected_date=expected_date,
        astrite_amount=astrite,
        pull_amount=pulls,
        pull_type=pull_type,
        scenario=scenario,
        is_received=received,
    )


def test_shared_astrite_is_allocated_once_by_priority_across_target_types() -> None:
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=_status(astrite=1600),
        targets=(
            _target("weapon", PullType.WEAPON, priority=20, conservative=5),
            _target("character", PullType.CHARACTER, priority=10, conservative=8),
        ),
        incomes=(),
    )

    allocations = result.scenario(ForecastScenario.CONSERVATIVE).allocations
    assert [item.target_id for item in allocations] == ["character", "weapon"]
    assert allocations[0].funded_pulls == 8
    assert allocations[0].astrite_used == 1280
    assert allocations[1].funded_pulls == 2
    assert allocations[1].missing_pulls == 3
    assert result.scenario(ForecastScenario.CONSERVATIVE).remaining_resources.astrite == 0


def test_dedicated_tickets_are_separate_and_used_before_shared_astrite() -> None:
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=_status(astrite=800, character_pulls=3, weapon_pulls=1),
        targets=(
            _target("character", PullType.CHARACTER, priority=1, conservative=5),
            _target("weapon", PullType.WEAPON, priority=2, conservative=4),
        ),
        incomes=(),
    )

    character, weapon = result.scenario(ForecastScenario.CONSERVATIVE).allocations
    assert (character.dedicated_pulls_used, character.astrite_used) == (3, 320)
    assert (weapon.dedicated_pulls_used, weapon.astrite_used) == (1, 480)
    assert character.is_fully_funded is True
    assert weapon.is_fully_funded is True


def test_income_forecasts_are_nested_without_cross_scenario_leakage() -> None:
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=_status(),
        targets=(
            _target(
                "character",
                PullType.CHARACTER,
                conservative=99,
                typical=99,
                optimistic=99,
            ),
        ),
        incomes=(
            _income("general", pulls=2, pull_type=PullType.CHARACTER),
            _income(
                "conservative",
                scenario=IncomeScenario.CONSERVATIVE,
                pulls=3,
                pull_type=PullType.CHARACTER,
            ),
            _income(
                "typical",
                scenario=IncomeScenario.TYPICAL,
                pulls=5,
                pull_type=PullType.CHARACTER,
            ),
            _income(
                "optimistic",
                scenario=IncomeScenario.OPTIMISTIC,
                pulls=7,
                pull_type=PullType.CHARACTER,
            ),
        ),
    )

    conservative = result.scenario(ForecastScenario.CONSERVATIVE)
    typical = result.scenario(ForecastScenario.TYPICAL)
    optimistic = result.scenario(ForecastScenario.OPTIMISTIC)
    assert conservative.allocations[0].funded_pulls == 5
    assert typical.allocations[0].funded_pulls == 10
    assert optimistic.allocations[0].funded_pulls == 17
    assert conservative.included_income_ids == ("conservative", "general")
    assert typical.included_income_ids == ("conservative", "general", "typical")
    assert optimistic.included_income_ids == (
        "conservative",
        "general",
        "optimistic",
        "typical",
    )


def test_untrusted_income_is_excluded_with_specific_warnings() -> None:
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=_status(),
        targets=(_target("character", PullType.CHARACTER, conservative=20),),
        incomes=(
            _income("received", astrite=160, received=True),
            _income("undated", expected_date=None, astrite=160),
            _income("after", expected_date=date(2026, 10, 1), astrite=160),
            _income("unknown-kind", astrite=160, pulls=50, pull_type=PullType.UNKNOWN),
            _income("unknown-ticket-only", pulls=50, pull_type=PullType.UNKNOWN),
            _income("unknown-scenario", scenario=None, astrite=160),
        ),
    )

    conservative = result.scenario(ForecastScenario.CONSERVATIVE)
    assert conservative.allocations[0].funded_pulls == 1
    assert conservative.included_income_ids == ("unknown-kind",)
    codes = {
        warning.code
        for warning in result.warnings
        if warning.scenario is ForecastScenario.CONSERVATIVE
    }
    assert {
        "income_already_received",
        "income_date_missing",
        "income_after_planning_horizon",
        "income_pull_type_unknown",
        "income_scenario_unknown",
    } <= codes


def test_missing_manual_requirement_stays_null_and_does_not_use_pity() -> None:
    status = GachaStatus(
        resources=GachaResources(astrite=16000),
        character_pity=79,
        character_guaranteed=True,
    )
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=status,
        targets=(
            _target(
                "character",
                PullType.CHARACTER,
                conservative=None,
                typical=None,
                optimistic=None,
            ),
        ),
        incomes=(),
    )

    for scenario in ForecastScenario:
        allocation = result.scenario(scenario).allocations[0]
        assert allocation.required_pulls is None
        assert allocation.funded_pulls is None
        assert allocation.missing_pulls is None
        assert allocation.is_fully_funded is None
        assert result.scenario(scenario).remaining_resources.astrite == 16000
    assert sum(warning.code == "manual_requirement_missing" for warning in result.warnings) == 3


def test_later_income_funds_high_priority_later_target_without_leaking_deadline() -> None:
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=_status(character_pulls=1),
        targets=(
            _target(
                "future-high-priority",
                PullType.CHARACTER,
                priority=1,
                target_date=date(2026, 9, 1),
                conservative=1,
            ),
            _target(
                "early-low-priority",
                PullType.CHARACTER,
                priority=2,
                target_date=date(2026, 8, 10),
                conservative=1,
            ),
        ),
        incomes=(
            _income(
                "late-ticket",
                expected_date=date(2026, 8, 20),
                pulls=1,
                pull_type=PullType.CHARACTER,
            ),
        ),
    )

    high, early = result.scenario(ForecastScenario.CONSERVATIVE).allocations
    assert high.funded_pulls == 1
    assert early.funded_pulls == 1
    assert any(
        warning.code == "income_after_target_date" and warning.target_id == "early-low-priority"
        for warning in result.warnings
    )


def test_target_without_date_uses_only_current_resources() -> None:
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=_status(character_pulls=1),
        targets=(
            _target(
                "undated",
                PullType.CHARACTER,
                target_date=None,
                conservative=2,
            ),
        ),
        incomes=(_income("future", pulls=10, pull_type=PullType.CHARACTER),),
    )

    allocation = result.scenario(ForecastScenario.CONSERVATIVE).allocations[0]
    assert allocation.funded_pulls == 1
    assert allocation.missing_pulls == 1
    assert any(warning.code == "target_date_missing" for warning in result.warnings)


def test_past_unreceived_income_is_excluded_and_past_target_is_visible() -> None:
    result = plan_gacha_resources(
        as_of=AS_OF,
        status=_status(character_pulls=1),
        targets=(
            _target(
                "past-target",
                PullType.CHARACTER,
                target_date=date(2026, 8, 7),
                conservative=3,
            ),
        ),
        incomes=(
            _income(
                "past-income",
                expected_date=date(2026, 8, 7),
                pulls=20,
                pull_type=PullType.CHARACTER,
            ),
        ),
    )

    conservative = result.scenario(ForecastScenario.CONSERVATIVE)
    assert conservative.allocations[0].funded_pulls == 1
    assert conservative.included_income_ids == ()
    codes = {
        warning.code
        for warning in result.warnings
        if warning.scenario is ForecastScenario.CONSERVATIVE
    }
    assert {"income_date_past", "target_date_past"} <= codes
