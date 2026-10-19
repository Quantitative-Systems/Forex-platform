"""Causal Stochastic Oscillator for research backtests.

Classic stochastic:
  %K  = 100 * (close - lowest_low[n]) / (highest_high[n] - lowest_low[n])
  %D  = SMA(%K, signal_period)

All values at index ``i`` depend only on bars ``0..i``. Values before
``length`` bars are ``NaN``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bars import rolling_high_max, rolling_low_min


@dataclass(frozen=True)
class StochasticParams:
    length: int = 25          # lookback window for high/low
    smooth_k: int = 1         # smoothing on %K (1 = raw)
    signal: int = 5           # %D signal line period


def stochastic(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    params: StochasticParams | None = None,
) -> dict[str, np.ndarray]:
    """Return causal stochastic arrays: ``k``, ``d``, ``osc`` (k-d)."""
    n = close.shape[0]
    params = params or StochasticParams()
    zero = np.zeros(n, dtype=np.float64)
    empty = {"k": zero, "d": zero, "osc": zero}
    if n == 0:
        return empty

    hh = rolling_high_max(high, params.length)
    ll = rolling_low_min(low, params.length)

    # %K raw
    k_raw = np.full(n, np.nan, dtype=np.float64)
    for i in range(n):
        if np.isnan(hh[i]) or np.isnan(ll[i]):
            continue
        denom = hh[i] - ll[i]
        if denom <= 0:
            k_raw[i] = 0.0
        else:
            k_raw[i] = 100.0 * (close[i] - ll[i]) / denom

    # Smooth %K if requested
    if params.smooth_k > 1:
        k = _rolling_smooth(k_raw, params.smooth_k)
    else:
        k = k_raw

    # %D = SMA of %K
    d = _sma_nan(k, params.signal)

    # Histogram / cross detection helper: k - d
    osc = np.where(np.isnan(k) | np.isnan(d), np.nan, k - d)

    return {"k": k, "d": d, "osc": osc}


def _rolling_smooth(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling mean, passing NaN for positions with fewer than ``window`` valid inputs."""
    n = arr.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if window <= 0 or n < window:
        return out
    valid_mask = ~np.isnan(arr)
    csum = np.cumsum(np.where(valid_mask, arr, 0.0))
    ccount = np.cumsum(valid_mask.astype(np.int64))
    for i in range(window - 1, n):
        s = csum[i] - csum[i - window]
        cnt = ccount[i] - ccount[i - window]
        if cnt >= window:  # full window of valid values
            out[i] = s / window
        else:
            out[i] = np.nan
    return out


def _sma_nan(arr: np.ndarray, period: int) -> np.ndarray:
    """SMA that produces NaN when the window contains any NaN."""
    n = arr.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)
    if period <= 0 or n < period:
        return out
    valid = ~np.isnan(arr)
    csum = np.cumsum(np.where(valid, arr, 0.0))
    cvalid = np.cumsum(valid.astype(np.int64))
    for i in range(period - 1, n):
        if cvalid[i] - (cvalid[i - period] if i >= period else 0) >= period:
            out[i] = (csum[i] - (csum[i - period] if i >= period else 0.0)) / period
    return out