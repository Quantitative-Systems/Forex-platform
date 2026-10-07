"""Core Pydantic/Polars schemas for the market model.

The market model is strategy-agnostic: these contracts describe WHAT price
is doing (structure), WHERE it is located (zones) and WHAT STAGE the move is
in (phase) for a single timeframe. All models are frozen (immutable) so a
captured :class:`TimeframeState` snapshot can never be mutated after the fact.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

import polars as pl
from pydantic import BaseModel, ConfigDict


class BreakType(str, Enum):
    """Type of structural break."""

    BOS = "bos"
    CHOCH = "choch"
    MSS = "mss"


class MarketPhase(str, Enum):
    """Market phase: pullback or continuation."""

    PULLBACK = "pullback"
    CONTINUATION = "continuation"


class SwingScope(str, Enum):
    """Swing scope: external (macro) or internal (subordinate)."""

    EXTERNAL = "external"
    INTERNAL = "internal"


class SwingType(str, Enum):
    """Swing point type: high or low."""

    HIGH = "high"
    LOW = "low"


class ZoneScope(str, Enum):
    """Scope of a zone: external (anchored to HTF) or internal (anchored to LTF pullback)."""

    EXTERNAL = "external"
    INTERNAL = "internal"


class ZoneType(str, Enum):
    """Type of zone."""

    FVG = "fvg"
    ORDER_BLOCK = "order_block"
    EQUAL_HIGH_LOW = "equal_high_low"
    SESSION_HIGH_LOW = "session_high_low"
    PREVIOUS_DAY_HIGH_LOW = "previous_day_high_low"
    PREVIOUS_WEEK_HIGH_LOW = "previous_week_high_low"


class SwingPoint(BaseModel):
    """A detected swing point (pivot)."""

    model_config = ConfigDict(frozen=True)

    index: int
    timestamp: datetime
    price: Decimal
    swing_type: SwingType
    scope: SwingScope = SwingScope.EXTERNAL
    is_protected: bool = False
    is_weak: bool = False
    strength: Optional[Decimal] = None


class StructureBreak(BaseModel):
    """A detected structure break (BOS/CHOCH/MSS)."""

    model_config = ConfigDict(frozen=True)

    index: int
    timestamp: datetime
    break_type: BreakType
    swing_index: int
    swing_price: Decimal
    direction: int
    body_close: Decimal


class MarketZone(BaseModel):
    """A detected market zone (e.g., FVG, OB, etc.)."""

    model_config = ConfigDict(frozen=True)

    zone_type: ZoneType
    scope: ZoneScope
    index_start: int
    index_end: int
    timestamp_start: datetime
    timestamp_end: datetime
    price_low: Decimal
    price_high: Decimal
    strength: Optional[Decimal] = None


class TimeframeState(BaseModel):
    """The complete market state for a single timeframe."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    timeframe: str
    symbol: str
    dataframe: pl.DataFrame
    swings: list[SwingPoint]
    breaks: list[StructureBreak]
    zones: list[MarketZone]
    phase: MarketPhase = MarketPhase.PULLBACK
    external_trend_direction: Optional[int] = None
    internal_trend_direction: Optional[int] = None
    protected_swing_high: Optional[SwingPoint] = None
    protected_swing_low: Optional[SwingPoint] = None
    weak_swing_high: Optional[SwingPoint] = None
    weak_swing_low: Optional[SwingPoint] = None


class MarketState(BaseModel):
    """Aggregated market state across multiple timeframes (for a given symbol)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframes: dict[str, TimeframeState]