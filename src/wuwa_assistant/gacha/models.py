"""Pure, immutable models for manual gacha resource planning.

The gacha planner intentionally knows nothing about banners, rates, or game
accounts.  Pull requirements are supplied by the user and a missing value stays
unknown instead of being inferred from pity or guarantee state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class PullType(StrEnum):
    CHARACTER = "character"
    WEAPON = "weapon"
    UNKNOWN = "unknown"


class ForecastScenario(StrEnum):
    CONSERVATIVE = "conservative"
    TYPICAL = "typical"
    OPTIMISTIC = "optimistic"


class IncomeScenario(StrEnum):
    GENERAL = "general"
    CONSERVATIVE = "conservative"
    TYPICAL = "typical"
    OPTIMISTIC = "optimistic"


@dataclass(frozen=True, slots=True)
class GachaResources:
    astrite: int = 0
    character_pulls: int = 0
    weapon_pulls: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("astrite", self.astrite),
            ("character pulls", self.character_pulls),
            ("weapon pulls", self.weapon_pulls),
        ):
            _non_negative_int(value, label)


@dataclass(frozen=True, slots=True)
class GachaStatus:
    resources: GachaResources
    character_pity: int = 0
    weapon_pity: int = 0
    character_guaranteed: bool = False
    weapon_guaranteed: bool = False

    def __post_init__(self) -> None:
        _non_negative_int(self.character_pity, "character pity")
        _non_negative_int(self.weapon_pity, "weapon pity")
        if type(self.character_guaranteed) is not bool:
            raise TypeError("character guaranteed state must be bool")
        if type(self.weapon_guaranteed) is not bool:
            raise TypeError("weapon guaranteed state must be bool")


@dataclass(frozen=True, slots=True)
class ManualPullRequirements:
    conservative: int | None = None
    typical: int | None = None
    optimistic: int | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("conservative requirement", self.conservative),
            ("typical requirement", self.typical),
            ("optimistic requirement", self.optimistic),
        ):
            if value is not None:
                _non_negative_int(value, label)

    def for_scenario(self, scenario: ForecastScenario) -> int | None:
        if scenario is ForecastScenario.CONSERVATIVE:
            return self.conservative
        if scenario is ForecastScenario.TYPICAL:
            return self.typical
        return self.optimistic


@dataclass(frozen=True, slots=True)
class GachaTarget:
    target_id: str
    target_type: PullType
    name: str
    target_copies: int
    target_date: date | None
    priority: int
    status: str
    requirements: ManualPullRequirements

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.name.strip():
            raise ValueError("target id and name must not be blank")
        if self.target_type is PullType.UNKNOWN:
            raise ValueError("gacha target type must be character or weapon")
        _positive_int(self.target_copies, "target copies")
        _non_negative_int(self.priority, "target priority")
        if not self.status.strip():
            raise ValueError("target status must not be blank")


@dataclass(frozen=True, slots=True)
class GachaIncome:
    income_id: str
    label: str
    expected_date: date | None
    astrite_amount: int
    pull_amount: int
    pull_type: PullType
    scenario: IncomeScenario | None
    is_received: bool = False

    def __post_init__(self) -> None:
        if not self.income_id.strip() or not self.label.strip():
            raise ValueError("income id and label must not be blank")
        _non_negative_int(self.astrite_amount, "income astrite")
        _non_negative_int(self.pull_amount, "income pulls")
        if type(self.is_received) is not bool:
            raise TypeError("income received state must be bool")


@dataclass(frozen=True, slots=True)
class PlanningWarning:
    code: str
    message: str
    scenario: ForecastScenario | None = None
    target_id: str | None = None
    income_id: str | None = None


@dataclass(frozen=True, slots=True)
class TargetAllocation:
    target_id: str
    target_name: str
    target_type: PullType
    target_date: date | None
    priority: int
    required_pulls: int | None
    funded_pulls: int | None
    missing_pulls: int | None
    dedicated_pulls_used: int | None
    astrite_used: int | None
    is_fully_funded: bool | None


@dataclass(frozen=True, slots=True)
class ScenarioPlan:
    scenario: ForecastScenario
    allocations: tuple[TargetAllocation, ...]
    remaining_resources: GachaResources
    included_income_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GachaPlan:
    scenarios: tuple[ScenarioPlan, ...]
    warnings: tuple[PlanningWarning, ...]

    def scenario(self, scenario: ForecastScenario) -> ScenarioPlan:
        return next(item for item in self.scenarios if item.scenario is scenario)


def _non_negative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _positive_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
