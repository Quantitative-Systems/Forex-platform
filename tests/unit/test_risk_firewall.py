"""
Unit tests for PreTradeRiskFirewall, CircuitBreakerEngine, and HierarchicalKillSwitch.
Verifies fail-closed behavior, 6-tier gating, and drawdown dampening.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest

from forex_platform.core.domain import CurrencyPair, LotSize, OrderIntent, OrderSide, OrderType
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import (
    MarketTelemetry,
    PreTradeRiskFirewall,
    RiskDecision,
)
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch


class TestRiskFirewallTiers:
    """Test 6-tier pre-trade risk firewall gates."""

    def setup_method(self):
        self.kill_switch = HierarchicalKillSwitch()
        self.circuit_breaker = CircuitBreakerEngine(initial_equity=Decimal("100000"))
        self.firewall = PreTradeRiskFirewall(
            kill_switch=self.kill_switch,
            circuit_breaker=self.circuit_breaker,
            allow_live_capital=False,
        )
        # Normal active Tuesday afternoon (London + NY overlap)
        self.base_time = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        self.pair = CurrencyPair.from_symbol("EURUSD")

    def _create_valid_intent(self, **kwargs) -> OrderIntent:
        lot = kwargs.pop("lot_size", LotSize.from_lots(1.0))
        params = {
            "intent_id": "test-intent-001",
            "symbol": "EURUSD",
            "side": OrderSide.BUY,
            "order_type": OrderType.MARKET,
            "lot_size": lot,
            "timestamp": self.base_time,
            "client_tag": "PAPER_TEST",
        }
        params.update(kwargs)
        return OrderIntent(**params)

    def _create_valid_telemetry(self, spread_pips: Decimal = Decimal("1.0"), **kwargs) -> MarketTelemetry:
        spread_price = self.pair.to_price(spread_pips)
        bid = Decimal("1.08500")
        ask = bid + spread_price
        params = {
            "symbol": "EURUSD",
            "bid": bid,
            "ask": ask,
            "quote_timestamp": self.base_time,
            "recent_spreads_pips": [Decimal("1.0")] * 20,
        }
        params.update(kwargs)
        return MarketTelemetry(**params)

    def test_valid_order_approved(self):
        intent = self._create_valid_intent()
        telemetry = self._create_valid_telemetry()
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=telemetry)
        assert decision.approved
        assert decision.tier_failed is None
        assert decision.sizing_multiplier == Decimal("1.0")

    def test_tier_1_invalid_symbol(self):
        # Using model_construct to bypass pydantic validation for testing firewall gatekeeper
        intent = OrderIntent.model_construct(
            intent_id="i-bad-sym",
            symbol="INVALID_SYM",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(1.0),
            timestamp=self.base_time,
        )
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=self._create_valid_telemetry())
        assert not decision.approved
        assert decision.tier_failed == 1

    def test_tier_1_live_capital_locked_rejected(self):
        intent = self._create_valid_intent(client_tag="LIVE_EXECUTION_REAL_MONEY")
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=self._create_valid_telemetry())
        assert not decision.approved
        assert decision.tier_failed == 1
        assert "Live capital routing is locked" in decision.reason

    def test_tier_1_invalid_stop_loss_orientation(self):
        # BUY order with Stop Loss ABOVE limit price
        intent = self._create_valid_intent(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("1.08000"),
            stop_loss=Decimal("1.08500"),  # Bad SL for BUY
        )
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=self._create_valid_telemetry())
        assert not decision.approved
        assert decision.tier_failed == 1
        assert "BUY Stop Loss" in decision.reason

    def test_tier_2_kill_switches(self):
        telemetry = self._create_valid_telemetry()
        intent = self._create_valid_intent()

        # 1. Global kill switch
        self.kill_switch.arm_global("Emergency maintenance")
        dec_global = self.firewall.evaluate_order(intent, self.base_time, telemetry=telemetry)
        assert not dec_global.approved
        assert dec_global.tier_failed == 2
        self.kill_switch.disarm_global()

        # 2. Pair kill switch
        self.kill_switch.arm_pair("EURUSD", "High volatility on EUR")
        dec_pair = self.firewall.evaluate_order(intent, self.base_time, telemetry=telemetry)
        assert not dec_pair.approved
        assert dec_pair.tier_failed == 2
        self.kill_switch.disarm_pair("EURUSD")

        # 3. Strategy kill switch
        self.kill_switch.arm_strategy("strat_alpha", "Strategy underperforming")
        dec_strat = self.firewall.evaluate_order(
            intent, self.base_time, telemetry=telemetry, strategy_id="strat_alpha"
        )
        assert not dec_strat.approved
        assert dec_strat.tier_failed == 2

    def test_tier_3_missing_telemetry_fails_closed(self):
        intent = self._create_valid_intent()
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=None)
        assert not decision.approved
        assert decision.tier_failed == 3
        assert "Fail-Closed" in decision.reason

    def test_tier_3_clock_drift_greater_than_200ms(self):
        intent = self._create_valid_intent(timestamp=self.base_time)
        telemetry = self._create_valid_telemetry()
        # Simulated server time 250ms ahead of order timestamp
        server_time = self.base_time + timedelta(milliseconds=250)
        decision = self.firewall.evaluate_order(intent, server_time, telemetry=telemetry)
        assert not decision.approved
        assert decision.tier_failed == 3
        assert "Clock drift" in decision.reason

    def test_tier_3_stale_quote_greater_than_1000ms(self):
        intent = self._create_valid_intent()
        # Telemetry quote is 1.5s old
        stale_telemetry = self._create_valid_telemetry(quote_timestamp=self.base_time - timedelta(milliseconds=1500))
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=stale_telemetry)
        assert not decision.approved
        assert decision.tier_failed == 3
        assert "Stale market quote" in decision.reason

    def test_tier_4_weekend_closure_blocked(self):
        # Saturday 14:00 UTC
        sat_time = datetime(2026, 1, 10, 14, 0, 0, tzinfo=timezone.utc)
        intent = self._create_valid_intent(timestamp=sat_time)
        telemetry = self._create_valid_telemetry(quote_timestamp=sat_time)
        decision = self.firewall.evaluate_order(intent, sat_time, telemetry=telemetry)
        assert not decision.approved
        assert decision.tier_failed == 4
        assert "weekend" in decision.reason.lower()

    def test_tier_5_rollover_blackout_blocks_non_carry(self):
        # Tuesday 21:05 UTC (inside 20:55 to 21:15 UTC rollover window)
        rollover_time = datetime(2026, 1, 6, 21, 5, 0, tzinfo=timezone.utc)
        intent = self._create_valid_intent(timestamp=rollover_time)
        telemetry = self._create_valid_telemetry(quote_timestamp=rollover_time)

        # Non-carry order: BLOCKED
        dec_non_carry = self.firewall.evaluate_order(intent, rollover_time, telemetry=telemetry, is_carry_order=False)
        assert not dec_non_carry.approved
        assert dec_non_carry.tier_failed == 5

        # Carry order: ALLOWED through rollover gate
        dec_carry = self.firewall.evaluate_order(intent, rollover_time, telemetry=telemetry, is_carry_order=True)
        assert dec_carry.approved

    def test_tier_6_spread_exceeds_3_pips_on_majors(self):
        # Spread is 3.5 pips (> 3.0 limit)
        telemetry = self._create_valid_telemetry(spread_pips=Decimal("3.5"))
        intent = self._create_valid_intent()
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=telemetry)
        assert not decision.approved
        assert decision.tier_failed == 6
        assert "major pair limit" in decision.reason

    def test_tier_6_spread_exceeds_2_5x_median(self):
        # Median is 0.8 pips. Current is 2.2 pips (> 0.8 * 2.5 = 2.0 pips)
        telemetry = self._create_valid_telemetry(
            spread_pips=Decimal("2.2"),
            recent_spreads_pips=[Decimal("0.8")] * 20,
        )
        intent = self._create_valid_intent()
        decision = self.firewall.evaluate_order(intent, self.base_time, telemetry=telemetry)
        assert not decision.approved
        assert decision.tier_failed == 6
        assert "2.5x rolling median threshold" in decision.reason


class TestCircuitBreakers:
    """Test daily loss dampener, daily loss halt, and all-time HWM safe mode."""

    def test_daily_loss_dampener_reduces_sizing_by_50_pct(self):
        engine = CircuitBreakerEngine(initial_equity=Decimal("100000"))
        now = datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone.utc)

        # Equity drops to $97,500 (-2.5% daily drawdown)
        status = engine.update_equity(Decimal("97500"), now)
        assert status.is_dampened
        assert not status.is_daily_halted
        assert status.sizing_multiplier == Decimal("0.50")

    def test_daily_loss_halt_at_minus_4_pct(self):
        engine = CircuitBreakerEngine(initial_equity=Decimal("100000"))
        now = datetime(2026, 1, 6, 15, 0, 0, tzinfo=timezone.utc)

        # Equity drops to $95,500 (-4.5% daily drawdown)
        status = engine.update_equity(Decimal("95500"), now)
        assert status.is_daily_halted
        assert status.sizing_multiplier == Decimal("0.0")
        assert "Daily equity drawdown" in str(status.reason)

    def test_hwm_drawdown_safe_mode_at_minus_10_pct(self):
        engine = CircuitBreakerEngine(initial_equity=Decimal("100000"))
        # First grew to $120,000 peak equity
        engine.update_equity(Decimal("120000"), datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc))
        assert engine.high_water_mark == Decimal("120000")

        # Then falls to $107,000 (drawdown from HWM = (107k - 120k)/120k = -10.83%)
        now = datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone.utc)
        status = engine.update_equity(Decimal("107000"), now)
        assert status.is_safe_mode
        assert status.sizing_multiplier == Decimal("0.0")
        assert "SAFE_MODE activated" in str(status.reason)
