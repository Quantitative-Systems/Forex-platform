"""
Systematic Parameter Calibration Sweeps for Forex Strategies.
Implements grid/random search across 5 native strategy plugins with
walk-forward partitions (DEV 70%, VAL 15%, OOS 15%).
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Callable

import numpy as np
import polars as pl

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.research_engine.backtester import EventDrivenBacktester, BacktestResult
from forex_platform.research_engine.walkforward import WalkForwardEngine, WalkForwardResult
from forex_platform.research_engine.evaluate import StrategyEvaluator, GateReport
from forex_platform.strategy_engine.base import BaseStrategy
from forex_platform.strategy_engine.scalping import AsianRangeFadeScalper
from forex_platform.strategy_engine.session_breakout import LondonSessionBreakout
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy
from forex_platform.strategy_engine.macro_carry import MacroCarryStrategy
from forex_platform.strategy_engine.stat_arb import TriangularStatisticalArbitrage


@dataclass
class ParameterGrid:
    """Defines a parameter grid for systematic search."""
    name: str
    params: Dict[str, List[Any]]

    def generate_combinations(self) -> List[Dict[str, Any]]:
        """Generate all parameter combinations (grid search)."""
        keys = list(self.params.keys())
        values = list(self.params.values())
        combinations = [dict(zip(keys, combo)) for combo in itertools.product(*values)]
        return combinations

    def sample_random(self, n: int, seed: int = 42) -> List[Dict[str, Any]]:
        """Sample n random combinations."""
        rng = random.Random(seed)
        all_combos = self.generate_combinations()
        if n >= len(all_combos):
            return all_combos
        return rng.sample(all_combos, n)


@dataclass
class WalkForwardPartition:
    """Walk-forward data partition indices."""
    dev_start: int
    dev_end: int
    val_start: int
    val_end: int
    oos_start: int
    oos_end: int

    @property
    def dev_size(self) -> int:
        return self.dev_end - self.dev_start

    @property
    def val_size(self) -> int:
        return self.val_end - self.val_start

    @property
    def oos_size(self) -> int:
        return self.oos_end - self.oos_start


@dataclass
class CalibrationResult:
    """Result of a single parameter combination calibration."""
    strategy_name: str
    symbol: str
    timeframe: Timeframe
    parameters: Dict[str, Any]
    dev_result: Optional[BacktestResult] = None
    val_result: Optional[BacktestResult] = None
    oos_result: Optional[BacktestResult] = None
    gate_report: Optional[GateReport] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ============================================================================
# PARAMETER GRIDS FOR EACH STRATEGY
# ============================================================================

ASIAN_SCALPER_GRID = ParameterGrid(
    name="AsianRangeFadeScalper",
    params={
        "period": [14, 16, 18, 20, 22, 24, 26, 28, 30],
        "z_threshold": [1.8, 2.0, 2.2, 2.4, 2.6],
        "lot_size": [0.3, 0.5, 0.7, 1.0],
        "tp_pips": [4.0, 5.0, 6.0, 7.0, 8.0],
        "sl_pips": [8.0, 10.0, 12.0, 15.0],
    }
)

LONDON_BREAKOUT_GRID = ParameterGrid(
    name="LondonSessionBreakout",
    params={
        "lot_size": [0.5, 0.75, 1.0, 1.5],
        "buffer_pips": [1.0, 1.5, 2.0, 2.5, 3.0],
        "atr_period": [10, 12, 14, 16, 18],
    }
)

TREND_CONTINUATION_GRID = ParameterGrid(
    name="TrendContinuationStrategy",
    params={
        "fast_period": [9, 12, 15, 20],
        "slow_period": [21, 26, 30, 40, 50],
        "ker_period": [8, 10, 12, 14],
        "ker_threshold": [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60],
        "lot_size": [0.5, 0.75, 1.0, 1.5],
    }
)

MACRO_CARRY_GRID = ParameterGrid(
    name="MacroCarryStrategy",
    params={
        "vol_shock_threshold": [1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0],
        "lot_size": [0.5, 0.75, 1.0, 1.5],
    }
)

TRIANGULAR_ARB_GRID = ParameterGrid(
    name="TriangularStatisticalArbitrage",
    params={
        "lookback_period": [60, 80, 100, 120, 150, 180, 200, 240],
        "z_threshold": [1.5, 1.75, 2.0, 2.25, 2.5],
        "lot_size": [0.3, 0.5, 0.75, 1.0],
    }
)

STRATEGY_GRIDS = {
    "AsianRangeFadeScalper": ASIAN_SCALPER_GRID,
    "LondonSessionBreakout": LONDON_BREAKOUT_GRID,
    "TrendContinuationStrategy": TREND_CONTINUATION_GRID,
    "MacroCarryStrategy": MACRO_CARRY_GRID,
    "TriangularStatisticalArbitrage": TRIANGULAR_ARB_GRID,
}

STRATEGY_FACTORIES = {
    "AsianRangeFadeScalper": lambda params: AsianRangeFadeScalper(
        strategy_id=f"asian_scalper_{params.get('period', 20)}_{params.get('z_threshold', 2.0)}",
        symbols=params.get("symbols", ["EURUSD", "USDJPY", "AUDUSD"]),
        period=params.get("period", 20),
        z_threshold=params.get("z_threshold", 2.0),
        lot_size=params.get("lot_size", 0.5),
        tp_pips=params.get("tp_pips", 6.0),
        sl_pips=params.get("sl_pips", 10.0),
    ),
    "LondonSessionBreakout": lambda params: LondonSessionBreakout(
        strategy_id=f"london_orb_{params.get('buffer_pips', 2.0)}_{params.get('atr_period', 14)}",
        symbols=params.get("symbols", ["EURUSD", "GBPUSD", "EURGBP"]),
        lot_size=params.get("lot_size", 1.0),
        buffer_pips=params.get("buffer_pips", 2.0),
        atr_period=params.get("atr_period", 14),
    ),
    "TrendContinuationStrategy": lambda params: TrendContinuationStrategy(
        strategy_id=f"trend_cont_{params.get('fast_period', 20)}_{params.get('slow_period', 50)}",
        symbols=params.get("symbols", ["EURUSD", "GBPUSD", "USDJPY"]),
        fast_period=params.get("fast_period", 20),
        slow_period=params.get("slow_period", 50),
        ker_period=params.get("ker_period", 10),
        ker_threshold=params.get("ker_threshold", 0.35),
        lot_size=params.get("lot_size", 1.0),
    ),
    "MacroCarryStrategy": lambda params: MacroCarryStrategy(
        strategy_id=f"macro_carry_{params.get('vol_shock_threshold', 2.0)}",
        symbols=params.get("symbols", ["USDJPY", "EURUSD", "AUDJPY"]),
        vol_shock_threshold=params.get("vol_shock_threshold", 2.0),
        lot_size=params.get("lot_size", 1.0),
    ),
    "TriangularStatisticalArbitrage": lambda params: TriangularStatisticalArbitrage(
        strategy_id=f"tri_arb_{params.get('lookback_period', 30)}_{params.get('z_threshold', 2.0)}",
        base_pair=params.get("base_pair", "EURUSD"),
        target_pair=params.get("target_pair", "GBPUSD"),
        cross_pair=params.get("cross_pair", "EURGBP"),
        lookback_period=params.get("lookback_period", 30),
        z_threshold=params.get("z_threshold", 2.0),
        lot_size=params.get("lot_size", 0.5),
    ),
}

STRATEGY_TIMEFRAMES = {
    "AsianRangeFadeScalper": [Timeframe.M5],
    "LondonSessionBreakout": [Timeframe.M5, Timeframe.M15],
    "TrendContinuationStrategy": [Timeframe.M15, Timeframe.H1],
    "MacroCarryStrategy": [Timeframe.H1, Timeframe.D1],
    "TriangularStatisticalArbitrage": [Timeframe.M1, Timeframe.M5],
}

STRATEGY_SYMBOLS = {
    "AsianRangeFadeScalper": ["EURUSD", "USDJPY", "AUDUSD"],
    "LondonSessionBreakout": ["EURUSD", "GBPUSD", "EURGBP"],
    "TrendContinuationStrategy": ["EURUSD", "GBPUSD", "USDJPY"],
    "MacroCarryStrategy": ["USDJPY", "EURUSD", "AUDJPY"],
    "TriangularStatisticalArbitrage": ["EURUSD", "GBPUSD", "EURGBP"],
}


# ============================================================================
# WALK-FORWARD PARTITIONING
# ============================================================================

def create_walkforward_partitions(
    total_bars: int,
    dev_ratio: float = 0.70,
    val_ratio: float = 0.15,
    oos_ratio: float = 0.15,
    min_dev_bars: int = 1000,
    min_val_bars: int = 200,
    min_oos_bars: int = 200,
) -> List[WalkForwardPartition]:
    """
    Create walk-forward partitions for time series data.
    Returns list of partitions for rolling walk-forward analysis.
    """
    # Validate ratios
    total_ratio = dev_ratio + val_ratio + oos_ratio
    if abs(total_ratio - 1.0) > 1e-6:
        raise ValueError(f"Ratios must sum to 1.0, got {total_ratio}")

    # Calculate sizes
    dev_size = int(total_bars * dev_ratio)
    val_size = int(total_bars * val_ratio)
    oos_size = total_bars - dev_size - val_size

    # Ensure minimums
    if dev_size < min_dev_bars:
        dev_size = min_dev_bars
    if val_size < min_val_bars:
        val_size = min_val_bars
    if oos_size < min_oos_bars:
        oos_size = min_oos_bars

    # Check if we have enough data
    if dev_size + val_size + oos_size > total_bars:
        raise ValueError(f"Insufficient data: need {dev_size + val_size + oos_size} bars, have {total_bars}")

    # Single partition for now (can be extended to rolling)
    partition = WalkForwardPartition(
        dev_start=0,
        dev_end=dev_size,
        val_start=dev_size,
        val_end=dev_size + val_size,
        oos_start=dev_size + val_size,
        oos_end=dev_size + val_size + oos_size,
    )

    return [partition]


def split_dataframe_by_partition(
    df: pl.DataFrame,
    partition: WalkForwardPartition,
) -> Tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Split DataFrame into DEV, VAL, OOS partitions."""
    dev_df = df.slice(partition.dev_start, partition.dev_size)
    val_df = df.slice(partition.val_start, partition.val_size)
    oos_df = df.slice(partition.oos_start, partition.oos_size)
    return dev_df, val_df, oos_df


