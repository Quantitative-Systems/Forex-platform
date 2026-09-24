"""
Raw ECN commission calculation engine.
Standard institutional pricing model: configurable roundturn per 100,000 base units ($7.00 default).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

from forex_platform.core.domain import LotSize, to_decimal


class CommissionModel:
    """
    Computes broker execution fees under a raw ECN commission structure.
    Default: $7.00 USD roundturn per standard lot (100,000 units), equivalent to $3.50 per side.
    """

    DEFAULT_ROUNDTURN_PER_100K = Decimal("7.00")
    STANDARD_LOT_UNITS = Decimal("100000")

    def __init__(
        self,
        roundturn_per_standard_lot: Decimal | float | str = DEFAULT_ROUNDTURN_PER_100K,
    ):
        self.roundturn_per_standard_lot = to_decimal(roundturn_per_standard_lot)
        self.half_turn_per_standard_lot = self.roundturn_per_standard_lot / Decimal("2")

    def calculate_commission(
        self,
        volume: Union[int, LotSize],
        is_roundturn: bool = False,
        commission_to_account_rate: Decimal | float | str = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculate commission for a given trade volume in units or LotSize.
        `is_roundturn=True` charges the full open + close cycle upfront.
        `is_roundturn=False` charges single-side execution (per fill).
        """
        if isinstance(volume, LotSize):
            units = Decimal(str(volume.units))
        else:
            units = Decimal(str(volume))

        if units <= Decimal("0"):
            return Decimal("0.00")

        rate_per_lot = (
            self.roundturn_per_standard_lot if is_roundturn else self.half_turn_per_standard_lot
        )
        base_commission = (units / self.STANDARD_LOT_UNITS) * rate_per_lot
        conv_rate = to_decimal(commission_to_account_rate)

        # Monetary amount in account currency rounded to 4 decimal places (fractional cent)
        return (base_commission * conv_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
