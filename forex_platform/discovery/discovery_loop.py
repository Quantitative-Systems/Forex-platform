"""
Continuous Discovery Loop and Automated Hypothesis Exploration.
Sweeps candidate strategy parameters across DEV (70%), VAL (15%), and OOS (15%),
enforces G1–G8 qualification gates, promotes winning hypotheses to
PROMOTABLE_PAPER_ONLY, and archives failures in research/failed/.
"""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
import polars as pl
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.provenance import DataProvenance
from forex_platform.research_engine.backtester import EventDrivenBacktester
from forex_platform.research_engine.evaluate import GateReport, StrategyEvaluator
from forex_platform.research_engine.walkforward import WalkForwardEngine
from forex_platform.strategy_engine.base import BaseStrategy
from forex_platform.strategy_engine.fractal_institutional import InstitutionalFractalStrategy
from forex_platform.strategy_engine.macro_carry import MacroCarryStrategy
from forex_platform.strategy_engine.scalping import AsianRangeFadeScalper
from forex_platform.strategy_engine.session_breakout import LondonSessionBreakout
from forex_platform.strategy_engine.stat_arb import TriangularStatisticalArbitrage
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy

logger = logging.getLogger(__name__)


def _block_bootstrap_null_pvalue(
    trade_pnls: list[float],
    *,
    seed: int,
    replicates: int = 500,
) -> Optional[float]:
    """One-sided, centered circular moving-block test of positive mean OOS PnL."""
    if len(trade_pnls) < 30:
        return None
    values = np.asarray(trade_pnls, dtype=float)
    observed = float(values.mean())
    if not np.isfinite(values).all() or observed <= 0:
        return 1.0
    centered = values - observed
    n = len(centered)
    block = max(1, round(math.sqrt(n)))
    block_count = math.ceil(n / block)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(replicates, block_count))
    offsets = np.arange(block)
    indices = (starts[:, :, None] + offsets[None, None, :]) % n
    draws = centered[indices].reshape(replicates, -1)[:, :n]
    null_means = draws.mean(axis=1)
    exceedances = int(np.count_nonzero(null_means >= observed))
    return (exceedances + 1) / (replicates + 1)


class PromotionStatus(str, Enum):
    PROMOTABLE_PAPER_ONLY = "PROMOTABLE_PAPER_ONLY"
    REJECTED_GATES = "REJECTED_GATES"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VALIDATION_PENDING = "VALIDATION_PENDING"


class CandidateHypothesis(BaseModel):
    """Specification of a quantitative strategy candidate for automated exploration."""
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    strategy_name: str
    symbol: str
    timeframe: Timeframe = Timeframe.M15
    parameters: Dict[str, Any] = Field(default_factory=dict)
    data_provenance: DataProvenance = DataProvenance.UNKNOWN


