"""
Asian Session Microstructure Range Fade Scalping Strategy.
Operates strictly during low-volatility Asian hours (Tokyo/Sydney) using Bollinger Band z-scores.
Maker/ECN limit orders only.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import math
from typing import List, Optional

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    SessionName,
    UrgencyLevel,
    to_decimal,
)
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class AsianRangeFadeScalper(BaseStrategy):
    """
    Mean-reversion scalping strategy designed for the quiet Asian trading session.
    Fades Bollinger Band extremes (z-score > 2.0 or < -2.0) with passive limit orders.
    """

    def __init__(
        self,
        strategy_id: str = "asian_scalper",
        symbols: Optional[List[str]] = None,
        period: int = 20,
        z_threshold: float = 2.0,
        lot_size: float = 0.5,
        tp_pips: float = 6.0,
        sl_pips: float = 10.0,
    ):
        target_symbols = symbols or ["EURUSD", "USDJPY", "AUDUSD"]
        super().__init__(
            strategy_id=strategy_id,
            name="Asian Session Microstructure Range Fade",
            symbols=target_symbols,
            timeframes=[Timeframe.M5],
            parameters={
                "period": period,
                "z_threshold": z_threshold,
                "lot_size": lot_size,
                "tp_pips": tp_pips,
                "sl_pips": sl_pips,
            },
        )
        self.period = period
        self.z_threshold = Decimal(str(z_threshold))
        self.default_lot = LotSize.from_lots(lot_size)
        self.tp_pips = Decimal(str(tp_pips))
        self.sl_pips = Decimal(str(sl_pips))

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        self.update_history(event)
        history = self.get_history(event.symbol)

        # 1. Time / Session Gate: strictly Asian session hours (Sydney or Tokyo, NO London or NY)
        active_sessions = ForexSessionEngine.get_active_sessions(event.timestamp)
        is_asian = (
            (SessionName.TOKYO in active_sessions or SessionName.SYDNEY in active_sessions)
            and SessionName.LONDON not in active_sessions
            and SessionName.NEW_YORK not in active_sessions
        )
        if not is_asian:
            return []

        # 2. Check sufficient history
        if len(history) < self.period:
            return []

        closes = [b.close for b in history[-self.period:]]
        mean = sum(closes, Decimal("0.0")) / Decimal(str(self.period))

        variance = sum((c - mean) ** 2 for c in closes) / Decimal(str(self.period))
        std_dev = Decimal(str(math.sqrt(float(variance))))

        if std_dev == Decimal("0.0"):
            return []

        z_score = (event.close - mean) / std_dev
        pair = CurrencyPair.from_symbol(event.symbol)

        # 3. Microstructure fade signals
        intents: List[OrderIntent] = []
        if z_score <= -self.z_threshold:
            # Price pierced lower band -> BUY limit order at current close
            limit_price = event.close
            tp_price = limit_price + pair.to_price(self.tp_pips)
            sl_price = limit_price - pair.to_price(self.sl_pips)
            intent = self.create_intent(
                symbol=pair.symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                limit_price=pair.round_price(limit_price),
                stop_loss=pair.round_price(sl_price),
                take_profit=pair.round_price(tp_price),
                urgency=UrgencyLevel.LOW,
                client_tag="ASIAN_FADE_BUY",
            )
            intents.append(intent)

        elif z_score >= self.z_threshold:
            # Price pierced upper band -> SELL limit order at current close
            limit_price = event.close
            tp_price = limit_price - pair.to_price(self.tp_pips)
            sl_price = limit_price + pair.to_price(self.sl_pips)
            intent = self.create_intent(
                symbol=pair.symbol,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                limit_price=pair.round_price(limit_price),
                stop_loss=pair.round_price(sl_price),
                take_profit=pair.round_price(tp_price),
                urgency=UrgencyLevel.LOW,
                client_tag="ASIAN_FADE_SELL",
            )
            intents.append(intent)

        return intents
