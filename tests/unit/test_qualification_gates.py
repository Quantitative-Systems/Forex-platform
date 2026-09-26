"""
Unit tests for G1-G7 Qualification Gates and Research Archival.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from forex_platform.core.domain import CurrencyPair, LotSize, OrderSide, OrderType
from forex_platform.research_engine.backtester import BacktestResult, TradeRecord
from forex_platform.research_engine.evaluate import GateReport, GateResult, StrategyEvaluator
from forex_platform.research_engine.walkforward import (
    RollingWalkForwardResult,
    WalkForwardResult,
)
from forex_platform.strategy_engine.base import BaseStrategy
from forex_platform.market_data.causal_aligner import Timeframe


class MockStrategy(BaseStrategy):
    """Mock strategy for testing."""

    def __init__(self, strategy_id: str = "test_strategy", **kwargs):
        super().__init__(
            strategy_id=strategy_id,
            name="Test Strategy",
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
            parameters=kwargs,
        )

    def on_bar(self, event):
        return []


def create_mock_backtest_result(
    total_trades: int = 50,
    expectancy: float = 1.0,
    max_drawdown_pct: float = 10.0,
    sharpe_ratio: float = 1.0,
    win_rate: float = 0.55,
    profit_factor: float = 1.5,
) -> BacktestResult:
    """Create a mock BacktestResult for testing."""
    trades = []
    for i in range(total_trades):
        pnl = expectancy + (np.random.random() - 0.5) * 2  # Random around expectancy
        trades.append(TradeRecord(
            trade_id=f"TRD-{i:04d}",
            symbol="EURUSD",
            side=OrderSide.BUY if i % 2 == 0 else OrderSide.SELL,
            units=100000,
            entry_time=datetime.now(timezone.utc),
            entry_price=Decimal("1.0850"),
            exit_time=datetime.now(timezone.utc),
            exit_price=Decimal("1.0860"),
            gross_pnl=Decimal(str(pnl + 0.7)),  # Add commission back
            commission=Decimal("0.70"),
            swap=Decimal("0.0"),
            net_pnl=Decimal(str(pnl)),
            exit_reason="TAKE_PROFIT" if pnl > 0 else "STOP_LOSS",
        ))

    return BacktestResult(
        strategy_id="test_strategy",
        initial_balance=Decimal("100000.00"),
        final_balance=Decimal("100000.00") + Decimal(str(expectancy * total_trades)),
        total_net_pnl=Decimal(str(expectancy * total_trades)),
        total_trades=total_trades,
        winning_trades=int(total_trades * win_rate),
        losing_trades=int(total_trades * (1 - win_rate)),
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=Decimal(str(expectancy)),
        max_drawdown_pct=max_drawdown_pct,
        sharpe_ratio=sharpe_ratio,
        trades=trades,
        equity_curve=[],
    )


class TestGateResult:
    """Tests for GateResult model."""

    def test_gate_result_creation(self):
        """Test GateResult creation."""
        result = GateResult(
            gate_name="G1_SAMPLE_SIZE",
            passed=True,
            observed_value=150.0,
            threshold_value=100.0,
            message="DEV=150 (min 100), VAL=40, OOS=40",
        )
        assert result.gate_name == "G1_SAMPLE_SIZE"
        assert result.passed is True
        assert result.observed_value == 150.0
        assert result.threshold_value == 100.0

    def test_gate_result_failed(self):
        """Test failed GateResult."""
        result = GateResult(
            gate_name="G6_MAX_DRAWDOWN",
            passed=False,
            observed_value=20.0,
            threshold_value=15.0,
            message="Max drawdown = 20.00% (limit <= 15.00%)",
        )
        assert result.passed is False
        assert result.observed_value == 20.0


class TestGateReport:
    """Tests for GateReport model."""

    def test_gate_report_all_passed(self):
        """Test GateReport with all gates passed."""
        gates = {
            "G1_SAMPLE_SIZE": GateResult(
                gate_name="G1_SAMPLE_SIZE", passed=True, observed_value=150.0,
                threshold_value=100.0, message="OK"
            ),
            "G2_ALPHA_CONSISTENCY": GateResult(
                gate_name="G2_ALPHA_CONSISTENCY", passed=True, observed_value=1.0,
                threshold_value=0.0, message="OK"
            ),
        }
        report = GateReport(
            strategy_id="test_strategy",
            passed_all_gates=True,
            failed_gate=None,
            gate_results=gates,
            archived_failed_path=None,
            promoted_path="/path/to/promoted.json",
        )
        assert report.passed_all_gates is True
        assert report.failed_gate is None
        assert report.promoted_path is not None

    def test_gate_report_with_failure(self):
        """Test GateReport with a failed gate."""
        gates = {
            "G1_SAMPLE_SIZE": GateResult(
                gate_name="G1_SAMPLE_SIZE", passed=True, observed_value=150.0,
                threshold_value=100.0, message="OK"
            ),
            "G6_MAX_DRAWDOWN": GateResult(
                gate_name="G6_MAX_DRAWDOWN", passed=False, observed_value=20.0,
                threshold_value=15.0, message="Max drawdown exceeded"
            ),
        }
        report = GateReport(
            strategy_id="test_strategy",
            passed_all_gates=False,
            failed_gate="G6_MAX_DRAWDOWN",
            gate_results=gates,
            archived_failed_path="/path/to/failed.json",
            promoted_path=None,
        )
        assert report.passed_all_gates is False
        assert report.failed_gate == "G6_MAX_DRAWDOWN"
        assert report.archived_failed_path is not None
        assert report.promoted_path is None


class TestStrategyEvaluator:
    """Tests for StrategyEvaluator class."""

    def test_evaluator_defaults(self):
        """Test evaluator default parameters."""
        evaluator = StrategyEvaluator()
        assert evaluator.min_dev_trades == 100
        assert evaluator.min_val_trades == 30
        assert evaluator.min_oos_trades == 30
        assert evaluator.max_drawdown_limit == 15.0
        assert evaluator.min_wfr == 0.50
        assert evaluator.bootstrap_p_val == 0.95
        assert evaluator.min_sharpe == 0.50

    def test_evaluator_custom_params(self):
        """Test evaluator with custom parameters."""
        evaluator = StrategyEvaluator(
            min_dev_trades=50,
            min_val_trades=20,
            min_oos_trades=20,
            max_drawdown_limit=10.0,
            min_wfr=0.60,
            bootstrap_p_val=0.90,
            min_sharpe=0.80,
        )
        assert evaluator.min_dev_trades == 50
        assert evaluator.max_drawdown_limit == 10.0
        assert evaluator.min_wfr == 0.60

    def test_g1_sample_size_pass(self):
        """Test G1 gate passes with sufficient trades."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5)

        dev = create_mock_backtest_result(total_trades=20)
        val = create_mock_backtest_result(total_trades=10)
        oos = create_mock_backtest_result(total_trades=10)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G1_SAMPLE_SIZE"].passed is True

    def test_g1_sample_size_fail(self):
        """Test G1 gate fails with insufficient trades."""
        evaluator = StrategyEvaluator(min_dev_trades=100, min_val_trades=30, min_oos_trades=30)

        dev = create_mock_backtest_result(total_trades=50)  # Below minimum
        val = create_mock_backtest_result(total_trades=10)
        oos = create_mock_backtest_result(total_trades=10)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G1_SAMPLE_SIZE"].passed is False
        assert report.failed_gate == "G1_SAMPLE_SIZE"

    def test_g2_alpha_consistency_pass(self):
        """Test G2 gate passes with positive expectancy across all partitions."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5)

        dev = create_mock_backtest_result(total_trades=20, expectancy=1.5)
        val = create_mock_backtest_result(total_trades=10, expectancy=1.0)
        oos = create_mock_backtest_result(total_trades=10, expectancy=0.8)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G2_ALPHA_CONSISTENCY"].passed is True

    def test_g2_alpha_consistency_fail(self):
        """Test G2 gate fails with negative expectancy in OOS."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5)

        dev = create_mock_backtest_result(total_trades=20, expectancy=1.5)
        val = create_mock_backtest_result(total_trades=10, expectancy=1.0)
        oos = create_mock_backtest_result(total_trades=10, expectancy=-0.5)  # Negative!

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G2_ALPHA_CONSISTENCY"].passed is False
        assert report.failed_gate == "G2_ALPHA_CONSISTENCY"

    def test_g3_bootstrap_significance_pass(self):
        """Test G3 gate passes with high bootstrap positive probability."""
        evaluator = StrategyEvaluator(
            min_dev_trades=10, min_val_trades=5, min_oos_trades=5,
            bootstrap_p_val=0.90,  # Lower threshold for test
        )

        # Create trades with strongly positive expectancy
        dev = create_mock_backtest_result(total_trades=30, expectancy=2.0)
        val = create_mock_backtest_result(total_trades=15, expectancy=1.5)
        oos = create_mock_backtest_result(total_trades=15, expectancy=1.0)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        # With strong positive expectancy, bootstrap should pass
        assert report.gate_results["G3_BOOTSTRAP_SIGNIFICANCE"].passed is True

    def test_g4_walkforward_ratio_pass(self):
        """Test G4 gate passes with good walk-forward ratio."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5, min_wfr=0.50)

        # OOS expectancy is 60% of DEV expectancy -> WFR = 0.60 > 0.50
        dev = create_mock_backtest_result(total_trades=20, expectancy=2.0)
        val = create_mock_backtest_result(total_trades=10, expectancy=1.5)
        oos = create_mock_backtest_result(total_trades=10, expectancy=1.2)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G4_WALKFORWARD_RATIO"].passed is True
        assert report.gate_results["G4_WALKFORWARD_RATIO"].observed_value >= 0.50

    def test_g4_walkforward_ratio_fail(self):
        """Test G4 gate fails with poor walk-forward ratio."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5, min_wfr=0.50)

        # OOS expectancy is only 30% of DEV expectancy -> WFR = 0.30 < 0.50
        dev = create_mock_backtest_result(total_trades=20, expectancy=2.0)
        val = create_mock_backtest_result(total_trades=10, expectancy=1.5)
        oos = create_mock_backtest_result(total_trades=10, expectancy=0.6)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G4_WALKFORWARD_RATIO"].passed is False
        assert report.failed_gate == "G4_WALKFORWARD_RATIO"

    def test_g5_cost_shock_stress_pass(self):
        """Test G5 gate passes with positive expectancy under cost shock."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5)

        dev = create_mock_backtest_result(total_trades=20, expectancy=2.0)
        val = create_mock_backtest_result(total_trades=10, expectancy=1.5)
        oos = create_mock_backtest_result(total_trades=10, expectancy=1.0)

        # Cost shock result still positive
        cost_shock = create_mock_backtest_result(total_trades=10, expectancy=0.5)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(
            strategy, CurrencyPair.from_symbol("EURUSD"), wf_result, cost_shock_result=cost_shock
        )

        assert report.gate_results["G5_COST_SHOCK_STRESS"].passed is True

    def test_g5_cost_shock_stress_fail(self):
        """Test G5 gate fails with negative expectancy under cost shock."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5)

        dev = create_mock_backtest_result(total_trades=20, expectancy=1.0)
        val = create_mock_backtest_result(total_trades=10, expectancy=0.8)
        oos = create_mock_backtest_result(total_trades=10, expectancy=0.5)

        # Cost shock makes it negative
        cost_shock = create_mock_backtest_result(total_trades=10, expectancy=-0.5)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(
            strategy, CurrencyPair.from_symbol("EURUSD"), wf_result, cost_shock_result=cost_shock
        )

        assert report.gate_results["G5_COST_SHOCK_STRESS"].passed is False
        assert report.failed_gate == "G5_COST_SHOCK_STRESS"

    def test_g6_max_drawdown_pass(self):
        """Test G6 gate passes with acceptable drawdown."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5, max_drawdown_limit=15.0)

        dev = create_mock_backtest_result(total_trades=20, max_drawdown_pct=10.0)
        val = create_mock_backtest_result(total_trades=10, max_drawdown_pct=8.0)
        oos = create_mock_backtest_result(total_trades=10, max_drawdown_pct=12.0)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G6_MAX_DRAWDOWN"].passed is True

    def test_g6_max_drawdown_fail(self):
        """Test G6 gate fails with excessive drawdown."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5, max_drawdown_limit=15.0)

        dev = create_mock_backtest_result(total_trades=20, max_drawdown_pct=10.0)
        val = create_mock_backtest_result(total_trades=10, max_drawdown_pct=8.0)
        oos = create_mock_backtest_result(total_trades=10, max_drawdown_pct=20.0)  # Exceeds limit

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G6_MAX_DRAWDOWN"].passed is False
        assert report.failed_gate == "G6_MAX_DRAWDOWN"

    def test_g7_sharpe_ratio_pass(self):
        """Test G7 gate passes with sufficient Sharpe ratio."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5, min_sharpe=0.50)

        dev = create_mock_backtest_result(total_trades=20, sharpe_ratio=1.0)
        val = create_mock_backtest_result(total_trades=10, sharpe_ratio=0.8)
        oos = create_mock_backtest_result(total_trades=10, sharpe_ratio=0.7)

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G7_SHARPE_RATIO"].passed is True

    def test_g7_sharpe_ratio_fail(self):
        """Test G7 gate fails with insufficient Sharpe ratio."""
        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5, min_sharpe=0.50)

        dev = create_mock_backtest_result(total_trades=20, sharpe_ratio=1.0)
        val = create_mock_backtest_result(total_trades=10, sharpe_ratio=0.8)
        oos = create_mock_backtest_result(total_trades=10, sharpe_ratio=0.3)  # Below threshold

        wf_result = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy()

        report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

        assert report.gate_results["G7_SHARPE_RATIO"].passed is False
        assert report.failed_gate == "G7_SHARPE_RATIO"

    def test_all_gates_pass_promotes_strategy(self):
        """Test that passing all gates promotes the strategy."""
        evaluator = StrategyEvaluator(
            min_dev_trades=10, min_val_trades=5, min_oos_trades=5,
            max_drawdown_limit=20.0, min_sharpe=0.30, bootstrap_p_val=0.80,
        )

        # Create strong results that should pass all gates
        dev = create_mock_backtest_result(total_trades=30, expectancy=2.0, max_drawdown_pct=5.0, sharpe_ratio=1.5)
        val = create_mock_backtest_result(total_trades=15, expectancy=1.5, max_drawdown_pct=4.0, sharpe_ratio=1.2)
        oos = create_mock_backtest_result(total_trades=15, expectancy=1.2, max_drawdown_pct=6.0, sharpe_ratio=1.0)
        cost_shock = create_mock_backtest_result(total_trades=15, expectancy=0.8)

        wf_result = WalkForwardResult(
            strategy_id="test_promo_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy(strategy_id="test_promo_strategy", period=20, z_threshold=2.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Patch the research directories
            with patch.object(evaluator, 'FAILED_RESEARCH_DIR', Path(tmpdir) / "failed"), \
                 patch.object(evaluator, 'PROMOTED_RESEARCH_DIR', Path(tmpdir) / "promoted"):

                report = evaluator.evaluate(
                    strategy, CurrencyPair.from_symbol("EURUSD"), wf_result, cost_shock_result=cost_shock
                )

                assert report.passed_all_gates is True
                assert report.promoted_path is not None
                assert report.archived_failed_path is None

                # Verify promoted file exists
                assert Path(report.promoted_path).exists()

                # Verify promoted file content
                with open(report.promoted_path) as f:
                    promo_data = json.load(f)
                assert promo_data["strategy_id"] == "test_promo_strategy"
                assert promo_data["status"] == "PROMOTABLE_PAPER_ONLY"
                assert "parameters" in promo_data
                assert "oos_performance" in promo_data

    def test_failed_gate_archives_negative_research(self):
        """Test that failing a gate archives negative research."""
        evaluator = StrategyEvaluator(min_dev_trades=100, min_val_trades=30, min_oos_trades=30)

        dev = create_mock_backtest_result(total_trades=50)  # Fails G1
        val = create_mock_backtest_result(total_trades=10)
        oos = create_mock_backtest_result(total_trades=10)

        wf_result = WalkForwardResult(
            strategy_id="test_fail_strategy",
            dev_result=dev, val_result=val, oos_result=oos
        )
        strategy = MockStrategy(strategy_id="test_fail_strategy")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(evaluator, 'FAILED_RESEARCH_DIR', Path(tmpdir) / "failed"), \
                 patch.object(evaluator, 'PROMOTED_RESEARCH_DIR', Path(tmpdir) / "promoted"):

                report = evaluator.evaluate(strategy, CurrencyPair.from_symbol("EURUSD"), wf_result)

                assert report.passed_all_gates is False
                assert report.archived_failed_path is not None
                assert report.promoted_path is None

                # Verify failed file exists
                assert Path(report.archived_failed_path).exists()

                # Verify failed file content
                with open(report.archived_failed_path) as f:
                    failed_data = json.load(f)
                assert failed_data["strategy_id"] == "test_fail_strategy"
                assert failed_data["failed_gate"] == "G1_SAMPLE_SIZE"
                assert "gates" in failed_data


    def test_g8_rolling_robustness_requires_positive_windows(self):
        evaluator = StrategyEvaluator(
            min_dev_trades=10, min_val_trades=5, min_oos_trades=5
        )
        positive = create_mock_backtest_result(total_trades=10, expectancy=1.0)
        negative = create_mock_backtest_result(total_trades=10, expectancy=-1.0)
        window = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=positive,
            val_result=positive,
            oos_result=positive,
        )
        robust = RollingWalkForwardResult(
            strategy_id="test_strategy",
            windows=[window, window, window],
        )
        report = evaluator.evaluate(
            MockStrategy(), CurrencyPair.from_symbol("EURUSD"),
            WalkForwardResult(strategy_id="test_strategy", dev_result=positive, val_result=positive, oos_result=positive),
            cost_shock_result=positive,
            rolling_result=robust,
        )
        assert report.gate_results["G8_ROLLING_ROBUSTNESS"].passed is True

        fragile_window = WalkForwardResult(
            strategy_id="test_strategy",
            dev_result=positive,
            val_result=positive,
            oos_result=negative,
        )
        fragile = RollingWalkForwardResult(
            strategy_id="test_strategy",
            windows=[window, fragile_window, fragile_window],
        )
        report = evaluator.evaluate(
            MockStrategy(), CurrencyPair.from_symbol("EURUSD"),
            WalkForwardResult(strategy_id="test_strategy", dev_result=positive, val_result=positive, oos_result=positive),
            cost_shock_result=positive,
            rolling_result=fragile,
        )
        assert report.gate_results["G8_ROLLING_ROBUSTNESS"].passed is False
        assert report.failed_gate == "G8_ROLLING_ROBUSTNESS"

    """Tests for walk-forward partitioning."""

    def test_create_walkforward_partitions(self):
        """Test partition creation."""
        from forex_platform.discovery.calibration import create_walkforward_partitions

        partitions = create_walkforward_partitions(10000)
        assert len(partitions) == 1
        p = partitions[0]
        assert p.dev_size == 7000
        assert p.val_size == 1500
        assert p.oos_size == 1500

    def test_create_walkforward_partitions_minimums(self):
        """Test partition creation respects minimums."""
        from forex_platform.discovery.calibration import create_walkforward_partitions

        # Small dataset - should still respect minimums
        partitions = create_walkforward_partitions(2000, min_dev_bars=1000, min_val_bars=200, min_oos_bars=200)
        p = partitions[0]
        assert p.dev_size >= 1000
        assert p.val_size >= 200
        assert p.oos_size >= 200

    def test_split_dataframe_by_partition(self):
        """Test DataFrame splitting by partition."""
        import polars as pl
        from forex_platform.discovery.calibration import split_dataframe_by_partition, WalkForwardPartition

        df = pl.DataFrame({"value": list(range(100))})
        partition = WalkForwardPartition(0, 70, 70, 85, 85, 100)

        dev_df, val_df, oos_df = split_dataframe_by_partition(df, partition)

        assert dev_df.height == 70
        assert val_df.height == 15
        assert oos_df.height == 15
        assert dev_df["value"][0] == 0
        assert dev_df["value"][-1] == 69
        assert val_df["value"][0] == 70
        assert oos_df["value"][0] == 85


if __name__ == "__main__":
    pytest.main([__file__, "-v"])