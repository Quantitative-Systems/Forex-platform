"""Causal market-regime classification for multi-style allocation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence
import math

from forex_platform.core.domain import CurrencyPair, to_decimal
from forex_platform.strategy_engine.base import BarEvent


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RegimeSnapshot:
    symbol: str
    regime: MarketRegime
    confidence: float
    atr_pips: float
    efficiency_ratio: float
    bar_count: int


class MarketRegimeEngine:
    """Classify a symbol from closed bars only.

    The thresholds are intentionally conservative. UNKNOWN means insufficient
    data and must be treated as no-trade by allocation callers.
    """

    MIN_BARS = 30
    TREND_EFFICIENCY = 0.35
    HIGH_VOLATILITY_ATR_PIPS = 18.0
    LOW_VOLATILITY_ATR_PIPS = 5.0

    @classmethod
    def classify(cls, symbol: str, bars: Sequence[BarEvent]) -> RegimeSnapshot:
        pair = CurrencyPair.from_symbol(symbol)
        if len(bars) < cls.MIN_BARS:
            return RegimeSnapshot(pair.symbol, MarketRegime.UNKNOWN, 0.0, 0.0, 0.0, len(bars))

        window = list(bars[-max(cls.MIN_BARS, 60):])
        true_ranges = []
        for previous, current in zip(window, window[1:]):
            true_ranges.append(max(
                float(current.high - current.low),
                abs(float(current.high - previous.close)),
                abs(float(current.low - previous.close)),
            ))
        atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
        atr_pips = float(pair.to_pips(atr)) if atr > 0 else 0.0
        net_change = abs(float(window[-1].close - window[0].close))
        path = sum(abs(float(current.close - previous.close)) for previous, current in zip(window, window[1:]))
        efficiency = net_change / path if path > 0 else 0.0
        confidence = min(1.0, len(window) / 60.0)
        if atr_pips >= cls.HIGH_VOLATILITY_ATR_PIPS:
            regime = MarketRegime.HIGH_VOLATILITY
        elif atr_pips <= cls.LOW_VOLATILITY_ATR_PIPS:
            regime = MarketRegime.LOW_VOLATILITY
        elif efficiency >= cls.TREND_EFFICIENCY:
            regime = MarketRegime.TRENDING
        else:
            regime = MarketRegime.RANGING
        return RegimeSnapshot(pair.symbol, regime, confidence, atr_pips, efficiency, len(window))

    @staticmethod
    def allows_trading(regime: MarketRegime) -> bool:
        return regime not in {MarketRegime.UNKNOWN}
