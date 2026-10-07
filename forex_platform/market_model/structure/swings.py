"""Deterministic swing (market-structure pivot) detection.

A swing high/low is a local extremum confirmed by ``lookback`` bars on each
side. Detection is causal and deterministic: for a fixed ``lookback`` the
result depends only on the input bars.

Strict inequalities are used on purpose: equal highs/lows are liquidity
(EQH/EQL), never pivots.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List

import polars as pl

from forex_platform.market_model.contracts import SwingPoint, SwingScope, SwingType


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def detect_swings(
    bars: pl.DataFrame,
    lookback: int = 5,  # Increased from 2 to filter minor M15 intraday noise
    *,
    high_col: str = "high",
    low_col: str = "low",
    timestamp_col: str = "timestamp",
    scope: SwingScope = SwingScope.EXTERNAL,
) -> List[SwingPoint]:
    """Detect swing highs/lows confirmed by ``lookback`` bars on each side.

    Bar ``i`` is a swing high iff ``high[i]`` is strictly greater than the
    highs of the ``lookback`` bars on each side; symmetric rule for lows.
    A bar cannot be both (strict comparisons + ``elif`` keep the alternation
    property required by the external/internal hierarchy).
    """
    if lookback < 1:
        raise ValueError(f"lookback must be >= 1, got {lookback}")
    for col in (high_col, low_col, timestamp_col):
        if col not in bars.columns:
            raise ValueError(f"required column {col!r} missing from bars")

    n = bars.height
    if n < 2 * lookback + 1:
        return []

    highs = bars[high_col].to_list()
    lows = bars[low_col].to_list()
    timestamps = bars[timestamp_col].to_list()

    swings: List[SwingPoint] = []
    for i in range(lookback, n - lookback):
        left = range(i - lookback, i)
        right = range(i + 1, i + lookback + 1)
        is_high = all(highs[i] > highs[j] for j in left) and all(
            highs[i] > highs[j] for j in right
        )
        is_low = all(lows[i] < lows[j] for j in left) and all(
            lows[i] < lows[j] for j in right
        )
        if is_high:
            swings.append(
                SwingPoint(
                    index=i,
                    timestamp=timestamps[i],
                    price=_dec(highs[i]),
                    swing_type=SwingType.HIGH,
                    scope=scope,
                )
            )
        elif is_low:
            swings.append(
                SwingPoint(
                    index=i,
                    timestamp=timestamps[i],
                    price=_dec(lows[i]),
                    swing_type=SwingType.LOW,
                    scope=scope,
                )
            )
    return swings