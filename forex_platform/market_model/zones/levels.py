"""Session liquidity-level extraction from timestamp-indexed bars.

Derives reference liquidity that price commonly reacts to:

- Asian session high / low (default 00:00-08:00 UTC).
- Previous-day high / low (PDH / PDL), taken from the previous trading day
  present in the data.
- Previous-week high / low (PWH / PWL), taken from the previous ISO week
  present in the data.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Sequence

import polars as pl
from pydantic import BaseModel, ConfigDict

from forex_platform.market_model.contracts import MarketZone, ZoneScope, ZoneType


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class LiquidityLevelType(str, Enum):
    """Kind of session liquidity level."""

    ASIAN_HIGH = "ASIAN_HIGH"
    ASIAN_LOW = "ASIAN_LOW"
    PDH = "PDH"
    PDL = "PDL"
    PWH = "PWH"
    PWL = "PWL"


class LiquidityLevel(BaseModel):
    """A single session liquidity level."""

    model_config = ConfigDict(frozen=True)

    level_type: LiquidityLevelType
    price: Decimal
    session_date: date
    established_at: datetime
    symbol: Optional[str] = None


def extract_session_levels(
    bars: pl.DataFrame,
    *,
    asian_start_hour: int = 0,
    asian_end_hour: int = 8,
    high_col: str = "high",
    low_col: str = "low",
    timestamp_col: str = "timestamp",
    symbol: Optional[str] = None,
) -> List[LiquidityLevel]:
    """Extract Asian-range and previous-day liquidity levels (plus PWH/PWL)."""
    for col in (high_col, low_col, timestamp_col):
        if col not in bars.columns:
            raise ValueError(f"required column {col!r} missing from bars")
    if not (0 <= asian_start_hour < asian_end_hour <= 24):
        raise ValueError(
            f"invalid asian session window [{asian_start_hour}, {asian_end_hour})"
        )
    if bars.is_empty():
        return []

    df = bars.sort(timestamp_col)
    timestamps = df[timestamp_col].to_list()
    highs = df[high_col].to_list()
    lows = df[low_col].to_list()

    levels: List[LiquidityLevel] = []

    days: Dict[date, List[int]] = {}
    for i, ts in enumerate(timestamps):
        days.setdefault(ts.date(), []).append(i)
    sorted_days = sorted(days)

    # ---- Asian range per day -------------------------------------------
    for day in sorted_days:
        idxs = [
            i
            for i in days[day]
            if asian_start_hour <= timestamps[i].hour < asian_end_hour
        ]
        if not idxs:
            continue
        high_i = max(idxs, key=lambda i: highs[i])
        low_i = min(idxs, key=lambda i: lows[i])
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.ASIAN_HIGH,
                price=_dec(highs[high_i]),
                session_date=day,
                established_at=timestamps[high_i],
                symbol=symbol,
            )
        )
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.ASIAN_LOW,
                price=_dec(lows[low_i]),
                session_date=day,
                established_at=timestamps[low_i],
                symbol=symbol,
            )
        )

    # ---- PDH / PDL from the previous day present in the data ------------
    for pos, day in enumerate(sorted_days):
        if pos == 0:
            continue
        prev_idxs = days[sorted_days[pos - 1]]
        high_i = max(prev_idxs, key=lambda i: highs[i])
        low_i = min(prev_idxs, key=lambda i: lows[i])
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.PDH,
                price=_dec(highs[high_i]),
                session_date=day,
                established_at=timestamps[high_i],
                symbol=symbol,
            )
        )
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.PDL,
                price=_dec(lows[low_i]),
                session_date=day,
                established_at=timestamps[low_i],
                symbol=symbol,
            )
        )

    # ---- PWH / PWL from the previous ISO week present in the data -------
    weeks: Dict[int, List[int]] = {}
    for i, ts in enumerate(timestamps):
        weeks.setdefault(ts.isocalendar().week, []).append(i)
    sorted_weeks = sorted(weeks)
    for pos, week in enumerate(sorted_weeks):
        if pos == 0:
            continue
        prev_idxs = weeks[sorted_weeks[pos - 1]]
        high_i = max(prev_idxs, key=lambda i: highs[i])
        low_i = min(prev_idxs, key=lambda i: lows[i])
        anchor_ts = timestamps[weeks[week][0]]
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.PWH,
                price=_dec(highs[high_i]),
                session_date=anchor_ts.date(),
                established_at=timestamps[high_i],
                symbol=symbol,
            )
        )
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.PWL,
                price=_dec(lows[low_i]),
                session_date=anchor_ts.date(),
                established_at=timestamps[low_i],
                symbol=symbol,
            )
        )

    return levels


def latest_levels(levels: List[LiquidityLevel]) -> Dict[LiquidityLevelType, LiquidityLevel]:
    """Return the most recent level available for each level type.

    "Most recent" is judged by ``established_at`` so a level from the
    current session supersedes older ones of the same type.
    """
    result: Dict[LiquidityLevelType, LiquidityLevel] = {}
    for level in levels:
        current = result.get(level.level_type)
        if current is None or level.established_at >= current.established_at:
            result[level.level_type] = level
    return result


def levels_to_zones(levels: Sequence[LiquidityLevel]) -> List[MarketZone]:
    """Convert session liquidity levels to generic :class:`MarketZone` records.

    Each level becomes a zero-height zone (``price_low == price_high``) so
    downstream consumers can treat levels and imbalances uniformly.
    """
    zone_type_by_level = {
        LiquidityLevelType.ASIAN_HIGH: ZoneType.SESSION_HIGH_LOW,
        LiquidityLevelType.ASIAN_LOW: ZoneType.SESSION_HIGH_LOW,
        LiquidityLevelType.PDH: ZoneType.PREVIOUS_DAY_HIGH_LOW,
        LiquidityLevelType.PDL: ZoneType.PREVIOUS_DAY_HIGH_LOW,
        LiquidityLevelType.PWH: ZoneType.PREVIOUS_WEEK_HIGH_LOW,
        LiquidityLevelType.PWL: ZoneType.PREVIOUS_WEEK_HIGH_LOW,
    }
    return [
        MarketZone(
            zone_type=zone_type_by_level[level.level_type],
            scope=ZoneScope.EXTERNAL,
            index_start=-1,
            index_end=-1,
            timestamp_start=level.established_at,
            timestamp_end=level.established_at,
            price_low=level.price,
            price_high=level.price,
        )
        for level in levels
    ]


