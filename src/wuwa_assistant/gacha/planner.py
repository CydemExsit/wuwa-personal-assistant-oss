"""Deterministic, offline gacha scenario allocation.

Income confidence is nested: a conservative forecast contains ``general`` and
``conservative`` entries, typical additionally contains ``typical``, and
optimistic additionally contains ``optimistic``.  Current status is always the
starting point.  Received or undated income is never added because it either is
already reflected in status or has no safe planning horizon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import (
    ForecastScenario,
    GachaIncome,
    GachaPlan,
    GachaResources,
    GachaStatus,
    GachaTarget,
    IncomeScenario,
    PlanningWarning,
    PullType,
    ScenarioPlan,
    TargetAllocation,
)

ASTRITE_PER_PULL = 160
_ACTIVE_TARGET_STATUSES = frozenset({"planned", "active"})
_INCLUDED_INCOME: dict[ForecastScenario, frozenset[IncomeScenario]] = {
    ForecastScenario.CONSERVATIVE: frozenset({IncomeScenario.GENERAL, IncomeScenario.CONSERVATIVE}),
    ForecastScenario.TYPICAL: frozenset(
        {IncomeScenario.GENERAL, IncomeScenario.CONSERVATIVE, IncomeScenario.TYPICAL}
    ),
    ForecastScenario.OPTIMISTIC: frozenset(IncomeScenario),
}


@dataclass(slots=True)
class _ResourceBucket:
    source_id: str
    available_on: date | None
    astrite: int = 0
    character_pulls: int = 0
    weapon_pulls: int = 0


def plan_gacha_resources(
    *,
    status: GachaStatus,
    targets: tuple[GachaTarget, ...],
    incomes: tuple[GachaIncome, ...],
    as_of: date,
) -> GachaPlan:
    """Allocate dedicated tickets and shared astrite without double counting.

    Pity and guarantee fields deliberately do not participate: only explicit
    per-target manual pull requirements are accepted by this V1 planner.
    """

    if type(as_of) is not date:
        raise ValueError("as_of must be a date")

    active_targets = tuple(
        sorted(
            (target for target in targets if target.status.casefold() in _ACTIVE_TARGET_STATUSES),
            key=lambda target: (
                target.priority,
                target.target_date is None,
                target.target_date or date.max,
                target.target_id,
            ),
        )
    )
    warnings: list[PlanningWarning] = []
    scenario_plans: list[ScenarioPlan] = []
    for scenario in ForecastScenario:
        scenario_plan, scenario_warnings = _plan_scenario(
            scenario=scenario,
            status=status,
            targets=active_targets,
            incomes=incomes,
            as_of=as_of,
        )
        scenario_plans.append(scenario_plan)
        warnings.extend(scenario_warnings)
    return GachaPlan(scenarios=tuple(scenario_plans), warnings=tuple(warnings))


def _plan_scenario(
    *,
    scenario: ForecastScenario,
    status: GachaStatus,
    targets: tuple[GachaTarget, ...],
    incomes: tuple[GachaIncome, ...],
    as_of: date,
) -> tuple[ScenarioPlan, list[PlanningWarning]]:
    warnings: list[PlanningWarning] = []
    max_horizon = max(
        (target.target_date for target in targets if target.target_date is not None),
        default=None,
    )
    buckets = [
        _ResourceBucket(
            source_id="current-status",
            available_on=None,
            astrite=status.resources.astrite,
            character_pulls=status.resources.character_pulls,
            weapon_pulls=status.resources.weapon_pulls,
        )
    ]
    included_income_ids: list[str] = []
    for income in sorted(incomes, key=lambda item: item.income_id):
        bucket = _income_bucket(
            income,
            scenario=scenario,
            max_horizon=max_horizon,
            as_of=as_of,
            warnings=warnings,
        )
        if bucket is not None:
            buckets.append(bucket)
            included_income_ids.append(income.income_id)

    allocations: list[TargetAllocation] = []
    for target in targets:
        if target.target_date is not None and target.target_date < as_of:
            warnings.append(
                PlanningWarning(
                    code="target_date_past",
                    message=f"{target.name} 的目標日期已過; 只配置目前仍持有的資源。",
                    scenario=scenario,
                    target_id=target.target_id,
                )
            )
        required = target.requirements.for_scenario(scenario)
        if required is None:
            warnings.append(
                PlanningWarning(
                    code="manual_requirement_missing",
                    message=(
                        f"{target.name} 缺少 {scenario.value} 情境的人工需求抽數; "
                        "未使用保底或機率猜測。"
                    ),
                    scenario=scenario,
                    target_id=target.target_id,
                )
            )
            allocations.append(
                TargetAllocation(
                    target_id=target.target_id,
                    target_name=target.name,
                    target_type=target.target_type,
                    target_date=target.target_date,
                    priority=target.priority,
                    required_pulls=None,
                    funded_pulls=None,
                    missing_pulls=None,
                    dedicated_pulls_used=None,
                    astrite_used=None,
                    is_fully_funded=None,
                )
            )
            continue
        if target.target_date is None:
            warnings.append(
                PlanningWarning(
                    code="target_date_missing",
                    message=f"{target.name} 未設定目標日期; 只配置目前持有資源。",
                    scenario=scenario,
                    target_id=target.target_id,
                )
            )
        else:
            for income in incomes:
                if (
                    not income.is_received
                    and income.expected_date is not None
                    and income.expected_date > target.target_date
                    and _income_applies(income, scenario)
                ):
                    warnings.append(
                        PlanningWarning(
                            code="income_after_target_date",
                            message=(
                                f"{income.label} 在 {target.name} 的目標日期後, 未配置給此目標。"
                            ),
                            scenario=scenario,
                            target_id=target.target_id,
                            income_id=income.income_id,
                        )
                    )

        dedicated_used = _consume_dedicated(
            buckets,
            pull_type=target.target_type,
            amount=required,
            horizon=target.target_date,
        )
        remaining = required - dedicated_used
        astrite_available = _eligible_astrite(buckets, target.target_date)
        astrite_pulls = min(remaining, astrite_available // ASTRITE_PER_PULL)
        astrite_used = astrite_pulls * ASTRITE_PER_PULL
        _consume_astrite(buckets, astrite_used, horizon=target.target_date)
        funded = dedicated_used + astrite_pulls
        allocations.append(
            TargetAllocation(
                target_id=target.target_id,
                target_name=target.name,
                target_type=target.target_type,
                target_date=target.target_date,
                priority=target.priority,
                required_pulls=required,
                funded_pulls=funded,
                missing_pulls=required - funded,
                dedicated_pulls_used=dedicated_used,
                astrite_used=astrite_used,
                is_fully_funded=funded >= required,
            )
        )

    return (
        ScenarioPlan(
            scenario=scenario,
            allocations=tuple(allocations),
            remaining_resources=GachaResources(
                astrite=sum(bucket.astrite for bucket in buckets),
                character_pulls=sum(bucket.character_pulls for bucket in buckets),
                weapon_pulls=sum(bucket.weapon_pulls for bucket in buckets),
            ),
            included_income_ids=tuple(included_income_ids),
        ),
        warnings,
    )


def _income_bucket(
    income: GachaIncome,
    *,
    scenario: ForecastScenario,
    max_horizon: date | None,
    as_of: date,
    warnings: list[PlanningWarning],
) -> _ResourceBucket | None:
    if income.is_received:
        warnings.append(
            PlanningWarning(
                code="income_already_received",
                message=f"{income.label} 已領取, 假設已反映在目前資源, 不重複計入。",
                scenario=scenario,
                income_id=income.income_id,
            )
        )
        return None
    if income.expected_date is None:
        warnings.append(
            PlanningWarning(
                code="income_date_missing",
                message=f"{income.label} 未設定日期, 未納入 {scenario.value} 情境。",
                scenario=scenario,
                income_id=income.income_id,
            )
        )
        return None
    if income.expected_date < as_of:
        warnings.append(
            PlanningWarning(
                code="income_date_past",
                message=(
                    f"{income.label} 的預計日期已過但尚未標記領取; 為避免重複計算, 未納入規劃。"
                ),
                scenario=scenario,
                income_id=income.income_id,
            )
        )
        return None
    if income.scenario is None:
        warnings.append(
            PlanningWarning(
                code="income_scenario_unknown",
                message=f"{income.label} 的收入情境未知, 未納入規劃。",
                scenario=scenario,
                income_id=income.income_id,
            )
        )
        return None
    if income.scenario not in _INCLUDED_INCOME[scenario]:
        return None
    if max_horizon is None or income.expected_date > max_horizon:
        warnings.append(
            PlanningWarning(
                code="income_after_planning_horizon",
                message=f"{income.label} 在目前最晚目標日期後, 未納入規劃。",
                scenario=scenario,
                income_id=income.income_id,
            )
        )
        return None

    character_pulls = 0
    weapon_pulls = 0
    if income.pull_amount:
        if income.pull_type is PullType.CHARACTER:
            character_pulls = income.pull_amount
        elif income.pull_type is PullType.WEAPON:
            weapon_pulls = income.pull_amount
        else:
            warnings.append(
                PlanningWarning(
                    code="income_pull_type_unknown",
                    message=(
                        f"{income.label} 的 {income.pull_amount} 張券種未知; "
                        "只納入其中可確認的星聲。"
                    ),
                    scenario=scenario,
                    income_id=income.income_id,
                )
            )
    if income.astrite_amount == 0 and character_pulls == 0 and weapon_pulls == 0:
        return None
    return _ResourceBucket(
        source_id=income.income_id,
        available_on=income.expected_date,
        astrite=income.astrite_amount,
        character_pulls=character_pulls,
        weapon_pulls=weapon_pulls,
    )


def _income_applies(income: GachaIncome, scenario: ForecastScenario) -> bool:
    return income.scenario is not None and income.scenario in _INCLUDED_INCOME[scenario]


def _eligible_buckets(
    buckets: list[_ResourceBucket], horizon: date | None
) -> list[_ResourceBucket]:
    eligible = [
        bucket
        for bucket in buckets
        if bucket.available_on is None or (horizon is not None and bucket.available_on <= horizon)
    ]
    # Consume the latest eligible income first.  This preserves earlier funds
    # for a lower-priority target with an earlier deadline while still giving
    # the higher-priority target first claim on all resources by its horizon.
    return sorted(
        eligible,
        key=lambda bucket: (bucket.available_on or date.min, bucket.source_id),
        reverse=True,
    )


def _consume_dedicated(
    buckets: list[_ResourceBucket],
    *,
    pull_type: PullType,
    amount: int,
    horizon: date | None,
) -> int:
    attribute = "character_pulls" if pull_type is PullType.CHARACTER else "weapon_pulls"
    remaining = amount
    for bucket in _eligible_buckets(buckets, horizon):
        available = getattr(bucket, attribute)
        used = min(available, remaining)
        setattr(bucket, attribute, available - used)
        remaining -= used
        if remaining == 0:
            break
    return amount - remaining


def _eligible_astrite(buckets: list[_ResourceBucket], horizon: date | None) -> int:
    return sum(bucket.astrite for bucket in _eligible_buckets(buckets, horizon))


def _consume_astrite(buckets: list[_ResourceBucket], amount: int, *, horizon: date | None) -> None:
    remaining = amount
    for bucket in _eligible_buckets(buckets, horizon):
        used = min(bucket.astrite, remaining)
        bucket.astrite -= used
        remaining -= used
        if remaining == 0:
            return
    if remaining:
        raise RuntimeError("gacha planner attempted to consume unavailable astrite")
