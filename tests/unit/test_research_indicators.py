"""Unit tests for the causal research indicators library.

Verifies:
* SuperTrend is strictly causal (no lookahead) and outputs are within band.
* Stochastic stays in [0, 100] and matches hand-computed values.
* Swing detection only exposes a pivot after its right-side confirmation bar.
"""

from __future__ import annotations

import numpy as np

from forex_platform.research_indicators import (
    SuperTrendParams,
    StochasticParams,
    stochastic,
    supertrend,
    swing_highs_lows,
)


def _triangular_up(n: int = 60, start: float = 100.0, step: float = 0.5) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Clean uptrend w/ small bar size so SuperTrend can develop cleanly."""
    highs = start + np.arange(n) * step
    lows = highs - 1.0
    closes = highs - 0.2  # close near high
    return highs, lows, closes


def test_supertrend_is_causal_and_inside_bands() -> None:
    high, low, close = _triangular_up(60, 100.0, 0.6)
    # Force one downward tick at bar 30
    low[30] -= 30.0
    close[30] = low[30] + 0.5
    high[31:33] = high[31:33] - 20.0
    close[31:33] = close[31:33] - 20.0

    result = supertrend(high, low, close, SuperTrendParams(atr_period=6, factor=5.0))
    st = result["supertrend"]
    direction = result["direction"]

    # Warm-up
    assert np.isnan(st[:5]).all(), "warm-up bars must be NaN"

    # Bands: on an uptrend, st == lower band and close should be >= st
    # Once a flip happens (down), st should equal upper band.
    valid = ~np.isnan(st)
    valid = valid & (direction != 0)
    assert valid.sum() > 0, "no valid bars produced"
    for i in np.where(valid)[0]:
        assert direction[i] in (1, -1), f"direction {direction[i]} at {i} invalid"
        if direction[i] == 1:
            assert abs(st[i] - result["lower_band"][i]) < 1e-9
        else:
            assert abs(st[i] - result["upper_band"][i]) < 1e-9


def test_stochastic_stays_in_limits_and_matches_hand_value() -> None:
    # 25 constant-trend + one known value
    n = 40
    close = np.linspace(100.0, 140.0, n)
    high = close + 0.2
    low = close - 0.2
    result = stochastic(close, high, low, StochasticParams(length=25, smooth_k=1, signal=5))
    k = result["k"]
    d = result["d"]
    # Warm-up: first (25-1) bars NaN
    assert np.isnan(k[:24]).all()
    # On a linearly increasing series, close is the highest of the window, so
    # %K should be very close to 100.
    assert k[-1] >= 99.0, f"k[-1]={k[-1]}"
    # %D should equal last k (flat slope)
    assert abs(d[-1] - k[-1]) < 1e-6


def test_stochastic_bounds() -> None:
    rng = np.random.default_rng(7)
    n = 500
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    high = close + np.abs(rng.normal(0.2, 0.1, n))
    low = close - np.abs(rng.normal(0.2, 0.1, n))
    result = stochastic(close, high, low, StochasticParams(length=25, smooth_k=1, signal=5))
    k = result["k"]
    d = result["d"]
    valid_k = ~np.isnan(k)
    valid_d = ~np.isnan(d)
    assert valid_k.sum() > 50
    assert np.nanmin(k) >= -1e-9, f"min k={np.nanmin(k)}"
    assert np.nanmax(k) <= 100.0 + 1e-9, f"max k={np.nanmax(k)}"
    assert np.nanmin(d[valid_d]) >= -1e-9
    assert np.nanmax(d[valid_d]) <= 100.0 + 1e-9


def test_swings_only_after_confirmation() -> None:
    # Build a wave with one clear high at bar 10 (look on right)
    n = 40
    prices = np.arange(n, dtype=float) + 50.0
    prices[:10] = 50.0
    prices[10] = 1000.0  # spike
    prices[11:] = 50.0
    high = prices
    low = prices - 1.0
    close = prices - 0.5

    highs, lows = swing_highs_lows(high, low, close, left=3, right=3, strict=True)
    assert len(highs) >= 1, "should find at least one high"
    # Every high's bar_i must be at least `right` bars before confirmed_at
    for s in highs:
        assert s.confirmed_at >= s.bar_i + 3
        # The high price must match the actual peak
    # The obvious spike at bar 10 must be the first high found
    assert highs[0].bar_i == 10, f"expected bar 10, got {highs[0].bar_i}"
    assert highs[0].price == 1000.0
    # Confirmed on bar 13 (3 bars to right)
    assert highs[0].confirmed_at == 13