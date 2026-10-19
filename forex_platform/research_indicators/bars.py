"""Causal OHLCV array helpers and swing detection for research backtests.

Conventions
-----------
* All arrays are 1-D ``numpy`` float64 in chronological order of *opened* bars.
* A helper that receives "open-stamped" highs/lows returns per-bar results that
  are only meaningful once the bar being indexed has *closed*. The campaign
  layer is responsible for exposing a value to the strategy only on the bar
  after it has closed (i.e. the next source bar).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


def _as_float_array(df: pl.DataFrame, column: str) -> np.ndarray:
    if df.is_empty():
        return np.zeros(0, dtype=np.float64)
    return df[column].to_list() and np.asarray(df[column].to_list(), dtype=np.float64)


def closed_bars(df: pl.DataFrame) -> dict[str, np.ndarray]:
    """Return plain float arrays for the given chronological OHLCV frame.

    The returned arrays are indexed by opened-bar timestamp. A caller wishing to
    apply these to a strategy that consumes *completed* bars must offset the
    results by one bar; the campaign layer handles that offset.
    """
    if df.is_empty():
        return {
            "timestamp": np.zeros(0, dtype="datetime64[ns]"),
            "open": np.zeros(0), "high": np.zeros(0),
            "low": np.zeros(0), "close": np.zeros(0),
            "volume": np.zeros(0), "spread": np.zeros(0),
        }
    return {
        "timestamp": df["timestamp"].to_numpy(),
        "open": np.asarray(df["open"].to_list(), dtype=np.float64),
        "high": np.asarray(df["high"].to_list(), dtype=np.float64),
        "low": np.asarray(df["low"].to_list(), dtype=np.float64),
        "close": np.asarray(df["close"].to_list(), dtype=np.float64),
        "volume": np.asarray(df["volume"].to_list(), dtype=np.float64),
        "spread": np.asarray(df.get("spread", pl.Series([1.0]*df.height)).to_list(), dtype=np.float64),
    }


def true_range(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True-range per opened bar. ``tr[0] = high-low`` since no prior close exists."""
    n = high.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    tr = np.empty(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    prev_close = close[:-1]
    tr[1:] = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - prev_close),
        np.abs(low[1:] - prev_close),
    ])
    return tr


def rolling_high_max(low_or_high: np.ndarray, period: int) -> np.ndarray:
    """Rolling maximum of the last ``period`` entries ending at the same index.

    Index ``i``'s value uses entries ``i-period+1 .. i``. The first ``period-1``
    outputs are ``NaN`` so callers can distinguish "not yet warm".
    """
    n = low_or_high.shape[0]
    if n == 0 or period <= 0:
        return np.full(n, np.nan, dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    # Cumulative max with a sliding-window trick is O(n) but easier to code
    # O(n*period) for a research run; we pick O(n) via a deque-free approach:
    # use a monotonic stack for max-in-window.
    from collections import deque
    dq: "deque[int]" = deque()
    for i, v in enumerate(low_or_high):
        while dq and low_or_high[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        while dq and dq[0] <= i - period:
            dq.popleft()
        if i >= period - 1:
            out[i] = low_or_high[dq[0]]
    return out


def rolling_low_min(low_or_high: np.ndarray, period: int) -> np.ndarray:
    """Rolling minimum of the last ``period`` entries (mirror of rolling_high_max)."""
    return -rolling_high_max(-low_or_high, period)


@dataclass(frozen=True)
class SwingLevel:
    """A confirmed swing level by bar index.

    ``bar_i`` is the index of the extreme bar (the bar containing the high/low).
    ``confirmed_at`` is the index of the bar whose close confirms the pivot
    (i.e. the first bar to the right of the pivot with a strict new extreme).
    """
    bar_i: int
    price: float
    kind: str  # "high" | "low"
    confirmed_at: int


def swing_highs_lows(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    left: int = 3,
    right: int = 3,
    strict: bool = True,
) -> tuple[list[SwingLevel], list[SwingLevel]]:
    """Causal fractal swing detection.

    A *high* swing at bar ``i`` is confirmed when
      - ``high[i]`` is strictly greater than the ``left`` bars to the left, and
      - ``high[i]`` is strictly greater than the ``right`` bars to the right.
    The pivot becomes visible to the strategy on bar ``i + right`` (after that
    bar's close). A *low* swing is the mirror.

    Returning both lists sorted by ``confirmed_at`` lets a streaming strategy
    attach the newly confirmed pivot to the current bar's decision step.
    """
    n = high.shape[0]
    highs: list[SwingLevel] = []
    lows: list[SwingLevel] = []
    if n < left + right + 1:
        return highs, lows

    is_strict = strict
    for i in range(left, n - right):
        window_h = high[i - left : i + right + 1]
        window_l = low[i - left : i + right + 1]
        center_h = high[i]
        center_l = low[i]
        # Skip tie at the boundary to avoid double-counting flat plateaus.
        if is_strict:
            left_older = window_h[:left]
            right_newer = window_h[left + 1:]
            left_lower = window_l[:left]
            right_higher = window_l[left + 1:]
        else:
            left_older = window_h[:left]
            right_newer = window_h[left + 1:]
            left_lower = window_l[:left]
            right_higher = window_l[left + 1:]

        is_high = bool(
            (center_h > left_older).all() if left_older.size else True
        ) and bool(
            (center_h > right_newer).all() if right_newer.size else True
        )
        is_low = bool(
            (center_l < left_lower).all() if left_lower.size else True
        ) and bool(
            (center_l < right_higher).all() if right_higher.size else True
        )
        if is_high:
            highs.append(SwingLevel(i, float(center_h), "high", i + right))
        if is_low:
            lows.append(SwingLevel(i, float(center_l), "low", i + right))

    highs.sort(key=lambda s: s.confirmed_at)
    lows.sort(key=lambda s: s.confirmed_at)
    return highs, lows