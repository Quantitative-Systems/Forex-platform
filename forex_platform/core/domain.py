"""
Core domain contracts, precision pip math, and trading primitives.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self == OrderSide.BUY else OrderSide.BUY

    @property
    def sign(self) -> int:
        return 1 if self == OrderSide.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class SessionName(str, Enum):
    SYDNEY = "SYDNEY"
    TOKYO = "TOKYO"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    WEEKEND = "WEEKEND"
    CLOSED = "CLOSED"


class UrgencyLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


def to_decimal(val: Decimal | float | int | str) -> Decimal:
    """Convert input safely to Decimal avoiding binary float artifacts."""
    if isinstance(val, Decimal):
        return val
    return Decimal(str(val))


class CurrencyPair(BaseModel):
    """
    Forex Currency Pair contract with exact pip and price precision specifications.
    Supports standard 4/5 decimal pairs and 2/3 decimal JPY crosses.
    """
    model_config = ConfigDict(frozen=True)

    base_currency: str
    quote_currency: str
    symbol: str
    price_precision: int
    pip_decimal_places: int
    pip_size: Decimal
    pipette_size: Decimal
    standard_lot_units: int = 100_000
    is_jpy_cross: bool = False

    @classmethod
    def from_symbol(cls, symbol: str, price_precision: int | None = None) -> CurrencyPair:
        """
        Factory method to construct a normalized CurrencyPair.
        Accepts formats: 'EURUSD', 'EUR/USD', 'eur_usd', 'USD_JPY', etc.
        """
        cleaned = symbol.strip().upper().replace("/", "").replace("_", "").replace("-", "")
        if len(cleaned) != 6:
            raise ValueError(f"Invalid currency pair symbol '{symbol}'. Expected 6-character code (e.g., 'EURUSD').")

        base = cleaned[:3]
        quote = cleaned[3:]
        is_jpy = quote == "JPY"

        if is_jpy:
            # JPY crosses: 1 pip = 0.01 (2nd decimal), 1 pipette = 0.001 (3rd decimal)
            pip_dec = 2
            prec = price_precision if price_precision is not None else 3
            pip_val = Decimal("0.01")
            pipette_val = Decimal("0.001")
        else:
            # Standard forex pairs: 1 pip = 0.0001 (4th decimal), 1 pipette = 0.00001 (5th decimal)
            pip_dec = 4
            prec = price_precision if price_precision is not None else 5
            pip_val = Decimal("0.0001")
            pipette_val = Decimal("0.00001")

        return cls(
            base_currency=base,
            quote_currency=quote,
            symbol=f"{base}{quote}",
            price_precision=prec,
            pip_decimal_places=pip_dec,
            pip_size=pip_val,
            pipette_size=pipette_val,
            standard_lot_units=100_000,
            is_jpy_cross=is_jpy,
        )

    def to_pips(self, price_diff: Decimal | float | int | str) -> Decimal:
        """Convert a price difference to pips with exact decimal math."""
        diff = to_decimal(price_diff)
        return diff / self.pip_size

    def to_price(self, pips: Decimal | float | int | str) -> Decimal:
        """Convert a pip amount to a price difference."""
        p = to_decimal(pips)
        return p * self.pip_size

    def round_price(self, price: Decimal | float | int | str) -> Decimal:
        """Quantize price to currency pair's exact precision."""
        p = to_decimal(price)
        quant = Decimal("10") ** -self.price_precision
        return p.quantize(quant, rounding=ROUND_HALF_UP)

    def round_pips(self, pips: Decimal | float | int | str, decimal_places: int = 1) -> Decimal:
        """Round pips to desired precision (default 1 decimal place for pipette)."""
        p = to_decimal(pips)
        quant = Decimal("10") ** -decimal_places
        return p.quantize(quant, rounding=ROUND_HALF_UP)


