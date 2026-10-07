"""Market-structure break detection.

BOS continues trend; CHOCH flips; MSS synonymous with CHOCH here.

A break requires a candle **body close** beyond a confirmed swing extreme;
a wick that merely penetrates the level is a sweep/test and does not
restructure price.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Sequence

import polars as pl

from forex_platform.market_model.contracts import (
    BreakType,
    StructureBreak,
    SwingPoint,
    SwingType,
)


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def detect_structure_breaks(
    bars: pl.DataFrame,
    swings: Sequence[SwingPoint],
    *,
    close_col: str = "close",
    timestamp_col: str = "timestamp",
    confirmation_bars: int = 0,
) -> List[StructureBreak]:
    """Detect BOS/CHoCH body-close breaks beyond confirmed swing extremes.

    Causality: a swing at index ``i`` only becomes actionable at bar
    ``i + confirmation_bars``. Each swing can break at most once and breaks
    are emitted in strict bar order. The first break (no established trend)
    and every break in the direction of the established trend are ``BOS``;
    a break against the established trend is ``CHOCH``.
    """
    if confirmation_bars < 0:
        raise ValueError(f"confirmation_bars must be >= 0, got {confirmation_bars}")
    if close_col not in bars.columns:
        raise ValueError(f"required column {close_col!r} missing from bars")
    if timestamp_col not in bars.columns:
        raise ValueError(f"required column {timestamp_col!r} missing from bars")

    closes = bars[close_col].to_list()
    timestamps = bars[timestamp_col].to_list()
    n = len(closes)

    pending: List[SwingPoint] = sorted(
        (s for s in swings if 0 <= s.index < n), key=lambda s: s.index
    )
    breaks: List[StructureBreak] = []
    trend = 0

    for i in range(n):
        close = _dec(closes[i])
        for swing in list(pending):
            if swing.index >= i:
                break  # swings are index-sorted: no later swing is eligible
            if i < swing.index + confirmation_bars:
                break  # not yet causally actionable (monotone in index)

            direction = 0
            if swing.swing_type == SwingType.HIGH and close > swing.price:
                direction = 1
            elif swing.swing_type == SwingType.LOW and close < swing.price:
                direction = -1
            if direction == 0:
                continue

            break_type = (
                BreakType.BOS
                if (trend == 0 or direction == trend)
                else BreakType.CHOCH
            )
            breaks.append(
                StructureBreak(
                    index=i,
                    timestamp=timestamps[i],
                    break_type=break_type,
                    swing_index=swing.index,
                    swing_price=swing.price,
                    direction=direction,
                    body_close=close,
                )
            )
            trend = direction
            pending.remove(swing)

    return breaks