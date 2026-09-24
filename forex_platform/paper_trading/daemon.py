"""
Forward Paper Trading Daemon.
Executes real-time forward paper trading by streaming market events through
PreTradeRiskFirewall, SmartOrderRouter, MicrostructurePaperSimulator, and SQLitePaperLedger.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    Fill,
    OrderIntent,
    Position,
)
from forex_platform.costs.swap_engine import SwapEngine
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.paper_trading.persistence import SQLitePaperLedger
from forex_platform.paper_trading.simulator import MicrostructurePaperSimulator
from forex_platform.risk_engine.firewall import MarketTelemetry
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class ForwardPaperTradingDaemon:
    """
    Automated forward paper trading coordinator with full persistence and crash recovery.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        oms: OrderManagementSystem,
        simulator: MicrostructurePaperSimulator,
        ledger: SQLitePaperLedger,
        currency_pair: CurrencyPair,
    ):
        self.strategy = strategy
        self.oms = oms
        self.simulator = simulator
        self.ledger = ledger
        self.pair = currency_pair
        self._last_processed_time: Optional[datetime] = None

    def recover(self) -> None:
        """Crash recovery: restore state from SQLite ledger."""
        active_orders, open_positions, recovered_balance = self.ledger.recover_state()
        self.oms.balance = recovered_balance
        for pos in open_positions:
            self.oms._open_positions[pos.symbol] = pos
        for ord_ in active_orders:
            self.oms._orders[ord_.order_id] = ord_

    def on_tick_or_bar(
        self,
        event: BarEvent,
        telemetry: MarketTelemetry,
    ) -> List[Fill]:
        """
        Process incoming market event:
        1. Check overnight rollover swap.
        2. Update live quotes in OMS.
        3. Invoke strategy on_bar.
        4. Route signals through PreTradeRiskFirewall.
        5. Simulate fills and persist to SQLite ledger.
        """
        now = event.timestamp if event.timestamp.tzinfo else event.timestamp.replace(tzinfo=timezone.utc)
        clean_sym = event.symbol.upper().replace("/", "").replace("_", "")

        # 1. Update live quotes in OMS
        self.oms.update_quotes(
            symbol=clean_sym,
            bid=telemetry.bid,
            ask=telemetry.ask,
        )

        # 2. Check 21:00 UTC rollover swap
        if self._last_processed_time is not None:
            prev = self._last_processed_time
            if prev.hour < 21 <= now.hour or (prev.date() < now.date() and now.hour >= 21):
                open_pos = self.oms.get_position(clean_sym)
                if open_pos:
                    SwapEngine.apply_rollover_to_position(
                        position=open_pos,
                        pair=self.pair,
                        rollover_dt=now,
                        long_swap_pips=Decimal("-0.6"),
                        short_swap_pips=Decimal("0.2"),
                    )
                    self.ledger.save_position(open_pos)

        self._last_processed_time = now

        # 3. Invoke strategy to generate intents
        intents = self.strategy.on_bar(event)
        executed_fills: List[Fill] = []

        for intent in intents:
            decision, orders = self.oms.submit_intent(
                intent=intent,
                current_time=now,
                telemetry=telemetry,
                strategy_id=self.strategy.strategy_id,
            )
            # Record risk decision
            self.ledger.save_risk_decision(intent.intent_id, decision, now)

            if decision.approved and orders:
                for ord_ in orders:
                    # Save submitted order
                    self.ledger.save_order(ord_)
                    # Simulate fill
                    fill = self.simulator.simulate_execution(ord_, telemetry, now)
                    pos = self.oms.process_fill(fill)

                    # Persist fill, order update, and position state
                    self.ledger.save_fill(fill)
                    self.ledger.save_order(ord_)
                    self.ledger.save_position(pos)
                    executed_fills.append(fill)

        # 4. Record periodic equity snapshot
        summary = self.oms.get_account_summary()
        self.ledger.save_equity_snapshot(
            timestamp=now,
            balance=summary.balance,
            equity=summary.equity,
            unrealized_pnl=summary.unrealized_pnl,
            realized_pnl=summary.realized_pnl,
        )

        return executed_fills
