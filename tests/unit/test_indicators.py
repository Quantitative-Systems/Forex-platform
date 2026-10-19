"""
Unit tests for causal indicators: Supertrend and Stochastic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_platform.indicators.stochastic import Stochastic
from forex_platform.indicators.supertrend import Supertrend
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent


def _make_event(ts, open_, high, low, close):
    return BarEvent(
        symbol="EURUSD", timeframe=Timeframe.H1, timestamp=ts,
        open=Decimal(str(open_)), high=Decimal(str(high)),
        low=Decimal(str(low)), close=Decimal(str(close)),
        volume=Decimal("1000"), spread=Decimal("1.0"),
    )


def test_supertrend_requires_enough_bars():
    st = Supertrend(atr_period=6, factor=Decimal("5"))
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        ev = _make_event(base + timedelta(hours=i), 1.1, 1.101, 1.099, 1.1005)
        res = st.update(ev)
        assert not res.initialized


def test_supertrend_bullish_trend():
    st = Supertrend(atr_period=6, factor=Decimal("3"))
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    res = None
    for i in range(20):
        close = 1.10 + i * 0.001
        ev = _make_event(base + timedelta(hours=i), close - 0.0005, close + 0.001, close - 0.001, close)
        res = st.update(ev)
    assert res.initialized
    assert res.trend == 1
    assert res.supertrend_value == res.final_lower


def test_supertrend_bearish_trend():
    st = Supertrend(atr_period=6, factor=Decimal("3"))
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    res = None
    for i in range(20):
        close = 1.10 - i * 0.001
        ev = _make_event(base + timedelta(hours=i), close + 0.0005, close + 0.001, close - 0.001, close)
        res = st.update(ev)
    assert res.initialized
    assert res.trend == -1
    assert res.supertrend_value == res.final_upper


def test_supertrend_trend_flip():
    st = Supertrend(atr_period=6, factor=Decimal("3"))
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    res = None
    for i in range(15):
        close = 1.10 + i * 0.001
        ev = _make_event(base + timedelta(hours=i), close - 0.0005, close + 0.001, close - 0.001, close)
        res = st.update(ev)
    assert res.trend == 1
    crash_close = float(res.final_lower) - 0.005
    ev = _make_event(base + timedelta(hours=15), crash_close, crash_close + 0.001, crash_close - 0.001, crash_close)
    res = st.update(ev)
    assert res.trend == -1


def test_supertrend_reset():
    st = Supertrend(atr_period=6, factor=Decimal("3"))
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(10):
        ev = _make_event(base + timedelta(hours=i), 1.1, 1.101, 1.099, 1.1005)
        st.update(ev)
    assert st.initialized
    st.reset()
    assert not st.initialized

def test_stochastic_requires_enough_bars():
    st = Stochastic(k_period=25, k_smoothing=5, d_smoothing=3)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(40):
        close = 1.10 + (i % 5) * 0.0001
        ev = _make_event(base + timedelta(minutes=i), close, close + 0.0002, close - 0.0002, close)
        res = st.update(ev)
    assert st.initialized


def test_stochastic_range_0_to_100():
    st = Stochastic(k_period=10, k_smoothing=3, d_smoothing=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(40):
        close = 1.10 + (i % 3) * 0.0005
        ev = _make_event(base + timedelta(minutes=i), close, close + 0.0003, close - 0.0003, close)
        res = st.update(ev)
        if res.k is not None:
            assert Decimal("0") <= res.k <= Decimal("100")
        if res.d is not None:
            assert Decimal("0") <= res.d <= Decimal("100")


def test_stochastic_raw_k_at_extreme():
    st = Stochastic(k_period=10, k_smoothing=1, d_smoothing=1)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(15):
        close = 1.10 + i * 0.001
        ev = _make_event(base + timedelta(minutes=i), close, close, close - 0.0001, close)
        res = st.update(ev)
    assert st.raw_k is not None
    assert st.raw_k >= Decimal("95")


def test_stochastic_reset():
    st = Stochastic(k_period=10, k_smoothing=3, d_smoothing=2)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(30):
        ev = _make_event(base + timedelta(minutes=i), 1.1, 1.101, 1.099, 1.1005)
        st.update(ev)
    assert st.initialized
    st.reset()
    assert not st.initialized
    assert st.k is None
    assert st.d is None