"""Causal SuperTrend indicator for research backtests.

Implements the classical trailing-band SuperTrend:

  mid        = (high + low) / 2
  upper      = mid + factor * ATR
  lower      = mid - factor * ATR

with the standard persistence rule that keeps the last band until the close
crosses through it, at which point the trend flips.

ATR is smoothed with Wilder's moving average (RMA) over ``atr_period`` true
ranges. Every returned series is causal: value at index ``i`` depends only on
bars ``0..i``. Bars before ``atr_period`` are ``NaN`` / ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bars import true_range


@dataclass(frozen=True)
class SuperTrendParams:
    atr_period: int = 6
    factor: float = 5.0
    atr_smooth: str = "wilder"  # "wilder" (RMA) or "sma"


def _wilder_atr(tr: np.ndarray, period: int) -> np.ndarray:
    """Wilder's ATR (moving-avg of true range). Causal. NaN until warm."""
    n = tr.shape[0]
    if n == 0 or period <= 0:
        return np.full(n, np.nan, dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    first = float(tr[:period].mean())
    out[period - 1] = first
    alpha = 1.0 / period
    prev = first
    for i in range(period, n):
        prev = (prev * (1.0 - alpha)) + (tr[i] * alpha)
        out[i] = prev
    return out


def _sma_atr(tr: np.ndarray, period: int) -> np.ndarray:
    n = tr.shape[0]
    if n == 0 or period <= 0:
        return np.full(n, np.nan, dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    csum = np.cumsum(tr)
    out[period - 1] = csum[period - 1] / period
    out[period:] = (csum[period:] - csum[:-period]) / period
    return out


def supertrend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    params: SuperTrendParams | None = None,
) -> dict[str, np.ndarray]:
    n = high.shape[0]
    params = params or SuperTrendParams()
    empty = {
        "supertrend": np.full(n, np.nan),
        "direction": np.zeros(n, dtype=np.int32),
        "atr": np.full(n, np.nan),
        "upper_band": np.full(n, np.nan),
        "lower_band": np.full(n, np.nan),
    }
    if n == 0:
        return empty

    tr = true_range(_zeros_like(high), high, low, close)
    atr = _wilder_atr(tr, params.atr_period) if params.atr_smooth == "wilder" else _sma_atr(tr, params.atr_period)

    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    trend_line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=np.int32)  # +1 bullish, -1 bearish, 0 undetermined

    for i in range(n):
        if np.isnan(atr[i]):
            continue
        mid = (high[i] + low[i]) / 2.0
        upper[i] = mid + params.factor * atr[i]
        lower[i] = mid - params.factor * atr[i]

        if i < 1 or np.isnan(trend_line[i - 1]):
            # Seed: close above mid => bullish band = lower, else bearish band = upper.
            if close[i] >= mid:
                final_upper[i] = upper[i]
                final_lower[i] = lower[i]
                trend_line[i] = lower[i]
                direction[i] = 1
            else:
                final_upper[i] = upper[i]
                final_lower[i] = lower[i]
                trend_line[i] = upper[i]
                direction[i] = -1
            continue

        prev_fu = final_upper[i - 1]
        prev_fl = final_lower[i - 1]
        prev_close = close[i - 1]

        # Persistence: keep bands until the close breaks through them.
        fu = upper[i] if (upper[i] < prev_fu or prev_close > prev_fu) else prev_fu
        fl = lower[i] if (lower[i] > prev_fl or prev_close < prev_fl) else prev_fl
        final_upper[i] = fu
        final_lower[i] = fl

        prev_trend = trend_line[i - 1]
        if prev_close > prev_trend:
            # Was bullish (or moving up): stay bullish unless close drops below lower band.
            if close[i] > fl:
                trend_line[i] = fl
                direction[i] = 1
            else:
                # Flip bearish.
                trend_line[i] = fu
                direction[i] = -1
        else:
            # Was bearish (or moving down): stay bearish unless close rises above upper band.
            if close[i] < fu:
                trend_line[i] = fu
                direction[i] = -1
            else:
                # Flip bullish.
                trend_line[i] = fl
                direction[i] = 1

    return {
        "supertrend": trend_line,
        "direction": direction,
        "atr": atr,
        "upper_band": final_upper,
        "lower_band": final_lower,
    }


def _zeros_like(arr: np.ndarray) -> np.ndarray:
    return np.zeros_like(arr, dtype=np.float64)