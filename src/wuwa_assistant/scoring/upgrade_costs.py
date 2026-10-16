"""Incremental five-star Echo costs, separate from scoring and past investment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from itertools import pairwise


@dataclass(frozen=True, slots=True)
class UpgradeNodeCost:
    current_level: int
    next_level: int
    experience: int
    shell_credits: int
    tuners: int
    exp_fraction_of_full: float
    explanation: str


@dataclass(frozen=True, slots=True)
class _CostData:
    cumulative_exp: tuple[int, ...]
    experience_per_shell_credit: int
    tuners_per_substat: int
    shell_credits_per_substat: int


@lru_cache(maxsize=1)
def _load_cost_data() -> _CostData:
    resource = files("wuwa_assistant.data.scoring").joinpath("upgrade-costs.v1.json")
    raw = json.loads(resource.read_text(encoding="utf-8"))
    cumulative = raw.get("cumulative_exp_by_level")
    if (
        raw.get("schema_version") != 1
        or raw.get("rarity") != 5
        or not isinstance(cumulative, list)
        or len(cumulative) != 26
        or any(type(value) is not int for value in cumulative)
        or cumulative[0] != 0
        or any(start >= end for start, end in pairwise(cumulative))
    ):
        raise ValueError("invalid five-star Echo experience table")
    constants = tuple(
        raw.get(key)
        for key in (
            "experience_per_shell_credit",
            "tuners_per_substat",
            "shell_credits_per_substat",
        )
    )
    if any(type(value) is not int or value <= 0 for value in constants):
        raise ValueError("invalid five-star Echo material costs")
    return _CostData(tuple(cumulative), *constants)


def next_node_cost(level: int) -> UpgradeNodeCost | None:
    """Price the next new tuning slot, from zero EXP inside the scanned level.

    The cost assumes all slots already unlocked by ``level`` have been tuned.
    No inventory, excess feed EXP, or hypothetical recycling is accounted for.
    Integer levels between nodes are retained, not rounded down to a node.
    """
    if type(level) is not int or not 0 <= level <= 25:
        raise ValueError("Echo level must be an integer from 0 to 25")
    if level == 25:
        return None
    return upgrade_cost_to_level(level, (level // 5 + 1) * 5)


def upgrade_cost_to_level(level: int, target_level: int) -> UpgradeNodeCost:
    """Price future EXP and all new tuning slots through a chosen checkpoint."""
    if type(level) is not int or not 0 <= level < 25:
        raise ValueError("Echo level must be an integer from 0 to 24")
    if (
        type(target_level) is not int
        or target_level not in {5, 10, 15, 20, 25}
        or target_level <= level
    ):
        raise ValueError("target must be a later tuning level from 5 to 25")
    data = _load_cost_data()
    next_level = target_level
    new_slots = target_level // 5 - level // 5
    experience = data.cumulative_exp[next_level] - data.cumulative_exp[level]
    shell_credits = (
        experience + data.experience_per_shell_credit - 1
    ) // data.experience_per_shell_credit + new_slots * data.shell_credits_per_substat
    return UpgradeNodeCost(
        current_level=level,
        next_level=next_level,
        experience=experience,
        shell_credits=shell_credits,
        tuners=new_slots * data.tuners_per_substat,
        exp_fraction_of_full=experience / data.cumulative_exp[25],
        explanation=(
            f"+{level} → +{next_level}: 最多尚需 {experience:,} 經驗, "
            f"貝幣約 {shell_credits:,} (含調諧), 調諧器 {new_slots * data.tuners_per_substat} 個。"
            "五星聲骸; 按本級 0 經驗估算, 已累積經驗可減少投入, "
            "材料取整另計; 未抵扣回收。成本來源為社群資料交叉核對。"
        ),
    )


__all__ = ["UpgradeNodeCost", "next_node_cost", "upgrade_cost_to_level"]
