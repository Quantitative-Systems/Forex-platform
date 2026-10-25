#!/usr/bin/env python3
"""
Correlated Risk Management
===========================
Position sizing and correlation penalties for multi-set fractal strategy.

Rules (from platform_enhancement_spec.md):
- 1% risk per trade (account equity)
- Correlation penalty: 0.05R per additional aligned set
- Max 3% daily drawdown circuit breaker
- Independent tracking per set, aggregated at account level
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum


class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class TradeIntent:
    pair: str
    set_key: str
    direction: Direction
    entry_price: float
    stop_price: float
    target_price: float
    risk_r: float = 1.0      # Base risk in R (1% of equity)
    rr_ratio: float = 4.0    # Reward:Risk ratio


@dataclass
class CorrelatedSizingResult:
    trade_intent: TradeIntent
    adjusted_risk_r: float
    lot_size: float
    correlation_penalty_r: float
    aligned_sets: List[str]
    approved: bool
    rejection_reason: Optional[str] = None


class CorrelatedRiskManager:
    def __init__(
        self,
        account_equity: float = 100_000.0,
        risk_per_trade_pct: float = 0.01,
        correlation_penalty_step: float = 0.05,
        max_daily_dd_pct: float = 0.03,
        rr_floor: float = 4.0
    ):
        self.account_equity = account_equity
        self.risk_per_trade_pct = risk_per_trade_pct
        self.correlation_penalty_step = correlation_penalty_step
        self.max_daily_dd_pct = max_daily_dd_pct
        self.rr_floor = rr_floor

        self.open_positions: Dict[str, List[TradeIntent]] = {}  # pair -> list of intents
        self.daily_pnl_r = 0.0
        self.trading_halted = False

    def calculate_lot_size(self, pair: str, risk_usd: float, stop_pips: float, pip_value: float) -> float:
        """Standard lot calculation: risk / (stop_pips * pip_value)."""
        if stop_pips <= 0 or pip_value <= 0:
            return 0.0
        return risk_usd / (stop_pips * pip_value)

    def get_aligned_sets(self, pair: str, direction: Direction) -> List[str]:
        """Find other sets with open positions in same direction on same pair."""
        aligned = []
        if pair in self.open_positions:
            for intent in self.open_positions[pair]:
                if intent.direction == direction:
                    aligned.append(intent.set_key)
        return aligned

    def compute_correlation_penalty(self, aligned_count: int) -> float:
        """0.05R per additional aligned set beyond the first."""
        if aligned_count <= 1:
            return 0.0
        return self.correlation_penalty_step * (aligned_count - 1)

    def evaluate_intent(self, intent: TradeIntent, pip_value: float) -> CorrelatedSizingResult:
        """Full risk evaluation with correlation penalty."""
        # Daily circuit breaker
        if self.trading_halted:
            return CorrelatedSizingResult(
                trade_intent=intent,
                adjusted_risk_r=0.0,
                lot_size=0.0,
                correlation_penalty_r=0.0,
                aligned_sets=[],
                approved=False,
                rejection_reason="DAILY_DD_CIRCUIT_BREAKER"
            )

        # RR floor check
        if intent.rr_ratio < self.rr_floor:
            return CorrelatedSizingResult(
                trade_intent=intent,
                adjusted_risk_r=0.0,
                lot_size=0.0,
                correlation_penalty_r=0.0,
                aligned_sets=[],
                approved=False,
                rejection_reason=f"RR_BELOW_FLOOR ({intent.rr_ratio} < {self.rr_floor})"
            )

        # Correlation penalty
        aligned = self.get_aligned_sets(intent.pair, intent.direction)
        penalty = self.compute_correlation_penalty(len(aligned))

        # Adjusted risk per trade
        base_risk_usd = self.account_equity * self.risk_per_trade_pct
        adjusted_risk_r = max(0.01, 1.0 - penalty)  # Minimum 0.01R to avoid zero
        adjusted_risk_usd = base_risk_usd * adjusted_risk_r

        # Lot size
        stop_pips = abs(intent.entry_price - intent.stop_price) * 10000  # Assuming 4-decimal pairs
        lot_size = self.calculate_lot_size(intent.pair, adjusted_risk_usd, stop_pips, pip_value)

        # Final RR after penalty
        effective_rr = intent.rr_ratio * adjusted_risk_r

        approved = effective_rr >= 0.95 * self.rr_floor

        return CorrelatedSizingResult(
            trade_intent=intent,
            adjusted_risk_r=adjusted_risk_r,
            lot_size=lot_size,
            correlation_penalty_r=penalty,
            aligned_sets=aligned,
            approved=approved,
            rejection_reason=None if approved else f"EFFECTIVE_RR_BELOW_THRESHOLD ({effective_rr:.2f})"
        )

    def on_fill(self, result: CorrelatedSizingResult):
        """Track open position after fill."""
        if not result.approved:
            return
        pair = result.trade_intent.pair
        if pair not in self.open_positions:
            self.open_positions[pair] = []
        self.open_positions[pair].append(result.trade_intent)

    def on_exit(self, pair: str, set_key: str, pnl_r: float):
        """Remove closed position, update daily P&L."""
        if pair in self.open_positions:
            self.open_positions[pair] = [
                t for t in self.open_positions[pair]
                if t.set_key != set_key
            ]
        self.daily_pnl_r += pnl_r

        # Check daily DD circuit
        if self.daily_pnl_r <= -self.max_daily_dd_pct:
            self.trading_halted = True
            print(f"[RISK] DAILY DD CIRCUIT BREAKER TRIGGERED: {self.daily_pnl_r:.2%}")

    def reset_daily(self):
        """Call at start of new trading day."""
        self.daily_pnl_r = 0.0
        self.trading_halted = False


# Pip values per standard lot (100k) for major pairs
PIP_VALUES = {
    "EURUSD": 10.0,
    "GBPUSD": 10.0,
    "USDJPY": 10.0 / 150.0,  # Approximate
    "AUDUSD": 10.0,
    "USDCAD": 10.0 / 1.35,
    "USDCHF": 10.0 / 0.90,
    "NZDUSD": 10.0,
}


def get_pip_value(pair: str) -> float:
    return PIP_VALUES.get(pair, 10.0)


if __name__ == "__main__":
    # Demo
    risk = CorrelatedRiskManager(account_equity=100_000)

    intent1 = TradeIntent(
        pair="GBPUSD", set_key="SET_5", direction=Direction.LONG,
        entry_price=1.3000, stop_price=1.2997, target_price=1.3012,
        rr_ratio=4.0
    )
    r1 = risk.evaluate_intent(intent1, get_pip_value("GBPUSD"))
    print(f"First trade: {r1}")

    intent2 = TradeIntent(
        pair="GBPUSD", set_key="SET_4", direction=Direction.LONG,
        entry_price=1.3001, stop_price=1.2998, target_price=1.3013,
        rr_ratio=4.0
    )
    r2 = risk.evaluate_intent(intent2, get_pip_value("GBPUSD"))
    print(f"Second trade (aligned): {r2}")

    intent3 = TradeIntent(
        pair="GBPUSD", set_key="SET_3", direction=Direction.SHORT,
        entry_price=1.2995, stop_price=1.2998, target_price=1.2983,
        rr_ratio=4.0
    )
    r3 = risk.evaluate_intent(intent3, get_pip_value("GBPUSD"))
    print(f"Third trade (opposite): {r3}")