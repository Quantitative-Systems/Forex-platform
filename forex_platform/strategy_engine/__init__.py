"""
Multi-Horizon Strategy Engine Plugins Catalog.
"""

from forex_platform.strategy_engine.base import BarEvent, BaseStrategy
from forex_platform.strategy_engine.macro_carry import MacroCarryStrategy
from forex_platform.strategy_engine.scalping import AsianRangeFadeScalper
from forex_platform.strategy_engine.session_breakout import LondonSessionBreakout
from forex_platform.strategy_engine.stat_arb import TriangularStatisticalArbitrage
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy

__all__ = [
    "AsianRangeFadeScalper",
    "BarEvent",
    "BaseStrategy",
    "LondonSessionBreakout",
    "MacroCarryStrategy",
    "TrendContinuationStrategy",
    "TriangularStatisticalArbitrage",
]