# ============================================================================
# STRATEGY CALIBRATION ENGINE
# ============================================================================

class StrategyCalibrator:
    """
    Systematic parameter calibration engine for Forex strategies.
    Runs grid/random search with walk-forward validation and G1–G8 gating.
    """

    def __init__(
        self,
        strategy_name: str,
        symbol: str,
        timeframe: Timeframe,
        data: pl.DataFrame,
        evaluator: Optional[StrategyEvaluator] = None,
        random_search: bool = False,
        random_samples: int = 100,
        seed: int = 42,
    ):
        self.strategy_name = strategy_name
        self.symbol = symbol
        self.timeframe = timeframe
        self.data = data
        self.evaluator = evaluator or StrategyEvaluator(
            min_dev_trades=30,
            min_val_trades=10,
            min_oos_trades=10,
        )
        self.random_search = random_search
        self.random_samples = random_samples
        self.seed = seed

        # Get strategy grid and factory
        self.grid = STRATEGY_GRIDS.get(strategy_name)
        if self.grid is None:
            raise ValueError(f"Unknown strategy: {strategy_name}")

        self.factory = STRATEGY_FACTORIES.get(strategy_name)
        if self.factory is None:
            raise ValueError(f"No factory for strategy: {strategy_name}")

        # Create walk-forward partitions
        self.partitions = create_walkforward_partitions(len(data))

    def run_calibration(
        self,
        max_combinations: Optional[int] = None,
        cost_shock_multiplier: float = 2.0,
    ) -> List[CalibrationResult]:
        """
        Run full calibration sweep across parameter grid.
        Returns list of CalibrationResult for each combination.
        """
        # Generate parameter combinations
        if self.random_search:
            combinations = self.grid.sample_random(
                self.random_samples if max_combinations is None else min(self.random_samples, max_combinations),
                seed=self.seed,
            )
        else:
            combinations = self.grid.generate_combinations()
            if max_combinations is not None:
                combinations = combinations[:max_combinations]

        print(f"Running calibration for {self.strategy_name} on {self.symbol} {self.timeframe.value}")
        print(f"Total combinations: {len(combinations)}")
        print(f"Walk-forward partitions: {len(self.partitions)}")

        results: List[CalibrationResult] = []

        for i, params in enumerate(combinations):
            print(f"\n[{i+1}/{len(combinations)}] Testing: {params}")

            try:
                result = self._evaluate_combination(params, cost_shock_multiplier)
                results.append(result)

                # Print summary
                if result.gate_report:
                    status = "PROMOTED" if result.gate_report.passed_all_gates else f"FAILED: {result.gate_report.failed_gate}"
                    print(f"  Result: {status} | OOS Expectancy: {result.oos_result.expectancy if result.oos_result else 'N/A'}")

            except Exception as e:
                print(f"  Error: {e}")
                # Create failed result
                failed_result = CalibrationResult(
                    strategy_name=self.strategy_name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    parameters=params,
                )
                results.append(failed_result)

        return results

    def _evaluate_combination(
        self,
        params: Dict[str, Any],
        cost_shock_multiplier: float,
    ) -> CalibrationResult:
        """Evaluate a single parameter combination across walk-forward partitions."""
        # Add symbol to params
        params = params.copy()
        params["symbols"] = STRATEGY_SYMBOLS.get(self.strategy_name, [self.symbol])

        # Create strategy instance
        strategy = self.factory(params)

        # Use first partition (can be extended to multiple)
        partition = self.partitions[0]
        dev_df, val_df, oos_df = split_dataframe_by_partition(self.data, partition)

        # Run backtests on each partition
        dev_result = self._run_backtest(strategy, dev_df, cost_multiplier=1.0)
        val_result = self._run_backtest(strategy, val_df, cost_multiplier=1.0)
        oos_result = self._run_backtest(strategy, oos_df, cost_multiplier=1.0)

        # Run cost shock test on OOS
        cost_shock_result = self._run_backtest(strategy, oos_df, cost_multiplier=cost_shock_multiplier)

        # Create walk-forward result
        wf_result = WalkForwardResult(
            dev_result=dev_result,
            val_result=val_result,
            oos_result=oos_result,
        )

        # Evaluate through G1–G8 gates
        gate_report = self.evaluator.evaluate(
            strategy=strategy,
            currency_pair=CurrencyPair.from_symbol(self.symbol),
            wf_result=wf_result,
            cost_shock_result=cost_shock_result,
        )

        return CalibrationResult(
            strategy_name=self.strategy_name,
            symbol=self.symbol,
            timeframe=self.timeframe,
            parameters=params,
            dev_result=dev_result,
            val_result=val_result,
            oos_result=oos_result,
            gate_report=gate_report,
        )

    def _run_backtest(
        self,
        strategy: BaseStrategy,
        df: pl.DataFrame,
        cost_multiplier: float = 1.0,
    ) -> BacktestResult:
        """Run backtest on given data."""
        if df.is_empty() or df.height < 10:
            # Return empty result
            from forex_platform.research_engine.backtester import BacktestResult
            return BacktestResult(
                strategy_id=strategy.strategy_id,
                initial_balance=Decimal("100000.00"),
                final_balance=Decimal("100000.00"),
                total_net_pnl=Decimal("0.0"),
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0.0,
                profit_factor=0.0,
                expectancy=Decimal("0.0"),
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                trades=[],
                equity_curve=[],
            )

        backtester = EventDrivenBacktester(
            strategy=strategy,
            currency_pair=CurrencyPair.from_symbol(self.symbol),
            initial_balance=Decimal("100000.00"),
            cost_multiplier=Decimal(str(cost_multiplier)),
        )

        return backtester.run(df, timeframe=self.timeframe)


