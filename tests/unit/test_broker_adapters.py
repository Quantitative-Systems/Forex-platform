"""
Unit tests for Broker Adapters: Non-Custodial Verification, Rate Limiting,
and Symbol Normalization.
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal

from forex_platform.broker_adapters.base import (
    BrokerAccountInfo,
    BrokerPosition,
    IBrokerAdapter,
    PermissionSecurityError,
    RateLimitExceededError,
    TokenBucketRateLimiter,
)
from forex_platform.broker_adapters.mt5_bridge import MT5BridgeAdapter
from forex_platform.broker_adapters.ctrader_adapter import CTraderAdapter
from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
)


class TestBrokerSecurityAndAdapters:

    def test_non_custodial_permission_audit_rejects_forbidden_rights(self):
        """Verify strict non-custodial audit rejects any withdrawal/admin rights."""
        forbidden_sets = [
            ["TRADE", "WITHDRAWAL"],
            ["TRADE", "TRANSFER"],
            ["FULL_ACCESS"],
            ["TRADE", "MANAGE_FUNDS"],
            ["ADMIN", "MARKET_DATA"],
        ]

        for perms in forbidden_sets:
            with pytest.raises(PermissionSecurityError, match="Non-custodial invariant breach"):
                MT5BridgeAdapter(account_id="ACC_123", permissions=perms)

            with pytest.raises(PermissionSecurityError, match="Non-custodial invariant breach"):
                CTraderAdapter(account_id="ACC_123", permissions=perms)

    def test_trade_only_permissions_allowed(self):
        """Verify legitimate trade-only permissions pass audit cleanly."""
        allowed_perms = ["TRADE", "READ_ONLY", "MARKET_DATA", "ORDER_MANAGEMENT"]
        adapter = MT5BridgeAdapter(account_id="ACC_123", permissions=allowed_perms)
        assert adapter.permissions == allowed_perms

    def test_rate_limiter_token_consumption_and_exhaustion(self):
        """Verify token bucket rate limiter permits bursts up to capacity and throttles overage."""
        limiter = TokenBucketRateLimiter(capacity=3, refill_rate=1.0)
        assert limiter.acquire(1) is True
        assert limiter.acquire(1) is True
        assert limiter.acquire(1) is True
        # Capacity exhausted
        assert limiter.acquire(1) is False

        with pytest.raises(RateLimitExceededError):
            limiter.consume(1)

    def test_mt5_symbol_normalization(self):
        """Verify broker symbol suffix normalization and denormalization."""
        raw_to_canonical = {
            "EURUSD.pro": "EURUSD",
            "EURUSD+": "EURUSD",
            "USDJPY.raw": "USDJPY",
            "GBPUSD.r": "GBPUSD",
            "AUDUSD.m": "AUDUSD",
            "EURGBP.ecn": "EURGBP",
            "NZD_USD": "NZDUSD",
            "USD/CHF": "USDCHF",
        }
        for raw, expected in raw_to_canonical.items():
            assert MT5BridgeAdapter.normalize_symbol(raw) == expected

        # Test broker denormalization
        vantage = MT5BridgeAdapter(account_id="1", broker_name="VANTAGE")
        assert vantage.denormalize_symbol("EURUSD") == "EURUSD.pro"

        hf = MT5BridgeAdapter(account_id="2", broker_name="HF_MARKETS")
        assert hf.denormalize_symbol("EURUSD") == "EURUSD+"

        ic = MT5BridgeAdapter(account_id="3", broker_name="IC_MARKETS")
        assert ic.denormalize_symbol("EURUSD") == "EURUSD.raw"

    def test_zero_live_capital_guard_fails_closed(self):
        """Verify zero live capital invariant blocks live order routing."""
        live_adapter = MT5BridgeAdapter(account_id="LIVE_1", is_live=True)
        live_adapter.connect()

        intent = OrderIntent(
            intent_id="INT_LIVE_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots("1.0"),
            timestamp=datetime.now(timezone.utc),
        )

        with pytest.raises(PermissionSecurityError, match="LIVE CAPITAL LOCKED"):
            live_adapter.send_order(intent)

    def test_secret_file_permissions_are_enforced(self, tmp_path):
        from forex_platform.production.brokers import BrokerError, resolve_secret

        secret = tmp_path / "mt5_secret"
        secret.write_text("secret-value", encoding="utf-8")
        secret.chmod(0o640)
        with pytest.raises(BrokerError, match="group or others"):
            resolve_secret({"hmac_secret_file": str(secret)}, "hmac_secret")
        secret.chmod(0o600)
        assert resolve_secret({"hmac_secret_file": str(secret)}, "hmac_secret") == "secret-value"

    def test_broker_registry_readiness_requires_all_enabled_accounts(self, tmp_path):
        from forex_platform.production.brokers import AccountSpec, BrokerRegistry
        from forex_platform.production.settings import Settings

        registry = BrokerRegistry(Settings(require_api_key=False), live_gate=object())
        first = AccountSpec(account_id="first", broker="PAPER", mode="PAPER", adapter="paper", enabled=True)
        second = AccountSpec(account_id="second", broker="PAPER", mode="PAPER", adapter="paper", enabled=True)
        registry.register(first)
        registry.register(second)
        first_adapter = registry.get_adapter("first")
        second_adapter = registry.get_adapter("second")
        first_adapter.connect()
        health = registry.health()
        assert health["enabled_accounts"] == 2
        assert health["connected_accounts"] == 1
        assert health["all_enabled_connected"] is False
        second_adapter.connect()
        assert registry.health()["all_enabled_connected"] is True

        from forex_platform.production.brokers import BrokerError, MT5RemoteAdapter

        with pytest.raises(BrokerError, match="HTTPS"):
            MT5RemoteAdapter("demo", "B", "http://agent")
        with pytest.raises(BrokerError, match="agent_token"):
            MT5RemoteAdapter("demo", "B", "https://agent")

    def test_mt5_bridge_order_lifecycle(self):
        """Verify simulated order placement, position query, and cancellation on MT5 bridge."""
        adapter = MT5BridgeAdapter(account_id="DEMO_MT5", broker_name="IC_MARKETS")
        adapter.connect()

        intent = OrderIntent(
            intent_id="INT_MT5_1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots("0.5"),
            limit_price=Decimal("1.08500"),
            timestamp=datetime.now(timezone.utc),
        )

        ack = adapter.send_order(intent)
        assert ack.status == OrderStatus.FILLED
        assert ack.broker_ticket.startswith("MT5_")

        positions = adapter.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "EURUSD"
        assert positions[0].units == 50000
        assert positions[0].side == OrderSide.BUY

        # Cancel/close position
        cancelled = adapter.cancel_order(ack.broker_ticket)
        assert cancelled is True
        assert len(adapter.get_positions()) == 0

    def test_ctrader_adapter_order_lifecycle(self):
        """Verify simulated order placement and account info on cTrader adapter."""
        adapter = CTraderAdapter(account_id="DEMO_CTRADER")
        adapter.connect()

        intent = OrderIntent(
            intent_id="INT_CT_1",
            symbol="GBPUSD",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots("1.0"),
            limit_price=Decimal("1.25000"),
            timestamp=datetime.now(timezone.utc),
        )

        ack = adapter.send_order(intent)
        assert ack.status == OrderStatus.FILLED
        assert ack.broker_ticket.startswith("CT_")

        info = adapter.get_account_info()
        assert info.broker_name == "CTRADER_ECN"
        assert info.balance == Decimal("100000.00")
        assert info.equity >= Decimal("100000.00")
