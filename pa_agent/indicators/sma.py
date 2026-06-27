"""Simple Moving Average (SMA) — full computation."""
from __future__ import annotations

import math


def sma_full(values: list[float], period: int) -> list[float]:
    """Compute SMA over *values* (oldest-first).

    Returns a list of the same length, aligned to ``values``:
    - Indices 0 .. period-2: ``nan``  (warm-up, not enough data yet)
    - Index period-1 .. end: arithmetic mean of the trailing *period* values

    This mirrors the warm-up semantics of ``ema_full`` so callers that skip
    ``nan`` points (e.g. chart rendering, snapshot frame building) work
    uniformly across EMA and SMA.

    Args:
        values: Price series, oldest first.
        period: SMA window (must be >= 1).
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    n = len(values)
    result = [math.nan] * n
    if n < period:
        return result

    # Sliding window sum: add new, drop old. Avoids O(n*period) recomputation.
    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period
    return result
