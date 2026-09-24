"""
Order Management System (OMS) and In-Memory Trading Ledger.
Coordinates order lifecycle, position tracking, fill attribution, and real-time PnL accounting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    Fill,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PipCalculator,
    Position,
    to_decimal,
)
from forex_platform.order_management.router import SmartOrderRouter
from forex_platform.order_management.state_machine import (
    IllegalStateTransitionError,
    OrderLifecycleState,
    OrderStateMachine,
    ReconciliationAlert,
)
from forex_platform.risk_engine.firewall import (
    MarketTelemetry,
    PreTradeRiskFirewall,
    RiskDecision,
)
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch


class AccountSummary(BaseModel):
    """Snapshot of account equity, margin, and PnL."""
    model_config = ConfigDict(frozen=True)

    balance: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    total_commission: Decimal
    total_swap: Decimal
    equity: Decimal
    open_position_count: int


class OrderManagementSystem:
    """
    Central Order Management System and Ledger.
    """

    def __init__(
        self,
        firewall: PreTradeRiskFirewall,
        router: SmartOrderRouter,
        kill_switch: HierarchicalKillSwitch,
        initial_balance: Decimal | float | str = Decimal("100000.00"),
    ):
        self.firewall = firewall
        self.router = router
        self.kill_switch = kill_switch
        self.initial_balance = to_decimal(initial_balance)
        self.balance = self.initial_balance

        # Order storage: order_id -> ExecutionOrder
        self._orders: Dict[str, ExecutionOrder] = {}
        # Order state machines: order_id -> OrderStateMachine
        self._state_machines: Dict[str, OrderStateMachine] = {}
        # Open positions: symbol -> Position
        self._open_positions: Dict[str, Position] = {}
        # Historical closed positions
        self._closed_positions: List[Position] = []
        # Fills: fill_id -> Fill
        self._fills: Dict[str, Fill] = {}
        # Latest quotes: symbol -> (bid, ask, quote_to_account_rate)
        self._latest_quotes: Dict[str, Tuple[Decimal, Decimal, Decimal]] = {}
        # Reconciliation alerts
        self._alerts: List[ReconciliationAlert] = []

    def _on_reconciliation_alert(self, alert: ReconciliationAlert) -> None:
        """Handle UNKNOWN state by locking currency pair and logging alert."""
        self._alerts.append(alert)
        # Lock the currency pair across the platform
        self.kill_switch.arm_pair(alert.symbol, f"UNKNOWN order {alert.order_id} triggered pair lock")

    def submit_intent(
        self,
        intent: OrderIntent,
        current_time: datetime,
        telemetry: Optional[MarketTelemetry] = None,
        tenant_id: Optional[str] = None,
        broker_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        is_carry_order: bool = False,
    ) -> Tuple[RiskDecision, List[ExecutionOrder]]:
        """
        Pass intent through PreTradeRiskFirewall. If approved, slice and route through SmartOrderRouter.
        """
        decision = self.firewall.evaluate_order(
            intent=intent,
            current_time=current_time,
            telemetry=telemetry,
            tenant_id=tenant_id,
            broker_id=broker_id,
            strategy_id=strategy_id,
            is_carry_order=is_carry_order,
        )

        if not decision.approved:
            return decision, []

        # If sizing dampener is active (< 1.0), adjust intent units
        adjusted_intent = intent
        if decision.sizing_multiplier < Decimal("1.0") and decision.sizing_multiplier > Decimal("0.0"):
            dampened_units = int(Decimal(str(intent.lot_size.units)) * decision.sizing_multiplier)
            if dampened_units > 0:
                adjusted_intent = OrderIntent(
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    order_type=intent.order_type,
                    lot_size=LotSize.from_units(dampened_units),
                    limit_price=intent.limit_price,
                    stop_loss=intent.stop_loss,
                    take_profit=intent.take_profit,
                    urgency=intent.urgency,
                    timestamp=intent.timestamp,
                    client_tag=intent.client_tag,
                )

        # Route and record child orders
        orders = self.router.route_intent(adjusted_intent)
        for ord_ in orders:
            self._orders[ord_.order_id] = ord_
            sm = OrderStateMachine(
                order_id=ord_.order_id,
                symbol=ord_.symbol,
                initial_state=OrderLifecycleState.CREATED,
                on_reconciliation_alert=self._on_reconciliation_alert,
            )
            sm.transition_to(OrderLifecycleState.RISK_CHECKED, current_time, "Risk check passed")
            sm.transition_to(OrderLifecycleState.SUBMITTED, current_time, "Submitted to router")
            self._state_machines[ord_.order_id] = sm
            ord_.status = OrderStatus.SUBMITTED

        return decision, orders

    def acknowledge_order(self, order_id: str, timestamp: datetime) -> None:
        """Acknowledge order receipt from broker/simulator."""
        if order_id not in self._orders:
            raise KeyError(f"Order ID {order_id} not found in OMS.")
        sm = self._state_machines[order_id]
        sm.transition_to(OrderLifecycleState.ACKED, timestamp, "Acknowledged by broker")
        self._orders[order_id].status = OrderStatus.SUBMITTED

    def process_fill(self, fill: Fill) -> Position:
        """
        Record fill, update order remaining units, and attribute to open position.
        """
        if fill.order_id not in self._orders:
            raise KeyError(f"Order ID {fill.order_id} not found for fill {fill.fill_id}.")

        order = self._orders[fill.order_id]
        sm = self._state_machines[fill.order_id]

        # Record fill
        self._fills[fill.fill_id] = fill

        # Update order stats
        new_filled = order.filled_units + fill.units
        remaining = order.lot_size.units - new_filled
        order.filled_units = new_filled
        order.remaining_units = max(0, remaining)
        order.average_fill_price = fill.fill_price
        order.updated_at = fill.timestamp

        # Update order lifecycle state
        if remaining <= 0:
            sm.transition_to(OrderLifecycleState.FILLED, fill.timestamp, "Filled completely")
            order.status = OrderStatus.FILLED
        else:
            sm.transition_to(OrderLifecycleState.PARTIALLY_FILLED, fill.timestamp, "Partial fill")
            order.status = OrderStatus.PARTIALLY_FILLED

        # Position attribution
        symbol = fill.symbol
        if symbol not in self._open_positions:
            # Create new open position
            pos = Position(
                position_id=f"POS-{symbol}-{fill.fill_id[:8]}",
                symbol=symbol,
                side=fill.side,
                units=fill.units,
                average_entry_price=fill.fill_price,
                current_price=fill.fill_price,
                realized_pnl=Decimal("0.0"),
                unrealized_pnl=Decimal("0.0"),
                total_commission=fill.commission,
                total_swap=Decimal("0.0"),
                opened_at=fill.timestamp,
                updated_at=fill.timestamp,
                is_open=True,
            )
            self._open_positions[symbol] = pos
        else:
            pos = self._open_positions[symbol]
            pos.apply_fill(fill)
            if not pos.is_open:
                # Position was closed
                self.balance += pos.realized_pnl
                self._closed_positions.append(pos)
                del self._open_positions[symbol]

        return pos

    def cancel_order(self, order_id: str, timestamp: datetime, reason: str = "") -> ExecutionOrder:
        """Cancel an open/submitted order."""
        if order_id not in self._orders:
            raise KeyError(f"Order ID {order_id} not found in OMS.")
        order = self._orders[order_id]
        sm = self._state_machines[order_id]
        sm.transition_to(OrderLifecycleState.CANCELLED, timestamp, reason)
        order.status = OrderStatus.CANCELLED
        order.updated_at = timestamp
        return order

    def mark_order_unknown(self, order_id: str, timestamp: datetime, reason: str) -> None:
        """Mark an order state as UNKNOWN, triggering reconciliation safeguards."""
        if order_id not in self._orders:
            raise KeyError(f"Order ID {order_id} not found in OMS.")
        sm = self._state_machines[order_id]
        sm.transition_to(OrderLifecycleState.UNKNOWN, timestamp, reason)
        self._orders[order_id].status = OrderStatus.REJECTED

    def update_quotes(
        self,
        symbol: str,
        bid: Decimal | float | str,
        ask: Decimal | float | str,
        quote_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> None:
        """Update live bid/ask quotes and recompute unrealized PnL."""
        dec_bid = to_decimal(bid)
        dec_ask = to_decimal(ask)
        dec_rate = to_decimal(quote_to_account_rate)
        clean_sym = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        self._latest_quotes[clean_sym] = (dec_bid, dec_ask, dec_rate)

        # Update position market prices
        if clean_sym in self._open_positions:
            pos = self._open_positions[clean_sym]
            # Valuate BUY positions at bid (selling price), SELL positions at ask (buying price)
            valuation_price = dec_bid if pos.side == OrderSide.BUY else dec_ask
            pos.update_market_price(valuation_price, quote_to_account_rate=dec_rate)

    def get_open_positions(self) -> List[Position]:
        return list(self._open_positions.values())

    def inject_position(self, position: Position) -> None:
        """Inject open position directly (useful for testing, reconciliation seed, and state recovery)."""
        clean_sym = position.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        self._open_positions[clean_sym] = position

    def get_position(self, symbol: str) -> Optional[Position]:
        clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        return self._open_positions.get(clean)

    def get_unrealized_pnl(self) -> Decimal:
        return sum((p.unrealized_pnl for p in self._open_positions.values()), Decimal("0.0"))

    def get_realized_pnl(self) -> Decimal:
        open_realized = sum((p.realized_pnl for p in self._open_positions.values()), Decimal("0.0"))
        closed_realized = sum((p.realized_pnl for p in self._closed_positions), Decimal("0.0"))
        return open_realized + closed_realized

    def get_total_commissions(self) -> Decimal:
        open_comm = sum((p.total_commission for p in self._open_positions.values()), Decimal("0.0"))
        closed_comm = sum((p.total_commission for p in self._closed_positions), Decimal("0.0"))
        return open_comm + closed_comm

    def get_total_swap(self) -> Decimal:
        open_swap = sum((p.total_swap for p in self._open_positions.values()), Decimal("0.0"))
        closed_swap = sum((p.total_swap for p in self._closed_positions), Decimal("0.0"))
        return open_swap + closed_swap

    def get_total_equity(self) -> Decimal:
        # Equity = initial_balance + realized_pnl + unrealized_pnl - commissions + swap
        return (
            self.initial_balance
            + self.get_realized_pnl()
            + self.get_unrealized_pnl()
            - self.get_total_commissions()
            + self.get_total_swap()
        )

    def get_account_summary(self) -> AccountSummary:
        return AccountSummary(
            balance=self.balance,
            unrealized_pnl=self.get_unrealized_pnl(),
            realized_pnl=self.get_realized_pnl(),
            total_commission=self.get_total_commissions(),
            total_swap=self.get_total_swap(),
            equity=self.get_total_equity(),
            open_position_count=len(self._open_positions),
        )
