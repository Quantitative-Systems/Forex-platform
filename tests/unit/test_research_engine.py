"""
Unit tests for Causal Research Engine:
Adverse-first intra-bar stop collision, next-bar-open causal fills,
walk-forward chronological partitioning, and G1–G7 qualification gate evaluation.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
import polars as pl
import pytest

from forex_platform.core.domain import CurrencyPair, LotSize, OrderIntent, OrderSide, OrderType
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.research_engine.backtester import (
    BacktestResult,
    EventDrivenBacktester,
    TradeRecord,
)
from forex_platform.research_engine.evaluate import GateReport, StrategyEvaluator
from forex_platform.research_engine.walkforward import WalkForwardEngine, WalkForwardResult
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class MockSignalStrategy(BaseStrategy):
    """Simple deterministic strategy for backtester testing."""

    def __init__(self, signal_indices: list[int]):
        super().__init__(
            strategy_id="mock_test_strat",
            name="Mock Test Strategy",
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )
        self.signal_indices = set(signal_indices)
        self.bar_index = 0

    def on_bar(self, event: BarEvent) -> list[OrderIntent]:
        self.update_history(event)
        idx = self.bar_index
        self.bar_index += 1

        if idx in self.signal_indices:
            # Emit BUY with SL 10 pips below, TP 20 pips above
            return [
                self.create_intent(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    lot_size=LotSize.from_lots(1.0),
                    timestamp=event.timestamp,
                    stop_loss=event.close - Decimal("0.0010"),
                    take_profit=event.close + Decimal("0.0020"),
                )
            ]
        return []


class TestCausalBacktester:
    """Test adverse-first stop priority, next-bar-open fill, and cost deductions."""

    def test_adverse_first_stop_collision(self):
        """
        When a candle touches both SL and TP in the same bar,
        the backtester MUST prioritize Stop Loss (adverse-first).
        """
        pair = CurrencyPair.from_symbol("EURUSD")
        start = datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)

        # Bar 0: Strategy triggers BUY at close 1.08500
        # Intent SL = 1.08400, TP = 1.08700
        # Bar 1: Fills BUY at open 1.08500. Bar range is [1.08350, 1.08750] (touches BOTH SL and TP!)
        records = [
            {"timestamp": start.isoformat(), "open": 1.0845, "high": 1.0855, "low": 1.0840, "close": 1.0850, "volume": 100, "spread": 0.8},
            {"timestamp": (start + timedelta(minutes=15)).isoformat(), "open": 1.0850, "high": 1.0875, "low": 1.0835, "close": 1.0860, "volume": 500, "spread": 0.8},
            {"timestamp": (start + timedelta(minutes=30)).isoformat(), "open": 1.0860, "high": 1.0865, "low": 1.0855, "close": 1.0860, "volume": 100, "spread": 0.8},
        ]
        df = pl.DataFrame(records)

        strat = MockSignalStrategy(signal_indices=[0])
        tester = EventDrivenBacktester(strat, pair)
        result = tester.run(df)

        assert result.total_trades == 1
        trade = result.trades[0]
        # Invariant: Adverse-First priority triggered Stop Loss!
        assert "STOP_LOSS" in trade.exit_reason
        assert trade.exit_price == Decimal("1.08400")
        assert trade.net_pnl < Decimal("0.0")

    def test_causal_next_bar_open_fill(self):
        """
        Verify that order emitted on bar 0 fills at bar 1's open, NOT bar 0's close.
        """
        pair = CurrencyPair.from_symbol("EURUSD")
        start = datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
        records = [
            {"timestamp": start.isoformat(), "open": 1.0800, "high": 1.0810, "low": 1.0790, "close": 1.0805, "volume": 100, "spread": 1.0},
            {"timestamp": (start + timedelta(minutes=15)).isoformat(), "open": 1.0820, "high": 1.0830, "low": 1.0815, "close": 1.0825, "volume": 150, "spread": 1.0},
            {"timestamp": (start + timedelta(minutes=30)).isoformat(), "open": 1.0825, "high": 1.0845, "low": 1.0820, "close": 1.0840, "volume": 200, "spread": 1.0},
        ]
        df = pl.DataFrame(records)

        strat = MockSignalStrategy(signal_indices=[0])
        tester = EventDrivenBacktester(strat, pair)
        result = tester.run(df)

        trade = result.trades[0]
        # Fills on bar 1 (open 1.0820 + half spread 0.00005 = 1.08205)
        assert trade.entry_price == Decimal("1.08205")
        assert trade.entry_time == start + timedelta(minutes=15)


class TestWalkForwardAndGates:
    """Test DEV/VAL/OOS partitioning and G1–G7 qualification gates."""

    def test_chronological_partitioning(self):
        # 100 bars from bar 0 to bar 99
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        records = [
            {"timestamp": (start + timedelta(hours=i)).isoformat(), "open": 1.08, "high": 1.09, "low": 1.07, "close": 1.08, "volume": 100, "spread": 1.0}
            for i in range(100)
        ]
        df = pl.DataFrame(records)

        dev_df, val_df, oos_df = WalkForwardEngine.partition_data(df, dev_ratio=0.70, val_ratio=0.15, oos_ratio=0.15)
        assert dev_df.height == 70
        assert val_df.height == 15
        assert oos_df.height == 15

        # Chronological boundary: dev_max < val_min < oos_min
        assert dev_df["timestamp"][-1] < val_df["timestamp"][0]
        assert val_df["timestamp"][-1] < oos_df["timestamp"][0]

    def test_g1_g7_gate_evaluation_and_negative_logger(self, tmp_path):
        evaluator = StrategyEvaluator(
            min_dev_trades=10,
            min_val_trades=5,
            min_oos_trades=5,
            min_wfr=0.50,
            max_drawdown_limit=15.0,
        )
        evaluator.FAILED_RESEARCH_DIR = tmp_path / "failed_research"

        now = datetime.now(timezone.utc)
        # Create mock trades with negative expectancy on OOS to test rejection
        trades_dev = [
            TradeRecord(trade_id=f"T-D{i}", symbol="EURUSD", side=OrderSide.BUY, units=100000, entry_time=now, entry_price=Decimal("1.08"), exit_time=now, exit_price=Decimal("1.09"), gross_pnl=Decimal("100"), commission=Decimal("7"), swap=Decimal("0"), net_pnl=Decimal("93"), exit_reason="TP")
            for i in range(15)
        ]
        trades_val = [
            TradeRecord(trade_id=f"T-V{i}", symbol="EURUSD", side=OrderSide.BUY, units=100000, entry_time=now, entry_price=Decimal("1.08"), exit_time=now, exit_price=Decimal("1.09"), gross_pnl=Decimal("50"), commission=Decimal("7"), swap=Decimal("0"), net_pnl=Decimal("43"), exit_reason="TP")
            for i in range(6)
        ]
        # Failing OOS: losing trades (negative expectancy)
        trades_oos = [
            TradeRecord(trade_id=f"T-O{i}", symbol="EURUSD", side=OrderSide.BUY, units=100000, entry_time=now, entry_price=Decimal("1.08"), exit_time=now, exit_price=Decimal("1.07"), gross_pnl=Decimal("-100"), commission=Decimal("7"), swap=Decimal("0"), net_pnl=Decimal("-107"), exit_reason="SL")
            for i in range(6)
        ]

        dev_res = BacktestResult(strategy_id="strat_test", initial_balance=Decimal("100000"), final_balance=Decimal("101395"), total_net_pnl=Decimal("1395"), total_trades=15, winning_trades=15, losing_trades=0, win_rate=1.0, profit_factor=999.0, expectancy=Decimal("93.0"), max_drawdown_pct=2.0, sharpe_ratio=2.5, trades=trades_dev, equity_curve=[])
        val_res = BacktestResult(strategy_id="strat_test", initial_balance=Decimal("100000"), final_balance=Decimal("100258"), total_net_pnl=Decimal("258"), total_trades=6, winning_trades=6, losing_trades=0, win_rate=1.0, profit_factor=999.0, expectancy=Decimal("43.0"), max_drawdown_pct=3.0, sharpe_ratio=1.8, trades=trades_val, equity_curve=[])
        oos_res = BacktestResult(strategy_id="strat_test", initial_balance=Decimal("100000"), final_balance=Decimal("99358"), total_net_pnl=Decimal("-642"), total_trades=6, winning_trades=0, losing_trades=6, win_rate=0.0, profit_factor=0.0, expectancy=Decimal("-107.0"), max_drawdown_pct=5.0, sharpe_ratio=-1.5, trades=trades_oos, equity_curve=[])

        wf_result = WalkForwardResult(strategy_id="strat_test", dev_result=dev_res, val_result=val_res, oos_result=oos_res)

        strat = MockSignalStrategy([0])
        pair = CurrencyPair.from_symbol("EURUSD")
        report = evaluator.evaluate(strat, pair, wf_result)

        assert not report.passed_all_gates
        # G2 Alpha Consistency fails because OOS Expectancy is negative (-107)
        assert report.failed_gate == "G2_ALPHA_CONSISTENCY"
        assert report.archived_failed_path is not None
        assert Path(report.archived_failed_path).exists()
