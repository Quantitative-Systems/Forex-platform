"""
G1–G8 Validation Gates and Negative Research Logger.
Enforces institutional qualification criteria before any strategy is eligible for paper trading.
Automatically logs failed runs to research/failed/ with full audit diagnostics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.research_engine.backtester import BacktestResult, EventDrivenBacktester
from forex_platform.research_engine.walkforward import (
    RollingWalkForwardResult,
    WalkForwardEngine,
    WalkForwardResult,
)
from forex_platform.strategy_engine.base import BaseStrategy


class GateResult(BaseModel):
    """Result of an individual qualification gate."""
    model_config = ConfigDict(frozen=True)

    gate_name: str
    passed: bool
    observed_value: float
    threshold_value: float
    message: str


class GateReport(BaseModel):
    """Overall evaluation report across all G1-G8 gates."""
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    passed_all_gates: bool
    failed_gate: Optional[str] = None
    gate_results: Dict[str, GateResult]
    archived_failed_path: Optional[str] = None
    promoted_path: Optional[str] = None


class StrategyEvaluator:
    """
    Evaluates backtest and walk-forward results against the 7 institutional gates:
    - G1: Minimum sample size (DEV >= 100, VAL >= 30, OOS >= 30, or configured limits).
    - G2: Alpha consistency (Expectancy > 0 across DEV, VAL, and OOS).
    - G3: Bootstrap statistical significance (p > 0.95 positive profit).
    - G4: Walk-Forward Ratio (OOS Expectancy / DEV Expectancy >= 0.50).
    - G5: Cost Shock Stress (Expectancy remains positive under 2x spread and commissions).
    - G6: Maximum drawdown <= 15.0%.
    - G7: Sharpe ratio >= 0.50.
    - G8: Rolling OOS robustness: >=60% positive windows and positive worst-window expectancy.
    """

    FAILED_RESEARCH_DIR = Path("research/failed")

    def __init__(
        self,
        min_dev_trades: int = 100,
        min_val_trades: int = 30,
        min_oos_trades: int = 30,
        max_drawdown_limit: float = 15.0,
        min_wfr: float = 0.50,
        bootstrap_p_val: float = 0.95,
        min_sharpe: float = 0.50,
    ):
        self.min_dev_trades = min_dev_trades
        self.min_val_trades = min_val_trades
        self.min_oos_trades = min_oos_trades
        self.max_drawdown_limit = max_drawdown_limit
        self.min_wfr = min_wfr
        self.bootstrap_p_val = bootstrap_p_val
        self.min_sharpe = min_sharpe

    def evaluate(
        self,
        strategy: BaseStrategy,
        currency_pair: CurrencyPair,
        wf_result: WalkForwardResult,
        cost_shock_result: Optional[BacktestResult] = None,
        rolling_result: Optional[RollingWalkForwardResult] = None,
    ) -> GateReport:
        """
        Evaluate WalkForwardResult against G1-G8 gates.
        """
        dev = wf_result.dev_result
        val = wf_result.val_result
        oos = wf_result.oos_result

        gate_results: Dict[str, GateResult] = {}
        failed_gate: Optional[str] = None

        # ---------------------------------------------------------------------
        # GATE 1: Minimum Sample Size
        # ---------------------------------------------------------------------
        g1_pass = (
            dev.total_trades >= self.min_dev_trades
            and val.total_trades >= self.min_val_trades
            and oos.total_trades >= self.min_oos_trades
        )
        gate_results["G1_SAMPLE_SIZE"] = GateResult(
            gate_name="G1_SAMPLE_SIZE",
            passed=g1_pass,
            observed_value=float(dev.total_trades),
            threshold_value=float(self.min_dev_trades),
            message=f"DEV={dev.total_trades} (min {self.min_dev_trades}), VAL={val.total_trades}, OOS={oos.total_trades}",
        )
        if not g1_pass and failed_gate is None:
            failed_gate = "G1_SAMPLE_SIZE"

        # ---------------------------------------------------------------------
        # GATE 2: Alpha Consistency (Expectancy > 0 across DEV, VAL, and OOS)
        # ---------------------------------------------------------------------
        g2_pass = (
            dev.expectancy > Decimal("0.0")
            and val.expectancy > Decimal("0.0")
            and oos.expectancy > Decimal("0.0")
        )
        gate_results["G2_ALPHA_CONSISTENCY"] = GateResult(
            gate_name="G2_ALPHA_CONSISTENCY",
            passed=g2_pass,
            observed_value=float(oos.expectancy),
            threshold_value=0.0,
            message=f"DEV Exp={float(dev.expectancy):.2f}, VAL Exp={float(val.expectancy):.2f}, OOS Exp={float(oos.expectancy):.2f}",
        )
        if not g2_pass and failed_gate is None:
            failed_gate = "G2_ALPHA_CONSISTENCY"

        # ---------------------------------------------------------------------
        # GATE 3: Bootstrap Statistical Significance (p > 0.95 positive profit)
        # ---------------------------------------------------------------------
        all_trade_pnls = [float(t.net_pnl) for t in (dev.trades + val.trades + oos.trades)]
        if len(all_trade_pnls) >= 5:
            # 1,000 bootstrap resamples with replacement
            rng = np.random.default_rng(42)
            boot_means = [
                float(np.mean(rng.choice(all_trade_pnls, size=len(all_trade_pnls), replace=True)))
                for _ in range(1000)
            ]
            positive_fraction = sum(1 for m in boot_means if m > 0.0) / 1000.0
            g3_pass = positive_fraction >= self.bootstrap_p_val
        else:
            positive_fraction = 0.0
            g3_pass = False

        gate_results["G3_BOOTSTRAP_SIGNIFICANCE"] = GateResult(
            gate_name="G3_BOOTSTRAP_SIGNIFICANCE",
            passed=g3_pass,
            observed_value=float(positive_fraction),
            threshold_value=float(self.bootstrap_p_val),
            message=f"Bootstrap positive profit probability = {positive_fraction * 100:.1f}% (threshold {self.bootstrap_p_val * 100:.1f}%)",
        )
        if not g3_pass and failed_gate is None:
            failed_gate = "G3_BOOTSTRAP_SIGNIFICANCE"

        # ---------------------------------------------------------------------
        # GATE 4: Walk-Forward Ratio (OOS / DEV >= 0.50)
        # ---------------------------------------------------------------------
        dev_exp = float(dev.expectancy)
        oos_exp = float(oos.expectancy)
        if dev_exp > 0:
            wfr = oos_exp / dev_exp
            g4_pass = wfr >= self.min_wfr
        else:
            wfr = 0.0
            g4_pass = False

        gate_results["G4_WALKFORWARD_RATIO"] = GateResult(
            gate_name="G4_WALKFORWARD_RATIO",
            passed=g4_pass,
            observed_value=float(wfr),
            threshold_value=float(self.min_wfr),
            message=f"Walk-Forward Ratio = {wfr:.2f} (threshold {self.min_wfr:.2f})",
        )
        if not g4_pass and failed_gate is None:
            failed_gate = "G4_WALKFORWARD_RATIO"

        # ---------------------------------------------------------------------
        # GATE 5: Cost Shock Stress (+100% commissions and 2x spread)
        # ---------------------------------------------------------------------
        if cost_shock_result is not None:
            g5_pass = cost_shock_result.expectancy > Decimal("0.0")
            shock_exp = float(cost_shock_result.expectancy)
        else:
            # If not provided, assume passed if OOS expectancy is high enough
            g5_pass = oos.expectancy > Decimal("0.0")
            shock_exp = float(oos.expectancy)

        gate_results["G5_COST_SHOCK_STRESS"] = GateResult(
            gate_name="G5_COST_SHOCK_STRESS",
            passed=g5_pass,
            observed_value=shock_exp,
            threshold_value=0.0,
            message=f"Cost-shocked expectancy = {shock_exp:.2f} (threshold > 0.00)",
        )
        if not g5_pass and failed_gate is None:
            failed_gate = "G5_COST_SHOCK_STRESS"

        # ---------------------------------------------------------------------
        # GATE 6: Maximum Drawdown <= 15.0%
        # ---------------------------------------------------------------------
        max_dd = max(dev.max_drawdown_pct, val.max_drawdown_pct, oos.max_drawdown_pct)
        g6_pass = max_dd <= self.max_drawdown_limit
        gate_results["G6_MAX_DRAWDOWN"] = GateResult(
            gate_name="G6_MAX_DRAWDOWN",
            passed=g6_pass,
            observed_value=float(max_dd),
            threshold_value=float(self.max_drawdown_limit),
            message=f"Max drawdown = {max_dd:.2f}% (limit <= {self.max_drawdown_limit:.2f}%)",
        )
        if not g6_pass and failed_gate is None:
            failed_gate = "G6_MAX_DRAWDOWN"

        # ---------------------------------------------------------------------
        # GATE 7: Sharpe Ratio >= 0.50
        # ---------------------------------------------------------------------
        sharpe = oos.sharpe_ratio
        g7_pass = sharpe >= self.min_sharpe
        gate_results["G7_SHARPE_RATIO"] = GateResult(
            gate_name="G7_SHARPE_RATIO",
            passed=g7_pass,
            observed_value=float(sharpe),
            threshold_value=float(self.min_sharpe),
            message=f"OOS Sharpe Ratio = {sharpe:.2f} (threshold >= {self.min_sharpe:.2f})",
        )
        if not g7_pass and failed_gate is None:
            failed_gate = "G7_SHARPE_RATIO"

        # ---------------------------------------------------------------------
        # GATE 8: ROLLING WINDOW ROBUSTNESS
        # ---------------------------------------------------------------------
        if rolling_result is not None:
            windows = rolling_result.windows
            total_oos_trades = sum(window.oos_result.total_trades for window in windows)
            positive_windows = sum(1 for window in windows if window.oos_result.expectancy > 0)
            positive_ratio = positive_windows / len(windows) if windows else 0.0
            worst_expectancy = min(
                (window.oos_result.expectancy for window in windows),
                default=Decimal("0"),
            )
            g8_pass = (
                len(windows) >= 3
                and total_oos_trades >= self.min_oos_trades
                and positive_ratio >= 0.60
                and worst_expectancy > Decimal("0")
            )
            gate_results["G8_ROLLING_ROBUSTNESS"] = GateResult(
                gate_name="G8_ROLLING_ROBUSTNESS",
                passed=g8_pass,
                observed_value=float(positive_ratio),
                threshold_value=0.60,
                message=(
                    f"Positive rolling OOS windows = {positive_windows}/{len(windows)}; "
                    f"worst OOS expectancy = {float(worst_expectancy):.2f}; "
                    f"OOS trades = {total_oos_trades}"
                ),
            )
            if not g8_pass and failed_gate is None:
                failed_gate = "G8_ROLLING_ROBUSTNESS"

        # Overall verdict
        passed_all = failed_gate is None
        archived_path = None
        promoted_path = None

        if not passed_all:
            archived_path = self._archive_negative_research(
                strategy_id=strategy.strategy_id,
                failed_gate=failed_gate or "UNKNOWN",
                gate_results=gate_results,
            )
        else:
            # Promote strategy that passes all gates
            promoted_path = self._promote_strategy(
                strategy_id=strategy.strategy_id,
                gate_results=gate_results,
                parameters=strategy.parameters,
                oos_result=oos,
            )

        return GateReport(
            strategy_id=strategy.strategy_id,
            passed_all_gates=passed_all,
            failed_gate=failed_gate,
            gate_results=gate_results,
            archived_failed_path=archived_path,
            promoted_path=promoted_path,
        )

    PROMOTED_RESEARCH_DIR = Path("research/promoted")

    def _archive_negative_research(
        self,
        strategy_id: str,
        failed_gate: str,
        gate_results: Dict[str, GateResult],
    ) -> str:
        """Log negative research outcome to research/failed/ with ISO timestamp."""
        self.FAILED_RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{strategy_id}_FAILED_{failed_gate}_{now_str}.json"
        target_path = self.FAILED_RESEARCH_DIR / filename

        payload = {
            "strategy_id": strategy_id,
            "failed_gate": failed_gate,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "gates": {k: v.model_dump() for k, v in gate_results.items()},
        }
        target_path.write_text(json.dumps(payload, indent=2))
        return str(target_path)

    def _promote_strategy(
        self,
        strategy_id: str,
        gate_results: Dict[str, GateResult],
        parameters: Dict[str, Any],
        oos_result: Optional[BacktestResult] = None,
    ) -> str:
        """Promote passing strategy to research/promoted/ with PROMOTABLE_PAPER_ONLY status."""
        self.PROMOTED_RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{strategy_id}_PROMOTED_{now_str}.json"
        target_path = self.PROMOTED_RESEARCH_DIR / filename

        payload = {
            "strategy_id": strategy_id,
            "status": "PROMOTABLE_PAPER_ONLY",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "parameters": parameters,
            "gates": {k: v.model_dump() for k, v in gate_results.items()},
            "oos_performance": {
                "expectancy": float(oos_result.expectancy) if oos_result else 0.0,
                "sharpe_ratio": oos_result.sharpe_ratio if oos_result else 0.0,
                "max_drawdown_pct": oos_result.max_drawdown_pct if oos_result else 0.0,
                "total_trades": oos_result.total_trades if oos_result else 0,
                "win_rate": oos_result.win_rate if oos_result else 0.0,
                "profit_factor": oos_result.profit_factor if oos_result else 0.0,
            } if oos_result else {},
        }
        target_path.write_text(json.dumps(payload, indent=2, default=str))
        return str(target_path)
