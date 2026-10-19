"""
Causal Stochastic Oscillator Indicator.

Strictly causal (zero lookahead), Decimal-based, consuming a stream of BarEvents.
Stochastic(%K_period, %K_smoothing, %D_smoothing):

  raw_k     = (close - lowest_low) / (highest_high - lowest_low) * 100
             over last %K_period bars
  %K        = SMA(raw_k, %K_smoothing)
  %D        = SMA(%K, %D_smoothing)

All values computed only from bars up to and including the current closed bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from forex_platform.strategy_engine.base import BarEvent


@dataclass
class StochasticResult:
    """Output of one Stochastic update."""
    k: Optional[Decimal]  # smoothed %K
    d: Optional[Decimal]  # smoothed %D
    raw_k: Optional[Decimal]  # unsmoothed %K for current bar
    initialized: bool


class Stochastic:
    """
    Causal Stochastic Oscillator.

    Maintains internal state across BarEvent updates. Each call to update()
    processes exactly one closed bar and returns the Stochastic state after
    that bar. No future data is ever used.
    """

    def __init__(
        self,
        k_period: int = 25,
        k_smoothing: int = 5,
        d_smoothing: int = 3,
    ):
        if k_period < 2:
            raise ValueError("k_period must be at least 2")
        if k_smoothing < 1:
            raise ValueError("k_smoothing must be at least 1")
        if d_smoothing < 1:
            raise ValueError("d_smoothing must be at least 1")
        self.k_period = k_period
        self.k_smoothing = k_smoothing
        self.d_smoothing = d_smoothing

        self._highs: list[Decimal] = []
        self._lows: list[Decimal] = []
        self._closes: list[Decimal] = []
        self._raw_k_history: list[Decimal] = []
        self._k_smooth_history: list[Decimal] = []

        self._initialized: bool = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def k(self) -> Optional[Decimal]:
        if not self._initialized or not self._k_smooth_history:
            return None
        return self._k_smooth_history[-1]

    @property
    def d(self) -> Optional[Decimal]:
        if not self._initialized or len(self._k_smooth_history) < self.d_smoothing:
            return None
        window = self._k_smooth_history[-self.d_smoothing:]
        return sum(window, Decimal("0")) / Decimal(str(self.d_smoothing))

    @property
    def raw_k(self) -> Optional[Decimal]:
        if not self._raw_k_history:
            return None
        return self._raw_k_history[-1]

    def _compute_raw_k(self) -> Optional[Decimal]:
        if len(self._highs) < self.k_period:
            return None
        lowest_low = min(self._lows[-self.k_period:])
        highest_high = max(self._highs[-self.k_period:])
        close = self._closes[-1]
        if highest_high == lowest_low:
            return Decimal("50")
        return (close - lowest_low) / (highest_high - lowest_low) * Decimal("100")

    def update(self, event: BarEvent) -> StochasticResult:
        self._highs.append(event.high)
        self._lows.append(event.low)
        self._closes.append(event.close)

        raw_k = self._compute_raw_k()
        if raw_k is None:
            return StochasticResult(None, None, None, False)

        self._raw_k_history.append(raw_k)
        if len(self._raw_k_history) > 500:
            self._raw_k_history.pop(0)

        # Smoothed %K = SMA of raw %K over k_smoothing periods
        if len(self._raw_k_history) < self.k_smoothing:
            return StochasticResult(None, None, raw_k, False)

        k_smooth = sum(
            self._raw_k_history[-self.k_smoothing:], Decimal("0")
        ) / Decimal(str(self.k_smoothing))

        self._k_smooth_history.append(k_smooth)
        if len(self._k_smooth_history) > 500:
            self._k_smooth_history.pop(0)

        # %D = SMA of smoothed %K over d_smoothing periods
        if len(self._k_smooth_history) < self.d_smoothing:
            return StochasticResult(k_smooth, None, raw_k, False)

        d_value = sum(
            self._k_smooth_history[-self.d_smoothing:], Decimal("0")
        ) / Decimal(str(self.d_smoothing))

        self._initialized = True
        return StochasticResult(
            k=k_smooth,
            d=d_value,
            raw_k=raw_k,
            initialized=True,
        )

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._raw_k_history.clear()
        self._k_smooth_history.clear()
        self._initialized = False
