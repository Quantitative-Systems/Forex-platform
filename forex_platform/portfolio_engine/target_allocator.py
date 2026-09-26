"""Target-position allocation and defensive currency hedge intents."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional

from forex_platform.core.domain import (
    CurrencyPair, LotSize, OrderIntent, OrderSide, OrderType, to_decimal,
)
from forex_platform.core.instruments import FXInstrumentRegistry
from forex_platform.portfolio_engine.regime import MarketRegime


@dataclass(frozen=True)
class TargetPosition:
    symbol: str
    target_units: int
    strategy_id: str = "target"
    confidence: float = 1.0


@dataclass(frozen=True)
class AllocationDecision:
    approved: bool
    intents: List[OrderIntent]
    rejected: List[str]
    regime: MarketRegime
    gross_units: int
    reason: str


class TargetPositionAllocator:
    """Translate strategy target positions into bounded order intents.

    Allocation is fail-closed: unknown instruments, unknown regimes, and
    oversized targets are rejected before the OMS/risk path.
    """

    def __init__(
        self,
        max_gross_units: int = 1_000_000,
        max_symbol_units: int = 500_000,
        max_currency_delta_units: int = 750_000,
        allowed_regimes: Optional[Iterable[MarketRegime]] = None,
    ) -> None:
        self.max_gross_units = max_gross_units
        self.max_symbol_units = max_symbol_units
        self.max_currency_delta_units = max_currency_delta_units
        self.allowed_regimes = set(allowed_regimes or {
            MarketRegime.TRENDING, MarketRegime.RANGING,
            MarketRegime.HIGH_VOLATILITY, MarketRegime.LOW_VOLATILITY,
        })

    def allocate(
        self,
        targets: Iterable[TargetPosition],
        current_units: Optional[Mapping[str, int]] = None,
        regime: MarketRegime = MarketRegime.UNKNOWN,
        timestamp: Optional[datetime] = None,
    ) -> AllocationDecision:
        rejected: List[str] = []
        if regime not in self.allowed_regimes:
            return AllocationDecision(False, [], ["regime"], regime, 0, "Regime is not tradable")
        current = {FXInstrumentRegistry.normalize(k): int(v) for k, v in (current_units or {}).items()}
        projected = dict(current)
        intents: List[OrderIntent] = []
        for target in targets:
            symbol = FXInstrumentRegistry.normalize(target.symbol)
            if not FXInstrumentRegistry.is_supported(symbol):
                rejected.append(f"unsupported:{target.symbol}")
                continue
            pair = CurrencyPair.from_symbol(symbol)
            delta = int(target.target_units) - projected.get(symbol, 0)
            if delta == 0:
                continue
            new_symbol_units = abs(projected.get(symbol, 0) + delta)
            if new_symbol_units > self.max_symbol_units:
                rejected.append(f"symbol_cap:{symbol}")
                continue
            gross = sum(abs(value) for value in projected.values()) + abs(delta)
            if gross > self.max_gross_units:
                rejected.append(f"gross_cap:{symbol}")
                continue
            base_delta = delta if pair.base_currency == "USD" else 0
            quote_delta = -delta if pair.quote_currency == "USD" else 0
            if abs(base_delta) > self.max_currency_delta_units or abs(quote_delta) > self.max_currency_delta_units:
                rejected.append(f"currency_cap:{symbol}")
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            intents.append(OrderIntent(
                intent_id=f"TARGET-{target.strategy_id}-{symbol}",
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                lot_size=LotSize.from_units(abs(delta)),
                timestamp=timestamp or datetime.now(timezone.utc),
                client_tag=f"TARGET:{target.strategy_id}",
            ))
            projected[symbol] = projected.get(symbol, 0) + delta
        gross = sum(abs(value) for value in projected.values())
        return AllocationDecision(not rejected, intents, rejected, regime, gross, "allocation complete")

    def hedge_currency_delta(
        self,
        currency: str,
        delta_units: int,
        hedge_currency: str = "USD",
        strategy_id: str = "hedge",
        timestamp: Optional[datetime] = None,
    ) -> Optional[OrderIntent]:
        """Build a USD hedge intent for a currency delta, when a pair exists."""
        code = currency.upper()
        hedge = hedge_currency.upper()
        if code == hedge or delta_units == 0:
            return None
        candidates = [
            symbol for symbol in FXInstrumentRegistry.symbols_for_currency(code)
            if hedge in (symbol[:3], symbol[3:])
        ]
        if not candidates:
            return None
        symbol = sorted(candidates, key=lambda s: (len(FXInstrumentRegistry.get(s).base_currency), s))[0]
        pair = CurrencyPair.from_symbol(symbol)
        if pair.base_currency == code:
            side = OrderSide.SELL if delta_units > 0 else OrderSide.BUY
        else:
            side = OrderSide.BUY if delta_units > 0 else OrderSide.SELL
        return OrderIntent(
            intent_id=f"HEDGE-{strategy_id}-{code}-{hedge}",
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_units(abs(int(delta_units))),
            timestamp=timestamp or __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            client_tag=f"HEDGE:{strategy_id}",
        )
