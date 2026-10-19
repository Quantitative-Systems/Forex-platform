"""
Expanded Forex Instrument Universe — Majors + Minors.

Supports the user's requirement to trade "all major and minor pairs."
Each pair defines pip size, precision, and standard lot units.
"""

from __future__ import annotations

from decimal import Decimal


@dataclass(frozen=True)
class ForexInstrument:
    symbol: str
    base: str
    quote: str
    pip_size: Decimal
    price_precision: int
    is_jpy_cross: bool = False
    standard_lot_units: int = 100_000


# Majors (highest liquidity, tightest spreads)
MAJORS: dict[str, ForexInstrument] = {
    "EURUSD": ForexInstrument("EURUSD", "EUR", "USD", Decimal("0.0001"), 5),
    "GBPUSD": ForexInstrument("GBPUSD", "GBP", "USD", Decimal("0.0001"), 5),
    "USDJPY": ForexInstrument("USDJPY", "USD", "JPY", Decimal("0.01"), 3, is_jpy_cross=True),
    "AUDUSD": ForexInstrument("AUDUSD", "AUD", "USD", Decimal("0.0001"), 5),
    "USDCAD": ForexInstrument("USDCAD", "USD", "CAD", Decimal("0.0001"), 5),
    "USDCHF": ForexInstrument("USDCHF", "USD", "CHF", Decimal("0.0001"), 5),
    "NZDUSD": ForexInstrument("NZDUSD", "NZD", "USD", Decimal("0.0001"), 5),
}

# Minors / Crosses (liquid but slightly wider spreads)
MINORS: dict[str, ForexInstrument] = {
    "EURGBP": ForexInstrument("EURGBP", "EUR", "GBP", Decimal("0.0001"), 5),
    "EURJPY": ForexInstrument("EURJPY", "EUR", "JPY", Decimal("0.01"), 3, is_jpy_cross=True),
    "EURAUD": ForexInstrument("EURAUD", "EUR", "AUD", Decimal("0.0001"), 5),
    "EURNZD": ForexInstrument("EURNZD", "EUR", "NZD", Decimal("0.0001"), 5),
    "EURCAD": ForexInstrument("EURCAD", "EUR", "CAD", Decimal("0.0001"), 5),
    "EURCHF": ForexInstrument("EURCHF", "EUR", "CHF", Decimal("0.0001"), 5),
    "GBPJPY": ForexInstrument("GBPJPY", "GBP", "JPY", Decimal("0.01"), 3, is_jpy_cross=True),
    "GBPAUD": ForexInstrument("GBPAUD", "GBP", "AUD", Decimal("0.0001"), 5),
    "GBPNZD": ForexInstrument("GBPNZD", "GBP", "NZD", Decimal("0.0001"), 5),
    "GBPCAD": ForexInstrument("GBPCAD", "GBP", "CAD", Decimal("0.0001"), 5),
    "GBPCHF": ForexInstrument("GBPCHF", "GBP", "CHF", Decimal("0.0001"), 5),
    "AUDJPY": ForexInstrument("AUDJPY", "AUD", "JPY", Decimal("0.01"), 3, is_jpy_cross=True),
    "AUDNZD": ForexInstrument("AUDNZD", "AUD", "NZD", Decimal("0.0001"), 5),
    "AUDCAD": ForexInstrument("AUDCAD", "AUD", "CAD", Decimal("0.0001"), 5),
    "AUDCHF": ForexInstrument("AUDCHF", "AUD", "CHF", Decimal("0.0001"), 5),
    "NZDJPY": ForexInstrument("NZDJPY", "NZD", "JPY", Decimal("0.01"), 3, is_jpy_cross=True),
    "CADJPY": ForexInstrument("CADJPY", "CAD", "JPY", Decimal("0.01"), 3, is_jpy_cross=True),
    "CHFJPY": ForexInstrument("CHFJPY", "CHF", "JPY", Decimal("0.01"), 3, is_jpy_cross=True),
}

ALL_PAIRS: dict[str, ForexInstrument] = {**MAJORS, **MINORS}

MAJOR_SYMBOLS: list[str] = sorted(MAJORS.keys())
MINOR_SYMBOLS: list[str] = sorted(MINORS.keys())
ALL_SYMBOLS: list[str] = sorted(ALL_PAIRS.keys())
