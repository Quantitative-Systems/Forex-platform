"""
Global Currency Net-Delta Exposure Matrix.
Decomposes multi-pair portfolio positions into raw currency units and base currency net deltas.
Computes portfolio exposure weights across USD, EUR, GBP, JPY, AUD, CAD, CHF, and NZD.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    Position,
    to_decimal,
)


class CurrencyDelta(BaseModel):
    """
    Net delta exposure in a single currency.
    """
    model_config = ConfigDict(frozen=True)

    currency: str
    raw_delta: Decimal           # In currency's own units (e.g. +100,000 EUR)
    rate_to_account: Decimal     # Conversion rate to account base (e.g. 1.0800 for EUR to USD)
    base_delta: Decimal          # Converted to account base currency (e.g. +108,000 USD)
    exposure_pct: Decimal        # base_delta / account_equity (can be negative for net short)
    abs_exposure_pct: Decimal    # abs(base_delta) / account_equity


class PortfolioExposureSnapshot(BaseModel):
    """
    Complete point-in-time snapshot of portfolio currency exposures.
    """
    model_config = ConfigDict(frozen=True)

    account_currency: str = "USD"
    account_equity: Decimal
    deltas: Dict[str, CurrencyDelta] = Field(default_factory=dict)
    max_currency_exposure_pct: Decimal = Decimal("0.0")
    dominant_currency: str = ""
    gross_exposure_base: Decimal = Decimal("0.0")
    is_balanced: bool = True
    active_pairs: List[str] = Field(default_factory=list)


class CurrencyExposureMatrix:
    """
    Institutional Currency Exposure Matrix.
    Tracks net currency delta exposures across all G8 currencies:
    USD, EUR, GBP, JPY, AUD, CAD, CHF, NZD.
    """

    SUPPORTED_CURRENCIES: Set[str] = {
        "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"
    }

    # Fallback standard spot exchange rates against USD
    DEFAULT_RATES: Dict[str, Decimal] = {
        "EURUSD": Decimal("1.0800"),
        "GBPUSD": Decimal("1.2800"),
        "USDJPY": Decimal("155.00"),
        "AUDUSD": Decimal("0.6500"),
        "USDCAD": Decimal("1.3800"),
        "USDCHF": Decimal("0.9000"),
        "NZDUSD": Decimal("0.6000"),
        "EURGBP": Decimal("0.8437"),
        "EURJPY": Decimal("167.40"),
        "GBPJPY": Decimal("198.40"),
    }

    def __init__(
        self,
        account_currency: str = "USD",
        default_rates: Optional[Dict[str, Decimal]] = None,
    ):
        self.account_currency = account_currency.upper()
        self._rates: Dict[str, Decimal] = dict(self.DEFAULT_RATES)
        if default_rates:
            for sym, rate in default_rates.items():
                clean_sym = sym.strip().upper().replace("/", "").replace("_", "")
                self._rates[clean_sym] = to_decimal(rate)

    def update_rate(self, symbol: str, rate: Decimal | float | str) -> None:
        """Update spot rate for a currency pair."""
        clean_sym = symbol.strip().upper().replace("/", "").replace("_", "")
        self._rates[clean_sym] = to_decimal(rate)

    def get_rate(self, symbol: str) -> Decimal:
        """Get spot rate for a symbol."""
        clean = symbol.strip().upper().replace("/", "").replace("_", "")
        if clean in self._rates:
            return self._rates[clean]
        # Try inverted
        if len(clean) == 6:
            inv = clean[3:] + clean[:3]
            if inv in self._rates and self._rates[inv] > Decimal("0"):
                return Decimal("1.0") / self._rates[inv]
        return Decimal("1.0")

    def get_rate_to_account(self, currency: str, current_rates: Optional[Dict[str, Decimal]] = None) -> Decimal:
        """
        Calculates conversion multiplier to translate 1 unit of `currency` into `account_currency`.
        Example: If account is USD:
        - EUR -> Rate(EURUSD) ~ 1.0800
        - JPY -> 1 / Rate(USDJPY) ~ 1 / 155.00
        - USD -> 1.0000
        """
        curr = currency.strip().upper()
        acct = self.account_currency

        if curr == acct:
            return Decimal("1.0")

        rates = dict(self._rates)
        if current_rates:
            for k, v in current_rates.items():
                rates[k.strip().upper().replace("/", "").replace("_", "")] = to_decimal(v)

        # Check direct pair CURR/ACCT (e.g. EURUSD)
        direct_sym = f"{curr}{acct}"
        if direct_sym in rates:
            return rates[direct_sym]

        # Check inverse pair ACCT/CURR (e.g. USDJPY)
        inv_sym = f"{acct}{curr}"
        if inv_sym in rates and rates[inv_sym] > Decimal("0"):
            return Decimal("1.0") / rates[inv_sym]

        # Check cross triangulation via USD if account is not USD
        if acct != "USD":
            # curr -> USD -> acct
            curr_to_usd = self._rate_between(curr, "USD", rates)
            usd_to_acct = self._rate_between("USD", acct, rates)
            return curr_to_usd * usd_to_acct

        return Decimal("1.0")

    def _rate_between(self, from_c: str, to_c: str, rates: Dict[str, Decimal]) -> Decimal:
        if from_c == to_c:
            return Decimal("1.0")
        direct = f"{from_c}{to_c}"
        if direct in rates:
            return rates[direct]
        inv = f"{to_c}{from_c}"
        if inv in rates and rates[inv] > Decimal("0"):
            return Decimal("1.0") / rates[inv]
        return Decimal("1.0")

    def decompose_position(
        self,
        position: Position,
        current_price: Optional[Decimal] = None,
    ) -> Dict[str, Decimal]:
        """
        Decomposes an open position into raw currency units.
        Returns: {base_currency: raw_units, quote_currency: raw_units}.
        Example: Long 1.0 lot EURUSD @ 1.0800:
          {"EUR": +100,000, "USD": -108,000}
        Example: Short 1.0 lot USDJPY @ 155.00:
          {"USD": -100,000, "JPY": +15,500,000}
        """
        if not position.is_open or position.units <= 0:
            return {}

        pair = CurrencyPair.from_symbol(position.symbol)
        price = to_decimal(current_price) if current_price is not None else position.current_price
        if price <= Decimal("0"):
            price = position.average_entry_price
        if price <= Decimal("0"):
            price = self.get_rate(pair.symbol)

        units = Decimal(str(position.units))
        quote_units = (units * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if position.side == OrderSide.BUY:
            return {
                pair.base_currency: units,
                pair.quote_currency: -quote_units,
            }
        else:
            return {
                pair.base_currency: -units,
                pair.quote_currency: quote_units,
            }

    def decompose_order_intent(
        self,
        intent: OrderIntent,
        current_price: Optional[Decimal] = None,
    ) -> Dict[str, Decimal]:
        """
        Decomposes a proposed OrderIntent into raw currency units.
        """
        if intent.lot_size.units <= 0:
            return {}

        pair = CurrencyPair.from_symbol(intent.symbol)
        price = to_decimal(current_price) if current_price is not None else (
            intent.limit_price or self.get_rate(pair.symbol)
        )
        if price is None or price <= Decimal("0"):
            price = self.get_rate(pair.symbol)

        units = Decimal(str(intent.lot_size.units))
        quote_units = (units * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if intent.side == OrderSide.BUY:
            return {
                pair.base_currency: units,
                pair.quote_currency: -quote_units,
            }
        else:
            return {
                pair.base_currency: -units,
                pair.quote_currency: quote_units,
            }

    def calculate_portfolio_exposure(
        self,
        positions: List[Position],
        account_equity: Decimal | float | str,
        pending_intents: Optional[List[OrderIntent]] = None,
        current_prices: Optional[Dict[str, Decimal]] = None,
        max_exposure_cap_pct: Decimal = Decimal("0.25"),
    ) -> PortfolioExposureSnapshot:
        """
        Calculates global portfolio net-delta exposures across all individual currencies.
        """
        equity = to_decimal(account_equity)
        if equity <= Decimal("0"):
            equity = Decimal("1.0")  # Avoid division by zero in degraded states

        raw_totals: Dict[str, Decimal] = {c: Decimal("0.0") for c in self.SUPPORTED_CURRENCIES}
        active_pairs: Set[str] = set()

        # 1. Accumulate open positions
        for pos in positions:
            if not pos.is_open:
                continue
            pair_sym = pos.symbol.upper().replace("/", "").replace("_", "")
            active_pairs.add(pair_sym)
            px = current_prices.get(pair_sym) if current_prices else None
            decomp = self.decompose_position(pos, current_price=px)
            for curr, amt in decomp.items():
                curr_up = curr.upper()
                raw_totals[curr_up] = raw_totals.get(curr_up, Decimal("0.0")) + amt

        # 2. Accumulate pending/simulated order intents
        if pending_intents:
            for intent in pending_intents:
                pair_sym = intent.symbol.upper().replace("/", "").replace("_", "")
                active_pairs.add(pair_sym)
                px = current_prices.get(pair_sym) if current_prices else None
                decomp = self.decompose_order_intent(intent, current_price=px)
                for curr, amt in decomp.items():
                    curr_up = curr.upper()
                    raw_totals[curr_up] = raw_totals.get(curr_up, Decimal("0.0")) + amt

        # 3. Convert all raw currency deltas to account base currency
        deltas: Dict[str, CurrencyDelta] = {}
        max_pct = Decimal("0.0")
        dominant = ""
        gross_base = Decimal("0.0")
        is_balanced = True

        for curr, raw_amt in raw_totals.items():
            rate = self.get_rate_to_account(curr, current_rates=current_prices)
            base_amt = (raw_amt * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            exp_pct = (base_amt / equity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            abs_exp_pct = abs(exp_pct)

            deltas[curr] = CurrencyDelta(
                currency=curr,
                raw_delta=raw_amt,
                rate_to_account=rate,
                base_delta=base_amt,
                exposure_pct=exp_pct,
                abs_exposure_pct=abs_exp_pct,
            )

            gross_base += abs(base_amt)
            if abs_exp_pct > max_pct:
                max_pct = abs_exp_pct
                dominant = curr

            if abs_exp_pct > max_exposure_cap_pct:
                is_balanced = False

        return PortfolioExposureSnapshot(
            account_currency=self.account_currency,
            account_equity=equity,
            deltas=deltas,
            max_currency_exposure_pct=max_pct,
            dominant_currency=dominant,
            gross_exposure_base=gross_base,
            is_balanced=is_balanced,
            active_pairs=sorted(list(active_pairs)),
        )

    def simulate_order_exposure(
        self,
        positions: List[Position],
        intent: OrderIntent,
        account_equity: Decimal | float | str,
        current_prices: Optional[Dict[str, Decimal]] = None,
        max_exposure_cap_pct: Decimal = Decimal("0.25"),
    ) -> PortfolioExposureSnapshot:
        """
        Simulate portfolio state after adding a proposed order intent.
        """
        return self.calculate_portfolio_exposure(
            positions=positions,
            account_equity=account_equity,
            pending_intents=[intent],
            current_prices=current_prices,
            max_exposure_cap_pct=max_exposure_cap_pct,
        )
