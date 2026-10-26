from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent
from forex_platform.strategy_engine.supertrend_stochastic import SuperTrendStochasticStructuralStrategy


def bar(i: int, close: str, *, low: str | None = None, high: str | None = None, context=None) -> BarEvent:
    c = Decimal(close)
    return BarEvent(
        symbol="GBPUSD", timeframe=Timeframe.M5,
        timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(minutes=5 * i),
        open=c, high=Decimal(high) if high else c + Decimal("0.0005"),
        low=Decimal(low) if low else c - Decimal("0.0005"), close=c,
        volume=Decimal("10"), spread=Decimal("1.0"), htf_data=context or {},
    )


def test_candidate_defaults_and_indicator_parameters():
    strategy = SuperTrendStochasticStructuralStrategy()
    assert strategy.symbols == ["GBPUSD"]
    assert strategy.timeframes == [Timeframe.M5]
    assert strategy.supertrend_atr_period == 6
    assert strategy.supertrend_multiplier == Decimal("5")
    assert strategy.stochastic_period == 25
    assert strategy.stochastic_k_smoothing == 5
    assert strategy.stochastic_d_smoothing == 3
    assert strategy.minimum_net_reward_risk == Decimal("4")


@pytest.mark.parametrize("kwargs", [
    {"supertrend_atr_period": 1}, {"stochastic_period": 1},
    {"oversold": 80, "overbought": 20}, {"minimum_net_reward_risk": "0.5"},
    {"lot_size": 0},
])
def test_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        SuperTrendStochasticStructuralStrategy(**kwargs)


def test_indicators_are_warmup_safe_and_do_not_rewrite_past_values():
    strategy = SuperTrendStochasticStructuralStrategy()
    bars = [bar(i, f"{1.0800 + i * 0.0001:.4f}") for i in range(20)]
    st = strategy.supertrend_series(bars, period=6, multiplier=5)
    k, d = strategy.stochastic_series(bars)
    assert len(st) == len(bars)
    assert all(direction is None for direction, _ in st[:6])
    assert all(value is None for value in k[:24])
    assert all(value is None for value in d)
    extended = strategy.supertrend_series(bars + [bar(20, "1.0820")], period=6, multiplier=5)
    assert extended[:len(st)] == st


def test_fails_closed_without_causal_htf_mtf_context():
    strategy = SuperTrendStochasticStructuralStrategy()
    for i in range(70):
        result = strategy.on_bar(bar(i, f"{1.0800 + (i % 9) * 0.0001:.4f}"))
    assert result == []


def test_rejects_wrong_timeframe():
    strategy = SuperTrendStochasticStructuralStrategy()
    event = BarEvent(
        symbol="GBPUSD", timeframe=Timeframe.M15,
        timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc),
        open=Decimal("1.0800"), high=Decimal("1.0810"),
        low=Decimal("1.0790"), close=Decimal("1.0800"), volume=Decimal("10"),
    )
    with pytest.raises(ValueError, match="requires completed 5m bars"):
        strategy.on_bar(event)


def test_invalid_zone_fails_closed():
    strategy = SuperTrendStochasticStructuralStrategy()
    context = {
        "htf_trend": "UP", "mtf_trend": "UP", "structure_fresh": True,
        "mtf_zone_low": "1.0790", "mtf_zone_high": "1.0780",
        "ltf_swing_low": "1.0770", "htf_target": "1.1000",
    }
    for i in range(70):
        result = strategy.on_bar(bar(i, f"{1.0800 + (i % 9) * 0.0001:.4f}", context=context))
    assert result == []
