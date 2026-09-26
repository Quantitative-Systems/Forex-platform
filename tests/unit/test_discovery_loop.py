"""
Unit tests for Continuous Discovery Loop, Hypothesis Exploration,
and G1–G7 Qualification Gate Promotion.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import numpy as np
import polars as pl
import pytest

from forex_platform.core.domain import CurrencyPair
from forex_platform.discovery.discovery_loop import (
    CandidateHypothesis,
    ContinuousDiscoveryLoop,
    PromotionStatus,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.research_engine.backtester import BacktestResult
from forex_platform.research_engine.evaluate import GateReport, GateResult, StrategyEvaluator
from forex_platform.research_engine.walkforward import WalkForwardResult
from forex_platform.strategy_engine.base import BaseStrategy
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy


def _make_sample_df(count: int = 200) -> pl.DataFrame:
    t0 = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    rows = []
    price = 1.0850
    for i in range(count):
        dt = t0 + timedelta(minutes=15 * i)
        step = 0.0002 if i % 2 == 0 else -0.0001
        open_ = price
        close_ = price + step
        high_ = max(open_, close_) + 0.0003
        low_ = min(open_, close_) - 0.0003
        rows.append({
            "timestamp": dt,
            "open": round(open_, 5),
            "high": round(high_, 5),
            "low": round(low_, 5),
            "close": round(close_, 5),
            "volume": 100.0,
        })
        price = close_
    return pl.DataFrame(rows)


class TestContinuousDiscoveryLoop:

    def test_instantiate_strategy_from_candidate(self):
        candidate = CandidateHypothesis(
            candidate_id="CAND_01",
            strategy_name="TrendContinuationStrategy",
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            parameters={"symbols": ["EURUSD"], "fast_period": 10},
        )
        loop = ContinuousDiscoveryLoop()
        strategy = loop.instantiate_strategy(candidate)
        assert isinstance(strategy, TrendContinuationStrategy)
        assert strategy.strategy_id == "CAND_01"
        assert strategy.fast_period == 10

    def test_unknown_strategy_name_raises(self):
        candidate = CandidateHypothesis(
            candidate_id="CAND_ERR",
            strategy_name="NonExistentStrategyPlugin",
            symbol="EURUSD",
        )
        loop = ContinuousDiscoveryLoop()
        with pytest.raises(ValueError, match="Unknown strategy class"):
            loop.instantiate_strategy(candidate)

    def test_evaluate_candidate_fails_gates_and_archives(self, tmp_path):
        """Verify candidate with insufficient trades fails G1 and is archived to research/failed/."""
        df = _make_sample_df(60)
        candidate = CandidateHypothesis(
            candidate_id="CAND_FAIL_TEST",
            strategy_name="TrendContinuationStrategy",
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            parameters={"symbols": ["EURUSD"]},
        )

        evaluator = StrategyEvaluator(min_dev_trades=10, min_val_trades=5, min_oos_trades=5)
        evaluator.FAILED_RESEARCH_DIR = tmp_path / "failed"

        loop = ContinuousDiscoveryLoop(evaluator=evaluator)
        loop.FAILED_DIR = tmp_path / "failed"
        loop.PROMOTED_DIR = tmp_path / "promoted"

        result = loop.evaluate_candidate(candidate, df)
        assert result.status == PromotionStatus.REJECTED_GATES
        assert result.gate_report is not None
        assert result.gate_report.passed_all_gates is False
        assert result.gate_report.archived_failed_path is not None
        assert Path(result.gate_report.archived_failed_path).exists()

    def test_evaluate_candidate_promotes_winner(self, tmp_path, monkeypatch):
        """Verify candidate meeting all G1-G7 criteria is promoted to PROMOTABLE_PAPER_ONLY."""
        df = _make_sample_df(60)
        candidate = CandidateHypothesis(
            candidate_id="CAND_WIN_TEST",
            strategy_name="TrendContinuationStrategy",
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            parameters={"symbols": ["EURUSD"]},
        )

        # Mock evaluator to simulate passing all G1-G7 gates
        mock_evaluator = StrategyEvaluator()
        def mock_evaluate(*args, **kwargs):
            return GateReport(
                strategy_id=candidate.candidate_id,
                passed_all_gates=True,
                failed_gate=None,
                gate_results={
                    "G1": GateResult(gate_name="G1", passed=True, observed_value=100, threshold_value=50, message="OK"),
                    "G2": GateResult(gate_name="G2", passed=True, observed_value=1.5, threshold_value=0.0, message="OK"),
                    "G3": GateResult(gate_name="G3", passed=True, observed_value=0.98, threshold_value=0.95, message="OK"),
                    "G4": GateResult(gate_name="G4", passed=True, observed_value=0.80, threshold_value=0.50, message="OK"),
                    "G5": GateResult(gate_name="G5", passed=True, observed_value=1.1, threshold_value=0.0, message="OK"),
                    "G6": GateResult(gate_name="G6", passed=True, observed_value=5.0, threshold_value=15.0, message="OK"),
                    "G7": GateResult(gate_name="G7", passed=True, observed_value=1.8, threshold_value=0.50, message="OK"),
                },
            )
        monkeypatch.setattr(mock_evaluator, "evaluate", mock_evaluate)

        loop = ContinuousDiscoveryLoop(evaluator=mock_evaluator)
        loop.PROMOTED_DIR = tmp_path / "promoted"

        result = loop.evaluate_candidate(candidate, df)
        assert result.status == PromotionStatus.PROMOTABLE_PAPER_ONLY
        assert result.promoted_artifact_path is not None
        assert Path(result.promoted_artifact_path).exists()

    def test_pipeline_requires_real_data_by_default(self, tmp_path):
        from forex_platform.discovery.pipeline_runner import AutomatedPipelineRunner

        runner = AutomatedPipelineRunner(
            symbols=["EURUSD"], bars=200, cache_dir=tmp_path / "cache",
            paper_db_path=tmp_path / "paper.db", allow_live_download=False,
        )
        with pytest.raises(RuntimeError, match="Real historical data is required"):
            runner.acquire_historical_data()

    def test_pipeline_allows_explicit_synthetic_smoke_data(self, tmp_path):
        from forex_platform.discovery.pipeline_runner import AutomatedPipelineRunner

        runner = AutomatedPipelineRunner(
            symbols=["EURUSD"], bars=200, cache_dir=tmp_path / "cache",
            paper_db_path=tmp_path / "paper.db", allow_live_download=False,
            require_real_data=False,
        )
        data = runner.acquire_historical_data()
        assert data["EURUSD"].height >= 200
        assert runner.data_provenance["EURUSD"].value == "SYNTHETIC"

        """Verify parameter exploration sweep handles multiple candidates."""
        df = _make_sample_df(60)
        candidates = [
            CandidateHypothesis(
                candidate_id="SWEEP_01",
                strategy_name="TrendContinuationStrategy",
                symbol="EURUSD",
                parameters={"symbols": ["EURUSD"], "fast_period": 10},
            ),
            CandidateHypothesis(
                candidate_id="SWEEP_02",
                strategy_name="TrendContinuationStrategy",
                symbol="GBPUSD",
                parameters={"symbols": ["GBPUSD"], "fast_period": 20},
            ),
        ]

        data_map = {"EURUSD": df, "GBPUSD": df}
        loop = ContinuousDiscoveryLoop()
        loop.FAILED_DIR = tmp_path / "failed"

        results = loop.run_discovery_sweep(candidates, data_map)
        assert len(results) == 2
        assert results[0].candidate_id == "SWEEP_01"
        assert results[1].candidate_id == "SWEEP_02"
