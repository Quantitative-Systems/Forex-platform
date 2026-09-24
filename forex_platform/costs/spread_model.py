"""
Session-aware dynamic spread calculation engine.
Models real-world institutional Forex liquidity regimes:
- Baseline spread during peak London/New York overlap
- 1.2x - 1.5x in single Western session
- 3x - 5x during Asian / quiet sessions
- 10x - 20x during daily 20:55 - 21:15 UTC bank rollover
- Market closed during weekends
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Union

from forex_platform.core.domain import CurrencyPair, to_decimal
from forex_platform.core.sessions import ForexSessionEngine


class MarketClosedError(Exception):
    """Raised when querying market prices/spread during market closure."""
    pass


class DynamicSpreadModel:
    """
    Computes dynamic bid/ask spreads based on temporal liquidity regimes and pair specifics.
    """

    DEFAULT_BASE_SPREADS: Dict[str, Decimal] = {
        "EURUSD": Decimal("0.8"),
        "GBPUSD": Decimal("1.0"),
        "USDJPY": Decimal("0.9"),
        "AUDUSD": Decimal("1.0"),
        "USDCAD": Decimal("1.2"),
        "USDCHF": Decimal("1.2"),
        "EURGBP": Decimal("1.2"),
        "EURJPY": Decimal("1.4"),
        "GBPJPY": Decimal("1.8"),
        "NZDUSD": Decimal("1.4"),
    }
    FALLBACK_BASE_SPREAD = Decimal("1.5")

    # Multipliers for liquidity regimes
    MULTIPLIER_LONDON_NY_OVERLAP = Decimal("1.0")    # Baseline tightest spread
    MULTIPLIER_WESTERN_SOLO = Decimal("1.3")          # London or NY alone
    MULTIPLIER_ASIAN_SESSION = Decimal("3.5")         # Widened by 3x - 5x
    MULTIPLIER_QUIET_HOURS = Decimal("4.0")           # Off-peak hours
    MULTIPLIER_ROLLOVER = Decimal("15.0")             # Widened by 10x - 20x during 21:00 UTC rollover

    def __init__(
        self,
        base_spreads: Dict[str, Decimal] | None = None,
        asian_multiplier: Decimal | float = MULTIPLIER_ASIAN_SESSION,
        rollover_multiplier: Decimal | float = MULTIPLIER_ROLLOVER,
    ):
        self.base_spreads = dict(self.DEFAULT_BASE_SPREADS)
        if base_spreads:
            for k, v in base_spreads.items():
                self.base_spreads[k.upper().replace("/", "").replace("_", "")] = to_decimal(v)

        self.asian_multiplier = to_decimal(asian_multiplier)
        self.rollover_multiplier = to_decimal(rollover_multiplier)

    def get_base_spread(self, symbol_or_pair: Union[str, CurrencyPair]) -> Decimal:
        """Retrieve baseline spread in pips for a currency pair."""
        if isinstance(symbol_or_pair, CurrencyPair):
            sym = symbol_or_pair.symbol
        else:
            sym = symbol_or_pair.strip().upper().replace("/", "").replace("_", "").replace("-", "")

        return self.base_spreads.get(sym, self.FALLBACK_BASE_SPREAD)

    def get_spread_multiplier(self, dt: datetime) -> Decimal:
        """
        Determine spread multiplier according to the session state at dt (UTC).
        Raises MarketClosedError if market is closed (weekend).
        """
        if ForexSessionEngine.is_weekend(dt):
            raise MarketClosedError(f"Forex market is closed for weekend at {dt} UTC.")

        if ForexSessionEngine.is_rollover(dt):
            return self.rollover_multiplier

        session_state = ForexSessionEngine.get_session_state(dt)

        if session_state.regime_tag == "OVERLAP_LONDON_NY":
            return self.MULTIPLIER_LONDON_NY_OVERLAP
        elif session_state.regime_tag in ("LONDON_SOLO", "NEW_YORK_SOLO", "OVERLAP_TOKYO_LONDON"):
            return self.MULTIPLIER_WESTERN_SOLO
        elif session_state.regime_tag in ("ASIAN_SESSION", "OVERLAP_SYDNEY_TOKYO"):
            return self.asian_multiplier
        else:
            return self.MULTIPLIER_QUIET_HOURS

    def calculate_spread(
        self,
        symbol_or_pair: Union[str, CurrencyPair],
        dt: datetime,
        custom_base_spread: Decimal | float | str | None = None,
    ) -> Decimal:
        """
        Calculate dynamic spread in pips at datetime dt.
        """
        base = to_decimal(custom_base_spread) if custom_base_spread is not None else self.get_base_spread(symbol_or_pair)
        multiplier = self.get_spread_multiplier(dt)
        return (base * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def get_bid_ask(
        self,
        symbol_or_pair: Union[str, CurrencyPair],
        mid_price: Decimal | float | str,
        dt: datetime,
        custom_base_spread: Decimal | float | str | None = None,
    ) -> tuple[Decimal, Decimal]:
        """
        Calculate realistic bid and ask prices given mid price and datetime.
        Prices are quantized to the currency pair's exact price precision.
        """
        pair = (
            symbol_or_pair
            if isinstance(symbol_or_pair, CurrencyPair)
            else CurrencyPair.from_symbol(symbol_or_pair)
        )
        mid = to_decimal(mid_price)
        spread_pips = self.calculate_spread(pair, dt, custom_base_spread=custom_base_spread)
        spread_price = pair.to_price(spread_pips)
        half_spread = spread_price / Decimal("2")

        bid = pair.round_price(mid - half_spread)
        ask = pair.round_price(mid + half_spread)
        return bid, ask
