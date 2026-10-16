"""Material gaps and deterministic daily waveplate planning."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal

from .models import (
    DailyActionStatus,
    DailyPlanAction,
    DailyPlanNotice,
    DailyPlanRequest,
    DailyPlanResult,
    InventoryCount,
    MaterialDefinition,
    MaterialGap,
    MaterialPlanningOverride,
    MaterialRequirement,
    PlanNoticeCode,
    ResourcePlanningTarget,
    TargetCompletionEstimate,
    TargetMaterialGap,
    WeeklyShortage,
)

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class _EffectiveMaterial:
    definition: MaterialDefinition
    stamina_cost_per_run: int | None
    average_yield: Decimal | None
    open_weekdays: tuple[int, ...] | None
    weekly_limit: int | None
    override: MaterialPlanningOverride | None


@dataclass(slots=True)
class _Simulation:
    gaps: dict[tuple[str, str], Decimal]
    completion_dates: dict[str, date]


def calculate_material_gaps(
    requirements: tuple[MaterialRequirement, ...],
    inventory: tuple[InventoryCount, ...],
) -> tuple[MaterialGap, ...]:
    """Return aggregate gaps from a *complete* inventory snapshot.

    This low-level calculator intentionally treats an absent material entry as
    known zero. Interactive planning must use :func:`build_daily_plan`, which
    distinguishes a missing ``InventoryCount`` from an explicit zero count.
    """

    inventory_map = _inventory_map(inventory)
    required: dict[str, Decimal] = {}
    for item in requirements:
        required[item.material_id] = required.get(item.material_id, _ZERO) + item.required_quantity
    return tuple(
        MaterialGap(
            material_id=material_id,
            required_quantity=quantity,
            inventory_quantity=inventory_map.get(material_id, _ZERO),
            gap_quantity=max(_ZERO, quantity - inventory_map.get(material_id, _ZERO)),
        )
        for material_id, quantity in sorted(required.items(), key=lambda item: item[0].casefold())
    )


def calculate_target_material_gaps(
    requirements: tuple[MaterialRequirement, ...],
    inventory: tuple[InventoryCount, ...],
    *,
    target_order: tuple[str, ...] = (),
) -> tuple[TargetMaterialGap, ...]:
    """Allocate a complete inventory snapshot in explicit breadth-first order.

    As with :func:`calculate_material_gaps`, absent entries are assumed to be
    known zero. ``build_daily_plan`` adds the user-confirmation safety gate.
    """

    order_index = {target_id: index for index, target_id in enumerate(target_order)}
    if len(order_index) != len(target_order):
        raise ValueError("target order must not contain duplicate ids")
    inventory_remaining = _inventory_map(inventory)
    required_by_pair: dict[tuple[str, str], Decimal] = {}
    for item in requirements:
        pair = (item.target_id, item.material_id)
        required_by_pair[pair] = required_by_pair.get(pair, _ZERO) + item.required_quantity
    ordered = sorted(
        required_by_pair.items(),
        key=lambda item: (
            order_index.get(item[0][0], len(order_index)),
            item[0][0].casefold(),
            item[0][1].casefold(),
        ),
    )
    results: list[TargetMaterialGap] = []
    for (target_id, material_id), required in ordered:
        available = inventory_remaining.get(material_id, _ZERO)
        applied = min(required, available)
        inventory_remaining[material_id] = available - applied
        results.append(
            TargetMaterialGap(
                target_id=target_id,
                material_id=material_id,
                required_quantity=required,
                inventory_applied=applied,
                gap_quantity=required - applied,
            )
        )
    return tuple(results)


def build_daily_plan(request: DailyPlanRequest) -> DailyPlanResult:
    """Build today's primary/secondary resource actions and conservative estimates.

    No fallback yield, stamina cost or opening schedule is invented. Missing
    metadata produces an explicit confirmation action and a null completion date.
    """

    _targets, materials, effective, weekly_used = _validate_and_index(request)
    active_targets = tuple(
        sorted(
            (target for target in request.targets if not target.is_completed),
            key=_target_sort_key,
        )
    )
    active_target_ids = {target.target_id for target in active_targets}
    active_requirements = tuple(
        item for item in request.requirements if item.target_id in active_target_ids
    )
    target_order = tuple(item.target_id for item in active_targets)
    known_inventory = {item.material_id for item in request.inventory}
    target_gaps = tuple(
        replace(
            item,
            inventory_is_known=item.material_id in known_inventory,
        )
        for item in calculate_target_material_gaps(
            active_requirements,
            request.inventory,
            target_order=target_order,
        )
    )
    material_gaps = tuple(
        replace(
            item,
            inventory_is_known=item.material_id in known_inventory,
        )
        for item in calculate_material_gaps(active_requirements, request.inventory)
    )
    estimates = _estimate_targets(
        request,
        active_targets,
        target_gaps,
        effective,
        weekly_used,
        known_inventory,
    )

    remaining_waveplates = request.daily_waveplates
    remaining_weekly_runs = {
        material_id: _remaining_weekly_runs(item, weekly_used.get(material_id, 0))
        for material_id, item in effective.items()
    }
    notices: list[DailyPlanNotice] = []
    notice_keys: set[tuple[PlanNoticeCode, str, str]] = set()
    actions: list[DailyPlanAction] = []
    mutable_gaps = {(item.target_id, item.material_id): item.gap_quantity for item in target_gaps}
    runs_used_today: dict[str, int] = {}

    for target in active_targets:
        for target_gap in _gaps_for_target(
            target.target_id,
            target_gaps,
            effective,
            request.plan_date,
            weekly_used,
        ):
            if len(actions) >= request.max_actions:
                break
            pair = (target.target_id, target_gap.material_id)
            gap = mutable_gaps[pair]
            if gap <= 0:
                continue
            material = materials[target_gap.material_id]
            item = effective[target_gap.material_id]
            metadata_codes = _unknown_metadata(
                item,
                allow_today_override=True,
                inventory_is_known=target_gap.material_id in known_inventory,
            )
            if metadata_codes:
                for code in metadata_codes:
                    _add_notice(
                        notices,
                        notice_keys,
                        code,
                        target,
                        material.material_id,
                        _notice_message(code, material.name),
                    )
                actions.append(
                    _manual_action(
                        len(actions) + 1,
                        target,
                        material,
                        gap,
                        estimates[target.target_id],
                        metadata_codes,
                    )
                )
                continue
            if item.override is not None:
                _add_notice(
                    notices,
                    notice_keys,
                    PlanNoticeCode.MANUAL_OVERRIDE,
                    target,
                    material.material_id,
                    f"{material.name} 使用本機手動覆寫值規劃。",
                )
            is_open = _is_open(item, request.plan_date, is_first_day=True)
            if not is_open:
                _add_notice(
                    notices,
                    notice_keys,
                    PlanNoticeCode.CLOSED_TODAY,
                    target,
                    material.material_id,
                    f"{material.name} 今日未開放, 已改排其他可執行項目。",
                )
                continue
            weekly_cap = remaining_weekly_runs[target_gap.material_id]
            if weekly_cap is not None and weekly_cap <= 0:
                _add_notice(
                    notices,
                    notice_keys,
                    PlanNoticeCode.WEEKLY_LIMIT_REACHED,
                    target,
                    material.material_id,
                    f"{material.name} 本週次數已用完。",
                )
                continue
            cost = item.stamina_cost_per_run
            average_yield = item.average_yield
            assert cost is not None and cost > 0
            assert average_yield is not None
            affordable_runs = remaining_waveplates // cost
            if affordable_runs <= 0:
                _add_notice(
                    notices,
                    notice_keys,
                    PlanNoticeCode.INSUFFICIENT_WAVEPLATES,
                    target,
                    material.material_id,
                    f"剩餘結晶波片不足以刷取一次 {material.name}。",
                )
                continue
            runs = min(_runs_for_gap(gap, average_yield), affordable_runs)
            if weekly_cap is not None:
                runs = min(runs, weekly_cap)
            if item.override is not None and item.override.max_runs_today is not None:
                remaining_daily_cap = max(
                    0,
                    item.override.max_runs_today - runs_used_today.get(target_gap.material_id, 0),
                )
                runs = min(runs, remaining_daily_cap)
            if runs <= 0:
                continue
            stamina_cost = runs * cost
            expected_quantity = average_yield * runs
            remaining_waveplates -= stamina_cost
            _allocate_material_yield(
                mutable_gaps,
                target_gap.material_id,
                expected_quantity,
                target_order,
            )
            gap_after = mutable_gaps[pair]
            runs_used_today[target_gap.material_id] = (
                runs_used_today.get(target_gap.material_id, 0) + runs
            )
            if weekly_cap is not None:
                remaining_weekly_runs[target_gap.material_id] = weekly_cap - runs
            actions.append(
                DailyPlanAction(
                    rank=len(actions) + 1,
                    target_id=target.target_id,
                    target_label=target.label,
                    character_id=target.character_id,
                    cultivation_target_id=target.cultivation_target_id,
                    material_id=material.material_id,
                    material_name=material.name,
                    status=DailyActionStatus.PLANNED,
                    requires_manual_confirmation=False,
                    recommended_runs=runs,
                    stamina_cost=stamina_cost,
                    expected_quantity=expected_quantity,
                    gap_before=gap,
                    gap_after=gap_after,
                    reason=(
                        f"{target.reason}; {material.name} 尚缺 {_quantity_text(gap)}, "
                        f"今日建議刷取 {runs} 次。"
                    ),
                    estimated_completion_date=estimates[target.target_id].estimated_date,
                )
            )
        if len(actions) >= request.max_actions:
            break

    week_end = request.plan_date + timedelta(days=7 - request.plan_date.isoweekday())
    weekly_simulation = _simulate(
        request,
        active_targets,
        target_gaps,
        effective,
        weekly_used,
        known_inventory,
        end_date=week_end,
    )
    weekly_shortages = _weekly_shortages(
        weekly_simulation,
        active_targets,
        materials,
        effective,
        known_inventory,
    )
    return DailyPlanResult(
        plan_date=request.plan_date,
        actions=tuple(actions),
        material_gaps=material_gaps,
        target_gaps=target_gaps,
        weekly_shortages=weekly_shortages,
        target_estimates=tuple(estimates[target.target_id] for target in active_targets),
        notices=tuple(notices),
        remaining_waveplates=remaining_waveplates,
        assumptions=(
            "只使用本機輸入的成本、平均產出、開放日與週限次; 未知值不套用猜測。",
            (
                f"完成日估計假設未來每日可使用 {request.daily_waveplates} 結晶波片, "
                "並每日重新依目前順位安排。"
            ),
            "平均產出是規劃值, 不代表單次掉落保證; 手動覆寫會在結果中標示。",
        ),
    )


def _validate_and_index(
    request: DailyPlanRequest,
) -> tuple[
    dict[str, ResourcePlanningTarget],
    dict[str, MaterialDefinition],
    dict[str, _EffectiveMaterial],
    dict[str, int],
]:
    targets = _unique_index(request.targets, lambda item: item.target_id, "planning target")
    materials = _unique_index(request.materials, lambda item: item.material_id, "material")
    inventory = _unique_index(request.inventory, lambda item: item.material_id, "inventory")
    weekly_usage = _unique_index(
        request.weekly_usage,
        lambda item: item.material_id,
        "weekly usage",
    )
    overrides = _unique_index(request.overrides, lambda item: item.material_id, "override")
    for item in request.requirements:
        if item.target_id not in targets:
            raise ValueError(f"requirement references unknown target: {item.target_id}")
        if item.material_id not in materials:
            raise ValueError(f"requirement references unknown material: {item.material_id}")
    for material_id in (*inventory, *weekly_usage, *overrides):
        if material_id not in materials:
            raise ValueError(f"resource input references unknown material: {material_id}")
    effective = {
        material_id: _effective_material(material, overrides.get(material_id))
        for material_id, material in materials.items()
    }
    return (
        targets,
        materials,
        effective,
        {key: value.completed_runs for key, value in weekly_usage.items()},
    )


def _unique_index[T](items: tuple[T, ...], key: Callable[[T], str], label: str) -> dict[str, T]:
    results: dict[str, T] = {}
    normalized: set[str] = set()
    for item in items:
        value = key(item)
        folded = value.casefold()
        if folded in normalized:
            raise ValueError(f"{label} ids must be unique")
        normalized.add(folded)
        results[value] = item
    return results


def _inventory_map(inventory: tuple[InventoryCount, ...]) -> dict[str, Decimal]:
    indexed = _unique_index(inventory, lambda item: item.material_id, "inventory")
    return {key: value.quantity for key, value in indexed.items()}


def _effective_material(
    material: MaterialDefinition,
    override: MaterialPlanningOverride | None,
) -> _EffectiveMaterial:
    if override is None:
        return _EffectiveMaterial(
            material,
            material.stamina_cost_per_run,
            material.average_yield,
            material.open_weekdays,
            material.weekly_limit,
            None,
        )
    return _EffectiveMaterial(
        material,
        override.stamina_cost_per_run
        if override.stamina_cost_per_run is not None
        else material.stamina_cost_per_run,
        override.average_yield if override.average_yield is not None else material.average_yield,
        override.open_weekdays if override.open_weekdays is not None else material.open_weekdays,
        override.weekly_limit if override.weekly_limit is not None else material.weekly_limit,
        override,
    )


def _target_sort_key(target: ResourcePlanningTarget) -> tuple[object, ...]:
    return (
        target.queue_rank,
        target.due_date is None,
        target.due_date or date.max,
        target.label.casefold(),
        target.target_id.casefold(),
    )


def _gaps_for_target(
    target_id: str,
    gaps: tuple[TargetMaterialGap, ...],
    effective: dict[str, _EffectiveMaterial],
    day: date,
    weekly_used: dict[str, int],
) -> tuple[TargetMaterialGap, ...]:
    return tuple(
        sorted(
            (item for item in gaps if item.target_id == target_id),
            key=lambda item: _material_urgency_key(
                item.material_id,
                effective,
                day,
                weekly_used,
            ),
        )
    )


def _material_urgency_key(
    material_id: str,
    effective: dict[str, _EffectiveMaterial],
    day: date,
    weekly_used: dict[str, int],
) -> tuple[object, ...]:
    """Put scarce schedules before flexible daily resources.

    The same key is used by today's plan and future simulation. A later next
    opening is more urgent; weekly-limited materials win otherwise equal ties.
    """

    item = effective[material_id]
    weekdays = item.open_weekdays
    is_daily = weekdays is not None and set(weekdays) == set(range(1, 8))
    days_until_next_open = _days_until_next_open(day, weekdays)
    remaining_weekly = _remaining_weekly_runs(item, weekly_used.get(material_id, 0))
    is_weekly_limited = remaining_weekly is not None
    return (
        is_daily,
        -days_until_next_open,
        not is_weekly_limited,
        remaining_weekly if remaining_weekly is not None else 2**31,
        item.definition.name.casefold(),
        material_id.casefold(),
    )


def _days_until_next_open(day: date, weekdays: tuple[int, ...] | None) -> int:
    if weekdays is None:
        # Unknown schedules are surfaced for confirmation before any farming.
        return 8
    available = set(weekdays)
    for offset in range(1, 8):
        if ((day.isoweekday() - 1 + offset) % 7) + 1 in available:
            return offset
    raise AssertionError("validated schedules always contain at least one weekday")


def _unknown_metadata(
    item: _EffectiveMaterial,
    *,
    allow_today_override: bool = False,
    inventory_is_known: bool = True,
) -> tuple[PlanNoticeCode, ...]:
    codes: list[PlanNoticeCode] = []
    if not inventory_is_known:
        codes.append(PlanNoticeCode.UNKNOWN_INVENTORY)
    today_is_explicit = (
        allow_today_override and item.override is not None and item.override.open_today is not None
    )
    if item.open_weekdays is None and not today_is_explicit:
        codes.append(PlanNoticeCode.UNKNOWN_SCHEDULE)
    if item.stamina_cost_per_run is None or item.stamina_cost_per_run <= 0:
        codes.append(PlanNoticeCode.UNKNOWN_COST)
    if item.average_yield is None:
        codes.append(PlanNoticeCode.UNKNOWN_YIELD)
    return tuple(codes)


def _is_open(item: _EffectiveMaterial, day: date, *, is_first_day: bool) -> bool | None:
    if is_first_day and item.override is not None and item.override.open_today is not None:
        return item.override.open_today
    if item.open_weekdays is None:
        return None
    return day.isoweekday() in item.open_weekdays


def _remaining_weekly_runs(item: _EffectiveMaterial, used: int) -> int | None:
    if item.weekly_limit is None:
        return None
    return max(0, item.weekly_limit - used)


def _allocate_material_yield(
    gaps: dict[tuple[str, str], Decimal],
    material_id: str,
    quantity: Decimal,
    target_order: tuple[str, ...],
) -> Decimal:
    """Apply one material drop across targets without losing expected surplus."""

    remaining = quantity
    for target_id in target_order:
        pair = (target_id, material_id)
        gap = gaps.get(pair, _ZERO)
        if gap <= 0:
            continue
        applied = min(gap, remaining)
        gaps[pair] = gap - applied
        remaining -= applied
        if remaining <= 0:
            break
    return remaining


def _runs_for_gap(gap: Decimal, average_yield: Decimal) -> int:
    return int((gap / average_yield).to_integral_value(rounding=ROUND_CEILING))


def _manual_action(
    rank: int,
    target: ResourcePlanningTarget,
    material: MaterialDefinition,
    gap: Decimal,
    estimate: TargetCompletionEstimate,
    unknown_inputs: tuple[PlanNoticeCode, ...],
) -> DailyPlanAction:
    inventory_is_unknown = PlanNoticeCode.UNKNOWN_INVENTORY in unknown_inputs
    reason = (
        f"{target.reason}; {material.name} 庫存未登錄, 無法計算素材缺口或建議刷取次數。"
        if inventory_is_unknown
        else (
            f"{target.reason}; {material.name} 尚缺 {_quantity_text(gap)}, "
            "但成本、平均產出或開放日資料不足, 需要人工確認。"
        )
    )
    return DailyPlanAction(
        rank=rank,
        target_id=target.target_id,
        target_label=target.label,
        character_id=target.character_id,
        cultivation_target_id=target.cultivation_target_id,
        material_id=material.material_id,
        material_name=material.name,
        status=DailyActionStatus.NEEDS_CONFIRMATION,
        requires_manual_confirmation=True,
        recommended_runs=None,
        stamina_cost=None,
        expected_quantity=None,
        gap_before=None if inventory_is_unknown else gap,
        gap_after=None,
        reason=reason,
        estimated_completion_date=estimate.estimated_date,
    )


def _estimate_targets(
    request: DailyPlanRequest,
    targets: tuple[ResourcePlanningTarget, ...],
    gaps: tuple[TargetMaterialGap, ...],
    effective: dict[str, _EffectiveMaterial],
    weekly_used: dict[str, int],
    known_inventory: set[str],
) -> dict[str, TargetCompletionEstimate]:
    end_date = request.plan_date + timedelta(days=request.estimation_horizon_days - 1)
    simulation = _simulate(
        request,
        targets,
        gaps,
        effective,
        weekly_used,
        known_inventory,
        end_date=end_date,
    )
    results: dict[str, TargetCompletionEstimate] = {}
    for target in targets:
        relevant = [
            item for item in gaps if item.target_id == target.target_id and item.gap_quantity > 0
        ]
        completion_date = simulation.completion_dates.get(target.target_id)
        requires_confirmation = any(
            bool(
                _unknown_metadata(
                    effective[item.material_id],
                    allow_today_override=completion_date == request.plan_date,
                    inventory_is_known=item.material_id in known_inventory,
                )
            )
            for item in relevant
        )
        results[target.target_id] = TargetCompletionEstimate(
            target_id=target.target_id,
            estimated_date=completion_date,
            requires_manual_confirmation=requires_confirmation,
        )
    return results


def _simulate(
    request: DailyPlanRequest,
    targets: tuple[ResourcePlanningTarget, ...],
    gaps: tuple[TargetMaterialGap, ...],
    effective: dict[str, _EffectiveMaterial],
    weekly_used: dict[str, int],
    known_inventory: set[str],
    *,
    end_date: date,
) -> _Simulation:
    mutable_gaps = {(item.target_id, item.material_id): item.gap_quantity for item in gaps}
    completed = {
        target.target_id: request.plan_date
        for target in targets
        if not any(
            quantity > 0
            for (target_id, _material_id), quantity in mutable_gaps.items()
            if target_id == target.target_id
        )
    }
    used = dict(weekly_used)
    target_order = tuple(target.target_id for target in targets)
    day = request.plan_date
    while day <= end_date and len(completed) < len(targets):
        if day > request.plan_date and day.isoweekday() == 1:
            used = {}
        waveplates = request.daily_waveplates
        actions_used = 0
        runs_used_today: dict[str, int] = {}
        for target in targets:
            if target.target_id in completed:
                continue
            for pair in sorted(
                (key for key in mutable_gaps if key[0] == target.target_id),
                key=lambda key: _material_urgency_key(key[1], effective, day, used),
            ):
                if actions_used >= request.max_actions:
                    break
                gap = mutable_gaps[pair]
                if gap <= 0:
                    continue
                item = effective[pair[1]]
                if _unknown_metadata(
                    item,
                    allow_today_override=day == request.plan_date,
                    inventory_is_known=pair[1] in known_inventory,
                ):
                    continue
                if not _is_open(item, day, is_first_day=day == request.plan_date):
                    continue
                cost = item.stamina_cost_per_run
                average_yield = item.average_yield
                assert cost is not None and cost > 0 and average_yield is not None
                runs = min(_runs_for_gap(gap, average_yield), waveplates // cost)
                weekly_remaining = _remaining_weekly_runs(item, used.get(pair[1], 0))
                if weekly_remaining is not None:
                    runs = min(runs, weekly_remaining)
                if (
                    day == request.plan_date
                    and item.override is not None
                    and item.override.max_runs_today is not None
                ):
                    remaining_daily_cap = max(
                        0,
                        item.override.max_runs_today - runs_used_today.get(pair[1], 0),
                    )
                    runs = min(runs, remaining_daily_cap)
                if runs <= 0:
                    continue
                _allocate_material_yield(
                    mutable_gaps,
                    pair[1],
                    average_yield * runs,
                    target_order,
                )
                waveplates -= cost * runs
                used[pair[1]] = used.get(pair[1], 0) + runs
                runs_used_today[pair[1]] = runs_used_today.get(pair[1], 0) + runs
                actions_used += 1
            if not any(
                quantity > 0
                for (target_id, _material_id), quantity in mutable_gaps.items()
                if target_id == target.target_id
            ):
                completed[target.target_id] = day
            if waveplates <= 0 or actions_used >= request.max_actions:
                break
        # A single run can satisfy more than one target that needs the same
        # material.  Mark every target whose gaps were covered by that shared
        # yield before advancing the simulated date, including targets the
        # action/waveplate limit prevented us from visiting in this loop.
        for candidate in targets:
            if candidate.target_id in completed:
                continue
            if not any(
                quantity > 0
                for (target_id, _material_id), quantity in mutable_gaps.items()
                if target_id == candidate.target_id
            ):
                completed[candidate.target_id] = day
        day += timedelta(days=1)
    return _Simulation(mutable_gaps, completed)


def _weekly_shortages(
    simulation: _Simulation,
    targets: tuple[ResourcePlanningTarget, ...],
    materials: dict[str, MaterialDefinition],
    effective: dict[str, _EffectiveMaterial],
    known_inventory: set[str],
) -> tuple[WeeklyShortage, ...]:
    active_target_ids = {target.target_id for target in targets}
    remaining_by_material: dict[str, Decimal] = {}
    for (target_id, material_id), quantity in simulation.gaps.items():
        if target_id in active_target_ids and quantity > 0:
            remaining_by_material[material_id] = (
                remaining_by_material.get(material_id, _ZERO) + quantity
            )
    results: list[WeeklyShortage] = []
    for material_id, quantity in sorted(
        remaining_by_material.items(), key=lambda item: item[0].casefold()
    ):
        item = effective[material_id]
        inventory_is_known = material_id in known_inventory
        manual = bool(
            _unknown_metadata(
                item,
                inventory_is_known=inventory_is_known,
            )
        )
        results.append(
            WeeklyShortage(
                material_id=material_id,
                material_name=materials[material_id].name,
                remaining_quantity=quantity if inventory_is_known else None,
                requires_manual_confirmation=manual,
                reason=(
                    (
                        "庫存未登錄, 無法計算本週素材缺口。"
                        if not inventory_is_known
                        else "成本、平均產出或開放日資料不足, 無法確認本週可完成數量。"
                    )
                    if manual
                    else "依目前開放日、週限次與每日波片預算, 本週仍無法補齊。"
                ),
            )
        )
    return tuple(results)


def _add_notice(
    notices: list[DailyPlanNotice],
    keys: set[tuple[PlanNoticeCode, str, str]],
    code: PlanNoticeCode,
    target: ResourcePlanningTarget,
    material_id: str,
    message: str,
) -> None:
    key = (code, target.target_id, material_id)
    if key in keys:
        return
    keys.add(key)
    notices.append(DailyPlanNotice(code, target.target_id, material_id, message))


def _notice_message(code: PlanNoticeCode, material_name: str) -> str:
    messages = {
        PlanNoticeCode.UNKNOWN_INVENTORY: f"{material_name} 尚未登錄庫存數量。",
        PlanNoticeCode.UNKNOWN_SCHEDULE: f"{material_name} 的開放日未知。",
        PlanNoticeCode.UNKNOWN_COST: f"{material_name} 的單次結晶波片成本未知或不可用。",
        PlanNoticeCode.UNKNOWN_YIELD: f"{material_name} 的平均產出未知。",
    }
    return messages[code]


def _quantity_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


__all__ = [
    "build_daily_plan",
    "calculate_material_gaps",
    "calculate_target_material_gaps",
]
