"""Structural trend state and invalidation logic.

The structural trend is read from the most recent break decision; when no
break exists yet, the last two highs and lows of the swing sequence give a
deterministic HH/HL vs LH/LL verdict (0 = neutral).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence, Union

from forex_platform.market_model.contracts import (
    StructureBreak,
    SwingPoint,
    SwingType,
)


def _dec(value: Union[float, Decimal]) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def get_trend_direction(
    swings: Sequence[SwingPoint],
    breaks: Sequence[StructureBreak],
) -> int:
    """Return +1 (bullish), -1 (bearish) or 0 (neutral) structural trend."""
    if breaks:
        return breaks[-1].direction

    ordered = sorted(swings, key=lambda s: s.index)
    highs = [s.price for s in ordered if s.swing_type == SwingType.HIGH]
    lows = [s.price for s in ordered if s.swing_type == SwingType.LOW]
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            return 1
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            return -1
    return 0


def is_trend_invalidated(
    swings: Sequence[SwingPoint],
    breaks: Sequence[StructureBreak],
    current_price: Union[float, Decimal],
) -> bool:
    """True when price closes beyond the trend's protected invalidation anchor.

    Bullish trend: invalidated by a close below the protected (fallback:
    latest) swing low. Bearish trend: invalidated by a close above the
    protected (fallback: latest) swing high. Neutral trend is never
    invalidated (there is no thesis to kill).
    """
    trend = get_trend_direction(swings, breaks)
    if trend == 0:
        return False

    price = _dec(current_price)
    ordered = sorted(swings, key=lambda s: s.index)
    if trend > 0:
        lows = [s for s in ordered if s.swing_type == SwingType.LOW]
        protected = [s for s in lows if s.is_protected]
        anchor = protected[-1] if protected else (lows[-1] if lows else None)
        if anchor is None:
            return False
        return price < anchor.price

    highs = [s for s in ordered if s.swing_type == SwingType.HIGH]
    protected = [s for s in highs if s.is_protected]
    anchor = protected[-1] if protected else (highs[-1] if highs else None)
    if anchor is None:
        return False
    return price > anchor.price