class DiscoveryResult(BaseModel):
    """Outcome of candidate evaluation and promotion gate checks."""
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    strategy_name: str
    symbol: str
    status: PromotionStatus
    gate_report: Optional[GateReport] = None
    error_message: Optional[str] = None
    promoted_artifact_path: Optional[str] = None
    trade_counts: Dict[str, int] = Field(default_factory=dict)
    performance_metrics: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContinuousDiscoveryLoop:
    """
    Automated discovery engine that explores the hypothesis space,
    partitions data chronologically, runs event-driven backtests,
    and enforces institutional qualification.
    """

    STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {
        "AsianRangeFadeScalper": AsianRangeFadeScalper,
        "LondonSessionBreakout": LondonSessionBreakout,
        "TrendContinuationStrategy": TrendContinuationStrategy,
        "InstitutionalFractalStrategy": InstitutionalFractalStrategy,
        "MacroCarryStrategy": MacroCarryStrategy,
        "TriangularStatisticalArbitrage": TriangularStatisticalArbitrage,
    }

    PROMOTED_DIR = Path("research/promoted")
    FAILED_DIR = Path("research/failed")

    def __init__(
        self,
        evaluator: Optional[StrategyEvaluator] = None,
        *,
        require_real_data: bool = False,
    ):
        self.evaluator = evaluator or StrategyEvaluator()
        self.require_real_data = require_real_data

    def instantiate_strategy(self, candidate: CandidateHypothesis) -> BaseStrategy:
        """Instantiate a strategy plugin from candidate specifications."""
        cls_ = self.STRATEGY_REGISTRY.get(candidate.strategy_name)
        if cls_ is None:
            raise ValueError(
                f"Unknown strategy class '{candidate.strategy_name}'. "
                f"Registered strategies: {list(self.STRATEGY_REGISTRY.keys())}"
            )
        params = dict(candidate.parameters)
        if "strategy_id" not in params:
            params["strategy_id"] = candidate.candidate_id
        if candidate.strategy_name == "InstitutionalFractalStrategy" and "symbols" not in params:
            params["symbols"] = [candidate.symbol]
        return cls_(**params)

    def evaluate_candidate(
        self,
        candidate: CandidateHypothesis,
        df: pl.DataFrame,
        *,
        allow_promotion: bool = True,
    ) -> DiscoveryResult:
        """
        Evaluate a candidate hypothesis end-to-end:
        1. Instantiate strategy.
        2. Partition data and run Walk-Forward (DEV, VAL, OOS).
        3. Execute cost-shock backtest on OOS (2x spread and commission).
        4. Apply G1–G8 qualification gates.
        5. If passed, promote to PROMOTABLE_PAPER_ONLY; if failed, archive to research/failed/.
        """
        now = datetime.now(timezone.utc)
        pair = CurrencyPair.from_symbol(candidate.symbol)

        if candidate.strategy_name == "InstitutionalFractalStrategy" and candidate.timeframe != Timeframe.M1:
            return DiscoveryResult(
                candidate_id=candidate.candidate_id,
                strategy_name=candidate.strategy_name,
                symbol=candidate.symbol,
                status=PromotionStatus.REJECTED_GATES,
                error_message="InstitutionalFractalStrategy requires M1 source bars to construct all five timeframe sets, including 3M.",
                timestamp=now,
            )

        must_use_real_data = self.require_real_data or candidate.strategy_name == "InstitutionalFractalStrategy"
        if must_use_real_data and candidate.data_provenance not in {
            DataProvenance.REAL_VENDOR,
            DataProvenance.BROKER_EXPORT,
        }:
            return DiscoveryResult(
                candidate_id=candidate.candidate_id,
                strategy_name=candidate.strategy_name,
                symbol=candidate.symbol,
                status=PromotionStatus.REJECTED_GATES,
                error_message=(
                    "Candidate rejected before backtesting: real vendor or broker-exported "
                    "data is required for qualification."
                ),
                timestamp=now,
            )

        try:
            strategy = self.instantiate_strategy(candidate)
        except Exception as e:
            logger.error("Failed to instantiate strategy for candidate %s: %s", candidate.candidate_id, e)
            return DiscoveryResult(
                candidate_id=candidate.candidate_id,
                strategy_name=candidate.strategy_name,
                symbol=candidate.symbol,
                status=PromotionStatus.EXECUTION_FAILED,
                error_message=str(e),
                timestamp=now,
            )

        try:
            # 1. Walk-Forward execution
            wf_result = WalkForwardEngine.run_walkforward(
                strategy=deepcopy(strategy),
                currency_pair=pair,
                df=df,
                timeframe=candidate.timeframe,
            )
            if candidate.strategy_name == "InstitutionalFractalStrategy" and candidate.timeframe == Timeframe.M1:
                # M1 needs multi-month structure warm-up. Short fixed windows
                # create thousands of tiny, context-starved validation slices.
                window_bars = max(30, int(df.height * 0.5))
                step_bars = max(15, window_bars // 2)
            else:
                window_bars = max(30, min(1000, int(df.height * 0.6)))
                step_bars = max(15, min(500, window_bars // 2))
            rolling_result = WalkForwardEngine.run_rolling_walkforward(
                strategy=deepcopy(strategy),
                currency_pair=pair,
                df=df,
                window_bars=window_bars,
                step_bars=step_bars,
                timeframe=candidate.timeframe,
            )

            # 2. Cost-shock test on OOS data (2.0x cost multiplier)
            _, _, oos_df = WalkForwardEngine.partition_data(df)
            shock_tester = EventDrivenBacktester(deepcopy(strategy), pair, cost_multiplier=2.0)
            cost_shock_result = shock_tester.run(oos_df, timeframe=candidate.timeframe)

            # 3. Qualification gates
            gate_report = self.evaluator.evaluate(
                strategy=strategy,
                currency_pair=pair,
                wf_result=wf_result,
                cost_shock_result=cost_shock_result,
                rolling_result=rolling_result,
            )

            # 4. Promotion decision
            if gate_report.passed_all_gates:
                if allow_promotion:
                    promoted_path = self._promote_candidate(candidate, gate_report)
                    status = PromotionStatus.PROMOTABLE_PAPER_ONLY
                else:
                    promoted_path = None
                    status = PromotionStatus.VALIDATION_PENDING
            else:
                promoted_path = None
                status = PromotionStatus.REJECTED_GATES

            trade_counts = {
                "development": wf_result.dev_result.total_trades,
                "validation": wf_result.val_result.total_trades,
                "oos": wf_result.oos_result.total_trades,
                "total": (
                    wf_result.dev_result.total_trades
                    + wf_result.val_result.total_trades
                    + wf_result.oos_result.total_trades
                ),
                "cost_shock_oos": cost_shock_result.total_trades,
            }
            oos_pnls = [float(trade.net_pnl) for trade in wf_result.oos_result.trades]
            top_winner_removed = (
                (sum(oos_pnls) - max(oos_pnls)) / (len(oos_pnls) - 1)
                if len(oos_pnls) > 1 else None
            )
            performance_metrics = {
                "development_expectancy": float(wf_result.dev_result.expectancy),
                "validation_expectancy": float(wf_result.val_result.expectancy),
                "oos_expectancy": float(wf_result.oos_result.expectancy),
                "oos_profit_factor": wf_result.oos_result.profit_factor,
                "oos_win_rate": wf_result.oos_result.win_rate,
                "oos_max_drawdown_pct": wf_result.oos_result.max_drawdown_pct,
                "oos_sharpe_ratio": wf_result.oos_result.sharpe_ratio,
                "cost_shock_oos_expectancy": float(cost_shock_result.expectancy),
                "oos_expectancy_after_top_winner_removal": top_winner_removed,
                "oos_block_bootstrap_positive_mean_p_value": _block_bootstrap_null_pvalue(
                    oos_pnls,
                    seed=sum(map(ord, candidate.candidate_id)),
                ),
            }

            return DiscoveryResult(
                candidate_id=candidate.candidate_id,
                strategy_name=candidate.strategy_name,
                symbol=candidate.symbol,
                status=status,
                gate_report=gate_report,
                promoted_artifact_path=promoted_path,
                trade_counts=trade_counts,
                performance_metrics=performance_metrics,
                timestamp=now,
            )

        except Exception as e:
            logger.exception("Error executing discovery loop for %s: %s", candidate.candidate_id, e)
            return DiscoveryResult(
                candidate_id=candidate.candidate_id,
                strategy_name=candidate.strategy_name,
                symbol=candidate.symbol,
                status=PromotionStatus.EXECUTION_FAILED,
                error_message=str(e),
                timestamp=now,
            )

    def _promote_candidate(self, candidate: CandidateHypothesis, report: GateReport) -> str:
        """
        Record candidate in research/promoted/ as PROMOTABLE_PAPER_ONLY.
        Invariant: strictly forbidden from live capital routing.
        """
        self.PROMOTED_DIR.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{candidate.candidate_id}_PROMOTED_{now_str}.json"
        target_path = self.PROMOTED_DIR / filename

        artifact = {
            "candidate_id": candidate.candidate_id,
            "strategy_name": candidate.strategy_name,
            "symbol": candidate.symbol,
            "timeframe": candidate.timeframe.value,
            "data_provenance": candidate.data_provenance.value,
            "parameters": candidate.parameters,
            "status": PromotionStatus.PROMOTABLE_PAPER_ONLY.value,
            "live_capital_authorized": False,  # Strict invariant
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "gate_report": {
                "passed_all_gates": report.passed_all_gates,
                "gates": {k: v.model_dump() for k, v in report.gate_results.items()},
            },
        }

        target_path.write_text(json.dumps(artifact, indent=2))
        return str(target_path)

    def run_discovery_sweep(
        self,
        candidates: List[CandidateHypothesis],
        data_map: Dict[str, pl.DataFrame],
    ) -> List[DiscoveryResult]:
        """
        Execute parameter exploration sweep across multiple candidates.
        """
        results: List[DiscoveryResult] = []
        for cand in candidates:
            clean_sym = cand.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
            df = data_map.get(clean_sym)
            if df is None:
                results.append(
                    DiscoveryResult(
                        candidate_id=cand.candidate_id,
                        strategy_name=cand.strategy_name,
                        symbol=cand.symbol,
                        status=PromotionStatus.EXECUTION_FAILED,
                        error_message=f"Market data DataFrame for '{clean_sym}' not provided.",
                    )
                )
                continue

            res = self.evaluate_candidate(cand, df)
            results.append(res)

        promoted_count = sum(1 for r in results if r.status == PromotionStatus.PROMOTABLE_PAPER_ONLY)
        rejected_count = sum(1 for r in results if r.status == PromotionStatus.REJECTED_GATES)
        logger.info(
            "Discovery Sweep complete: %d evaluated, %d promoted to PROMOTABLE_PAPER_ONLY, %d rejected.",
            len(results),
            promoted_count,
            rejected_count,
        )
        return results
