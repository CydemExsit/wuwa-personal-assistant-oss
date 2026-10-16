from __future__ import annotations

import json
from dataclasses import replace
from itertools import combinations, product
from math import comb
from pathlib import Path

import numpy as np
import pytest

from wuwa_assistant.scoring.calibration import score_points
from wuwa_assistant.scoring.projection import (
    _score_cents,
    score_distribution,
    summarize_distribution,
)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("opened", [1, 2, 3, 4, 5])
def test_full_engine_preserves_pre_optimization_projection(legacy: bool, opened: int) -> None:
    from wuwa_assistant.scoring import EchoScoringEngine, ObservedStat, load_scoring_catalog
    from wuwa_assistant.scoring.data import LEGACY_ROLL_VERSION

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "projections-1.0.34.json").read_text(encoding="utf-8")
    )
    profile = load_scoring_catalog().profile("generic_dps_v1")
    if legacy:
        profile = replace(
            profile, profile_id="generic_dps_legacy", roll_data_version=LEGACY_ROLL_VERSION
        )
    stats = tuple(ObservedStat(name, value, unit, 0.99) for name, value, unit in fixture["stats"])
    actual = EchoScoringEngine().enumerate_remaining(
        profile, stats[:opened], improvement_target=fixture["target"]
    )
    assert actual.to_dict() == fixture["cases"][f"{profile.profile_id}/+{opened * 5}"]


@pytest.mark.parametrize("reference,ideal", [(2.1, None), (2.1, 3.0), (0.0, None), (2.1, 2.1)])
def test_native_rounding_matches_scalar_including_decimal_near_ties(
    reference: float, ideal: float | None
) -> None:
    rng = np.random.default_rng(135)
    # Deliberately exercise float representations on either side of half cents.
    scores = np.arange(10_000, dtype=np.float64) / 100 + 0.005
    tie_points = scores / 100 * reference
    earned = np.concatenate(
        (
            rng.uniform(-1, 5, 10_000),
            tie_points,
            np.nextafter(tie_points, -np.inf),
            np.nextafter(tie_points, np.inf),
        )
    )
    expected = [round(score_points(float(p), reference, ideal) * 100) for p in earned]
    assert _score_cents(earned, reference, ideal).tolist() == expected


@pytest.mark.parametrize("remaining", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("ideal", [None, 3.7])
def test_distribution_matches_independent_scalar_enumeration(
    remaining: int, ideal: float | None
) -> None:
    options = tuple(((i * 0.015, 0.7), (i * 0.073, 0.25), (i * 0.09, 0.05)) for i in range(1, 7))
    expected = np.zeros(10_001)
    for selected in combinations(options, remaining):
        for outcome in product(*selected):
            probability = 1 / comb(len(options), remaining)
            for _, mass in outcome:
                probability *= mass
            score = score_points(0.031 + sum(p for p, _ in outcome), 2.0, ideal)
            expected[round(score * 100)] += probability
    actual = score_distribution(options, remaining, 0.031, 2.0, ideal)
    assert np.allclose(actual, expected, rtol=1e-13, atol=1e-14)
    assert not actual.flags.writeable
    thresholds = {"below": 0.0, "edge": 55.01, "maximum": 100.0}
    for target in (0.0, 55.01, 55.010000001, 100.0, 200.0):
        observed = summarize_distribution(actual, thresholds, target)
        baseline = summarize_distribution(expected, thresholds, target)
        for index in (0, 1, 2, 3, 5, 6):
            assert observed[index] == pytest.approx(baseline[index], abs=1e-12)
        assert observed[4] == pytest.approx(baseline[4], abs=1e-12)


def test_projection_cache_keys_numeric_inputs_and_has_bounded_storage() -> None:
    score_distribution.cache_clear()
    options = (((0.5, 0.7), (1.0, 0.3)),)
    first = score_distribution(options, 1, 0.0, 1.0, None)
    assert score_distribution(options, 1, 0.0, 1.0, None) is first
    assert score_distribution.cache_info().hits == 1
    assert not np.array_equal(first, score_distribution(options, 1, 0.1, 1.0, None))
    for i in range(40):
        score_distribution(options, 1, i / 100, 1.0, None)
    assert score_distribution.cache_info().currsize == 32
    score_distribution.cache_clear()
