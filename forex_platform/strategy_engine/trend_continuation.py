"""
Multi-Timeframe Dual EMA Trend Continuation Strategy.
Combines H1 macro trend regime filter with M15 pullback entry continuation,
governed by the Kaufman Efficiency Ratio (KER) noise filter.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

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


class TrendContinuationStrategy(BaseStrategy):
    """
    Multi-Timeframe Trend Continuation Strategy.
    - Macro Trend: H1 Dual EMA (Fast EMA 20 vs Slow EMA 50) passed via htf_data.
    - Micro Filter: M15 Kaufman Efficiency Ratio (KER > 0.40) to weed out sideways chop.
    - Execution: Pullback towards M15 EMA 20 inside the dominant macro trend.
    """

    def __init__(
        self,
        strategy_id: str = "trend_continuation",
        symbols: Optional[List[str]] = None,
        fast_period: int = 20,
        slow_period: int = 50,
        ker_period: int = 10,
        ker_threshold: float = 0.35,
        lot_size: float = 1.0,
    ):
        target_symbols = symbols or ["EURUSD", "GBPUSD", "USDJPY"]
        super().__init__(
            strategy_id=strategy_id,
            name="Multi-Timeframe Dual EMA Trend Continuation",
            symbols=target_symbols,
            timeframes=[Timeframe.M15, Timeframe.H1],
            parameters={
                "fast_period": fast_period,
                "slow_period": slow_period,
                "ker_period": ker_period,
                "ker_threshold": ker_threshold,
                "lot_size": lot_size,
            },
        )
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.ker_period = ker_period
        self.ker_threshold = Decimal(str(ker_threshold))
        self.default_lot = LotSize.from_lots(lot_size)

    def calculate_ker(self, closes: List[Decimal], period: int) -> Optional[Decimal]:
        """
        Kaufman Efficiency Ratio = Net Change / Total Path Traveled.
        Values near 1.0 indicate strong directional trend; near 0.0 indicate choppy noise.
        """
        if len(closes) < period + 1:
            return None
        net_change = abs(closes[-1] - closes[-1 - period])
        total_path = sum(abs(closes[-i] - closes[-i - 1]) for i in range(1, period + 1))
        if total_path == Decimal("0.0"):
            return Decimal("0.0")
        return net_change / total_path

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        self.update_history(event)
        history = self.get_history(event.symbol)

        if len(history) < self.slow_period + self.ker_period:
            return []

        closes = [b.close for b in history]

        # 1. Kaufman Efficiency Ratio on base timeframe (M15)
        ker = self.calculate_ker(closes, self.ker_period)
        if ker is None or ker < self.ker_threshold:
            return []

        # 2. Base timeframe EMAs
        fast_ema = self.calculate_ema(closes, self.fast_period)
        slow_ema = self.calculate_ema(closes, self.slow_period)
        if fast_ema is None or slow_ema is None:
            return []

        # 3. Macro H1 trend confirmation (from causally aligned htf_data if present, or slow EMA)
        htf_trend = event.htf_data.get("h1_trend")
        if htf_trend == "UP" or (htf_trend is None and fast_ema > slow_ema):
            is_uptrend = True
            is_downtrend = False
        elif htf_trend == "DOWN" or (htf_trend is None and fast_ema < slow_ema):
            is_uptrend = False
            is_downtrend = True
        else:
            return []

        pair = CurrencyPair.from_symbol(event.symbol)
        atr = self.calculate_atr(history, period=14) or pair.to_price(Decimal("15.0"))

        intents: List[OrderIntent] = []
        prev_bar = history[-2]

        # Bullish Pullback Continuation:
        # Previous bar touched or dipped near fast EMA, current bar closed back above fast EMA
        if is_uptrend and prev_bar.low <= fast_ema and event.close > fast_ema:
            sl = event.close - (atr * Decimal("1.5"))
            tp = event.close + (atr * Decimal("2.5"))
            intent = self.create_intent(
                symbol=pair.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                stop_loss=pair.round_price(sl),
                take_profit=pair.round_price(tp),
                urgency=UrgencyLevel.MEDIUM,
                client_tag="TREND_PULLBACK_BUY",
            )
            intents.append(intent)

        # Bearish Pullback Continuation:
        elif is_downtrend and prev_bar.high >= fast_ema and event.close < fast_ema:
            sl = event.close + (atr * Decimal("1.5"))
            tp = event.close - (atr * Decimal("2.5"))
            intent = self.create_intent(
                symbol=pair.symbol,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                stop_loss=pair.round_price(sl),
                take_profit=pair.round_price(tp),
                urgency=UrgencyLevel.MEDIUM,
                client_tag="TREND_PULLBACK_SELL",
            )
            intents.append(intent)

        return intents
