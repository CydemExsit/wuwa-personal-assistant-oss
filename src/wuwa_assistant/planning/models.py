"""Pure domain models for account-wide cultivation and resource planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import IntEnum, StrEnum
from math import isfinite


class ProgressionPhase(IntEnum):
    """Breadth-first phases; lower values are always considered first."""

    INVALID_ECHO = 10
    MISSING_ECHO = 20
    FUNCTIONAL_THRESHOLD = 30
    WEAKEST_ECHO = 40
    SKILL_OR_WEAPON = 50
    CONFIRMATION = 60


class ProgressionIssue(StrEnum):
    ECHO_VALIDITY_UNKNOWN = "echo_validity_unknown"
    ECHO_SET_OR_MAIN_INVALID = "echo_set_or_main_invalid"
    ECHO_SLOT_MISSING = "echo_slot_missing"
    FUNCTIONAL_THRESHOLD_UNKNOWN = "functional_threshold_unknown"
    FUNCTIONAL_THRESHOLD_UNMET = "functional_threshold_unmet"
    ECHO_SCORE_UNKNOWN = "echo_score_unknown"
    WEAKEST_ECHO = "weakest_echo"
    WEAPON_INCOMPLETE = "weapon_incomplete"
    SKILL_INCOMPLETE = "skill_incomplete"
    CULTIVATION_TASK_INCOMPLETE = "cultivation_task_incomplete"


class ThresholdDirection(StrEnum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


@dataclass(frozen=True, slots=True)
class FunctionalThreshold:
    key: str
    label: str
    current: float | None
    target: float
    unit: str | None = None
    direction: ThresholdDirection = ThresholdDirection.AT_LEAST
    cultivation_target_id: str | None = None
    material_id: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.label.strip():
            raise ValueError("threshold key and label must not be blank")
        if not isfinite(self.target):
            raise ValueError("threshold target must be finite")
        if self.current is not None and not isfinite(self.current):
            raise ValueError("threshold current value must be finite")
        if not isinstance(self.direction, ThresholdDirection):
            raise TypeError("threshold direction must be ThresholdDirection")
        _validate_optional_id(self.cultivation_target_id, "cultivation target id")
        _validate_optional_id(self.material_id, "material id")

    @property
    def at_goal(self) -> bool | None:
        if self.current is None:
            return None
        if self.direction is ThresholdDirection.AT_LEAST:
            return self.current >= self.target
        return self.current <= self.target


@dataclass(frozen=True, slots=True)
class CultivationTaskProgress:
    key: str
    label: str
    current: int | None
    target: int
    cultivation_target_id: str | None = None
    material_id: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.label.strip():
            raise ValueError("task key and label must not be blank")
        if self.current is not None and self.current < 0:
            raise ValueError("task current value must be non-negative")
        if self.target <= 0:
            raise ValueError("task target must be positive")
        _validate_optional_id(self.cultivation_target_id, "cultivation target id")
        _validate_optional_id(self.material_id, "material id")

    @property
    def at_goal(self) -> bool | None:
        return None if self.current is None else self.current >= self.target


@dataclass(frozen=True, slots=True)
class PlanningEchoSlot:
    slot_index: int
    is_filled: bool
    set_is_valid: bool | None = None
    main_stat_is_valid: bool | None = None
    score: float | None = None
    cultivation_target_id: str | None = None
    material_id: str | None = None

    def __post_init__(self) -> None:
        if self.slot_index <= 0:
            raise ValueError("echo slot index must be positive")
        if type(self.is_filled) is not bool:
            raise TypeError("echo filled state must be bool")
        if any(
            state is not None and type(state) is not bool
            for state in (self.set_is_valid, self.main_stat_is_valid)
        ):
            raise TypeError("echo validity must be bool or None")
        if self.score is not None and (not isfinite(self.score) or not 0 <= self.score <= 100):
            raise ValueError("echo score must be finite and between zero and one hundred")
        if not self.is_filled and any(
            value is not None for value in (self.set_is_valid, self.main_stat_is_valid, self.score)
        ):
            raise ValueError("empty echo slots must not contain assessment data")
        _validate_optional_id(self.cultivation_target_id, "cultivation target id")
        _validate_optional_id(self.material_id, "material id")


@dataclass(frozen=True, slots=True)
class CharacterPlanningState:
    character_id: str
    character_name: str
    user_priority: int
    is_candidate: bool
    is_current_goal_reached: bool
    echo_slots: tuple[PlanningEchoSlot, ...]
    functional_thresholds: tuple[FunctionalThreshold, ...] = ()
    weapon_tasks: tuple[CultivationTaskProgress, ...] = ()
    skill_tasks: tuple[CultivationTaskProgress, ...] = ()
    cultivation_tasks: tuple[CultivationTaskProgress, ...] = ()
    required_echo_slots: int = 5
    echo_passing_score: float = 70.0

    def __post_init__(self) -> None:
        if not self.character_id.strip() or not self.character_name.strip():
            raise ValueError("character id and name must not be blank")
        if self.user_priority < 0:
            raise ValueError("character priority must be non-negative")
        if type(self.is_candidate) is not bool or type(self.is_current_goal_reached) is not bool:
            raise TypeError("character flags must be bool")
        if self.required_echo_slots <= 0:
            raise ValueError("required echo slot count must be positive")
        if not 0 < self.echo_passing_score <= 100:
            raise ValueError("echo passing score must be above zero and at most one hundred")
        slot_indices = [slot.slot_index for slot in self.echo_slots]
        if len(slot_indices) != len(set(slot_indices)):
            raise ValueError("echo slot indices must be unique per character")
        if any(index > self.required_echo_slots for index in slot_indices):
            raise ValueError("echo slot index exceeds the required slot count")
        threshold_keys = [item.key.casefold() for item in self.functional_thresholds]
        if len(threshold_keys) != len(set(threshold_keys)):
            raise ValueError("functional threshold keys must be unique per character")
        task_keys = (
            [f"weapon:{item.key.casefold()}" for item in self.weapon_tasks]
            + [f"skill:{item.key.casefold()}" for item in self.skill_tasks]
            + [f"cultivation:{item.key.casefold()}" for item in self.cultivation_tasks]
        )
        if len(task_keys) != len(set(task_keys)):
            raise ValueError("cultivation task keys must be unique per task kind")


@dataclass(frozen=True, slots=True)
class ProgressionAction:
    rank: int
    action_id: str
    character_id: str
    character_name: str
    character_priority: int
    cultivation_target_id: str | None
    material_id: str | None
    phase: ProgressionPhase
    issue: ProgressionIssue
    target_key: str
    title: str
    reason: str
    requires_manual_confirmation: bool
    current_goal_reached: bool


class StaticDataSourceStatus(StrEnum):
    MANUAL = "manual"
    VERIFIED = "verified"
    COMMUNITY_UNVERIFIED = "community_unverified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MaterialDefinition:
    material_id: str
    name: str
    category: str
    stamina_cost_per_run: int | None
    average_yield: Decimal | None
    open_weekdays: tuple[int, ...] | None
    weekly_limit: int | None = None
    source_status: StaticDataSourceStatus = StaticDataSourceStatus.MANUAL
    source_version: str | None = None

    def __post_init__(self) -> None:
        if not self.material_id.strip() or not self.name.strip() or not self.category.strip():
            raise ValueError("material id, name and category must not be blank")
        if self.stamina_cost_per_run is not None and self.stamina_cost_per_run < 0:
            raise ValueError("stamina cost must be non-negative")
        _validate_decimal(self.average_yield, "average yield", positive=True)
        _validate_weekdays(self.open_weekdays)
        if self.weekly_limit is not None and self.weekly_limit < 0:
            raise ValueError("weekly limit must be non-negative")
        if not isinstance(self.source_status, StaticDataSourceStatus):
            raise TypeError("source status must be StaticDataSourceStatus")
        if self.source_version is not None and not self.source_version.strip():
            raise ValueError("source version must not be blank")


@dataclass(frozen=True, slots=True)
class InventoryCount:
    material_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.material_id.strip():
            raise ValueError("inventory material id must not be blank")
        _validate_decimal(self.quantity, "inventory quantity", non_negative=True)


@dataclass(frozen=True, slots=True)
class MaterialRequirement:
    target_id: str
    material_id: str
    required_quantity: Decimal

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.material_id.strip():
            raise ValueError("requirement target and material ids must not be blank")
        _validate_decimal(self.required_quantity, "required quantity", non_negative=True)


@dataclass(frozen=True, slots=True)
class MaterialGap:
    material_id: str
    required_quantity: Decimal
    inventory_quantity: Decimal
    gap_quantity: Decimal
    inventory_is_known: bool = True


@dataclass(frozen=True, slots=True)
class TargetMaterialGap:
    target_id: str
    material_id: str
    required_quantity: Decimal
    inventory_applied: Decimal
    gap_quantity: Decimal
    inventory_is_known: bool = True


@dataclass(frozen=True, slots=True)
class ResourcePlanningTarget:
    target_id: str
    label: str
    queue_rank: int
    reason: str
    is_completed: bool = False
    character_id: str | None = None
    cultivation_target_id: str | None = None
    due_date: date | None = None

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.label.strip():
            raise ValueError("planning target id and label must not be blank")
        if self.queue_rank <= 0:
            raise ValueError("planning target queue rank must be positive")
        if not self.reason.strip():
            raise ValueError("planning target reason must not be blank")
        if type(self.is_completed) is not bool:
            raise TypeError("planning target completed state must be bool")
        _validate_optional_id(self.character_id, "character id")
        _validate_optional_id(self.cultivation_target_id, "cultivation target id")


@dataclass(frozen=True, slots=True)
class WeeklyMaterialUsage:
    material_id: str
    completed_runs: int

    def __post_init__(self) -> None:
        if not self.material_id.strip():
            raise ValueError("weekly usage material id must not be blank")
        if self.completed_runs < 0:
            raise ValueError("weekly completed runs must be non-negative")


@dataclass(frozen=True, slots=True)
class MaterialPlanningOverride:
    """Explicit local override. ``None`` always means no override."""

    material_id: str
    stamina_cost_per_run: int | None = None
    average_yield: Decimal | None = None
    open_weekdays: tuple[int, ...] | None = None
    open_today: bool | None = None
    weekly_limit: int | None = None
    max_runs_today: int | None = None

    def __post_init__(self) -> None:
        if not self.material_id.strip():
            raise ValueError("override material id must not be blank")
        if self.stamina_cost_per_run is not None and self.stamina_cost_per_run < 0:
            raise ValueError("override stamina cost must be non-negative")
        _validate_decimal(self.average_yield, "override average yield", positive=True)
        _validate_weekdays(self.open_weekdays)
        if self.open_today is not None and type(self.open_today) is not bool:
            raise TypeError("open_today override must be bool or None")
        if self.weekly_limit is not None and self.weekly_limit < 0:
            raise ValueError("override weekly limit must be non-negative")
        if self.max_runs_today is not None and self.max_runs_today < 0:
            raise ValueError("override maximum runs must be non-negative")


@dataclass(frozen=True, slots=True)
class DailyPlanRequest:
    plan_date: date
    daily_waveplates: int
    targets: tuple[ResourcePlanningTarget, ...]
    materials: tuple[MaterialDefinition, ...]
    requirements: tuple[MaterialRequirement, ...]
    inventory: tuple[InventoryCount, ...] = ()
    weekly_usage: tuple[WeeklyMaterialUsage, ...] = ()
    overrides: tuple[MaterialPlanningOverride, ...] = ()
    max_actions: int = 2
    estimation_horizon_days: int = 365

    def __post_init__(self) -> None:
        if self.daily_waveplates < 0:
            raise ValueError("daily waveplates must be non-negative")
        if self.max_actions <= 0:
            raise ValueError("maximum action count must be positive")
        if not 1 <= self.estimation_horizon_days <= 3660:
            raise ValueError("estimation horizon must be between 1 and 3660 days")


class DailyActionStatus(StrEnum):
    PLANNED = "planned"
    NEEDS_CONFIRMATION = "needs_confirmation"


class PlanNoticeCode(StrEnum):
    UNKNOWN_INVENTORY = "unknown_inventory"
    UNKNOWN_SCHEDULE = "unknown_schedule"
    UNKNOWN_COST = "unknown_cost"
    UNKNOWN_YIELD = "unknown_yield"
    CLOSED_TODAY = "closed_today"
    WEEKLY_LIMIT_REACHED = "weekly_limit_reached"
    INSUFFICIENT_WAVEPLATES = "insufficient_waveplates"
    MANUAL_OVERRIDE = "manual_override"


@dataclass(frozen=True, slots=True)
class DailyPlanNotice:
    code: PlanNoticeCode
    target_id: str
    material_id: str
    message: str


@dataclass(frozen=True, slots=True)
class DailyPlanAction:
    rank: int
    target_id: str
    target_label: str
    character_id: str | None
    cultivation_target_id: str | None
    material_id: str | None
    material_name: str
    status: DailyActionStatus
    requires_manual_confirmation: bool
    recommended_runs: int | None
    stamina_cost: int | None
    expected_quantity: Decimal | None
    gap_before: Decimal | None
    gap_after: Decimal | None
    reason: str
    estimated_completion_date: date | None


@dataclass(frozen=True, slots=True)
class WeeklyShortage:
    material_id: str
    material_name: str
    remaining_quantity: Decimal | None
    requires_manual_confirmation: bool
    reason: str


@dataclass(frozen=True, slots=True)
class TargetCompletionEstimate:
    target_id: str
    estimated_date: date | None
    requires_manual_confirmation: bool


@dataclass(frozen=True, slots=True)
class DailyPlanResult:
    plan_date: date
    actions: tuple[DailyPlanAction, ...]
    material_gaps: tuple[MaterialGap, ...]
    target_gaps: tuple[TargetMaterialGap, ...]
    weekly_shortages: tuple[WeeklyShortage, ...]
    target_estimates: tuple[TargetCompletionEstimate, ...]
    notices: tuple[DailyPlanNotice, ...]
    remaining_waveplates: int
    assumptions: tuple[str, ...]


def _validate_decimal(
    value: Decimal | None,
    label: str,
    *,
    non_negative: bool = False,
    positive: bool = False,
) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal):
        raise TypeError(f"{label} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    if non_negative and value < 0:
        raise ValueError(f"{label} must be non-negative")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")


def _validate_optional_id(value: str | None, label: str) -> None:
    if value is not None and not value.strip():
        raise ValueError(f"{label} must not be blank")


def _validate_weekdays(weekdays: tuple[int, ...] | None) -> None:
    if weekdays is None:
        return
    if not weekdays:
        raise ValueError("open weekdays must not be empty; use None for unknown")
    if any(day < 1 or day > 7 for day in weekdays):
        raise ValueError("open weekdays must use ISO values from 1 to 7")
    if len(weekdays) != len(set(weekdays)):
        raise ValueError("open weekdays must not contain duplicates")


__all__ = [
    "CharacterPlanningState",
    "CultivationTaskProgress",
    "DailyActionStatus",
    "DailyPlanAction",
    "DailyPlanNotice",
    "DailyPlanRequest",
    "DailyPlanResult",
    "FunctionalThreshold",
    "InventoryCount",
    "MaterialDefinition",
    "MaterialGap",
    "MaterialPlanningOverride",
    "MaterialRequirement",
    "PlanNoticeCode",
    "PlanningEchoSlot",
    "ProgressionAction",
    "ProgressionIssue",
    "ProgressionPhase",
    "ResourcePlanningTarget",
    "StaticDataSourceStatus",
    "TargetCompletionEstimate",
    "TargetMaterialGap",
    "ThresholdDirection",
    "WeeklyMaterialUsage",
    "WeeklyShortage",
]
