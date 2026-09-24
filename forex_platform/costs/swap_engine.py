"""
Daily swap financing calculation engine.
Accurately models overnight interest rate differentials (carry cost/credit)
including Wednesday triple-swap settlement for T+2 FX spot transactions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Union

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderSide,
    PipCalculator,
    Position,
    to_decimal,
)
from forex_platform.core.sessions import ForexSessionEngine


class SwapEngine:
    """
    Engine for evaluating overnight interest and financing charges (swaps).
    Rollover occurs daily at 21:00 UTC.
    Wednesday rollover incurs a 3x multiplier to account for weekend settlement.
    """

    @classmethod
    def get_swap_multiplier(cls, rollover_dt: datetime) -> int:
        """
        Determines the swap multiplier for the rollover event.
        Wednesday rollover charges 3 days; all other trading days charge 1 day.
        """
        # Wednesday is weekday 2
        utc_dt = ForexSessionEngine._ensure_utc(rollover_dt)
        if utc_dt.weekday() == 2:
            return 3
        return 1

    @classmethod
    def calculate_swap(
        cls,
        pair: CurrencyPair,
        lot_size: LotSize,
        side: OrderSide,
        rollover_dt: datetime,
        long_swap_pips: Decimal | float | str,
        short_swap_pips: Decimal | float | str,
        quote_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculate the monetary swap debit (-) or credit (+) for holding an open position across rollover.

        Formula:
        swap_pips = (long_swap_pips if BUY else short_swap_pips) * multiplier
        pip_value = PipCalculator.pip_value(pair, lot_size, quote_to_account_rate)
        total_swap = swap_pips * pip_value
        """
        multiplier = Decimal(str(cls.get_swap_multiplier(rollover_dt)))

        if side == OrderSide.BUY:
            rate_pips = to_decimal(long_swap_pips)
        else:
            rate_pips = to_decimal(short_swap_pips)

        total_swap_pips = rate_pips * multiplier
        pip_val = PipCalculator.pip_value(pair, lot_size, quote_to_account_rate)

        swap_monetary = total_swap_pips * pip_val
        return swap_monetary.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @classmethod
    def apply_rollover_to_position(
        cls,
        position: Position,
        pair: CurrencyPair,
        rollover_dt: datetime,
        long_swap_pips: Decimal | float | str,
        short_swap_pips: Decimal | float | str,
        quote_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculates swap and directly updates position.total_swap.
        Returns the incremental swap amount applied.
        """
        if not position.is_open or position.units <= 0:
            return Decimal("0.0000")

        lot_size = LotSize.from_units(position.units)
        swap_amount = cls.calculate_swap(
            pair=pair,
            lot_size=lot_size,
            side=position.side,
            rollover_dt=rollover_dt,
            long_swap_pips=long_swap_pips,
            short_swap_pips=short_swap_pips,
            quote_to_account_rate=quote_to_account_rate,
        )
        position.apply_swap(swap_amount)
        return swap_amount
