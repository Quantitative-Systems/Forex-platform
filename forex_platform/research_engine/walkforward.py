"""
Walk-Forward Engine with Strict Chronological Partitioning.
Partitions historical data into DEV (70%), VAL (15%), and OOS (15%) splits
with zero shuffling or cross-boundary leakage.
"""

from __future__ import annotations

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

        dev_tester = EventDrivenBacktester(strategy, currency_pair, cost_multiplier=cost_multiplier)
        val_tester = EventDrivenBacktester(strategy, currency_pair, cost_multiplier=cost_multiplier)
        oos_tester = EventDrivenBacktester(strategy, currency_pair, cost_multiplier=cost_multiplier)

        dev_res = dev_tester.run(dev_df, timeframe=timeframe)
        val_res = val_tester.run(val_df, timeframe=timeframe)
        oos_res = oos_tester.run(oos_df, timeframe=timeframe)

        return WalkForwardResult(
            strategy_id=strategy.strategy_id,
            dev_result=dev_res,
            val_result=val_res,
            oos_result=oos_res,
        )
