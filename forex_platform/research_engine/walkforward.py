"""
Walk-Forward Engine with Strict Chronological Partitioning.
Partitions historical data into DEV (70%), VAL (15%), and OOS (15%) splits
with zero shuffling or cross-boundary leakage.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Tuple
import polars as pl
from pydantic import BaseModel, ConfigDict

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.research_engine.backtester import BacktestResult, EventDrivenBacktester
from forex_platform.strategy_engine.base import BaseStrategy


class WalkForwardResult(BaseModel):
    """Container for Walk-Forward partitioned backtest outcomes."""
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    dev_result: BacktestResult
    val_result: BacktestResult
    oos_result: BacktestResult


class RollingWalkForwardResult(BaseModel):
    """OOS results from multiple chronological rolling windows."""

    model_config = ConfigDict(frozen=True)

    strategy_id: str
    windows: list[WalkForwardResult]


class WalkForwardEngine:
    """
    Executes chronological Walk-Forward analysis across:
    - DEV (Development / In-Sample): 70%
    - VAL (Validation / Tuning):    15%
    - OOS (Out-Of-Sample / Test):   15%
    """

    @classmethod
    def partition_data(
        cls,
        df: pl.DataFrame,
        dev_ratio: float = 0.70,
        val_ratio: float = 0.15,
        oos_ratio: float = 0.15,
    ) -> Tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
        """
        Chronologically partition historical DataFrame without shuffling.
        """
        if abs((dev_ratio + val_ratio + oos_ratio) - 1.0) > 1e-4:
            raise ValueError(f"Ratios must sum to 1.0, got {dev_ratio + val_ratio + oos_ratio}")

        # Ensure strict sort by timestamp
        sorted_df = df.sort("timestamp")
        n = sorted_df.height

        if n < 30:
            raise ValueError(f"Dataset too small ({n} bars) for 3-way chronological partitioning.")

        dev_end = int(n * dev_ratio)
        val_end = dev_end + int(n * val_ratio)

        dev_df = sorted_df.slice(0, dev_end)
        val_df = sorted_df.slice(dev_end, val_end - dev_end)
        oos_df = sorted_df.slice(val_end, n - val_end)

        return dev_df, val_df, oos_df

    @classmethod
    def run_rolling_walkforward(
        cls,
        strategy: BaseStrategy,
        currency_pair: CurrencyPair,
        df: pl.DataFrame,
        *,
        window_bars: int = 1000,
        step_bars: int = 250,
        timeframe: Timeframe = Timeframe.M15,
        cost_multiplier: float = 1.0,
    ) -> RollingWalkForwardResult:
        """Run independent chronological DEV/VAL/OOS windows.

        Each window uses a deep-copied strategy so indicator buffers, signal
        counters, and internal state cannot leak from one partition to the next.
        """
        if window_bars < 30 or step_bars <= 0:
            raise ValueError("window_bars must be >= 30 and step_bars must be positive")
        sorted_df = df.sort("timestamp")
        windows: list[WalkForwardResult] = []
        start = 0
        while start + window_bars <= sorted_df.height:
            window = sorted_df.slice(start, window_bars)
            dev, val, oos = cls.partition_data(window)
            windows.append(
                cls.run_walkforward(
                    deepcopy(strategy), currency_pair, pl.concat([dev, val, oos], how="vertical"),
                    timeframe=timeframe, cost_multiplier=cost_multiplier,
                )
            )
            start += step_bars
        if not windows:
            raise ValueError(
                f"Dataset has {sorted_df.height} bars; need at least {window_bars} for one rolling window"
            )
        return RollingWalkForwardResult(strategy_id=strategy.strategy_id, windows=windows)

    @classmethod
    def run_walkforward(
        cls,
        strategy: BaseStrategy,
        currency_pair: CurrencyPair,
        df: pl.DataFrame,
        timeframe: Timeframe = Timeframe.M15,
        cost_multiplier: float = 1.0,
    ) -> WalkForwardResult:
        """
        Execute sequential walk-forward backtests on DEV, VAL, and OOS slices.
        """
        dev_df, val_df, oos_df = cls.partition_data(df)

        dev_tester = EventDrivenBacktester(deepcopy(strategy), currency_pair, cost_multiplier=cost_multiplier)
        val_tester = EventDrivenBacktester(deepcopy(strategy), currency_pair, cost_multiplier=cost_multiplier)
        oos_tester = EventDrivenBacktester(deepcopy(strategy), currency_pair, cost_multiplier=cost_multiplier)

        dev_res = dev_tester.run(dev_df, timeframe=timeframe)
        val_res = val_tester.run(val_df, timeframe=timeframe)
        oos_res = oos_tester.run(oos_df, timeframe=timeframe)

        return WalkForwardResult(
            strategy_id=strategy.strategy_id,
            dev_result=dev_res,
            val_result=val_res,
            oos_result=oos_res,
        )
