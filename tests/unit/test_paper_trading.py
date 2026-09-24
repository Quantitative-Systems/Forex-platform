"""
Unit tests for Paper Trading subsystem:
MicrostructurePaperSimulator, SQLitePaperLedger (ACID WAL), and ForwardPaperTradingDaemon crash recovery.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import pytest

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.order_management.router import SmartOrderRouter
from forex_platform.paper_trading.daemon import ForwardPaperTradingDaemon
from forex_platform.paper_trading.persistence import SQLitePaperLedger
from forex_platform.paper_trading.simulator import MicrostructurePaperSimulator
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import MarketTelemetry, PreTradeRiskFirewall
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class SingleTradeStrategy(BaseStrategy):
    """Strategy that triggers a single BUY order on the first bar."""

    def __init__(self):
        super().__init__(
            strategy_id="single_trade_strat",
            name="Single Trade Strategy",
            symbols=["EURUSD"],
            timeframes=[Timeframe.M5],
        )
        self.traded = False

    def on_bar(self, event: BarEvent) -> list[OrderIntent]:
        if not self.traded:
            self.traded = True
            return [
                self.create_intent(
                    symbol="EURUSD",
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    lot_size=LotSize.from_lots(1.0),
                    timestamp=event.timestamp,
                    client_tag="TEST_PAPER_FILL",
                )
            ]
        return []


class TestMicrostructureSimulator:
    """Test spread crossing, slippage scaling, and execution latency."""

    def test_spread_crossing_and_slippage(self):
        sim = MicrostructurePaperSimulator(base_slippage_pips=0.1, slippage_gamma=0.2)
        pair = CurrencyPair.from_symbol("EURUSD")
        now = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)

        telemetry = MarketTelemetry(
            symbol="EURUSD",
            bid=Decimal("1.08500"),
            ask=Decimal("1.08510"),  # 1 pip spread
            quote_timestamp=now,
        )

        order_buy = ExecutionOrder(
            order_id="ORD-BUY-01",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),  # 100,000 units
            status=OrderStatus.SUBMITTED,
            created_at=now,
            updated_at=now,
            remaining_units=100_000,
        )

        fill = sim.simulate_execution(order_buy, telemetry, now)

        # BUY crosses at ask (1.08510) + slippage (0.1 + 0.2*sqrt(1) = 0.3 pips = 0.00003)
        # Expected fill price around 1.08513
        assert fill.side == OrderSide.BUY
        assert fill.fill_price >= Decimal("1.08510")
        assert fill.commission == Decimal("3.5000")
        # Verify simulated latency (between 50ms and 150ms)
        latency_ms = (fill.timestamp - now).total_seconds() * 1000.0
        assert 50 <= latency_ms <= 150


class TestSQLitePaperLedgerAndRecovery:
    """Test persistence to SQLite WAL database and crash recovery."""

    def test_ledger_persistence_and_crash_recovery(self, tmp_path):
        db_file = tmp_path / "test_ledger.db"
        ledger = SQLitePaperLedger(db_path=db_file)
        now = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)

        # 1. Save order and open position
        order = ExecutionOrder(
            order_id="ORD-REC-01",
            intent_id="INT-REC-01",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),
            status=OrderStatus.SUBMITTED,
            created_at=now,
            updated_at=now,
            remaining_units=100_000,
        )
        ledger.save_order(order)

        pos = Position(
            position_id="POS-EURUSD-001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=100_000,
            average_entry_price=Decimal("1.08500"),
            current_price=Decimal("1.08550"),
            realized_pnl=Decimal("0.0"),
            unrealized_pnl=Decimal("50.0"),
            total_commission=Decimal("3.50"),
            total_swap=Decimal("0.0"),
            opened_at=now,
            updated_at=now,
            is_open=True,
        )
        ledger.save_position(pos)
        ledger.save_equity_snapshot(now, Decimal("100000"), Decimal("100046.50"), Decimal("50"), Decimal("0"))

        # 2. Simulate process crash and recovery on new ledger instance
        new_ledger = SQLitePaperLedger(db_path=db_file)
        recovered_orders, recovered_positions, balance = new_ledger.recover_state()

        assert len(recovered_orders) == 1
        assert recovered_orders[0].order_id == "ORD-REC-01"
        assert len(recovered_positions) == 1
        assert recovered_positions[0].symbol == "EURUSD"
        assert recovered_positions[0].units == 100_000
        assert recovered_positions[0].average_entry_price == Decimal("1.08500")
        assert balance == Decimal("100000")


class TestForwardPaperDaemon:
    """Test ForwardPaperTradingDaemon event loop and ledger synchronization."""

    def test_daemon_lifecycle_execution(self, tmp_path):
        db_file = tmp_path / "daemon_test.db"
        ledger = SQLitePaperLedger(db_path=db_file)

        kill_switch = HierarchicalKillSwitch()
        cb = CircuitBreakerEngine(initial_equity=Decimal("100000"))
        firewall = PreTradeRiskFirewall(kill_switch, cb)
        router = SmartOrderRouter()
        oms = OrderManagementSystem(firewall, router, kill_switch, initial_balance=Decimal("100000"))
        sim = MicrostructurePaperSimulator()
        pair = CurrencyPair.from_symbol("EURUSD")
        strat = SingleTradeStrategy()

        daemon = ForwardPaperTradingDaemon(
            strategy=strat,
            oms=oms,
            simulator=sim,
            ledger=ledger,
            currency_pair=pair,
        )

        now = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        bar = BarEvent(
            symbol="EURUSD",
            timeframe=Timeframe.M5,
            timestamp=now,
            open=Decimal("1.0850"),
            high=Decimal("1.0860"),
            low=Decimal("1.0845"),
            close=Decimal("1.0855"),
            volume=Decimal("500"),
        )
        telemetry = MarketTelemetry(
            symbol="EURUSD",
            bid=Decimal("1.0850"),
            ask=Decimal("1.0851"),
            quote_timestamp=now,
            recent_spreads_pips=[Decimal("1.0")] * 20,
        )

        # Feed market tick/bar to daemon
        fills = daemon.on_tick_or_bar(bar, telemetry)
        assert len(fills) == 1
        fill = fills[0]
        assert fill.symbol == "EURUSD"
        assert fill.units == 100_000

        # Verify state in OMS
        assert len(oms.get_open_positions()) == 1
        open_pos = oms.get_position("EURUSD")
        assert open_pos is not None
        assert open_pos.units == 100_000

        # Verify state persisted to database and recoverable
        orders_rec, pos_rec, _ = ledger.recover_state()
        assert len(pos_rec) == 1
        assert pos_rec[0].symbol == "EURUSD"
