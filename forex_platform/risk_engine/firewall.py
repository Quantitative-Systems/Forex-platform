"""
6-Tier Pre-Trade Risk Firewall and Gatekeeper.
Strictly non-custodial and fail-closed: any corrupted telemetry, clock drift > 200ms,
or missing quotes immediately return approved=False.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
import statistics
from typing import Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    to_decimal,
)
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.portfolio_engine.allocator import CurrencyExposureGovernor
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine, CircuitBreakerStatus
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch


class MarketTelemetry(BaseModel):
    """
    Live market telemetry required for latency, spread, and quote freshness gating.
    """
    model_config = ConfigDict(frozen=True)

    symbol: str
    bid: Decimal
    ask: Decimal
    quote_timestamp: datetime
    recent_spreads_pips: List[Decimal] = Field(default_factory=list)

    @property
    def spread_pips(self) -> Decimal:
        pair = CurrencyPair.from_symbol(self.symbol)
        diff = self.ask - self.bid
        return pair.to_pips(diff)

    @property
    def median_spread_pips(self) -> Decimal:
        if not self.recent_spreads_pips:
            return self.spread_pips
        med = statistics.median([float(s) for s in self.recent_spreads_pips])
        return to_decimal(round(med, 2))


class RiskDecision(BaseModel):
    """
    Pre-trade risk assessment verdict.
    """
    model_config = ConfigDict(frozen=True)

    approved: bool
    tier_failed: Optional[int] = None
    reason: str
    sizing_multiplier: Decimal = Decimal("1.0")
    details: Dict[str, str] = Field(default_factory=dict)


class PreTradeRiskFirewall:
    """
    Institutional 6-Tier Pre-Trade Risk Firewall:
    - Tier 1: Order Sanity Gate (Lot size, symbol, price sanity, zero-live-capital constraint).
    - Tier 2: Hierarchical Kill Switch Gate (Global, Tenant, Broker, Pair, Strategy).
    - Tier 3: Latency & Clock Drift Gate (Fail-closed on drift > 200ms or stale quotes > 1000ms).
    - Tier 4: Weekend Closure Gate (Rejects order placements during weekend closure).
    - Tier 5: Rollover Blackout Gate (Blocks non-carry orders between 20:55 and 21:15 UTC).
    - Tier 6: Dynamic Spread Gate (Spread > 3.0 pips on majors or > 2.5x 20-period median).
    """

    MAX_CLOCK_DRIFT_MS = 200  # 200 milliseconds
    MAX_QUOTE_AGE_MS = 1000   # 1.0 second
    MAJOR_SPREAD_LIMIT_PIPS = Decimal("3.0")
    RELATIVE_SPREAD_MULTIPLIER = Decimal("2.5")
    MAJOR_SYMBOLS = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"}

    def __init__(
        self,
        kill_switch: HierarchicalKillSwitch,
        circuit_breaker: CircuitBreakerEngine,
        allow_live_capital: bool = False,  # Strict zero live capital constraint
        exposure_governor: Optional[CurrencyExposureGovernor] = None,
        account_equity: Decimal = Decimal("100000.00"),
    ):
        self.kill_switch = kill_switch
        self.circuit_breaker = circuit_breaker
        self.allow_live_capital = allow_live_capital
        self.exposure_governor = exposure_governor or CurrencyExposureGovernor()
        self.account_equity = account_equity

    def evaluate_order(
        self,
        intent: OrderIntent,
        current_time: datetime,
        telemetry: Optional[MarketTelemetry] = None,
        tenant_id: Optional[str] = None,
        broker_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        is_carry_order: bool = False,
        open_positions: Optional[List[Position]] = None,
        account_equity: Optional[Decimal] = None,
    ) -> RiskDecision:
        """
        Evaluate order intent through all 6 sequential tiers.
        Fails closed on any anomaly or exception.
        """
        try:
            return self._run_tiers(
                intent=intent,
                current_time=current_time,
                telemetry=telemetry,
                tenant_id=tenant_id,
                broker_id=broker_id,
                strategy_id=strategy_id,
                is_carry_order=is_carry_order,
                open_positions=open_positions,
                account_equity=account_equity,
            )
        except Exception as e:
            # Absolute Fail-Closed invariant
            return RiskDecision(
                approved=False,
                tier_failed=0,
                reason=f"FAIL-CLOSED: Unexpected exception during pre-trade risk evaluation: {e}",
                sizing_multiplier=Decimal("0.0"),
                details={"exception": str(e)},
            )

    def _run_tiers(
        self,
        intent: OrderIntent,
        current_time: datetime,
        telemetry: Optional[MarketTelemetry],
        tenant_id: Optional[str],
        broker_id: Optional[str],
        strategy_id: Optional[str],
        is_carry_order: bool,
        open_positions: Optional[List[Position]] = None,
        account_equity: Optional[Decimal] = None,
    ) -> RiskDecision:
        utc_now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        order_time = intent.timestamp if intent.timestamp.tzinfo else intent.timestamp.replace(tzinfo=timezone.utc)

        # ---------------------------------------------------------------------
        # TIER 1: ORDER SANITY & ZERO LIVE CAPITAL GATE
        # ---------------------------------------------------------------------
        # Check lot size
        if intent.lot_size.units <= 0:
            return RiskDecision(
                approved=False,
                tier_failed=1,
                reason="Tier 1 Failure: Lot size units must be positive.",
            )

        # Check symbol validity
        try:
            pair = CurrencyPair.from_symbol(intent.symbol)
        except Exception as e:
            return RiskDecision(
                approved=False,
                tier_failed=1,
                reason=f"Tier 1 Failure: Invalid symbol '{intent.symbol}': {e}",
            )

        # Zero Live Capital Invariant
        if not self.allow_live_capital:
            if intent.client_tag and "LIVE_EXECUTION" in intent.client_tag:
                return RiskDecision(
                    approved=False,
                    tier_failed=1,
                    reason="Tier 1 Failure: Live capital routing is locked at $0.00.",
                )

        # Price sanity on limit / stop orders
        if intent.order_type in (OrderType.LIMIT, OrderType.STOP):
            if intent.limit_price is None or intent.limit_price <= Decimal("0"):
                return RiskDecision(
                    approved=False,
                    tier_failed=1,
                    reason=f"Tier 1 Failure: {intent.order_type.value} order requires positive limit_price.",
                )

        # Stop loss / Take profit logical orientation checks
        ref_price = intent.limit_price
        if ref_price is not None:
            if intent.stop_loss is not None:
                if intent.side == OrderSide.BUY and intent.stop_loss >= ref_price:
                    return RiskDecision(
                        approved=False,
                        tier_failed=1,
                        reason=f"Tier 1 Failure: BUY Stop Loss ({intent.stop_loss}) must be below price ({ref_price}).",
                    )
                elif intent.side == OrderSide.SELL and intent.stop_loss <= ref_price:
                    return RiskDecision(
                        approved=False,
                        tier_failed=1,
                        reason=f"Tier 1 Failure: SELL Stop Loss ({intent.stop_loss}) must be above price ({ref_price}).",
                    )

            if intent.take_profit is not None:
                if intent.side == OrderSide.BUY and intent.take_profit <= ref_price:
                    return RiskDecision(
                        approved=False,
                        tier_failed=1,
                        reason=f"Tier 1 Failure: BUY Take Profit ({intent.take_profit}) must be above price ({ref_price}).",
                    )
                elif intent.side == OrderSide.SELL and intent.take_profit >= ref_price:
                    return RiskDecision(
                        approved=False,
                        tier_failed=1,
                        reason=f"Tier 1 Failure: SELL Take Profit ({intent.take_profit}) must be below price ({ref_price}).",
                    )

        # ---------------------------------------------------------------------
        # TIER 2: HIERARCHICAL KILL SWITCHES & CIRCUIT BREAKER GATE
        # ---------------------------------------------------------------------
        is_killed, kill_reason = self.kill_switch.is_killed(
            tenant_id=tenant_id,
            broker_id=broker_id,
            symbol=intent.symbol,
            strategy_id=strategy_id,
        )
        if is_killed:
            return RiskDecision(
                approved=False,
                tier_failed=2,
                reason=f"Tier 2 Failure: Kill switch active: {kill_reason}",
                details={"kill_reason": str(kill_reason)},
            )

        # Circuit breaker equity check
        cb_status = self.circuit_breaker.update_equity(self.circuit_breaker.current_equity, utc_now)
        if cb_status.is_safe_mode or cb_status.is_daily_halted:
            return RiskDecision(
                approved=False,
                tier_failed=2,
                reason=f"Tier 2 Failure: Circuit breaker triggered: {cb_status.reason}",
                sizing_multiplier=Decimal("0.0"),
                details={"circuit_breaker": cb_status.model_dump_json()},
            )

        sizing_mult = cb_status.sizing_multiplier

        # ---------------------------------------------------------------------
        # TIER 3: LATENCY & CLOCK DRIFT GATE (FAIL-CLOSED)
        # ---------------------------------------------------------------------
        # Telemetry is strictly required for live trade gating
        if telemetry is None:
            return RiskDecision(
                approved=False,
                tier_failed=3,
                reason="Tier 3 Failure: Missing live market telemetry (Fail-Closed).",
            )

        # Clock drift: |current_time - order_time| > 200ms
        drift_ms = abs((utc_now - order_time).total_seconds() * 1000.0)
        if drift_ms > self.MAX_CLOCK_DRIFT_MS:
            return RiskDecision(
                approved=False,
                tier_failed=3,
                reason=f"Tier 3 Failure: Clock drift / order latency {drift_ms:.1f}ms exceeds {self.MAX_CLOCK_DRIFT_MS}ms threshold.",
                details={"drift_ms": str(drift_ms)},
            )

        # Quote staleness: (current_time - quote_time) > 1000ms
        quote_dt = telemetry.quote_timestamp if telemetry.quote_timestamp.tzinfo else telemetry.quote_timestamp.replace(tzinfo=timezone.utc)
        quote_age_ms = (utc_now - quote_dt).total_seconds() * 1000.0
        if quote_age_ms > self.MAX_QUOTE_AGE_MS:
            return RiskDecision(
                approved=False,
                tier_failed=3,
                reason=f"Tier 3 Failure: Stale market quote ({quote_age_ms:.1f}ms old) exceeds {self.MAX_QUOTE_AGE_MS}ms threshold.",
                details={"quote_age_ms": str(quote_age_ms)},
            )

        # ---------------------------------------------------------------------
        # TIER 4: PORTFOLIO EXPOSURE & WEEKEND CLOSURE GATE
        # ---------------------------------------------------------------------
        if ForexSessionEngine.is_weekend(utc_now):
            return RiskDecision(
                approved=False,
                tier_failed=4,
                reason="Tier 4 Failure: Forex market is closed for the weekend (Friday 21:00 - Sunday 21:00 UTC).",
            )

        # Portfolio Currency Net-Delta Exposure Governor Check
        if self.exposure_governor is not None and open_positions is not None:
            eq = account_equity if account_equity is not None else self.account_equity
            curr_prices = {telemetry.symbol: telemetry.bid} if telemetry else None

            gov_decision = self.exposure_governor.evaluate_intent(
                intent=intent,
                open_positions=open_positions,
                account_equity=eq,
                current_prices=curr_prices,
            )
            if not gov_decision.approved:
                return RiskDecision(
                    approved=False,
                    tier_failed=4,
                    reason=f"Tier 4 Failure: {gov_decision.reason}",
                    sizing_multiplier=Decimal("0.0"),
                    details={
                        "breached_currency": gov_decision.breached_currency or "",
                        "current_exposure_pct": str(gov_decision.current_exposure_pct or Decimal("0.0")),
                        "projected_exposure_pct": str(gov_decision.projected_exposure_pct or Decimal("0.0")),
                    },
                )
            elif gov_decision.downsized and gov_decision.allowed_lots is not None:
                orig_u = Decimal(str(intent.lot_size.units))
                allow_u = Decimal(str(gov_decision.allowed_lots.units))
                ratio = (allow_u / orig_u).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                sizing_mult = min(sizing_mult, ratio)

        # ---------------------------------------------------------------------
        # TIER 5: ROLLOVER BLACKOUT GATE
        # ---------------------------------------------------------------------
        if ForexSessionEngine.is_rollover(utc_now):
            if not is_carry_order:
                return RiskDecision(
                    approved=False,
                    tier_failed=5,
                    reason="Tier 5 Failure: Daily rollover blackout window (20:55 - 21:15 UTC). Non-carry orders blocked.",
                )

        # ---------------------------------------------------------------------
        # TIER 6: DYNAMIC SPREAD GATE
        # ---------------------------------------------------------------------
        current_spread_pips = telemetry.spread_pips
        clean_sym = pair.symbol

        # Check absolute spread threshold on major currency pairs
        if clean_sym in self.MAJOR_SYMBOLS and current_spread_pips > self.MAJOR_SPREAD_LIMIT_PIPS:
            return RiskDecision(
                approved=False,
                tier_failed=6,
                reason=(
                    f"Tier 6 Failure: Current spread {current_spread_pips:.2f} pips exceeds "
                    f"major pair limit of {self.MAJOR_SPREAD_LIMIT_PIPS:.2f} pips."
                ),
                details={"current_spread": str(current_spread_pips)},
            )

        # Check relative spread multiplier against 20-period rolling median
        median_spread = telemetry.median_spread_pips
        if median_spread > Decimal("0"):
            max_allowed_spread = median_spread * self.RELATIVE_SPREAD_MULTIPLIER
            if current_spread_pips > max_allowed_spread:
                return RiskDecision(
                    approved=False,
                    tier_failed=6,
                    reason=(
                        f"Tier 6 Failure: Current spread {current_spread_pips:.2f} pips exceeds "
                        f"2.5x rolling median threshold ({max_allowed_spread:.2f} pips, median={median_spread:.2f})."
                    ),
                    details={
                        "current_spread": str(current_spread_pips),
                        "median_spread": str(median_spread),
                        "threshold": str(max_allowed_spread),
                    },
                )

        # All 6 tiers passed!
        return RiskDecision(
            approved=True,
            tier_failed=None,
            reason="All 6 Pre-Trade Risk Tiers Approved.",
            sizing_multiplier=sizing_mult,
            details={
                "current_spread_pips": str(current_spread_pips),
                "sizing_multiplier": str(sizing_mult),
            },
        )
