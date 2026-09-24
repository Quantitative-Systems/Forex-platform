"""
Macroeconomic Interest Rate Differential Carry Engine.
Captures structural overnight swap credits while dynamically hedging sovereign
and liquidity tail risk using volatility expansion circuit breakers.
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


class MacroCarryStrategy(BaseStrategy):
    """
    Macro Carry Engine.
    Enters long/short carry positions on pairs with net positive daily swaps
    (e.g., Long USD/JPY or Long USD/CHF when Fed funds rate > BOJ/SNB policy rates).
    Guards against carry trade unwind panic via rolling ATR tail-risk detector.
    """

    def __init__(
        self,
        strategy_id: str = "macro_carry",
        symbols: Optional[List[str]] = None,
        pair_swap_profiles: Optional[Dict[str, Dict[str, float]]] = None,
        vol_shock_threshold: float = 2.0,
        lot_size: float = 1.0,
    ):
        target_symbols = symbols or ["USDJPY", "EURUSD", "AUDJPY"]
        super().__init__(
            strategy_id=strategy_id,
            name="Macroeconomic Interest Rate Differential Carry",
            symbols=target_symbols,
            timeframes=[Timeframe.H1, Timeframe.D1],
            parameters={
                "vol_shock_threshold": vol_shock_threshold,
                "lot_size": lot_size,
            },
        )
        self.default_lot = LotSize.from_lots(lot_size)
        self.vol_shock_threshold = Decimal(str(vol_shock_threshold))

        # Expected swap profiles in pips/day: symbol -> {"LONG": swap_pips, "SHORT": swap_pips}
        self.swap_profiles: Dict[str, Dict[str, Decimal]] = {}
        defaults = {
            "USDJPY": {"LONG": Decimal("1.2"), "SHORT": Decimal("-2.5")},   # Positive Long carry
            "AUDJPY": {"LONG": Decimal("0.8"), "SHORT": Decimal("-1.8")},   # Positive Long carry
            "EURUSD": {"LONG": Decimal("-0.8"), "SHORT": Decimal("0.3")},   # Positive Short carry
        }
        if pair_swap_profiles:
            for s, prof in pair_swap_profiles.items():
                clean = s.upper().replace("/", "").replace("_", "")
                self.swap_profiles[clean] = {
                    "LONG": to_decimal(prof.get("LONG", 0.0)),
                    "SHORT": to_decimal(prof.get("SHORT", 0.0)),
                }
        else:
            self.swap_profiles = defaults

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        self.update_history(event)
        history = self.get_history(event.symbol)

        clean_sym = event.symbol.upper().replace("/", "").replace("_", "")
        if clean_sym not in self.swap_profiles:
            return []

        profile = self.swap_profiles[clean_sym]
        long_swap = profile.get("LONG", Decimal("0.0"))
        short_swap = profile.get("SHORT", Decimal("0.0"))

        # Determine carry direction
        if long_swap > Decimal("0.5"):
            desired_side = OrderSide.BUY
        elif short_swap > Decimal("0.5"):
            desired_side = OrderSide.SELL
        else:
            return []  # Insufficient carry yield

        # Volatility Tail-Risk Check:
        # Detect rapid volatility shocks (e.g. sudden Yen carry unwind)
        if len(history) >= 20:
            prev_close = history[-2].close if len(history) >= 2 else event.open
            curr_tr = max(
                event.high - event.low,
                abs(event.high - prev_close),
                abs(event.low - prev_close),
            )
            long_atr = self.calculate_atr(history, period=min(20, len(history) - 1))
            if long_atr and long_atr > Decimal("0.0"):
                if curr_tr > (long_atr * self.vol_shock_threshold):
                    # Tail-risk shock active: inhibit carry entry to prevent catching a falling knife
                    return []

        pair = CurrencyPair.from_symbol(event.symbol)
        atr = self.calculate_atr(history, period=14) or pair.to_price(Decimal("30.0"))

        # Wide stop loss suitable for multi-day carry holding
        if desired_side == OrderSide.BUY:
            sl = event.close - (atr * Decimal("3.0"))
            tp = event.close + (atr * Decimal("4.0"))
        else:
            sl = event.close + (atr * Decimal("3.0"))
            tp = event.close - (atr * Decimal("4.0"))

        intent = self.create_intent(
            symbol=pair.symbol,
            side=desired_side,
            order_type=OrderType.MARKET,
            lot_size=self.default_lot,
            timestamp=event.timestamp,
            stop_loss=pair.round_price(sl),
            take_profit=pair.round_price(tp),
            urgency=UrgencyLevel.LOW,
            client_tag="MACRO_CARRY_ROLLOVER",
        )
        return [intent]
