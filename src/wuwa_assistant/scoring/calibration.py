"""Bounded, deterministic random-five-piece reference distribution.

Each of the 13 types is either skipped or chosen once. The coefficient for five
chosen types gives all equally likely subsets, convolved with the disclosed
conditional value probabilities. This is a project score reference, not damage
or a claim that an observed Echo has a particular percentile score.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np


def score_points(earned: float, reference: float, ideal: float | None = None) -> float:
    """P99 maps to 90; preserve strictly increasing headroom for exceptional gear.

    Legacy profiles have no ideal and retain their original linear 0..100 scale.
    Degenerate distributions with identical P99/ideal use the linear scale too.
    """
    if reference <= 0:
        return 0.0
    if ideal is None or ideal <= reference + 1e-9:
        score = earned / reference * 100
    elif earned <= reference:
        score = earned / reference * 90
    else:
        score = 90 + (earned - reference) / (ideal - reference) * 10
    return round(min(100.0, max(0.0, score)), 2)


@lru_cache(maxsize=256)
def probability_reference(
    options: tuple[tuple[tuple[float, float], ...], ...], quantile: float = 0.99
) -> float:
    """Return the weighted-points quantile; discretization error <= .025% of max.

    Quantize each contribution into 10,000 units of the achievable maximum.
    Five nearest-bin roundings move a total by at most 2.5 units. Scaling the
    bins bounds memory even for large user-defined weights and multipliers.
    """
    maximum = sum(sorted((max(p for p, _ in row) for row in options), reverse=True)[:5])
    if maximum <= 0:
        return 0.0
    step = maximum / 10_000
    distribution = np.zeros((6, 10_004), dtype=np.float64)
    distribution[0, 0] = 1.0
    for index, row in enumerate(options):
        grouped: dict[int, float] = {}
        for points, probability in row:
            shift = round(points / step)
            grouped[shift] = grouped.get(shift, 0.0) + probability
        for count in range(min(index + 1, 5), 0, -1):
            for shift, probability in grouped.items():
                if shift == 0:
                    distribution[count] += distribution[count - 1] * probability
                else:
                    distribution[count, shift:] += distribution[count - 1, :-shift] * probability
    mass = distribution[5]
    if mass.sum() <= 0:
        return maximum
    position = int(np.searchsorted(np.cumsum(mass), quantile * mass.sum()))
    return position * step
