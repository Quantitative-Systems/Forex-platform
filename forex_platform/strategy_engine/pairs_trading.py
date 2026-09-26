"""
Pairs Trading Strategy.
Identifies and trades cointegrated currency pairs using z-score mean reversion.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    UrgencyLevel,
    to_decimal,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class PairsTradingStrategy(BaseStrategy):
    """
    Pairs Trading Strategy.
    Trades the spread between two cointegrated currency pairs.
    Enters long-short positions when the spread deviates from its mean.
    """

    def __init__(
        self,
        strategy_id: str = "pairs_trading",
        base_pair: str = "EUR/USD",
        quote_pair: str = "GBP/USD",
        lookback_period: int = 50,
        z_entry: float = 2.0,
        z_exit: float = 0.5,
        lot_size: float = 0.5,
    ):
        symbols = [base_pair, quote_pair]
        super().__init__(
            strategy_id=strategy_id,
            name="Pairs Trading Strategy",
            symbols=symbols,
            timeframes=[Timeframe.M15, Timeframe.H1, Timeframe.D1],
            parameters={
                "base_pair": base_pair,
                "quote_pair": quote_pair,
                "lookback_period": lookback_period,
                "z_entry": z_entry,
                "z_exit": z_exit,
                "lot_size": lot_size,
            },
        )
        self.base_sym = base_pair.upper().replace("/", "").replace("_", "")
        self.quote_sym = quote_pair.upper().replace("/", "").replace("_", "")
        self.lookback_period = lookback_period
        self.z_entry = Decimal(str(z_entry))
        self.z_exit = Decimal(str(z_exit))
        self.default_lot = LotSize.from_lots(lot_size)
        
        # Spread history
        self._spread_history: List[Decimal] = []
        # Latest prices
        self._latest_prices: Dict[str, Decimal] = {}

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        self.update_history(event)
        clean_sym = event.symbol.upper().replace("/", "").replace("_", "")
        self._latest_prices[clean_sym] = event.close

        # Need both prices
        if self.base_sym not in self._latest_prices or self.quote_sym not in self._latest_prices:
            return []

        base_price = self._latest_prices[self.base_sym]
        quote_price = self._latest_prices[self.quote_sym]

        if base_price <= Decimal("0") or quote_price <= Decimal("0"):
            return []

        # Calculate spread (log ratio for stationarity)
        # Spread = ln(base) - ln(quote)
        import math
        spread = Decimal(str(math.log(float(base_price)) - math.log(float(quote_price))))
        self._spread_history.append(spread)
        
        # Keep only lookback period
        if len(self._spread_history) > self.lookback_period:
            self._spread_history.pop(0)

        # Need enough history
        if len(self._spread_history) < self.lookback_period:
            return []

        # Calculate z-score of current spread
        window = self._spread_history[-self.lookback_period:]
        mean = sum(window, Decimal("0")) / Decimal(str(len(window)))
        variance = sum((x - mean) ** 2 for x in window) / Decimal(str(len(window)))
        if variance <= Decimal("0"):
            return []
        std = Decimal(str(float(variance) ** 0.5))
        if std == Decimal("0"):
            return []
            
        z_score = (spread - mean) / std

        intents: List[OrderIntent] = []
        
        # Entry signals
        if z_score >= self.z_entry:
            # Spread is high: short base, long quote
            intent_short_base = self.create_intent(
                symbol=self.base_sym,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag=f"PAIRS_SHORT_BASE_{z_score:.2f}",
            )
            intent_long_quote = self.create_intent(
                symbol=self.quote_sym,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag=f"PAIRS_LONG_QUOTE_{z_score:.2f}",
            )
            intents.extend([intent_short_base, intent_long_quote])
            
        elif z_score <= -self.z_entry:
            # Spread is low: long base, short quote
            intent_long_base = self.create_intent(
                symbol=self.base_sym,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag=f"PAIRS_LONG_BASE_{z_score:.2f}",
            )
            intent_short_quote = self.create_intent(
                symbol=self.quote_sym,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag=f"PAIRS_SHORT_QUOTE_{z_score:.2f}",
            )
            intents.extend([intent_long_base, intent_short_quote])
            
        # Exit signals (optional: could also close on opposite signal)
        # For simplicity, we'll exit when z-score crosses zero or reaches exit threshold
        elif abs(z_score) <= self.z_exit:
            # Close any existing positions - in a real system you'd track position
            # For now, we'll just return empty (no new signals)
            pass

        return intents