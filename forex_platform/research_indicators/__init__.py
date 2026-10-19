"""Research-only causal indicator library.

Every function in this package is computed from a stream of *closed* bars only.
A value returned for index ``i`` depends exclusively on bars ``0..i``, so feeding
bars in chronological order yields strictly causal (no-lookahead) series that can
be aligned bar-for-bar into the event-driven backtester.

This package is intentionally decoupled from the production ``Decimal`` domain
types: it operates on plain float OHLC arrays backed by ``numpy``/``polars`` for
speed, which is appropriate for research backtests.
"""

from __future__ import annotations

from .bars import (
    SwingLevel,
    closed_bars,
    rolling_high_max,
    rolling_low_min,
    swing_highs_lows,
    true_range,
)
from .supertrend import SuperTrendParams, supertrend
from .stochastic import StochasticParams, stochastic

__all__ = [
    "SwingLevel",
    "closed_bars",
    "rolling_high_max",
    "rolling_low_min",
    "swing_highs_lows",
    "true_range",
    "SuperTrendParams",
    "supertrend",
    "StochasticParams",
    "stochastic",
]