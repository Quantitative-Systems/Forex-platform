"""
Causal Supertrend Indicator.

Strictly causal (zero lookahead), Decimal-based, consuming a stream of BarEvents.
Supertrend(ATR=period, Factor=multiplier):

  basic_upper   = (high + low) / 2 + factor * atr
  basic_lower   = (high + low) / 2 - factor * atr
  final_upper   = min(basic_upper, prev_final_upper) if prev_close <= prev_final_upper else basic_upper
  final_lower   = max(basic_lower, prev_final_lower) if prev_close >= prev_final_lower else basic_lower
  trend         = 1 (bullish) if close > final_lower else -1 (bearish)
  supertrend    = final_lower when bullish, final_upper when bearish

All values computed only from bars up to and including the current closed bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from forex_platform.strategy_engine.base import BarEvent


@dataclass
class SupertrendResult:
    """Output of one Supertrend update."""
    trend: int  # 1 = bullish, -1 = bearish, 0 = uninitialized
    supertrend_value: Optional[Decimal]
    final_upper: Optional[Decimal]
    final_lower: Optional[Decimal]
    atr: Optional[Decimal]
    initialized: bool


class Supertrend:
    """
    Causal Supertrend indicator.

    Maintains internal state across BarEvent updates. Each call to update()
    processes exactly one closed bar and returns the Supertrend state after
    that bar. No future data is ever used.
    """

    def __init__(self, atr_period: int = 6, factor: Decimal | str | float = Decimal("5")):
        if atr_period < 2:
            raise ValueError("atr_period must be at least 2")
        self.atr_period = atr_period
        self.factor = Decimal(str(factor))

        self._highs: list[Decimal] = []
        self._lows: list[Decimal] = []
        self._closes: list[Decimal] = []

        self._final_upper: Optional[Decimal] = None
        self._final_lower: Optional[Decimal] = None
        self._trend: int = 0
        self._initialized: bool = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def trend(self) -> int:
        return self._trend

    @property
    def supertrend_value(self) -> Optional[Decimal]:
        if not self._initialized:
            return None
        return self._final_lower if self._trend == 1 else self._final_upper

    @property
    def final_upper(self) -> Optional[Decimal]:
        return self._final_upper

    @property
    def final_lower(self) -> Optional[Decimal]:
        return self._final_lower

    @property
    def atr(self) -> Optional[Decimal]:
        return self._atr()

    def _atr(self) -> Optional[Decimal]:
        if len(self._highs) < self.atr_period + 1:
            return None
        trs: list[Decimal] = []
        for i in range(1, len(self._highs)):
            tr = max(
                self._highs[i] - self._lows[i],
                abs(self._highs[i] - self._closes[i - 1]),
                abs(self._lows[i] - self._closes[i - 1]),
            )
            trs.append(tr)
        if len(trs) < self.atr_period:
            return None
        window = trs[-self.atr_period:]
        return sum(window, Decimal("0")) / Decimal(str(self.atr_period))

    def update(self, event: BarEvent) -> SupertrendResult:
        self._highs.append(event.high)
        self._lows.append(event.low)
        self._closes.append(event.close)

        atr = self._atr()
        if atr is None or atr == Decimal("0"):
            return SupertrendResult(0, None, None, None, None, False)

        hl2 = (self._highs[-1] + self._lows[-1]) / Decimal("2")
        basic_upper = hl2 + self.factor * atr
        basic_lower = hl2 - self.factor * atr

        prev_close = self._closes[-2]
        prev_final_upper = self._final_upper if self._initialized else basic_upper
        prev_final_lower = self._final_lower if self._initialized else basic_lower

        if prev_close <= prev_final_upper:
            final_upper = min(basic_upper, prev_final_upper)
        else:
            final_upper = basic_upper

        if prev_close >= prev_final_lower:
            final_lower = max(basic_lower, prev_final_lower)
        else:
            final_lower = basic_lower

        close = self._closes[-1]
        if self._initialized and self._trend == 1:
            trend = -1 if close < final_lower else 1
        elif self._initialized and self._trend == -1:
            trend = 1 if close > final_upper else -1
        else:
            trend = 1 if close > final_lower else -1

        self._trend = trend
        self._final_upper = final_upper
        self._final_lower = final_lower
        self._initialized = True

        st_value = final_lower if trend == 1 else final_upper

        return SupertrendResult(
            trend=trend,
            supertrend_value=st_value,
            final_upper=final_upper,
            final_lower=final_lower,
            atr=atr,
            initialized=True,
        )

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._final_upper = None
        self._final_lower = None
        self._trend = 0
        self._initialized = False
