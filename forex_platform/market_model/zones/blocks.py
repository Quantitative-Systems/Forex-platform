"""Order Block (OB) detection.

An Order Block is the last opposing candle preceding a displacement leg that
causes a confirmed BOS/CHoCH: for a bullish break, the last bearish candle
before the displacement leg; for a bearish break, the last bullish candle.

The OB zone spans the candle's full range (high/low). Scope follows the
broken swing's scope: a break of an EXTERNAL swing yields an EXTERNAL OB,
otherwise INTERNAL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Sequence

import polars as pl
from pydantic import BaseModel, ConfigDict

from forex_platform.market_model.contracts import (
    MarketZone,
    StructureBreak,
    SwingPoint,
    ZoneScope,
    ZoneType,
)


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class OBDirection(str, Enum):
    """Directional bias of the order block."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class OrderBlock(BaseModel):
    """An order-block zone anchored to the last opposing candle."""

    model_config = ConfigDict(frozen=True)

    index: int
    start_index: int
    timestamp: datetime
    direction: OBDirection
    top: Decimal
    bottom: Decimal
    midpoint: Decimal
    break_index: int
    scope: ZoneScope = ZoneScope.EXTERNAL


def detect_order_blocks(
    bars: pl.DataFrame,
    breaks: Sequence[StructureBreak],
    swings: Sequence[SwingPoint] | None = None,
    *,
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    timestamp_col: str = "timestamp",
    max_leg_bars: int = 5,
) -> List[OrderBlock]:
    """Detect order blocks from confirmed structure breaks.

    For every structural break the displacement leg immediately preceding the
    break is walked backwards (at most ``max_leg_bars`` candles). The first
    candle whose body opposes the break direction is the order block. The
    search is strictly causal: only candles before the break bar are
    considered, and a break with no opposing candle within the window is
    skipped rather than guessed.
    """
    if max_leg_bars < 1:
        raise ValueError(f"max_leg_bars must be >= 1, got {max_leg_bars}")
    for col in (open_col, high_col, low_col, close_col, timestamp_col):
        if col not in bars.columns:
            raise ValueError(f"required column {col!r} missing from bars")

    opens = bars[open_col].to_list()
    closes = bars[close_col].to_list()
    highs = bars[high_col].to_list()
    lows = bars[low_col].to_list()
    timestamps = bars[timestamp_col].to_list()
    n = len(closes)

    swing_scope = {s.index: s.scope for s in swings} if swings else {}

    blocks: List[OrderBlock] = []
    for brk in sorted(breaks, key=lambda b: b.index):
        if not (0 <= brk.index < n) or brk.index == 0:
            continue
        direction = OBDirection.BULLISH if brk.direction > 0 else OBDirection.BEARISH
        window_end = max(0, brk.index - max_leg_bars)
        ob_index: Optional[int] = None
        for j in range(brk.index - 1, window_end - 1, -1):
            bullish_candle = closes[j] > opens[j]
            bearish_candle = closes[j] < opens[j]
            opposing = (
                bearish_candle if direction == OBDirection.BULLISH else bullish_candle
            )
            if opposing:
                ob_index = j
                break
        if ob_index is None:
            continue

        top = _dec(highs[ob_index])
        bottom = _dec(lows[ob_index])
        blocks.append(
            OrderBlock(
                index=ob_index,
                start_index=ob_index,
                timestamp=timestamps[ob_index],
                direction=direction,
                top=top,
                bottom=bottom,
                midpoint=(top + bottom) / 2,
                break_index=brk.index,
                scope=swing_scope.get(brk.swing_index, ZoneScope.EXTERNAL),
            )
        )
    return blocks


def order_blocks_to_zones(blocks: Sequence[OrderBlock]) -> List[MarketZone]:
    """Convert order blocks to generic :class:`MarketZone` records."""
    return [
        MarketZone(
            zone_type=ZoneType.ORDER_BLOCK,
            scope=block.scope,
            index_start=block.start_index,
            index_end=block.index,
            timestamp_start=block.timestamp,
            timestamp_end=block.timestamp,
            price_low=block.bottom,
            price_high=block.top,
        )
        for block in blocks
    ]