class LotSize(BaseModel):
    """
    Representation of position volume in Forex lots and base units.
    Standard Lot: 1.0 = 100,000 units
    Mini Lot:     0.1 = 10,000 units
    Micro Lot:    0.01 = 1,000 units
    Nano Lot:     0.001 = 100 units
    """
    model_config = ConfigDict(frozen=True)

    units: int
    standard_lots: Decimal

    @classmethod
    def from_lots(cls, lots: Decimal | float | int | str, standard_lot_units: int = 100_000) -> LotSize:
        dec_lots = to_decimal(lots)
        if dec_lots <= Decimal("0"):
            raise ValueError(f"Lot size must be positive, got {lots}")
        units = int((dec_lots * Decimal(str(standard_lot_units))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return cls(units=units, standard_lots=dec_lots)

    @classmethod
    def from_units(cls, units: int, standard_lot_units: int = 100_000) -> LotSize:
        if units <= 0:
            raise ValueError(f"Units must be positive, got {units}")
        dec_lots = (Decimal(str(units)) / Decimal(str(standard_lot_units))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return cls(units=units, standard_lots=dec_lots)

    @property
    def mini_lots(self) -> Decimal:
        return self.standard_lots * Decimal("10")

    @property
    def micro_lots(self) -> Decimal:
        return self.standard_lots * Decimal("100")

    @property
    def nano_lots(self) -> Decimal:
        return self.standard_lots * Decimal("1000")

    def __add__(self, other: LotSize) -> LotSize:
        if not isinstance(other, LotSize):
            return NotImplemented
        return LotSize.from_units(self.units + other.units)

    def __sub__(self, other: LotSize) -> LotSize:
        if not isinstance(other, LotSize):
            return NotImplemented
        diff = self.units - other.units
        if diff <= 0:
            raise ValueError(f"Subtracting {other.units} from {self.units} results in non-positive units ({diff})")
        return LotSize.from_units(diff)

    def __mul__(self, factor: Decimal | float | int) -> LotSize:
        dec_factor = to_decimal(factor)
        new_units = int((Decimal(str(self.units)) * dec_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if new_units <= 0:
            raise ValueError(f"Multiplication resulted in non-positive units ({new_units})")
        return LotSize.from_units(new_units)

    def __lt__(self, other: LotSize) -> bool:
        if not isinstance(other, LotSize):
            return NotImplemented
        return self.units < other.units

    def __le__(self, other: LotSize) -> bool:
        if not isinstance(other, LotSize):
            return NotImplemented
        return self.units <= other.units

    def __gt__(self, other: LotSize) -> bool:
        if not isinstance(other, LotSize):
            return NotImplemented
        return self.units > other.units

    def __ge__(self, other: LotSize) -> bool:
        if not isinstance(other, LotSize):
            return NotImplemented
        return self.units >= other.units


class PipCalculator:
    """
    Mathematical engine for pip value conversion and PnL determination across account currencies.
    Zero floating-point rounding errors via Decimal precision.
    """

    @staticmethod
    def pip_value_in_quote(pair: CurrencyPair, lot_size: LotSize) -> Decimal:
        """
        Calculates the monetary value of 1 pip in the quote currency of the pair.
        Formula: units * pip_size
        """
        return Decimal(str(lot_size.units)) * pair.pip_size

    @staticmethod
    def pip_value(
        pair: CurrencyPair,
        lot_size: LotSize,
        quote_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculates the monetary value of 1 pip converted into the account currency.
        `quote_to_account_rate` is the exchange rate to convert 1 unit of quote currency to account currency.
        - If quote currency == account currency (e.g. EUR/USD in USD account): rate = 1.0.
        - If base currency == account currency (e.g. USD/JPY in USD account): rate = 1 / USDJPY price.
        - If cross pair (e.g. EUR/GBP in USD account): rate = GBP/USD price.
        """
        quote_val = PipCalculator.pip_value_in_quote(pair, lot_size)
        rate = to_decimal(quote_to_account_rate)
        return quote_val * rate

    @staticmethod
    def price_diff_in_pips(
        side: OrderSide,
        entry_price: Decimal | float | str,
        exit_price: Decimal | float | str,
        pair: CurrencyPair,
    ) -> Decimal:
        """
        Calculate price movement in pips, respecting order side (BUY vs SELL).
        """
        entry = to_decimal(entry_price)
        exit_ = to_decimal(exit_price)
        if side == OrderSide.BUY:
            diff = exit_ - entry
        else:
            diff = entry - exit_
        return pair.to_pips(diff)

    @staticmethod
    def calculate_pnl(
        side: OrderSide,
        entry_price: Decimal | float | str,
        exit_price: Decimal | float | str,
        lot_size: LotSize,
        pair: CurrencyPair,
        quote_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculates exact monetary PnL in account currency without intermediate float loss.
        """
        entry = to_decimal(entry_price)
        exit_ = to_decimal(exit_price)
        rate = to_decimal(quote_to_account_rate)
        units = Decimal(str(lot_size.units))

        if side == OrderSide.BUY:
            price_change = exit_ - entry
        else:
            price_change = entry - exit_

        # PnL in quote currency = units * price_change
        pnl_quote = units * price_change
        # PnL in account currency = pnl_quote * quote_to_account_rate
        return pnl_quote * rate


class OrderIntent(BaseModel):
    """
    Declaration of order placement intention prior to risk checks and broker routing.
    """
    model_config = ConfigDict(frozen=True)

    intent_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    lot_size: LotSize
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    urgency: UrgencyLevel = UrgencyLevel.MEDIUM
    timestamp: datetime
    client_tag: str | None = None

    @field_validator("limit_price", "stop_loss", "take_profit", mode="before")
    @classmethod
    def convert_decimal(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return to_decimal(v)


class ExecutionOrder(BaseModel):
    """
    Active execution order managed by the execution state machine.
    """
    order_id: str
    intent_id: str | None = None
    symbol: str
    side: OrderSide
    order_type: OrderType
    lot_size: LotSize
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime
    updated_at: datetime
    filled_units: int = 0
    remaining_units: int = 0
    average_fill_price: Decimal | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.remaining_units == 0 and self.filled_units == 0:
            self.remaining_units = self.lot_size.units

    @field_validator("limit_price", "stop_loss", "take_profit", "average_fill_price", mode="before")
    @classmethod
    def convert_decimal(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        return to_decimal(v)


class Fill(BaseModel):
    """
    Execution fill record documenting a matched trade transaction.
    """
    model_config = ConfigDict(frozen=True)

    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    fill_price: Decimal
    units: int
    commission: Decimal = Decimal("0.0")
    timestamp: datetime
    liquidity_flag: str = "TAKER"  # "MAKER" or "TAKER"

    @field_validator("fill_price", "commission", mode="before")
    @classmethod
    def convert_decimal(cls, v: Any) -> Decimal:
        return to_decimal(v)


class Position(BaseModel):
    """
    State of an open/closed position with strict PnL tracking and fee accounting.
    """
    position_id: str
    symbol: str
    side: OrderSide
    units: int
    average_entry_price: Decimal
    current_price: Decimal
    realized_pnl: Decimal = Decimal("0.0")
    unrealized_pnl: Decimal = Decimal("0.0")
    total_commission: Decimal = Decimal("0.0")
    total_swap: Decimal = Decimal("0.0")
    opened_at: datetime
    updated_at: datetime
    is_open: bool = True

    @field_validator(
        "average_entry_price",
        "current_price",
        "realized_pnl",
        "unrealized_pnl",
        "total_commission",
        "total_swap",
        mode="before",
    )
    @classmethod
    def convert_decimal(cls, v: Any) -> Decimal:
        return to_decimal(v)

    def update_market_price(
        self,
        new_price: Decimal | float | str,
        quote_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> None:
        """Update unrealized PnL based on new market price."""
        self.current_price = to_decimal(new_price)
        rate = to_decimal(quote_to_account_rate)
        units_dec = Decimal(str(self.units))

        if self.side == OrderSide.BUY:
            price_diff = self.current_price - self.average_entry_price
        else:
            price_diff = self.average_entry_price - self.current_price

        self.unrealized_pnl = units_dec * price_diff * rate

    def apply_fill(self, fill: Fill) -> None:
        """Apply an additional execution fill to this position."""
        if fill.symbol != self.symbol:
            raise ValueError(f"Fill symbol {fill.symbol} does not match position symbol {self.symbol}")

        self.total_commission += fill.commission

        if fill.side == self.side:
            # Increasing position size: update weighted average entry price
            current_units = Decimal(str(self.units))
            fill_units = Decimal(str(fill.units))
            total_units = current_units + fill_units

            total_cost = (current_units * self.average_entry_price) + (fill_units * fill.fill_price)
            self.average_entry_price = total_cost / total_units
            self.units = int(total_units)
        else:
            # Reducing or closing position
            closed_units = min(self.units, fill.units)
            closed_units_dec = Decimal(str(closed_units))

            if self.side == OrderSide.BUY:
                trade_diff = fill.fill_price - self.average_entry_price
            else:
                trade_diff = self.average_entry_price - fill.fill_price

            realized = closed_units_dec * trade_diff
            self.realized_pnl += realized

            remaining = self.units - closed_units
            self.units = remaining
            if remaining == 0:
                self.is_open = False
                self.unrealized_pnl = Decimal("0.0")

    def apply_swap(self, swap_amount: Decimal | float | str) -> None:
        """Apply financing/swap debit or credit to position."""
        self.total_swap += to_decimal(swap_amount)

    def close(
        self,
        closing_price: Decimal | float | str,
        quote_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> Decimal:
        """Close position at given price and return final realized trade PnL."""
        price = to_decimal(closing_price)
        rate = to_decimal(quote_to_account_rate)
        units_dec = Decimal(str(self.units))

        if self.side == OrderSide.BUY:
            diff = price - self.average_entry_price
        else:
            diff = self.average_entry_price - price

        closing_pnl = units_dec * diff * rate
        self.realized_pnl += closing_pnl
        self.unrealized_pnl = Decimal("0.0")
        self.units = 0
        self.is_open = False
        return closing_pnl
