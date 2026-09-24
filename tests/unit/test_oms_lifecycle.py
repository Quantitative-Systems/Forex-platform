"""
Unit tests for Order Management System (OMS), OrderStateMachine, and SmartOrderRouter.
Verifies legal/illegal state transitions, idempotent routing, TWAP clip slicing,
fill attribution, and exact pip-value PnL accounting.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest

from forex_platform.core.domain import (
    CurrencyPair,
    Fill,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.order_management.router import LiveCapitalLockedError, SmartOrderRouter
from forex_platform.order_management.state_machine import (
    IllegalStateTransitionError,
    OrderLifecycleState,
    OrderStateMachine,
    ReconciliationAlert,
)
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import MarketTelemetry, PreTradeRiskFirewall
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch


class TestOrderStateMachine:
    """Test legal state transitions and rejection of illegal state jumps."""

    def test_legal_lifecycle_transitions(self):
        now = datetime.now(timezone.utc)
        sm = OrderStateMachine(order_id="ORD-100", symbol="EURUSD")
        assert sm.current_state == OrderLifecycleState.CREATED

        sm.transition_to(OrderLifecycleState.RISK_CHECKED, now)
        assert sm.current_state == OrderLifecycleState.RISK_CHECKED

        sm.transition_to(OrderLifecycleState.SUBMITTED, now)
        assert sm.current_state == OrderLifecycleState.SUBMITTED

        sm.transition_to(OrderLifecycleState.ACKED, now)
        assert sm.current_state == OrderLifecycleState.ACKED

        sm.transition_to(OrderLifecycleState.PARTIALLY_FILLED, now)
        assert sm.current_state == OrderLifecycleState.PARTIALLY_FILLED

        sm.transition_to(OrderLifecycleState.FILLED, now)
        assert sm.current_state == OrderLifecycleState.FILLED
        assert sm.is_terminal

    def test_illegal_state_jump_raises(self):
        now = datetime.now(timezone.utc)
        sm = OrderStateMachine(order_id="ORD-101", symbol="EURUSD")

        # CREATED cannot jump directly to FILLED
        with pytest.raises(IllegalStateTransitionError):
            sm.transition_to(OrderLifecycleState.FILLED, now)

        # Move to terminal state FILLED
        sm.transition_to(OrderLifecycleState.RISK_CHECKED, now)
        sm.transition_to(OrderLifecycleState.SUBMITTED, now)
        sm.transition_to(OrderLifecycleState.ACKED, now)
        sm.transition_to(OrderLifecycleState.FILLED, now)

        # FILLED is terminal; cannot transition to SUBMITTED
        with pytest.raises(IllegalStateTransitionError):
            sm.transition_to(OrderLifecycleState.SUBMITTED, now)

    def test_unknown_state_emits_reconciliation_alert(self):
        now = datetime.now(timezone.utc)
        alerts_received = []

        def on_alert(alert: ReconciliationAlert):
            alerts_received.append(alert)

        sm = OrderStateMachine(
            order_id="ORD-102",
            symbol="EURUSD",
            on_reconciliation_alert=on_alert,
        )
        sm.transition_to(OrderLifecycleState.RISK_CHECKED, now)
        sm.transition_to(OrderLifecycleState.SUBMITTED, now)
        sm.transition_to(OrderLifecycleState.UNKNOWN, now, reason="Broker socket dropped")

        assert sm.current_state == OrderLifecycleState.UNKNOWN
        assert len(alerts_received) == 1
        assert alerts_received[0].order_id == "ORD-102"
        assert alerts_received[0].severity == "CRITICAL"
        assert alerts_received[0].symbol == "EURUSD"


class TestSmartOrderRouter:
    """Test deterministic CIDs, TWAP slicing, and live capital lock."""

    def test_idempotent_cid_generation(self):
        now = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        intent = OrderIntent(
            intent_id="intent-idem-001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),
            timestamp=now,
        )
        router = SmartOrderRouter()
        cid1 = router.generate_client_order_id(intent, slice_index=0)
        cid2 = router.generate_client_order_id(intent, slice_index=0)
        assert cid1 == cid2

        # Duplicate route call returns existing order
        orders1 = router.route_intent(intent)
        orders2 = router.route_intent(intent)
        assert orders1[0].order_id == orders2[0].order_id

    def test_twap_slicing_exceeding_5_lots(self):
        now = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        # 12 standard lots = 1,200,000 units (> 500,000 max clip)
        intent = OrderIntent(
            intent_id="intent-twap-1200k",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(12.0),
            timestamp=now,
        )
        router = SmartOrderRouter()
        slices = router.slice_intent(intent)

        # Expected slices: 500,000 + 500,000 + 200,000 = 3 slices
        assert len(slices) == 3
        assert slices[0].lot_size.units == 500_000
        assert slices[1].lot_size.units == 500_000
        assert slices[2].lot_size.units == 200_000

        total_units = sum(s.lot_size.units for s in slices)
        assert total_units == 1_200_000

    def test_live_capital_route_fails_closed(self):
        now = datetime.now(timezone.utc)
        intent = OrderIntent(
            intent_id="intent-live-test",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),
            timestamp=now,
        )
        router = SmartOrderRouter(allow_live_routing=False)
        with pytest.raises(LiveCapitalLockedError):
            router.route_intent(intent, destination="LIVE")


class TestOrderManagementSystem:
    """Test end-to-end OMS ledger, fill attribution, and real-time PnL."""

    def setup_method(self):
        self.kill_switch = HierarchicalKillSwitch()
        self.circuit_breaker = CircuitBreakerEngine(initial_equity=Decimal("100000"))
        self.firewall = PreTradeRiskFirewall(
            kill_switch=self.kill_switch,
            circuit_breaker=self.circuit_breaker,
            allow_live_capital=False,
        )
        self.router = SmartOrderRouter()
        self.oms = OrderManagementSystem(
            firewall=self.firewall,
            router=self.router,
            kill_switch=self.kill_switch,
            initial_balance=Decimal("100000.00"),
        )
        self.now = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        self.pair = CurrencyPair.from_symbol("EURUSD")

    def _get_telemetry(self, spread_pips: Decimal = Decimal("1.0")) -> MarketTelemetry:
        bid = Decimal("1.08500")
        ask = bid + self.pair.to_price(spread_pips)
        return MarketTelemetry(
            symbol="EURUSD",
            bid=bid,
            ask=ask,
            quote_timestamp=self.now,
            recent_spreads_pips=[Decimal("1.0")] * 20,
        )

    def test_order_submission_and_fill_accounting(self):
        # 1. Submit valid BUY intent for 1.0 standard lot EURUSD (100,000 units)
        intent = OrderIntent(
            intent_id="intent-oms-001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),
            timestamp=self.now,
        )
        decision, orders = self.oms.submit_intent(intent, self.now, telemetry=self._get_telemetry())
        assert decision.approved
        assert len(orders) == 1
        order = orders[0]
        assert order.status == OrderStatus.SUBMITTED

        # 2. Acknowledge order
        self.oms.acknowledge_order(order.order_id, self.now)

        # 3. Process fill at 1.08500 with $3.50 commission
        fill = Fill(
            fill_id="FILL-001",
            order_id=order.order_id,
            symbol="EURUSD",
            side=OrderSide.BUY,
            fill_price=Decimal("1.08500"),
            units=100_000,
            commission=Decimal("3.50"),
            timestamp=self.now,
        )
        pos = self.oms.process_fill(fill)
        assert pos.is_open
        assert pos.units == 100_000
        assert pos.average_entry_price == Decimal("1.08500")
        assert len(self.oms.get_open_positions()) == 1

        # 4. Market price advances by 25 pips (1.08500 -> 1.08750 bid)
        self.oms.update_quotes(
            symbol="EURUSD",
            bid=Decimal("1.08750"),
            ask=Decimal("1.08760"),
            quote_to_account_rate=Decimal("1.0"),
        )

        # 100,000 * 0.00250 = $250.00 floating PnL
        unrealized = self.oms.get_unrealized_pnl()
        assert unrealized == Decimal("250.00000")

        # Total equity = $100,000 + $250 - $3.50 commission = $100,246.50
        equity = self.oms.get_total_equity()
        assert equity == Decimal("100246.50000")

        # 5. Close position by selling 100,000 units at 1.08750 with $3.50 commission
        close_intent = OrderIntent(
            intent_id="intent-oms-close",
            symbol="EURUSD",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),
            timestamp=self.now,
        )
        _, close_orders = self.oms.submit_intent(close_intent, self.now, telemetry=self._get_telemetry())
        close_fill = Fill(
            fill_id="FILL-002",
            order_id=close_orders[0].order_id,
            symbol="EURUSD",
            side=OrderSide.SELL,
            fill_price=Decimal("1.08750"),
            units=100_000,
            commission=Decimal("3.50"),
            timestamp=self.now,
        )
        closed_pos = self.oms.process_fill(close_fill)
        assert not closed_pos.is_open
        assert len(self.oms.get_open_positions()) == 0

        # Realized PnL is now $250.00
        assert self.oms.get_realized_pnl() == Decimal("250.00000")
        assert self.oms.get_unrealized_pnl() == Decimal("0.0")

    def test_unknown_state_arms_pair_kill_switch(self):
        intent = OrderIntent(
            intent_id="intent-unknown-test",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),
            timestamp=self.now,
        )
        _, orders = self.oms.submit_intent(intent, self.now, telemetry=self._get_telemetry())
        order_id = orders[0].order_id

        # Mark order as UNKNOWN (e.g. gateway dropped)
        self.oms.mark_order_unknown(order_id, self.now, reason="Network timeout on broker")

        # Verify pair is now killed
        is_killed, reason = self.kill_switch.is_killed(symbol="EURUSD")
        assert is_killed
        assert "EURUSD" in str(reason)
