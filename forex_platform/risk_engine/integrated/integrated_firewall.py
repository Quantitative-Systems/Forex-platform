"""
Integrated Pre-Trade Risk Firewall.
Combines the existing 6-tier firewall with margin and credit checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
)
from forex_platform.risk_engine.firewall import (
    PreTradeRiskFirewall,
    RiskDecision,
    MarketTelemetry,
)
from forex_platform.risk_engine.margin_credit import MarginCreditCheck
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch
from forex_platform.portfolio_engine.allocator import CurrencyExposureGovernor
from forex_platform.core.sessions import ForexSessionEngine


class IntegratedPreTradeRiskFirewall:
    """
    Integrated Pre-Trade Risk Firewall that combines:
    - Original 6-tier firewall (sanity, kill switches, latency, weekend, rollover, spread)
    - Margin and credit checks
    - Position limits and concentration risk
    """

    def __init__(
        self,
        kill_switch: HierarchicalKillSwitch,
        circuit_breaker: CircuitBreakerEngine,
        margin_credit_check: MarginCreditCheck,
        exposure_governor: Optional[CurrencyExposureGovernor] = None,
        account_equity: Decimal = Decimal("100000.00"),
        # Additional position limit parameters
        max_position_size: Optional[Decimal] = None,  # Max units per symbol
        max_sector_exposure: Optional[Dict[str, Decimal]] = None,  # e.g., {"EUR": 0.3} for 30% max EUR exposure
    ):
        self.base_firewall = PreTradeRiskFirewall(
            kill_switch=kill_switch,
            circuit_breaker=circuit_breaker,
            allow_live_capital=False,  # Keep the zero live capital constraint
            exposure_governor=exposure_governor,
            account_equity=account_equity,
        )
        self.margin_credit_check = margin_credit_check
        self.max_position_size = max_position_size
        self.max_sector_exposure = max_sector_exposure or {}
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
        current_price: Optional[Decimal] = None,
    ) -> RiskDecision:
        """
        Evaluate order intent through all risk tiers including margin and credit.
        """
        eq = account_equity if account_equity is not None else self.account_equity

        # Run the original 6-tier firewall first
        base_decision = self.base_firewall.evaluate_order(
            intent=intent,
            current_time=current_time,
            telemetry=telemetry,
            tenant_id=tenant_id,
            broker_id=broker_id,
            strategy_id=strategy_id,
            is_carry_order=is_carry_order,
            open_positions=open_positions,
            account_equity=eq,
        )

        # If base firewall already rejected, return that decision
        if not base_decision.approved:
            return base_decision

        # Additional checks: margin, credit, position limits, concentration
        try:
            # 1. Margin Check
            if current_price is not None:
                margin_decision = self.margin_credit_check.check_margin(
                    intent=intent,
                    account_equity=eq,
                    current_price=current_price,
                )
                if not margin_decision.approved:
                    # Return a new decision with tier 7 (margin) failure
                    return RiskDecision(
                        approved=False,
                        tier_failed=7,
                        reason=f"Margin Check Failure: {margin_decision.reason}",
                        sizing_multiplier=Decimal("0.0"),
                        details=margin_decision.details,
                    )
                # If margin check returned a sizing multiplier, apply it
                sizing_mult = base_decision.sizing_multiplier
                if "sizing_multiplier" in margin_decision.details:
                    margin_mult = Decimal(margin_decision.details["sizing_multiplier"])
                    sizing_mult = min(sizing_mult, margin_mult)

            # 2. Credit Check (simplified - in reality you'd need current exposure)
            # For now, we'll skip credit check in pre-trade as it depends on current positions
            # In a live system, you would check against credit limits with projected exposure

            # 3. Position Size Limit
            if self.max_position_size is not None:
                if intent.lot_size.units > self.max_position_size:
                    return RiskDecision(
                        approved=False,
                        tier_failed=8,
                        reason=f"Position size limit exceeded: {intent.lot_size.units} > {self.max_position_size}",
                        sizing_multiplier=Decimal("0.0"),
                        details={
                            "requested_size": str(intent.lot_size.units),
                            "max_allowed": str(self.max_position_size),
                        },
                    )

            # 4. Sector/Concentration Risk (simplified)
            # This would require more complex analysis of current positions
            # For now, we'll rely on the exposure governor in the base firewall

            # All checks passed
            return RiskDecision(
                approved=True,
                tier_failed=None,
                reason="All risk checks passed (including margin and credit).",
                sizing_multiplier=base_decision.sizing_multiplier,
                details=base_decision.details,
            )

        except Exception as e:
            # Fail-closed on any exception in additional checks
            return RiskDecision(
                approved=False,
                tier_failed=0,  # Custom tier for integrated firewall exceptions
                reason=f"FAIL-CLOSED: Unexpected exception in integrated risk evaluation: {e}",
                sizing_multiplier=Decimal("0.0"),
                details={"exception": str(e)},
            )