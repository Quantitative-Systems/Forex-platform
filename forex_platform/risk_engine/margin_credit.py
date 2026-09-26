"""
Margin and Credit Risk Checks for Pre-Trade Risk Firewall.
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
from forex_platform.risk_engine.firewall import RiskDecision


class MarginCreditCheck:
    """
    Margin and Credit Check for pre-trade risk.
    """

    def __init__(
        self,
        # Margin parameters
        leverage_limit: Decimal = Decimal("50.0"),  # 50:1 leverage max
        # Credit parameters: counterparty credit limits (in account currency)
        credit_limits: Optional[Dict[str, Decimal]] = None,
    ):
        """
        :param leverage_limit: Maximum allowed leverage (e.g., 50 for 50:1).
        :param credit_limits: Dict of counterparty -> credit limit (in account currency, e.g., USD).
        """
        self.leverage_limit = leverage_limit
        self.credit_limits = credit_limits or {}
        # Track used credit per counterparty (simplified)
        self._used_credit: Dict[str, Decimal] = {}

    def check_margin(
        self,
        intent: OrderIntent,
        account_equity: Decimal,
        current_price: Decimal,
    ) -> RiskDecision:
        """
        Check if the order exceeds margin limits based on leverage.
        :param intent: The order intent.
        :param account_equity: Current account equity.
        :param current_price: Current mid price for the symbol (in quote currency).
        :return: RiskDecision.
        """
        # Calculate notional value of the order in quote currency
        # For forex, 1 lot = 100,000 units of base currency.
        # Notional in quote currency = lots * 100,000 * exchange rate (if quote is USD, etc.)
        # We'll assume the account currency is the quote currency for simplicity.
        # In a multi-currency account, we would need to convert.
        lot_size = intent.lot_size
        # Units of base currency
        units_base = lot_size.units
        # Notional in base currency = units_base
        # To get notional in quote currency, we need the exchange rate.
        # For example, EUR/USD: 1 EUR = current_price USD.
        # So notional in USD = units_base * current_price.
        # However, if the account is in a different currency, we need conversion.
        # For simplicity, we assume account currency is the quote currency of the pair.
        pair = CurrencyPair.from_symbol(intent.symbol)
        # If the account currency is not the quote, we would need a conversion rate.
        # We'll skip that for now and assume quote currency is account currency.
        notional_quote = Decimal(units_base) * current_price

        # Required margin = notional / leverage
        required_margin = notional_quote / self.leverage_limit

        # Available margin = account_equity (simplified, ignoring existing margin usage)
        # In reality, we would subtract used margin from open positions.
        available_margin = account_equity

        if required_margin > available_margin:
            return RiskDecision(
                approved=False,
                reason=f"Margin check failed: required margin {required_margin:.2f} > available margin {available_margin:.2f}",
                details={
                    "required_margin": str(required_margin),
                    "available_margin": str(available_margin),
                    "leverage_used": str(notional_quote / account_equity if account_equity > 0 else Decimal("inf")),
                },
            )

        # If we want to return a sizing multiplier based on margin usage:
        # sizing_multiplier = available_margin / required_margin (capped at 1.0)
        # But we'll just approve and let the caller adjust size if needed.
        return RiskDecision(
            approved=True,
            reason="Margin check passed.",
            details={
                "required_margin": str(required_margin),
                "available_margin": str(available_margin),
            },
        )

    def check_credit(
        self,
        intent: OrderIntent,
        counterparty: str,
        additional_exposure: Decimal,
    ) -> RiskDecision:
        """
        Check if adding the order would exceed the counterparty's credit limit.
        :param intent: The order intent.
        :param counterparty: The counterparty (e.g., broker, bank).
        :param additional_exposure: The additional credit exposure from this order (in account currency).
        :return: RiskDecision.
        """
        limit = self.credit_limits.get(counterparty)
        if limit is None:
            # No limit set, assume unlimited (or could reject)
            return RiskDecision(
                approved=True,
                reason=f"No credit limit set for counterparty {counterparty}.",
            )

        used = self._used_credit.get(counterparty, Decimal("0"))
        new_used = used + additional_exposure
        if new_used > limit:
            return RiskDecision(
                approved=False,
                reason=f"Credit limit exceeded for counterparty {counterparty}: used {new_used:.2f} > limit {limit:.2f}",
                details={
                    "counterparty": counterparty,
                    "used_credit": str(used),
                    "additional_exposure": str(additional_exposure),
                    "limit": str(limit),
                },
            )

        # Update used credit (in a real system, this would be done on order fill)
        # For pre-trade, we might not update until fill, but we can show the projected usage.
        return RiskDecision(
            approved=True,
            reason=f"Credit check passed for counterparty {counterparty}.",
            details={
                "counterparty": counterparty,
                "used_credit": str(used),
                "projected_used": str(new_used),
                "limit": str(limit),
            },
        )

    def update_used_credit(self, counterparty: str, amount: Decimal) -> None:
        """Update the used credit for a counterparty (e.g., on order fill)."""
        self._used_credit[counterparty] = self._used_credit.get(counterparty, Decimal("0")) + amount

    def reset_used_credit(self, counterparty: Optional[str] = None) -> None:
        """Reset used credit for a counterparty or all."""
        if counterparty is None:
            self._used_credit.clear()
        else:
            self._used_credit.pop(counterparty, None)