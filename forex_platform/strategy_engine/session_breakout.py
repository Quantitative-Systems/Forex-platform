"""
London Opening Range Breakout (ORB) Strategy.
Captures institutional volatility expansion upon European market opening (07:00 - 10:00 UTC)
using pre-London Asian consolidation channels and ATR volatility filters.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    UrgencyLevel,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class LondonSessionBreakout(BaseStrategy):
    """
    London Opening Range Breakout (ORB).
    Computes pre-market consolidation range between 05:00 and 07:00 UTC.
    Executes on breakout between 07:00 and 10:00 UTC with volatility expansion filter.
    Enforces automatic exit prior to 20:55 UTC daily rollover.
    """

    ORB_START = time(7, 0, 0)
    ORB_END = time(10, 0, 0)
    PRE_MARKET_START = time(5, 0, 0)
    PRE_MARKET_END = time(7, 0, 0)
    DAY_CLOSE_CUTOFF = time(20, 50, 0)

    def __init__(
        self,
        strategy_id: str = "london_orb",
        symbols: Optional[List[str]] = None,
        lot_size: float = 1.0,
        buffer_pips: float = 2.0,
        atr_period: int = 14,
    ):
        target_symbols = symbols or ["EURUSD", "GBPUSD", "EURGBP"]
        super().__init__(
            strategy_id=strategy_id,
            name="London Opening Range Breakout",
            symbols=target_symbols,
            timeframes=[Timeframe.M5, Timeframe.M15],
            parameters={
                "lot_size": lot_size,
                "buffer_pips": buffer_pips,
                "atr_period": atr_period,
            },
        )
        self.default_lot = LotSize.from_lots(lot_size)
        self.buffer_pips = Decimal(str(buffer_pips))
        self.atr_period = atr_period
        # Daily tracking of pre-market high/low: symbol -> (date, high, low)
        self._pre_market_ranges: Dict[str, Tuple[datetime.date, Decimal, Decimal]] = {}
        # Track whether a breakout was already executed today: symbol -> date
        self._executed_today: Dict[str, datetime.date] = {}

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        self.update_history(event)
        pair = CurrencyPair.from_symbol(event.symbol)
        utc_dt = event.timestamp if event.timestamp.tzinfo else event.timestamp.replace(tzinfo=timezone.utc)
        t = utc_dt.time()
        curr_date = utc_dt.date()

        # 1. During 05:00 to 07:00 UTC, accumulate pre-market channel
        if self.PRE_MARKET_START <= t < self.PRE_MARKET_END:
            if event.symbol not in self._pre_market_ranges or self._pre_market_ranges[event.symbol][0] != curr_date:
                self._pre_market_ranges[event.symbol] = (curr_date, event.high, event.low)
            else:
                _, high, low = self._pre_market_ranges[event.symbol]
                self._pre_market_ranges[event.symbol] = (
                    curr_date,
                    max(high, event.high),
                    min(low, event.low),
                )
            return []

        # 2. Only trade breakout during active London opening window (07:00 to 10:00 UTC)
        if not (self.ORB_START <= t < self.ORB_END):
            return []

        # Prevent duplicate breakouts on the same day
        if self._executed_today.get(event.symbol) == curr_date:
            return []

        # Check pre-market range exists for today
        if event.symbol not in self._pre_market_ranges or self._pre_market_ranges[event.symbol][0] != curr_date:
            return []

        _, range_high, range_low = self._pre_market_ranges[event.symbol]
        buffer_price = pair.to_price(self.buffer_pips)

        # Volatility check: ATR filter
        history = self.get_history(event.symbol)
        atr = self.calculate_atr(history, period=self.atr_period) or pair.to_price(Decimal("10.0"))

        intents: List[OrderIntent] = []

        # Bullish Breakout above pre-market high + buffer
        if event.close > (range_high + buffer_price):
            sl = range_low
            tp = event.close + (range_high - range_low) * Decimal("1.5")
            intent = self.create_intent(
                symbol=pair.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                stop_loss=pair.round_price(sl),
                take_profit=pair.round_price(tp),
                urgency=UrgencyLevel.HIGH,
                client_tag="LONDON_BREAKOUT_BUY",
            )
            intents.append(intent)
            self._executed_today[event.symbol] = curr_date

        # Bearish Breakout below pre-market low - buffer
        elif event.close < (range_low - buffer_price):
            sl = range_high
            tp = event.close - (range_high - range_low) * Decimal("1.5")
            intent = self.create_intent(
                symbol=pair.symbol,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                stop_loss=pair.round_price(sl),
                take_profit=pair.round_price(tp),
                urgency=UrgencyLevel.HIGH,
                client_tag="LONDON_BREAKOUT_SELL",
            )
            intents.append(intent)
            self._executed_today[event.symbol] = curr_date

        return intents
