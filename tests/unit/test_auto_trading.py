"""
Unit tests for 24/7/365 Auto-Trading Controller.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from forex_platform.auto_trading.controller import (
    AutoTradingController,
    AutoTradingStatus,
    BrokerAccountConfig,
    BrokerAccountManager,
    BrokerConnectionState,
    ConnectionHealth,
)
from forex_platform.broker_adapters.base import (
    BrokerAccountInfo,
    BrokerOrderAck,
    BrokerPosition,
    IBrokerAdapter,
    OrderStatus,
    PermissionSecurityError,
    RateLimitExceededError,
)
from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    to_decimal,
)
from forex_platform.market_data.adapters.base import MarketDataAdapter
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.risk_engine.firewall import MarketTelemetry, RiskDecision
from forex_platform.strategy_engine.base import BaseStrategy, BarEvent


class MockBrokerAdapter(IBrokerAdapter):
    """Mock broker adapter for testing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connected = False
        self._positions = {}
        self._balance = Decimal("100000.00")

    def connect(self) -> bool:
        self._connected = True
        self._is_connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False
        self._is_connected = False

    def send_order(self, intent: OrderIntent) -> BrokerOrderAck:
        if not self._connected:
            raise ConnectionError("Not connected")
        ticket = f"MOCK_{intent.intent_id}"
        return BrokerOrderAck(
            order_id=intent.intent_id,
            broker_ticket=ticket,
            status=OrderStatus.FILLED,
            timestamp=datetime.now(timezone.utc),
            message="Mock order filled",
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_positions(self):
        return list(self._positions.values())

    def get_account_info(self) -> BrokerAccountInfo:
        return BrokerAccountInfo(
            account_id=self.account_id,
            broker_name=self.broker_name,
            currency="USD",
            balance=self._balance,
            equity=self._balance,
            margin=Decimal("0.0"),
            free_margin=self._balance,
            is_live=self.is_live,
            permissions=list(self.permissions),
        )


class MockMarketDataAdapter(MarketDataAdapter):
    """Mock market data adapter for testing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connected = False

    def connect(self) -> None:
        self._connected = True
        self._is_connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._is_connected = False

    def subscribe_bars(self, symbols, timeframes):
        pass

    def subscribe_ticks(self, symbols):
        pass


class MockStrategy(BaseStrategy):
    """Mock strategy for testing."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.intents_to_return = []

    def on_bar(self, event: BarEvent):
        return self.intents_to_return


class TestConnectionHealth:
    """Tests for ConnectionHealth class."""

    def test_initial_state(self):
        health = ConnectionHealth()
        assert health.success_rate == 0.0
        assert health.is_healthy is False
        assert health.consecutive_errors == 0

    def test_success_rate_calculation(self):
        health = ConnectionHealth()
        health.total_requests = 100
        health.failed_requests = 5
        assert health.success_rate == 0.95

    def test_healthy_after_success(self):
        health = ConnectionHealth()
        health.total_requests = 10
        health.failed_requests = 0
        health.last_success = datetime.now(timezone.utc)
        health.consecutive_errors = 0
        assert health.is_healthy is True

    def test_unhealthy_after_errors(self):
        health = ConnectionHealth()
        health.consecutive_errors = 1
        assert health.is_healthy is False


class TestBrokerAccountManager:
    """Tests for BrokerAccountManager class."""

    def test_initial_state(self):
        config = BrokerAccountConfig(
            account_id="test_account",
            broker_name="TEST_BROKER",
            broker_type="MT5",
        )
        manager = BrokerAccountManager(config)
        assert manager.state == BrokerConnectionState.DISCONNECTED
        assert manager.is_connected() is False

    def test_connect_success(self):
        config = BrokerAccountConfig(
            account_id="test_account",
            broker_name="TEST_BROKER",
            broker_type="MT5",
        )
        manager = BrokerAccountManager(config)

        # Patch the adapter creation
        with patch.object(manager, 'adapter', MockBrokerAdapter(
            account_id="test_account",
            broker_name="TEST_BROKER",
        )):
            result = manager.connect()
            assert result is True
            assert manager.state == BrokerConnectionState.CONNECTED
            assert manager.is_connected() is True

    def test_disconnect(self):
        config = BrokerAccountConfig(
            account_id="test_account",
            broker_name="TEST_BROKER",
            broker_type="MT5",
        )
        manager = BrokerAccountManager(config)

        with patch.object(manager, 'adapter', MockBrokerAdapter(
            account_id="test_account",
            broker_name="TEST_BROKER",
        )):
            manager.connect()
            manager.disconnect()
            assert manager.state == BrokerConnectionState.DISCONNECTED
            assert manager.is_connected() is False

    def test_get_account_info(self):
        config = BrokerAccountConfig(
            account_id="test_account",
            broker_name="TEST_BROKER",
            broker_type="MT5",
        )
        manager = BrokerAccountManager(config)

        mock_adapter = MockBrokerAdapter(
            account_id="test_account",
            broker_name="TEST_BROKER",
        )
        with patch.object(manager, 'adapter', mock_adapter):
            manager.connect()
            info = manager.get_account_info()
            assert info is not None
            assert info.account_id == "test_account"
            assert info.balance == Decimal("100000.00")

    def test_send_order(self):
        config = BrokerAccountConfig(
            account_id="test_account",
            broker_name="TEST_BROKER",
            broker_type="MT5",
        )
        manager = BrokerAccountManager(config)

        mock_adapter = MockBrokerAdapter(
            account_id="test_account",
            broker_name="TEST_BROKER",
        )
        with patch.object(manager, 'adapter', mock_adapter):
            manager.connect()

            intent = OrderIntent(
                intent_id="TEST_INTENT_001",
                symbol="EURUSD",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=LotSize.from_units(100000),
                timestamp=datetime.now(timezone.utc),
            )

            ack = manager.send_order(intent)
            assert ack is not None
            assert ack.status == OrderStatus.FILLED


class TestAutoTradingController:
    """Tests for AutoTradingController class."""

    def test_initial_state(self):
        strategy = MockStrategy(
            strategy_id="TEST_STRAT",
            name="Test Strategy",
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        broker_configs = [
            BrokerAccountConfig(
                account_id="test_account",
                broker_name="TEST_BROKER",
                broker_type="MT5",
            ),
        ]

        market_data = MockMarketDataAdapter(
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        controller = AutoTradingController(
            strategies=[strategy],
            broker_configs=broker_configs,
            market_data_adapter=market_data,
            initial_balance=Decimal("100000.00"),
        )

        assert controller.status == AutoTradingStatus.STOPPED
        assert controller.is_running() is False
        assert len(controller.broker_managers) == 1

    def test_start_stop(self):
        strategy = MockStrategy(
            strategy_id="TEST_STRAT",
            name="Test Strategy",
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        broker_configs = [
            BrokerAccountConfig(
                account_id="test_account",
                broker_name="TEST_BROKER",
                broker_type="MT5",
            ),
        ]

        market_data = MockMarketDataAdapter(
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        controller = AutoTradingController(
            strategies=[strategy],
            broker_configs=broker_configs,
            market_data_adapter=market_data,
            initial_balance=Decimal("100000.00"),
        )

        # Mock the broker manager connect
        for manager in controller.broker_managers.values():
            manager.adapter = MockBrokerAdapter(
                account_id=manager.config.account_id,
                broker_name=manager.config.broker_name,
            )

        # Start
        result = controller.start()
        assert result is True
        assert controller.status == AutoTradingStatus.RUNNING
        assert controller.is_running() is True

        # Stop
        controller.stop()
        assert controller.status == AutoTradingStatus.STOPPED
        assert controller.is_running() is False

    def test_get_status(self):
        strategy = MockStrategy(
            strategy_id="TEST_STRAT",
            name="Test Strategy",
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        broker_configs = [
            BrokerAccountConfig(
                account_id="test_account",
                broker_name="TEST_BROKER",
                broker_type="MT5",
            ),
        ]

        market_data = MockMarketDataAdapter(
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        controller = AutoTradingController(
            strategies=[strategy],
            broker_configs=broker_configs,
            market_data_adapter=market_data,
            initial_balance=Decimal("100000.00"),
        )

        status = controller.get_status()
        assert status["controller_status"] == AutoTradingStatus.STOPPED.value
        assert "broker_statuses" in status
        assert "test_account" in status["broker_statuses"]

    def test_process_order_intent(self):
        strategy = MockStrategy(
            strategy_id="TEST_STRAT",
            name="Test Strategy",
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        broker_configs = [
            BrokerAccountConfig(
                account_id="test_account",
                broker_name="TEST_BROKER",
                broker_type="MT5",
            ),
        ]

        market_data = MockMarketDataAdapter(
            symbols=["EURUSD"],
            timeframes=[Timeframe.M15],
        )

        controller = AutoTradingController(
            strategies=[strategy],
            broker_configs=broker_configs,
            market_data_adapter=market_data,
            initial_balance=Decimal("100000.00"),
        )

        # Mock broker manager
        for manager in controller.broker_managers.values():
            manager.adapter = MockBrokerAdapter(
                account_id=manager.config.account_id,
                broker_name=manager.config.broker_name,
            )
            manager.connect()
            manager.start_health_monitor()

        # Mock market data
        market_data.connect()

        # Create a test intent on a deterministic weekday inside the market session.
        test_time = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
        intent = OrderIntent(
            intent_id="TEST_INTENT_001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_units(100000),
            timestamp=test_time,
        )

        # Process the intent
        controller._process_order_intent(intent, test_time, strategy)

        # Check that order was processed
        assert len(controller.oms._orders) > 0


class TestBrokerAccountConfig:
    """Tests for BrokerAccountConfig dataclass."""

    def test_default_values(self):
        config = BrokerAccountConfig(
            account_id="test",
            broker_name="TEST",
            broker_type="MT5",
        )
        assert config.is_live is False
        assert config.auto_reconnect is True
        assert config.reconnect_interval_seconds == 30
        assert config.max_reconnect_attempts == 5
        assert config.rate_limit_capacity == 50
        assert config.rate_limit_refill == 50.0
        assert config.permissions is None

    def test_custom_values(self):
        config = BrokerAccountConfig(
            account_id="test",
            broker_name="TEST",
            broker_type="CTRADER",
            is_live=True,
            auto_reconnect=False,
            reconnect_interval_seconds=60,
            max_reconnect_attempts=10,
            rate_limit_capacity=100,
            rate_limit_refill=100.0,
            permissions=["TRADE", "READ_ONLY"],
        )
        assert config.is_live is True
        assert config.auto_reconnect is False
        assert config.reconnect_interval_seconds == 60
        assert config.max_reconnect_attempts == 10
        assert config.rate_limit_capacity == 100
        assert config.rate_limit_refill == 100.0
        assert config.permissions == ["TRADE", "READ_ONLY"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])