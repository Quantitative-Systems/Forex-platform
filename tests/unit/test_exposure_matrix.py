"""
Unit tests for Global Currency Net-Delta Exposure Matrix and Governor (Milestone 2).
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
)
from forex_platform.portfolio_engine.exposure_matrix import (
    CurrencyExposureMatrix,
    CurrencyDelta,
    PortfolioExposureSnapshot,
)
from forex_platform.portfolio_engine.allocator import (
    CurrencyExposureGovernor,
    GovernorDecision,
)
from forex_platform.risk_engine.firewall import (
    PreTradeRiskFirewall,
    MarketTelemetry,
)
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch


def _create_position(
    symbol: str,
    side: OrderSide,
    lots: float,
    entry_price: float,
    pos_id: str = "pos_1",
) -> Position:
    lot = LotSize.from_lots(lots)
    now = datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc)
    return Position(
        position_id=pos_id,
        symbol=symbol,
        side=side,
        units=lot.units,
        average_entry_price=Decimal(str(entry_price)),
        current_price=Decimal(str(entry_price)),
        opened_at=now,
        updated_at=now,
        is_open=True,
    )


def _create_intent(
    symbol: str,
    side: OrderSide,
    lots: float,
    limit_price: float = 1.0800,
    intent_id: str = "intent_1",
    timestamp: datetime | None = None,
) -> OrderIntent:
    lot = LotSize.from_lots(lots)
    now = timestamp or datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc)
    return OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=side,
        order_type=OrderType.LIMIT,
        lot_size=lot,
        limit_price=Decimal(str(limit_price)),
        timestamp=now,
    )


class TestCurrencyExposureMatrix:
    """Test multi-pair delta decomposition and base-currency conversions."""

    def test_single_pair_decomposition_eurusd(self):
        matrix = CurrencyExposureMatrix(account_currency="USD")
        pos = _create_position("EURUSD", OrderSide.BUY, 1.0, 1.0800)

        decomp = matrix.decompose_position(pos)
        assert decomp["EUR"] == Decimal("100000")
        assert decomp["USD"] == Decimal("-108000.00")

    def test_single_pair_decomposition_usdjpy_short(self):
        matrix = CurrencyExposureMatrix(account_currency="USD")
        pos = _create_position("USDJPY", OrderSide.SELL, 1.0, 155.00)

        decomp = matrix.decompose_position(pos)
        assert decomp["USD"] == Decimal("-100000")
        assert decomp["JPY"] == Decimal("15500000.00")

    def test_cross_pair_decomposition_eurgbp(self):
        matrix = CurrencyExposureMatrix(account_currency="USD")
        pos = _create_position("EURGBP", OrderSide.BUY, 0.5, 0.8400)

        decomp = matrix.decompose_position(pos)
        assert decomp["EUR"] == Decimal("50000")
        assert decomp["GBP"] == Decimal("-42000.00")

    def test_portfolio_exposure_snapshot_multi_pair(self):
        matrix = CurrencyExposureMatrix(account_currency="USD")
        equity = Decimal("100000.00")

        # Long 0.20 lots EURUSD @ 1.0800: +20,000 EUR (~$21,600 USD), -21,600 USD
        # Long 0.10 lots GBPUSD @ 1.2800: +10,000 GBP (~$12,800 USD), -12,800 USD
        pos1 = _create_position("EURUSD", OrderSide.BUY, 0.20, 1.0800, "pos1")
        pos2 = _create_position("GBPUSD", OrderSide.BUY, 0.10, 1.2800, "pos2")

        snapshot = matrix.calculate_portfolio_exposure([pos1, pos2], equity)

        assert "USD" in snapshot.deltas
        assert "EUR" in snapshot.deltas
        assert "GBP" in snapshot.deltas

        # Total USD net delta = -21,600 + -12,800 = -34,400 USD
        usd_delta = snapshot.deltas["USD"]
        assert usd_delta.base_delta == Decimal("-34400.00")
        # USD exposure = 34,400 / 100,000 = 34.4%
        assert usd_delta.abs_exposure_pct == Decimal("0.3440")
        assert not snapshot.is_balanced  # Exceeds 25% default cap
        assert snapshot.dominant_currency == "USD"


class TestCurrencyExposureGovernor:
    """Test single-currency concentration cap enforcement and auto-downsizing."""

    def test_governor_approves_safe_order(self):
        governor = CurrencyExposureGovernor(max_single_currency_exposure_pct=Decimal("0.25"))
        equity = Decimal("100000.00")
        # Order of 0.10 lots EURUSD = ~10,800 USD exposure (10.8% of equity <= 25%)
        intent = _create_intent("EURUSD", OrderSide.BUY, 0.10, 1.0800)

        decision = governor.evaluate_intent(intent, open_positions=[], account_equity=equity)
        assert decision.approved
        assert not decision.downsized
        assert decision.allowed_lots is not None
        assert decision.allowed_lots.standard_lots == Decimal("0.10")

    def test_governor_blocks_5x_correlated_usd_bet(self):
        governor = CurrencyExposureGovernor(
            max_single_currency_exposure_pct=Decimal("0.25"),
            allow_downsizing=False,  # Strict rejection mode
        )
        equity = Decimal("100000.00")  # Max allowed single-currency exposure = $25,000

        # Create open positions already heavily exposed to Short USD
        # Long EURUSD 0.15 lots = -$16,200 USD
        # Long GBPUSD 0.08 lots = -$10,240 USD
        # Total USD delta = -$26,440 (already at 26.4% exposure)
        pos1 = _create_position("EURUSD", OrderSide.BUY, 0.15, 1.0800, "pos1")
        pos2 = _create_position("GBPUSD", OrderSide.BUY, 0.08, 1.2800, "pos2")

        # Propose another long AUDUSD trade that further shorts USD
        intent = _create_intent("AUDUSD", OrderSide.BUY, 0.10, 0.6500)

        decision = governor.evaluate_intent(intent, open_positions=[pos1, pos2], account_equity=equity)
        assert not decision.approved
        assert decision.breached_currency == "USD"
        assert "exceeding hard cap of 25.0%" in decision.reason

    def test_governor_auto_downsizing_to_fit_cap(self):
        governor = CurrencyExposureGovernor(
            max_single_currency_exposure_pct=Decimal("0.25"),
            allow_downsizing=True,
        )
        equity = Decimal("100000.00")  # Max cap = $25,000

        # Existing position: Long EURUSD 0.15 lots (-$16,200 USD net delta)
        # Headroom left on USD: $25,000 - $16,200 = $8,800
        pos1 = _create_position("EURUSD", OrderSide.BUY, 0.15, 1.0800, "pos1")

        # Proposed order: Long EURUSD 0.15 lots (wants another -$16,200 USD, which would total -$32,400)
        intent = _create_intent("EURUSD", OrderSide.BUY, 0.15, 1.0800)

        decision = governor.evaluate_intent(intent, open_positions=[pos1], account_equity=equity)
        assert decision.approved
        assert decision.downsized
        assert decision.allowed_lots is not None
        # Should be downsized to ~0.08 lots ($8,640 <= $8,800 headroom)
        assert decision.allowed_lots.standard_lots < Decimal("0.15")
        assert decision.allowed_lots.units >= 1000

    def test_governor_allows_risk_reducing_order(self):
        governor = CurrencyExposureGovernor(max_single_currency_exposure_pct=Decimal("0.25"))
        equity = Decimal("100000.00")

        # Existing: Long EURUSD 0.30 lots (-$32,400 USD, 32.4% exposure, breached)
        pos = _create_position("EURUSD", OrderSide.BUY, 0.30, 1.0800, "pos1")

        # Proposed order: SELL EURUSD 0.10 lots (+10,800 USD, reducing exposure to 21.6%)
        hedge_intent = _create_intent("EURUSD", OrderSide.SELL, 0.10, 1.0800)

        decision = governor.evaluate_intent(hedge_intent, open_positions=[pos], account_equity=equity)
        # Risk-reducing order must be approved
        assert decision.approved
        assert not decision.downsized


class TestFirewallTier4ExposureIntegration:
    """Test PreTradeRiskFirewall Tier 4 portfolio net-delta check."""

    def setup_method(self):
        self.kill_switch = HierarchicalKillSwitch()
        self.circuit_breaker = CircuitBreakerEngine(initial_equity=Decimal("100000.00"))
        self.governor = CurrencyExposureGovernor(
            max_single_currency_exposure_pct=Decimal("0.25"),
            allow_downsizing=False,  # Fail-closed rejection mode for firewall test
        )
        self.firewall = PreTradeRiskFirewall(
            kill_switch=self.kill_switch,
            circuit_breaker=self.circuit_breaker,
            allow_live_capital=False,
            exposure_governor=self.governor,
            account_equity=Decimal("100000.00"),
        )
        # Wednesday 14:00 UTC (active London/NY session, not weekend, not rollover)
        self.valid_time = datetime(2026, 6, 10, 14, 0, 0, tzinfo=timezone.utc)

    def _telemetry(self, sym: str = "EURUSD", bid: float = 1.0800, ask: float = 1.0801):
        return MarketTelemetry(
            symbol=sym,
            bid=Decimal(str(bid)),
            ask=Decimal(str(ask)),
            quote_timestamp=self.valid_time,
            recent_spreads_pips=[Decimal("1.0")],
        )

    def test_firewall_tier_4_approves_when_within_exposure(self):
        intent = _create_intent("EURUSD", OrderSide.BUY, 0.10, 1.0800, timestamp=self.valid_time)
        telemetry = self._telemetry("EURUSD", 1.0800, 1.0801)

        decision = self.firewall.evaluate_order(
            intent=intent,
            current_time=self.valid_time,
            telemetry=telemetry,
            open_positions=[],
            account_equity=Decimal("100000.00"),
        )
        assert decision.approved
        assert decision.tier_failed is None

    def test_firewall_tier_4_blocks_when_exposure_limit_breached(self):
        # Open positions already at 24% USD exposure
        pos1 = _create_position("EURUSD", OrderSide.BUY, 0.22, 1.0800, "pos1")
        # Proposed order wants another 0.10 lots, pushing USD exposure to ~34.5%
        intent = _create_intent("EURUSD", OrderSide.BUY, 0.10, 1.0800, timestamp=self.valid_time)
        telemetry = self._telemetry("EURUSD", 1.0800, 1.0801)

        decision = self.firewall.evaluate_order(
            intent=intent,
            current_time=self.valid_time,
            telemetry=telemetry,
            open_positions=[pos1],
            account_equity=Decimal("100000.00"),
        )
        assert not decision.approved
        assert decision.tier_failed == 4
        assert "Tier 4 Failure" in decision.reason
        assert "exceeding hard cap" in decision.reason
