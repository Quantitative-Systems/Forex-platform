"""
Walk-Forward Analysis Engine for Institutional Backtesting.

Part 1: imports, dataclasses, class definition, __init__
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional
from forex_platform.research_engine.backtester import (
    EventDrivenBacktester,
)
from forex_platform.core.domain import to_decimal


@dataclass
class WalkForwardPartition:
    partition_id: int
    is_start: int
    is_end: int
    val_start: int
    val_end: int
    oos_start: int
    oos_end: int


@dataclass
class WalkForwardResult:
    strategy_id: str
    partition_results: list[dict]
    is_expectancy: Decimal
    oos_expectancy: Decimal
    walk_forward_efficiency: float
    total_trades_ois: int
    total_trades_oos: int
    passes_gates: bool


class WalkForwardAnalyzer:
    def __init__(
        self,
        backtester: EventDrivenBacktester,
        i_s_size: float = 0.6,
        validation_size: float = 0.2,
        oos_size: float = 0.2,
        n_overlap: int = 5,
        min_trades: int = 50,
        test_strategy=None,
    ):
        if not (0 < i_s_size < 1):
            raise ValueError("i_s_size must be between 0 and 1")
        if not (0 < validation_size < 1 and 0 < oos_size < 1):
            raise ValueError("validation_size and oos_size must be between 0 and 1")
        if abs((i_s_size + validation_size + oos_size) - 1.0) > 0.01:
            raise ValueError("IS + VAL + OOS fractions must sum to 1.0")
        self.backtester = backtester
        self.i_s_size = i_s_size
        self.validation_size = validation_size
        self.oos_size = oos_size
        self.n_overlap = n_overlap
        self.min_trades = min_trades
        self.test_strategy = test_strategy
    def _partition_indices(self, total_bars: int) -> list[WalkForwardPartition]:
        if total_bars < 100:
            return []
        windows = []
        is_end = int(total_bars * self.i_s_size)
        val_end = is_end + int(total_bars * self.validation_size)
        oos_end = total_bars
        if oos_end <= is_end or val_end <= is_end:
            return []
        windows.append(WalkForwardPartition(
            partition_id=0, is_start=0, is_end=is_end,
            val_start=is_end, val_end=val_end,
            oos_start=val_end, oos_end=oos_end,
        ))
        return windows

    def _run_partition(self, df_subset: pl.DataFrame) -> dict:
        result = self.backtester.run(df_subset)
        return dict(
            total_trades=result.total_trades,
            winning=result.winning_trades,
            losing=result.losing_trades,
            win_rate=result.win_rate,
            profit_factor=result.profit_factor,
            expectancy=float(result.expectancy),
            max_drawdown_pct=result.max_drawdown_pct,
            sharpe_ratio=result.sharpe_ratio,
            final_balance=float(result.final_balance),
        )

    def run_all(self, symbols: list[str] = None) -> list[WalkForwardResult]:
        if symbols is None:
            from forex_platform.market_data.instruments import ALL_SYMBOLS
            symbols = ALL_SYMBOLS
        results = []
        for symbol in symbols:
            r = WalkForwardResult(strategy_id=symbol + "_WFA")
            results.append(r)
        return results

    def evaluate(self, results: list[WalkForwardResult]) -> dict:
        scored = []
        for r in results:
            wfe = 0.0
            is_exp = r.is_expectancy
            oos_exp = r.oos_expectancy
            if r.total_trades_oos >= 1:
                wfe = float(oos_exp / (r.is_expectancy + 1e-10))
            passes = (
                wfe >= 0.6
                and r.total_trades_oos >= self.min_trades
                and is_exp > 0
                and oos_exp > 0
            )
            scored.append(dict(
                strategy_id=r.strategy_id,
                is_expectancy=float(is_exp),
                oos_expectancy=float(oos_exp),
                wfe=wfe,
                oos_trades=r.total_trades_oos,
                is_trades=r.total_trades_ois,
                passes_gates=passes,
            ))
        return scored

