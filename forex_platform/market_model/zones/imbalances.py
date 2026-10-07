"""Fair Value Gap (FVG) detection.

A three-candle imbalance exists when the middle candle moves so aggressively
that the wicks of the first and third candles do not overlap:

- Bullish FVG: ``low[i] > high[i-2]`` (displacement higher).
- Bearish FVG: ``high[i] < low[i-2]`` (displacement lower).

The gap is bounded by the wick of the first candle and the wick of the third
candle, which are the two levels that must later be rebalanced.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Sequence

import polars as pl
from pydantic import BaseModel, ConfigDict

from forex_platform.market_model.contracts import MarketZone, ZoneScope, ZoneType


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class FVGDirection(str, Enum):
    """Direction of price displacement that produced the gap."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class FairValueGap(BaseModel):
    """A three-bar price inefficiency (imbalance)."""

    model_config = ConfigDict(frozen=True)

    index: int
    start_index: int
    timestamp: datetime
    direction: FVGDirection
    top: Decimal
    bottom: Decimal
    midpoint: Decimal


def detect_fair_value_gaps(
    bars: pl.DataFrame,
    *,
    high_col: str = "high",
    low_col: str = "low",
    timestamp_col: str = "timestamp",
    min_gap: Optional[Decimal] = None,
) -> List[FairValueGap]:
    """Detect bullish and bearish fair value gaps.

    ``index`` is the third bar (the bar that confirms the gap, carrying the
    causally-valid timestamp); ``start_index`` is the first bar of the
    three-bar pattern. ``min_gap`` filters out gaps narrower than the given
    size (``top - bottom``).
    """
    if min_gap is not None and min_gap < 0:
        raise ValueError(f"min_gap must be >= 0, got {min_gap}")
    for col in (high_col, low_col, timestamp_col):
        if col not in bars.columns:
            raise ValueError(f"required column {col!r} missing from bars")

    n = bars.height
    if n < 3:
        return []

    highs = bars[high_col].to_list()
    lows = bars[low_col].to_list()
    timestamps = bars[timestamp_col].to_list()

    gaps: List[FairValueGap] = []
    for i in range(2, n):
        if lows[i] > highs[i - 2]:
            direction = FVGDirection.BULLISH
            bottom = _dec(highs[i - 2])
            top = _dec(lows[i])
        elif highs[i] < lows[i - 2]:
            direction = FVGDirection.BEARISH
            bottom = _dec(highs[i])
            top = _dec(lows[i - 2])
        else:
            continue
        if min_gap is not None and (top - bottom) < min_gap:
            continue
        gaps.append(
            FairValueGap(
                index=i,
                start_index=i - 2,
                timestamp=timestamps[i],
                direction=direction,
                top=top,
                bottom=bottom,
                midpoint=(top + bottom) / 2,
            )
        )
    return gaps


def fvgs_to_zones(
    gaps: Sequence[FairValueGap],
    *,
    scope: ZoneScope = ZoneScope.INTERNAL,
) -> List[MarketZone]:
    """Convert fair value gaps to generic :class:`MarketZone` records.

    The gap spans ``start_index`` (first bar) to ``index`` (confirming bar).
    Only the confirming bar's timestamp is carried by
    :class:`FairValueGap`, so both zone timestamps use it.
    """
    return [
        MarketZone(
            zone_type=ZoneType.FVG,
            scope=scope,
            index_start=gap.start_index,
            index_end=gap.index,
            timestamp_start=gap.timestamp,
            timestamp_end=gap.timestamp,
            price_low=gap.bottom,
            price_high=gap.top,
        )
        for gap in gaps
    ]