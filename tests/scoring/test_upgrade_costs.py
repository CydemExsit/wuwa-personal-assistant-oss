from __future__ import annotations

import json
from importlib.resources import files

import pytest

from wuwa_assistant.scoring.upgrade_costs import next_node_cost


@pytest.mark.parametrize(
    ("level", "target", "experience", "shell_credits"),
    [
        (0, 5, 4_400, 2_440),
        (5, 10, 12_100, 3_210),
        (10, 15, 23_100, 4_310),
        (15, 20, 39_500, 5_950),
        (17, 20, 26_000, 4_600),
        (20, 25, 63_500, 8_350),
        (22, 25, 41_500, 6_150),
        (24, 25, 15_100, 3_510),
    ],
)
def test_incremental_cost_uses_actual_level_not_previous_node(
    level: int, target: int, experience: int, shell_credits: int
) -> None:
    cost = next_node_cost(level)
    assert cost is not None
    assert cost.current_level == level
    assert cost.next_level == target
    assert cost.experience == experience
    assert cost.shell_credits == shell_credits
    assert cost.tuners == 10
    assert cost.exp_fraction_of_full == pytest.approx(experience / 142_600)


def test_node_costs_reconcile_with_full_cost_and_expose_estimate_basis() -> None:
    costs = [next_node_cost(level) for level in (0, 5, 10, 15, 20)]
    assert sum(c.experience for c in costs if c is not None) == 142_600
    assert sum(c.shell_credits for c in costs if c is not None) == 24_260
    assert sum(c.tuners for c in costs if c is not None) == 50
    final = costs[-1]
    assert final is not None
    assert final.exp_fraction_of_full == pytest.approx(0.4453, abs=0.0001)
    assert "本級 0 經驗" in final.explanation
    assert "未抵扣回收" in final.explanation
    assert "社群" in final.explanation
    assert next_node_cost(25) is None


def test_all_intermediate_levels_decrease_cost_until_next_new_slot() -> None:
    for node in (0, 5, 10, 15, 20):
        previous_exp = 200_000
        for level in range(node, node + 5):
            cost = next_node_cost(level)
            assert cost is not None
            assert cost.next_level == node + 5
            assert 0 < cost.experience < previous_exp
            previous_exp = cost.experience


@pytest.mark.parametrize("invalid", [-1, 26, True, 17.5, "22", None])
def test_invalid_level_cannot_produce_plausible_cost(invalid: object) -> None:
    with pytest.raises(ValueError, match="integer from 0 to 25"):
        next_node_cost(invalid)  # type: ignore[arg-type]


def test_cost_source_is_not_misrepresented_as_official_probability_data() -> None:
    resource = files("wuwa_assistant.data.scoring").joinpath("upgrade-costs.v1.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    assert data["is_official"] is False
    assert data["source_status"] == "community_cross_checked"
    assert len(data["sources"]) >= 2
    assert all(s["url"].startswith("https://") for s in data["sources"])
    assert len(data["cumulative_exp_by_level"]) == 26


@pytest.mark.parametrize(
    ("level", "target"), [(10, 10), (15, 10), (5, 14), (25, 25), (True, 15), (0, True)]
)
def test_checkpoint_cost_rejects_invalid_or_nonfuture_node(level, target):
    from wuwa_assistant.scoring.upgrade_costs import upgrade_cost_to_level

    with pytest.raises(ValueError):
        upgrade_cost_to_level(level, target)


def test_exploration_cost_equals_sum_of_new_slots_without_past_cost():
    from wuwa_assistant.scoring.upgrade_costs import upgrade_cost_to_level

    start = upgrade_cost_to_level(0, 15)
    at_five = upgrade_cost_to_level(5, 15)
    at_ten = upgrade_cost_to_level(10, 15)
    assert (start.experience, at_five.experience, at_ten.experience) == (39600, 35200, 23100)
    assert (start.tuners, at_five.tuners, at_ten.tuners) == (30, 20, 10)
    nodes = [next_node_cost(level) for level in (0, 5, 10)]
    assert start.experience == sum(c.experience for c in nodes)
    assert start.shell_credits == sum(c.shell_credits for c in nodes)
    assert start.tuners == sum(c.tuners for c in nodes)
