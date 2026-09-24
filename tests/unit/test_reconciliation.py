"""
Unit tests for Position and State Reconciliation Engine.
Verifies fail-closed halt, global kill switch arming, and SAFE_MODE triggering
on any discrepancy between local OMS and remote broker gateway.
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from forex_platform.broker_adapters.base import BrokerPosition
from forex_platform.broker_adapters.mt5_bridge import MT5BridgeAdapter
from forex_platform.core.domain import (
    CurrencyPair,
    Fill,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.order_management.router import SmartOrderRouter
from forex_platform.reconciliation.reconciler import (
    DiscrepancyType,
    PositionReconciler,
)
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import MarketTelemetry, PreTradeRiskFirewall
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch


@pytest.fixture
def test_env():
    kill_switch = HierarchicalKillSwitch()
    circuit_breaker = CircuitBreakerEngine(initial_equity=Decimal("100000.00"))
    firewall = PreTradeRiskFirewall(kill_switch=kill_switch, circuit_breaker=circuit_breaker)
    router = SmartOrderRouter(allow_live_routing=False)
    oms = OrderManagementSystem(firewall=firewall, router=router, kill_switch=kill_switch)
    broker = MT5BridgeAdapter(account_id="RECON_TEST", broker_name="IC_MARKETS")
    broker.connect()

    reconciler = PositionReconciler(
        oms=oms,
        broker=broker,
        kill_switch=kill_switch,
        circuit_breaker=circuit_breaker,
        interval_seconds=0.1,
    )

    return {
        "kill_switch": kill_switch,
        "circuit_breaker": circuit_breaker,
        "oms": oms,
        "broker": broker,
        "reconciler": reconciler,
    }


class TestStateReconciliation:

    def test_clean_reconciliation_when_positions_match(self, test_env):
        """Verify clean reconciliation report when OMS and broker have matching states."""
        oms: OrderManagementSystem = test_env["oms"]
        broker: MT5BridgeAdapter = test_env["broker"]
        reconciler: PositionReconciler = test_env["reconciler"]
        kill_switch: HierarchicalKillSwitch = test_env["kill_switch"]

        # Populate OMS position
        pos = Position(
            position_id="POS_EURUSD_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=100_000,
            average_entry_price=Decimal("1.08500"),
            current_price=Decimal("1.08500"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        oms.inject_position(pos)

        # Match exactly on broker
        b_pos = BrokerPosition(
            ticket_id="TICKET_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=100_000,
            open_price=Decimal("1.08500"),
        )
        broker.inject_remote_position(b_pos)

        report = reconciler.reconcile_once()
        assert report.is_clean is True
        assert len(report.discrepancies) == 0
        assert report.matched_positions_count == 1

        is_killed, _ = kill_switch.is_killed()
        assert is_killed is False

    def test_phantom_position_arms_global_kill_switch(self, test_env):
        """Verify broker position not present in OMS triggers phantom position alert and halts platform."""
        broker: MT5BridgeAdapter = test_env["broker"]
        reconciler: PositionReconciler = test_env["reconciler"]
        kill_switch: HierarchicalKillSwitch = test_env["kill_switch"]
        circuit_breaker: CircuitBreakerEngine = test_env["circuit_breaker"]

        # Broker has position, OMS is empty
        b_pos = BrokerPosition(
            ticket_id="PHANTOM_1",
            symbol="GBPUSD",
            side=OrderSide.SELL,
            units=50_000,
            open_price=Decimal("1.25000"),
        )
        broker.inject_remote_position(b_pos)

        report = reconciler.reconcile_once()
        assert report.is_clean is False
        assert len(report.discrepancies) == 1
        assert report.discrepancies[0].discrepancy_type == DiscrepancyType.PHANTOM_POSITION
        assert report.discrepancies[0].symbol == "GBPUSD"

        # Verify fail-closed enforcement
        is_killed, reason = kill_switch.is_killed()
        assert is_killed is True
        assert "RECONCILIATION_BREACH" in reason
        assert circuit_breaker._safe_mode is True

    def test_lost_position_arms_global_kill_switch(self, test_env):
        """Verify position present in OMS but absent on broker triggers lost position discrepancy."""
        oms: OrderManagementSystem = test_env["oms"]
        reconciler: PositionReconciler = test_env["reconciler"]
        kill_switch: HierarchicalKillSwitch = test_env["kill_switch"]

        # OMS has position, Broker has none
        pos = Position(
            position_id="POS_USDJPY_1",
            symbol="USDJPY",
            side=OrderSide.BUY,
            units=100_000,
            average_entry_price=Decimal("150.00"),
            current_price=Decimal("150.00"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        oms.inject_position(pos)

        report = reconciler.reconcile_once()
        assert report.is_clean is False
        assert len(report.discrepancies) == 1
        assert report.discrepancies[0].discrepancy_type == DiscrepancyType.LOST_POSITION
        assert report.discrepancies[0].symbol == "USDJPY"

        is_killed, _ = kill_switch.is_killed()
        assert is_killed is True

    def test_size_mismatch_arms_kill_switch(self, test_env):
        """Verify position volume mismatch between OMS and broker triggers size mismatch."""
        oms: OrderManagementSystem = test_env["oms"]
        broker: MT5BridgeAdapter = test_env["broker"]
        reconciler: PositionReconciler = test_env["reconciler"]
        kill_switch: HierarchicalKillSwitch = test_env["kill_switch"]

        # OMS: 100,000 units BUY
        pos = Position(
            position_id="POS_EURUSD_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=100_000,
            average_entry_price=Decimal("1.08500"),
            current_price=Decimal("1.08500"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        oms.inject_position(pos)

        # Broker: 50,000 units BUY
        b_pos = BrokerPosition(
            ticket_id="T3",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=50_000,
            open_price=Decimal("1.08500"),
        )
        broker.inject_remote_position(b_pos)

        report = reconciler.reconcile_once()
        assert report.is_clean is False
        assert len(report.discrepancies) == 1
        assert report.discrepancies[0].discrepancy_type == DiscrepancyType.SIZE_MISMATCH

        is_killed, _ = kill_switch.is_killed()
        assert is_killed is True

    def test_side_mismatch_arms_kill_switch(self, test_env):
        """Verify opposing position sides (BUY vs SELL) triggers side mismatch."""
        oms: OrderManagementSystem = test_env["oms"]
        broker: MT5BridgeAdapter = test_env["broker"]
        reconciler: PositionReconciler = test_env["reconciler"]
        kill_switch: HierarchicalKillSwitch = test_env["kill_switch"]

        # OMS: BUY
        pos = Position(
            position_id="POS_EURUSD_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=100_000,
            average_entry_price=Decimal("1.08500"),
            current_price=Decimal("1.08500"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        oms.inject_position(pos)

        # Broker: SELL
        b_pos = BrokerPosition(
            ticket_id="T4",
            symbol="EURUSD",
            side=OrderSide.SELL,
            units=100_000,
            open_price=Decimal("1.08500"),
        )
        broker.inject_remote_position(b_pos)

        report = reconciler.reconcile_once()
        assert report.is_clean is False
        assert len(report.discrepancies) == 1
        assert report.discrepancies[0].discrepancy_type == DiscrepancyType.SIDE_MISMATCH

        is_killed, _ = kill_switch.is_killed()
        assert is_killed is True

    def test_reconciler_daemon_lifecycle(self, test_env):
        """Verify asynchronous reconciliation daemon starts and stops cleanly."""
        reconciler: PositionReconciler = test_env["reconciler"]
        assert reconciler.is_running() is False

        reconciler.start()
        assert reconciler.is_running() is True

        reconciler.stop()
        assert reconciler.is_running() is False
