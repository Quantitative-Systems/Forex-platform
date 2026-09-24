"""
Triangular Statistical Arbitrage Strategy.
Exploits temporary micro-pricing discrepancies across triangular currency loops:
EUR/USD, GBP/USD, and EUR/GBP.
Trades the cointegrated synthetic residual with rolling z-score reversion.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import math
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


class TriangularStatisticalArbitrage(BaseStrategy):
    """
    Triangular Cointegration / Statistical Arbitrage Strategy.
    Synthetic Loop:
    - Base pair: EURUSD
    - Target pair: GBPUSD
    - Cross pair: EURGBP
    Parity: EURGBP * GBPUSD = EURUSD
    Residual = ln(EURUSD) - ln(GBPUSD) - ln(EURGBP)
    """

    def __init__(
        self,
        strategy_id: str = "triangular_stat_arb",
        base_pair: str = "EURUSD",
        target_pair: str = "GBPUSD",
        cross_pair: str = "EURGBP",
        lookback_period: int = 30,
        z_threshold: float = 2.0,
        lot_size: float = 0.5,
    ):
        symbols = [base_pair, target_pair, cross_pair]
        super().__init__(
            strategy_id=strategy_id,
            name="Triangular Statistical Arbitrage",
            symbols=symbols,
            timeframes=[Timeframe.M1, Timeframe.M5],
            parameters={
                "base_pair": base_pair,
                "target_pair": target_pair,
                "cross_pair": cross_pair,
                "lookback_period": lookback_period,
                "z_threshold": z_threshold,
                "lot_size": lot_size,
            },
        )
        self.base_sym = base_pair.upper().replace("/", "").replace("_", "")
        self.target_sym = target_pair.upper().replace("/", "").replace("_", "")
        self.cross_sym = cross_pair.upper().replace("/", "").replace("_", "")
        self.lookback_period = lookback_period
        self.z_threshold = Decimal(str(z_threshold))
        self.default_lot = LotSize.from_lots(lot_size)

        # Residual history buffer
        self._residual_history: List[Decimal] = []
        # Latest prices
        self._latest_prices: Dict[str, Decimal] = {}

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        self.update_history(event)
        clean_sym = event.symbol.upper().replace("/", "").replace("_", "")
        self._latest_prices[clean_sym] = event.close

        # Require all 3 triangular legs to have prices
        if not (self.base_sym in self._latest_prices and self.target_sym in self._latest_prices and self.cross_sym in self._latest_prices):
            return []

        p_base = float(self._latest_prices[self.base_sym])
        p_target = float(self._latest_prices[self.target_sym])
        p_cross = float(self._latest_prices[self.cross_sym])

        if p_base <= 0 or p_target <= 0 or p_cross <= 0:
            return []

        # Residual = ln(EURUSD) - ln(GBPUSD) - ln(EURGBP)
        residual = math.log(p_base) - math.log(p_target) - math.log(p_cross)
        dec_res = to_decimal(round(residual, 8))

        self._residual_history.append(dec_res)
        if len(self._residual_history) > 200:
            self._residual_history.pop(0)

        if len(self._residual_history) < self.lookback_period:
            return []

        window = self._residual_history[-self.lookback_period:]
        mean = sum(window, Decimal("0.0")) / Decimal(str(self.lookback_period))
        variance = sum((r - mean) ** 2 for r in window) / Decimal(str(self.lookback_period))
        std = Decimal(str(math.sqrt(float(variance))))

        if std == Decimal("0.0"):
            return []

        z_score = (dec_res - mean) / std

        intents: List[OrderIntent] = []
        # Upper breach: EURUSD overpriced relative to cross and target
        # Action: Sell EURUSD, Buy GBPUSD
        if z_score >= self.z_threshold:
            intent_sell_base = self.create_intent(
                symbol=self.base_sym,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag="STAT_ARB_SELL_BASE",
            )
            intent_buy_target = self.create_intent(
                symbol=self.target_sym,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag="STAT_ARB_BUY_TARGET",
            )
            intents.extend([intent_sell_base, intent_buy_target])

        # Lower breach: EURUSD underpriced relative to cross and target
        # Action: Buy EURUSD, Sell GBPUSD
        elif z_score <= -self.z_threshold:
            intent_buy_base = self.create_intent(
                symbol=self.base_sym,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag="STAT_ARB_BUY_BASE",
            )
            intent_sell_target = self.create_intent(
                symbol=self.target_sym,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                lot_size=self.default_lot,
                timestamp=event.timestamp,
                client_tag="STAT_ARB_SELL_TARGET",
            )
            intents.extend([intent_buy_base, intent_sell_target])

        return intents
