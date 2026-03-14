"""Shared numeric helpers for loadtest scripts."""

from __future__ import annotations

import math


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    lower_value = sorted_values[lower]
    upper_value = sorted_values[upper]
    fraction = rank - lower
    return lower_value + (upper_value - lower_value) * fraction
