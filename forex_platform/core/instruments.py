"""Canonical spot-FX instrument registry.

The existing domain model accepts any six-character pair for convenience. This
module is the explicit tradable-universe boundary used by research, allocation,
data acquisition, and execution planning. It covers all 28 conventional spot
pairs formed by the eight G10 currencies currently modeled by the platform.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Tuple

from forex_platform.core.domain import CurrencyPair, to_decimal


CURRENCIES: Tuple[str, ...] = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD")


class LiquidityTier(str, Enum):
    MAJOR = "MAJOR"
    CROSS = "CROSS"


@dataclass(frozen=True)
class FXInstrumentSpec:
    symbol: str
    base_currency: str
    quote_currency: str
    liquidity_tier: LiquidityTier
    typical_spread_pips: float
    price_precision: int
    pip_decimal_places: int

    @property
    def pair(self) -> CurrencyPair:
        return CurrencyPair.from_symbol(
            self.symbol, price_precision=self.price_precision
        )

    @property
    def is_jpy_cross(self) -> bool:
        return self.quote_currency == "JPY" or self.base_currency == "JPY"

    def to_pips(self, price_difference: float | str) -> float:
        return float(self.pair.to_pips(price_difference))

    def to_price(self, pips: float | str) -> float:
        return float(self.pair.to_price(pips))


class FXInstrumentRegistry:
    """Immutable registry of the platform's conventional spot-FX universe."""

    USD_MAJORS: Tuple[str, ...] = (
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"
    )
    CROSSES: Tuple[str, ...] = (
        "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURNZD", "EURCAD",
        "GBPJPY", "GBPCHF", "GBPAUD", "GBPNZD", "GBPCAD",
        "AUDJPY", "AUDCHF", "AUDNZD", "AUDCAD",
        "NZDJPY", "NZDCHF", "NZDCAD",
        "CADJPY", "CADCHF", "CHFJPY",
    )
    SYMBOLS: Tuple[str, ...] = USD_MAJORS + CROSSES

    @classmethod
    def normalize(cls, symbol: str) -> str:
        return symbol.strip().upper().replace("/", "").replace("_", "").replace("-", "")

    @classmethod
    def _spec(cls, symbol: str) -> FXInstrumentSpec:
        clean = cls.normalize(symbol)
        if clean not in cls.SYMBOLS:
            raise ValueError(f"Unsupported spot-FX instrument: {symbol}")
        base, quote = clean[:3], clean[3:]
        jpy = quote == "JPY"
        price_precision = 3 if jpy else 5
        pip_places = 2 if jpy else 4
        typical = 0.9 if clean in cls.USD_MAJORS else 1.8
        if clean in {"EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"}:
            typical = 1.5
        return FXInstrumentSpec(
            symbol=clean,
            base_currency=base,
            quote_currency=quote,
            liquidity_tier=LiquidityTier.MAJOR if clean in cls.USD_MAJORS else LiquidityTier.CROSS,
            typical_spread_pips=typical,
            price_precision=price_precision,
            pip_decimal_places=pip_places,
        )

    @classmethod
    def get(cls, symbol: str) -> FXInstrumentSpec:
        return cls._spec(symbol)

    @classmethod
    def is_supported(cls, symbol: str) -> bool:
        return cls.normalize(symbol) in cls.SYMBOLS

    @classmethod
    def all_specs(cls) -> List[FXInstrumentSpec]:
        return [cls._spec(symbol) for symbol in cls.SYMBOLS]

    @classmethod
    def symbols_for_currency(cls, currency: str) -> List[str]:
        code = currency.strip().upper()
        return [spec.symbol for spec in cls.all_specs() if code in {spec.base_currency, spec.quote_currency}]

    @classmethod
    def summary(cls) -> Dict[str, object]:
        specs = cls.all_specs()
        return {
            "currencies": list(CURRENCIES),
            "total_pairs": len(specs),
            "majors": len(cls.USD_MAJORS),
            "crosses": len(cls.CROSSES),
            "symbols": list(cls.SYMBOLS),
        }
