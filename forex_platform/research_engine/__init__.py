"""
Causal Research Engine: Backtesting, Walk-Forward, and G1–G7 Validation Gates.
"""

from forex_platform.research_engine.backtester import (
    BacktestResult,
    EventDrivenBacktester,
    TradeRecord,
)
from forex_platform.research_engine.evaluate import (
    GateReport,
    GateResult,
    StrategyEvaluator,
)
from forex_platform.research_engine.walkforward import (
    WalkForwardEngine,
    WalkForwardResult,
)

__all__ = [
    "BacktestResult",
    "EventDrivenBacktester",
    "GateReport",
    "GateResult",
    "StrategyEvaluator",
    "TradeRecord",
    "WalkForwardEngine",
    "WalkForwardResult",
]