# ============================================================================
# MULTI-STRATEGY CALIBRATION ORCHESTRATOR
# ============================================================================

class MultiStrategyCalibrator:
    """
    Orchestrates calibration across all 5 strategy plugins.
    """

    def __init__(
        self,
        symbols: List[str],
        timeframes: List[Timeframe],
        years: int = 2,
        cache_dir: Optional[Path] = None,
        evaluator: Optional[StrategyEvaluator] = None,
        random_search: bool = False,
        random_samples: int = 100,
        seed: int = 42,
    ):
        self.symbols = symbols
        self.timeframes = timeframes
        self.years = years
        self.cache_dir = cache_dir or HistoricalECNFetcher.CACHE_DIR
        self.evaluator = evaluator or StrategyEvaluator()
        self.random_search = random_search
        self.random_samples = random_samples
        self.seed = seed

        # Load or download data
        self.data_cache: Dict[str, Dict[Timeframe, pl.DataFrame]] = {}
        self._load_data()

    def _load_data(self) -> None:
        """Load historical data for all symbol/timeframe combinations."""
        fetcher = HistoricalECNFetcher()

        for symbol in self.symbols:
            self.data_cache[symbol] = {}
            for tf in self.timeframes:
                try:
                    df = fetcher.fetch_or_load(
                        symbol=symbol,
                        timeframe=tf,
                        cache_dir=self.cache_dir,
                        auto_generate_synthetic_if_missing=True,
                        synthetic_bars=self.years * 365 * 24 * 4,  # Approximate M15 bars
                    )
                    self.data_cache[symbol][tf] = df
                    print(f"Loaded {df.height} bars for {symbol} {tf.value}")
                except Exception as e:
                    print(f"Failed to load {symbol} {tf.value}: {e}")

    def run_full_calibration(
        self,
        max_combinations_per_strategy: Optional[int] = None,
        cost_shock_multiplier: float = 2.0,
    ) -> Dict[str, List[CalibrationResult]]:
        """
        Run calibration for all strategies on all applicable symbols/timeframes.
        """
        all_results: Dict[str, List[CalibrationResult]] = {}

        for strategy_name, grid in STRATEGY_GRIDS.items():
            strategy_symbols = STRATEGY_SYMBOLS.get(strategy_name, self.symbols)
            strategy_timeframes = STRATEGY_TIMEFRAMES.get(strategy_name, self.timeframes)

            for symbol in strategy_symbols:
                if symbol not in self.data_cache:
                    continue

                for tf in strategy_timeframes:
                    if tf not in self.data_cache[symbol]:
                        continue

                    data = self.data_cache[symbol][tf]
                    if data.height < 1000:
                        print(f"Skipping {strategy_name} {symbol} {tf.value}: insufficient data ({data.height} bars)")
                        continue

                    print(f"\n{'='*60}")
                    print(f"CALIBRATING: {strategy_name} | {symbol} | {tf.value}")
                    print(f"{'='*60}")

                    calibrator = StrategyCalibrator(
                        strategy_name=strategy_name,
                        symbol=symbol,
                        timeframe=tf,
                        data=data,
                        evaluator=self.evaluator,
                        random_search=self.random_search,
                        random_samples=self.random_samples,
                        seed=self.seed,
                    )

                    results = calibrator.run_calibration(
                        max_combinations=max_combinations_per_strategy,
                        cost_shock_multiplier=cost_shock_multiplier,
                    )

                    key = f"{strategy_name}_{symbol}_{tf.value}"
                    all_results[key] = results

        return all_results

    def save_results(
        self,
        results: Dict[str, List[CalibrationResult]],
        output_dir: Path = Path("research/calibration"),
    ) -> None:
        """Save calibration results to JSON files."""
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        for key, result_list in results.items():
            # Save all results
            output_file = output_dir / f"{key}_calibration_{timestamp}.json"

            serializable_results = []
            for r in result_list:
                serializable_results.append({
                    "strategy_name": r.strategy_name,
                    "symbol": r.symbol,
                    "timeframe": r.timeframe.value,
                    "parameters": r.parameters,
                    "dev_result": r.dev_result.model_dump() if r.dev_result else None,
                    "val_result": r.val_result.model_dump() if r.val_result else None,
                    "oos_result": r.oos_result.model_dump() if r.oos_result else None,
                    "gate_report": r.gate_report.model_dump() if r.gate_report else None,
                    "timestamp": r.timestamp,
                })

            with open(output_file, 'w') as f:
                json.dump(serializable_results, f, indent=2, default=str)

            print(f"Saved {len(result_list)} results to {output_file}")

            # Save promoted results separately
            promoted = [r for r in result_list if r.gate_report and r.gate_report.passed_all_gates]
            if promoted:
                promo_file = output_dir / f"{key}_PROMOTED_{timestamp}.json"
                promo_data = []
                for r in promoted:
                    promo_data.append({
                        "strategy_name": r.strategy_name,
                        "symbol": r.symbol,
                        "timeframe": r.timeframe.value,
                        "parameters": r.parameters,
                        "oos_expectancy": float(r.oos_result.expectancy) if r.oos_result else 0.0,
                        "oos_sharpe": r.oos_result.sharpe_ratio if r.oos_result else 0.0,
                        "oos_max_dd": r.oos_result.max_drawdown_pct if r.oos_result else 0.0,
                        "gate_report": r.gate_report.model_dump(),
                        "timestamp": r.timestamp,
                    })
                with open(promo_file, 'w') as f:
                    json.dump(promo_data, f, indent=2, default=str)
                print(f"Saved {len(promoted)} PROMOTED results to {promo_file}")


