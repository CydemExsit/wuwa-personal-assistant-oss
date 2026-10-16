"""Exact remaining-roll distributions evaluated in bounded native NumPy batches.

Input contributions already include every version/profile/context modifier. Cache
keys contain these numeric inputs, not mutable profile IDs. Only rounded score
mass is retained (at most 10,001 bins per entry), never the Cartesian outcomes.
"""

from __future__ import annotations

from functools import lru_cache
from itertools import combinations
from math import comb, prod

import numpy as np
from numpy.typing import NDArray

from .calibration import score_points

type RollOptions = tuple[tuple[tuple[float, float], ...], ...]

_BATCH_SIZE = 32_768


def _score_cents(
    earned: NDArray[np.float64], reference: float, ideal: float | None
) -> NDArray[np.int64]:
    """Keep Python round's decimal tie behavior, including float near-ties.

    NumPy's fast decimal rounding is not identical to Python's. Values near a
    half-cent are routed through the scalar reference instead of changing any
    threshold or replacement decision at a rounding boundary.
    """
    if reference <= 0:
        return np.zeros(earned.size, dtype=np.int64)
    if ideal is None or ideal <= reference + 1e-9:
        scores = earned / reference * 100
    else:
        scores = np.where(
            earned <= reference,
            earned / reference * 90,
            90 + (earned - reference) / (ideal - reference) * 10,
        )
    scaled = np.clip(scores, 0, 100) * 100
    cents = np.rint(scaled).astype(np.int64)
    ties = np.abs(scaled - (np.floor(scaled) + 0.5)) <= 4 * np.spacing(np.maximum(scaled, 1))
    for index in np.flatnonzero(ties):
        cents[index] = round(score_points(float(earned[index]), reference, ideal) * 100)
    return cents


@lru_cache(maxsize=32)
def score_distribution(
    options: RollOptions,
    remaining: int,
    earned: float,
    reference: float,
    ideal: float | None,
) -> NDArray[np.float64]:
    """Enumerate all outcomes exactly; bound temporary and retained storage.

    Subsets retain equal probability; rolls retain their disclosed conditional
    probabilities. Addition/multiplication order matches the scalar enumerator.
    The immutable result is safe to share across workers and different targets.
    """
    mass = np.zeros(10_001, dtype=np.float64)
    subset_probability = 1.0 / comb(len(options), remaining)
    for selected in combinations(options, remaining):
        count = prod(len(row) for row in selected)
        values = tuple(np.asarray(row, dtype=np.float64) for row in selected)
        for start in range(0, count, _BATCH_SIZE):
            ordinal = np.arange(start, min(start + _BATCH_SIZE, count), dtype=np.int64)
            points = np.zeros(ordinal.size, dtype=np.float64)
            correction = np.zeros(ordinal.size, dtype=np.float64)
            probability = np.full(ordinal.size, subset_probability, dtype=np.float64)
            stride = count
            for row in values:
                stride //= len(row)
                indices = ordinal // stride % len(row)
                contribution = row[indices, 0]
                updated = points + contribution
                # Python 3.12 sum() uses Neumaier compensation. Matching it is
                # necessary when the completed score lies on a half-cent boundary.
                correction += np.where(
                    np.abs(points) >= np.abs(contribution),
                    (points - updated) + contribution,
                    (contribution - updated) + points,
                )
                points = updated
                probability *= row[indices, 1]
            points += correction
            points += earned
            cents = _score_cents(points, reference, ideal)
            mass += np.bincount(cents, weights=probability, minlength=10_001)
    mass.flags.writeable = False
    return mass


def summarize_distribution(
    mass: NDArray[np.float64], thresholds: dict[str, float], target: float | None
) -> tuple[float, float, float, float, dict[str, float], float, float]:
    """Reduce the same distribution for arbitrary thresholds/replacement targets."""
    nonzero = np.flatnonzero(mass)
    if not nonzero.size:
        return 0.0, 0.0, 0.0, 0.0, dict.fromkeys(thresholds, 0.0), 0.0, 0.0
    scores = nonzero.astype(np.float64) / 100
    probabilities = mass[nonzero]
    total = float(probabilities.sum())
    threshold_mass = {
        name: float(probabilities[scores >= value].sum()) for name, value in thresholds.items()
    }
    better = np.zeros(scores.size, dtype=np.bool_) if target is None else scores > target
    improvement = float(probabilities[better].sum())
    gain = 0.0 if target is None else float(np.dot(scores[better] - target, probabilities[better]))
    return (
        float(scores[0]),
        float(scores[-1]),
        float(np.dot(scores, probabilities)),
        total,
        threshold_mass,
        improvement,
        gain,
    )
