"""
Unit tests for Strategy Engine Plugins:
Asian Range Fade Scalping, London Session Breakout, Dual EMA Trend Continuation,
Macro Carry, and Triangular Statistical Arbitrage.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest

from forex_platform.core.domain import OrderSide, OrderType
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent
from forex_platform.strategy_engine.macro_carry import MacroCarryStrategy
from forex_platform.strategy_engine.scalping import AsianRangeFadeScalper
from forex_platform.strategy_engine.session_breakout import LondonSessionBreakout
from forex_platform.strategy_engine.stat_arb import TriangularStatisticalArbitrage
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy


class TestAsianRangeFadeScalper:
    """Test Asian session range fade scalper."""

    def test_session_hour_gating(self):
        scalper = AsianRangeFadeScalper(period=5)
        # 14:00 UTC is London + NY overlap -> MUST NOT TRADE
        dt_london = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        bar = BarEvent(
            symbol="EURUSD",
            timeframe=Timeframe.M5,
            timestamp=dt_london,
            open=Decimal("1.0800"),
            high=Decimal("1.0810"),
            low=Decimal("1.0790"),
            close=Decimal("1.0805"),
            volume=Decimal("100"),
        )
        intents = scalper.on_bar(bar)
        assert len(intents) == 0

    def test_mean_reversion_signal_in_asian_session(self):
        scalper = AsianRangeFadeScalper(period=5, z_threshold=1.5)
        # 02:00 UTC is Tokyo session
        dt_base = datetime(2026, 1, 6, 2, 0, 0, tzinfo=timezone.utc)

        # Feed 5 normal bars at 1.0850
        for i in range(5):
            bar = BarEvent(
                symbol="EURUSD",
                timeframe=Timeframe.M5,
                timestamp=dt_base + timedelta(minutes=i * 5),
                open=Decimal("1.0850"),
                high=Decimal("1.0852"),
                low=Decimal("1.0848"),
                close=Decimal("1.0850"),
                volume=Decimal("50"),
            )
            scalper.on_bar(bar)

        # 6th bar: sharp drop to 1.0820 (lower band pierced)
        drop_bar = BarEvent(
            symbol="EURUSD",
            timeframe=Timeframe.M5,
            timestamp=dt_base + timedelta(minutes=25),
            open=Decimal("1.0848"),
            high=Decimal("1.0848"),
            low=Decimal("1.0815"),
            close=Decimal("1.0820"),
            volume=Decimal("150"),
        )
        intents = scalper.on_bar(drop_bar)
        assert len(intents) == 1
        assert intents[0].side == OrderSide.BUY
        assert intents[0].order_type == OrderType.LIMIT
        assert "ASIAN_FADE_BUY" in intents[0].client_tag


class TestLondonSessionBreakout:
    """Test London Opening Range Breakout."""

    def test_consolidation_accumulation_and_breakout(self):
        strat = LondonSessionBreakout()
        curr_date = datetime(2026, 1, 6, tzinfo=timezone.utc)

        # 1. Feed pre-market consolidation bars between 05:00 and 07:00 UTC
        # High = 1.0860, Low = 1.0840
        for m in range(0, 120, 15):
            bar_time = curr_date.replace(hour=5, minute=0) + timedelta(minutes=m)
            bar = BarEvent(
                symbol="EURUSD",
                timeframe=Timeframe.M15,
                timestamp=bar_time,
                open=Decimal("1.0850"),
                high=Decimal("1.0860"),
                low=Decimal("1.0840"),
                close=Decimal("1.0855"),
                volume=Decimal("200"),
            )
            strat.on_bar(bar)

        # 2. At 07:30 UTC, breakout above 1.0860 + 2 pips buffer (1.0862)
        breakout_time = curr_date.replace(hour=7, minute=30)
        breakout_bar = BarEvent(
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            timestamp=breakout_time,
            open=Decimal("1.0858"),
            high=Decimal("1.0875"),
            low=Decimal("1.0855"),
            close=Decimal("1.0870"),  # > 1.0862
            volume=Decimal("800"),
        )
        intents = strat.on_bar(breakout_bar)
        assert len(intents) == 1
        assert intents[0].side == OrderSide.BUY
        assert intents[0].stop_loss == Decimal("1.08400")  # pre-market low


class TestTrendContinuationStrategy:
    """Test Multi-Timeframe Trend Continuation with KER filter."""

    def test_ker_filtering_and_pullback(self):
        strat = TrendContinuationStrategy(
            fast_period=5,
            slow_period=10,
            ker_period=5,
            ker_threshold=0.30,
        )
        base_time = datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone.utc)

        # Feed strong uptrending bars (high KER)
        price = Decimal("1.0800")
        for i in range(20):
            bar = BarEvent(
                symbol="EURUSD",
                timeframe=Timeframe.M15,
                timestamp=base_time + timedelta(minutes=i * 15),
                open=price,
                high=price + Decimal("0.0010"),
                low=price - Decimal("0.0002"),
                close=price + Decimal("0.0008"),
                volume=Decimal("300"),
                htf_data={"h1_trend": "UP"},
            )
            strat.on_bar(bar)
            price += Decimal("0.0008")

        # Pullback bar that dips below fast EMA and closes above it
        pullback_bar = BarEvent(
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            timestamp=base_time + timedelta(minutes=300),
            open=price,
            high=price + Decimal("0.0005"),
            low=price - Decimal("0.0030"),  # dips near fast EMA
            close=price + Decimal("0.0002"),
            volume=Decimal("450"),
            htf_data={"h1_trend": "UP"},
        )
        intents = strat.on_bar(pullback_bar)
        assert len(intents) == 1
        assert intents[0].side == OrderSide.BUY


class TestMacroCarryStrategy:
    """Test macro interest rate differential carry strategy."""

    def test_carry_entry_and_vol_shock_gate(self):
        strat = MacroCarryStrategy(symbols=["USDJPY"])
        base_time = datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone.utc)

        # Feed normal volatility bars
        price = Decimal("150.00")
        for i in range(35):
            bar = BarEvent(
                symbol="USDJPY",
                timeframe=Timeframe.H1,
                timestamp=base_time + timedelta(hours=i),
                open=price,
                high=price + Decimal("0.20"),
                low=price - Decimal("0.20"),
                close=price + Decimal("0.05"),
                volume=Decimal("500"),
            )
            intents = strat.on_bar(bar)
            price += Decimal("0.05")

        # In normal volatility regime, USDJPY long carry is emitted
        assert len(intents) == 1
        assert intents[0].side == OrderSide.BUY
        assert "MACRO_CARRY" in intents[0].client_tag

        # Now simulate a massive volatility shock (e.g. 5.00 yen flash crash / carry unwind)
        shock_bar = BarEvent(
            symbol="USDJPY",
            timeframe=Timeframe.H1,
            timestamp=base_time + timedelta(hours=36),
            open=price,
            high=price + Decimal("1.00"),
            low=price - Decimal("5.00"),  # Huge shock!
            close=price - Decimal("4.50"),
            volume=Decimal("5000"),
        )
        shock_intents = strat.on_bar(shock_bar)
        # Volatility shock filter triggers -> inhibits new carry entry!
        assert len(shock_intents) == 0


class TestTriangularStatisticalArbitrage:
    """Test Triangular Statistical Arbitrage."""

    def test_triangular_pricing_divergence(self):
        strat = TriangularStatisticalArbitrage(
            lookback_period=5,
            z_threshold=1.5,
        )
        now = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)

        # By cross-rate parity: EURGBP = EURUSD / GBPUSD
        # Suppose EURUSD = 1.1000, GBPUSD = 1.3750 -> EURGBP = 1.1000 / 1.3750 = 0.8000
        # Feed 5 balanced bars
        for i in range(5):
            ts = now + timedelta(minutes=i)
            strat.on_bar(BarEvent(symbol="EURUSD", timeframe=Timeframe.M1, timestamp=ts, open=Decimal("1.1000"), high=Decimal("1.1005"), low=Decimal("1.0995"), close=Decimal("1.1000"), volume=Decimal("100")))
            strat.on_bar(BarEvent(symbol="GBPUSD", timeframe=Timeframe.M1, timestamp=ts, open=Decimal("1.3750"), high=Decimal("1.3755"), low=Decimal("1.3745"), close=Decimal("1.3750"), volume=Decimal("100")))
            strat.on_bar(BarEvent(symbol="EURGBP", timeframe=Timeframe.M1, timestamp=ts, open=Decimal("0.8000"), high=Decimal("0.8005"), low=Decimal("0.7995"), close=Decimal("0.8000"), volume=Decimal("100")))

        # 6th bar: EURUSD spikes upwards to 1.1080 (overpriced relative to GBPUSD and EURGBP)
        ts_shock = now + timedelta(minutes=6)
        strat.on_bar(BarEvent(symbol="EURGBP", timeframe=Timeframe.M1, timestamp=ts_shock, open=Decimal("0.8000"), high=Decimal("0.8005"), low=Decimal("0.7995"), close=Decimal("0.8000"), volume=Decimal("100")))
        strat.on_bar(BarEvent(symbol="GBPUSD", timeframe=Timeframe.M1, timestamp=ts_shock, open=Decimal("1.3750"), high=Decimal("1.3755"), low=Decimal("1.3745"), close=Decimal("1.3750"), volume=Decimal("100")))
        intents = strat.on_bar(BarEvent(symbol="EURUSD", timeframe=Timeframe.M1, timestamp=ts_shock, open=Decimal("1.1000"), high=Decimal("1.1090"), low=Decimal("1.1000"), close=Decimal("1.1080"), volume=Decimal("500")))

        # Should emit SELL EURUSD and BUY GBPUSD
        assert len(intents) == 2
        symbols_sides = {i.symbol: i.side for i in intents}
        assert symbols_sides["EURUSD"] == OrderSide.SELL
        assert symbols_sides["GBPUSD"] == OrderSide.BUY