# ============================================================================
# CLI INTEGRATION
# ============================================================================

def add_calibrate_parser(subparsers):
    """Add calibrate command to CLI parser."""
    parser = subparsers.add_parser(
        "calibrate",
        help="Run systematic parameter calibration sweeps across all strategies"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="EURUSD,GBPUSD,USDJPY,EURGBP",
        help="Comma-separated list of symbols"
    )
    parser.add_argument(
        "--timeframes",
        type=str,
        default="M5,M15,H1",
        help="Comma-separated list of timeframes"
    )
    parser.add_argument(
        "--years",
        type=int,
        default=2,
        help="Years of historical data to use"
    )
    parser.add_argument(
        "--strategies",
        type=str,
        default="all",
        help="Comma-separated list of strategies (or 'all')"
    )
    parser.add_argument(
        "--max-combinations",
        type=int,
        default=None,
        help="Max combinations per strategy (default: full grid)"
    )
    parser.add_argument(
        "--random-search",
        action="store_true",
        help="Use random search instead of grid search"
    )
    parser.add_argument(
        "--random-samples",
        type=int,
        default=100,
        help="Number of random samples for random search"
    )
    parser.add_argument(
        "--cost-shock",
        type=float,
        default=2.0,
        help="Cost shock multiplier for G5 gate (default: 2.0)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="research/calibration",
        help="Output directory for results"
    )
    parser.set_defaults(func=cmd_calibrate)
    return parser


def cmd_calibrate(args) -> int:
    """CLI command to run calibration sweeps."""
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    timeframes = [Timeframe[tf.strip().upper()] for tf in args.timeframes.split(",")]
    output_dir = Path(args.output_dir)

    print("=" * 70)
    print(" SYSTEMATIC PARAMETER CALIBRATION SWEEP")
    print("=" * 70)
    print(f" Symbols:           {', '.join(symbols)}")
    print(f" Timeframes:        {', '.join(tf.value for tf in timeframes)}")
    print(f" Years of data:     {args.years}")
    print(f" Strategies:        {args.strategies}")
    print(f" Max combinations:  {args.max_combinations or 'full grid'}")
    print(f" Search mode:       {'Random' if args.random_search else 'Grid'}")
    if args.random_search:
        print(f" Random samples:    {args.random_samples}")
    print(f" Cost shock mult:   {args.cost_shock}x")
    print(f" Output directory:  {output_dir}")
    print("-" * 70)

    try:
        calibrator = MultiStrategyCalibrator(
            symbols=symbols,
            timeframes=timeframes,
            years=args.years,
            random_search=args.random_search,
            random_samples=args.random_samples,
        )

        results = calibrator.run_full_calibration(
            max_combinations_per_strategy=args.max_combinations,
            cost_shock_multiplier=args.cost_shock,
        )

        calibrator.save_results(results, output_dir)

        # Summary
        print("\n" + "=" * 70)
        print(" CALIBRATION SUMMARY")
        print("=" * 70)

        total_tested = 0
        total_promoted = 0
        for key, result_list in results.items():
            promoted = [r for r in result_list if r.gate_report and r.gate_report.passed_all_gates]
            total_tested += len(result_list)
            total_promoted += len(promoted)
            print(f"  {key}: {len(result_list)} tested, {len(promoted)} promoted")

        print(f"\n TOTAL: {total_tested} combinations tested, {total_promoted} promoted")
        print("=" * 70)

        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    # Quick test
    import sys
    logging.basicConfig(level=logging.INFO)

    # Test parameter grid generation
    print("Testing parameter grids...")
    for name, grid in STRATEGY_GRIDS.items():
        combos = grid.generate_combinations()
        print(f"  {name}: {len(combos)} combinations")

    print("\nAll grids generated successfully!")