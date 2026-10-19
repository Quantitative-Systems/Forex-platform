"""
SuperTrend-Stochastic Multi-Timeframe Strategy â€” Forex Implementation.

Implements the user's multi-timeframe fractal concept:
  HTF bias: Supertrend(ATR=6, Factor=5) | Stochastic(25,5,3) pullback to 25
  LTF entry on K/D crossover | 3-tier TP | SL = LTF Supertrend | Min 1:6 RR
  Cross-timeframe trailing | 1% risk per trade | 6 correlated sets
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from forex_platform.core.domain import (
    CurrencyPair, LotSize, OrderIntent, OrderSide, OrderType,
    UrgencyLevel, to_decimal,
)
from forex_platform.indicators.supertrend import Supertrend
from forex_platform.indicators.stochastic import Stochastic
from forex_platform.fractal_engine.timeframes import (
    TIMEFRAME_SETS, CanonicalTimeframe, normalized_set_key,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class TradeState(str, Enum):
    WAITING = "WAITING"
    SETUP = "SETUP"
    ACTIVE = "ACTIVE"
    TRAILING = "TRAILING"
    CLOSED = "CLOSED"


@dataclass
class SetConfig:
    set_key: str
    htf: CanonicalTimeframe
    mtf: CanonicalTimeframe
    ltf: CanonicalTimeframe
    name: str
    style: str


@dataclass
class ActiveTrade:
    symbol: str
    set_key: str
    side: OrderSide
    entry_price: Decimal
    entry_time: datetime
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    take_profit_3: Decimal
    units: int
    risk_amount: Decimal
    state: TradeState = TradeState.ACTIVE
    tp1_hit: bool = False
    tp2_hit: bool = False
    trailing_timeframe: Optional[CanonicalTimeframe] = None
    trailing_price: Optional[Decimal] = None
    current_stop: Optional[Decimal] = None



class SupertrendStochasticStrategy(BaseStrategy):
    """
    HTF Supertrend bias + Stochastic pullback-to-25 across 6 correlated sets.
    LTF entry on Stochastic K/D crossover after candle close.
    Three-tier profit targets with minimum 1:6 RR on TP3.
    """

    SUPER_TREND_ATR_PERIOD = 6
    SUPER_TREND_FACTOR = Decimal("5")
    STOCH_K_PERIOD = 25
    STOCH_K_SMOOTHING = 5
    STOCH_D_SMOOTHING = 3
    STOCH_OVERSOLD = Decimal("25")
    STOCH_OVERBOUGHT = Decimal("75")
    MIN_RR_TP3 = Decimal("6")
    RISK_PER_TRADE = Decimal("0.01")
    NEWS_BLOCK_MINUTES = 30

    SET_CONFIGS: dict[str, SetConfig] = {
        "SET_1": SetConfig("SET_1", CanonicalTimeframe.MONTH, CanonicalTimeframe.WEEK,
                           CanonicalTimeframe.DAY, "SET_1", "Macro Context"),
        "SET_2": SetConfig("SET_2", CanonicalTimeframe.WEEK, CanonicalTimeframe.DAY,
                           CanonicalTimeframe.H4, "SET_2", "Inter-Week Swing"),
        "SET_3": SetConfig("SET_3", CanonicalTimeframe.DAY, CanonicalTimeframe.H4,
                           CanonicalTimeframe.H1, "SET_3", "Intra-Week Swing"),
        "SET_4": SetConfig("SET_4", CanonicalTimeframe.H4, CanonicalTimeframe.H1,
                           CanonicalTimeframe.M15, "SET_4", "Primary Alpha Engine"),
        "SET_5": SetConfig("SET_5", CanonicalTimeframe.H1, CanonicalTimeframe.M15,
                           CanonicalTimeframe.M5, "SET_5", "Session Scalping"),
        "SET_6": SetConfig("SET_6", CanonicalTimeframe.M15, CanonicalTimeframe.M5,
                           CanonicalTimeframe.M1, "SET_6", "Micro Execution"),
    }

    SUBSCRIBED_TIMEFRAMES: list[Timeframe] = [
        Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5, Timeframe.M1,
    ]

    # Map Timeframe vendor values (e.g. "15m") to CanonicalTimeframe keys (e.g. "15M")
    _TIMEFRAME_TO_CANONICAL: dict[str, str] = {
        "1m": "1m", "5m": "5M", "15m": "15M", "1h": "1H", "4h": "4H",
    }

    def __init__(
        self,
        strategy_id: str = "supertrend_stochastic",
        symbols: Optional[list[str]] = None,
        *,
        account_equity: Decimal | str | float = Decimal("100000"),
        account_currency: str = "USD",
        risk_fraction: Decimal | str | float = Decimal("0.01"),
        min_rr_tp3: Decimal | str | float = Decimal("6.0"),
        max_concurrent_trades_per_symbol: int = 6,
        enable_news_filter: bool = True,
    ):
        target_symbols = symbols or ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
        super().__init__(
            strategy_id=strategy_id,
            name="SuperTrend-Stochastic MTF",
            symbols=target_symbols,
            timeframes=self.SUBSCRIBED_TIMEFRAMES,
            parameters={},
        )
        self.account_equity = to_decimal(account_equity)
        self.account_currency = account_currency
        self.risk_fraction = to_decimal(risk_fraction)
        self.min_rr_tp3 = to_decimal(min_rr_tp3)
        self.max_concurrent_trades_per_symbol = max_concurrent_trades_per_symbol
        self.enable_news_filter = enable_news_filter

        self._supertrends: dict[str, dict[str, Supertrend]] = {}
        self._stochastics: dict[str, dict[str, Stochastic]] = {}

        for sym in self.symbols:
            self._supertrends[sym] = {}
            self._stochastics[sym] = {}
            for tf in CanonicalTimeframe:
                self._supertrends[sym][tf.value] = Supertrend(
                    atr_period=self.SUPER_TREND_ATR_PERIOD,
                    factor=self.SUPER_TREND_FACTOR,
                )
                self._stochastics[sym][tf.value] = Stochastic(
                    k_period=self.STOCH_K_PERIOD,
                    k_smoothing=self.STOCH_K_SMOOTHING,
                    d_smoothing=self.STOCH_D_SMOOTHING,
                )

        self._active_trades: dict[tuple[str, str], ActiveTrade] = {}
        self._last_ltf_processed: dict[str, dict[str, Optional[datetime]]] = {
            s: {sk: None for sk in self.SET_CONFIGS}
            for s in self.symbols
        }

    # ------------------------------------------------------------------
    # Bar event handler
    # ------------------------------------------------------------------
    def on_bar(self, event: BarEvent) -> list[OrderIntent]:
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        if symbol not in self.symbols:
            return []
        tf_key = self._TIMEFRAME_TO_CANONICAL.get(event.timeframe.value, event.timeframe.value)
        st = self._supertrends[symbol][tf_key]
        stoch = self._stochastics[symbol][tf_key]
        st.update(event)
        stoch.update(event)
        intents: list[OrderIntent] = []
        for set_key, config in self.SET_CONFIGS.items():
            if config.ltf.value != tf_key:
                continue
            last = self._last_ltf_processed[symbol][set_key]
            if last is not None and last == event.timestamp:
                continue
            existing = self._active_trades.get((symbol, set_key))
            if existing is not None and existing.state in (TradeState.ACTIVE, TradeState.TRAILING):
                continue
            intent = self._evaluate_set(symbol, set_key, event)
            if intent is not None:
                intents.append(intent)
                self._last_ltf_processed[symbol][set_key] = event.timestamp

    # ------------------------------------------------------------------
    # Set evaluation
    # ------------------------------------------------------------------
    def _evaluate_set(self, symbol: str, set_key: str,
                      event: BarEvent) -> Optional[OrderIntent]:
        config = self.SET_CONFIGS[set_key]
        htf_st = self._supertrends[symbol][config.htf.value]
        mtf_st = self._supertrends[symbol][config.mtf.value]
        ltf_st = self._supertrends[symbol][config.ltf.value]
        htf_stoch = self._stochastics[symbol][config.htf.value]
        mtf_stoch = self._stochastics[symbol][config.mtf.value]
        ltf_stoch = self._stochastics[symbol][config.ltf.value]
        if not (htf_st.initialized and mtf_st.initialized and ltf_st.initialized):
            return None
        if not (htf_stoch.initialized and mtf_stoch.initialized and ltf_stoch.initialized):
            return None
        htf_trend = htf_st.trend
        if htf_trend == 0:
            return None
        is_bullish = htf_trend == 1
        if is_bullish:
            if htf_stoch.k is None or htf_stoch.k > self.STOCH_OVERSOLD:
                return None
        else:
            if htf_stoch.k is None or htf_stoch.k < self.STOCH_OVERBOUGHT:
                return None
        htf_target = self._get_swing_target(symbol, config.htf, is_bullish)
        if htf_target is None:
            return None
        if mtf_st.trend != htf_trend:
            return None
        if is_bullish:
            if mtf_stoch.k is None or mtf_stoch.k > self.STOCH_OVERSOLD:
                return None
        else:
            if mtf_stoch.k is None or mtf_stoch.k < self.STOCH_OVERBOUGHT:
                return None
        mtf_target = self._get_swing_target(symbol, config.mtf, is_bullish)
        if mtf_target is None:
            return None
        if ltf_st.trend != htf_trend:
            return None
        if ltf_stoch.k is None or ltf_stoch.d is None:
            return None
        if is_bullish:
            if ltf_stoch.k > self.STOCH_OVERSOLD:
                return None
        else:
            if ltf_stoch.k < self.STOCH_OVERBOUGHT:
                return None
        k_cross = self._detect_kd_crossover(ltf_stoch, is_bullish)
        if not k_cross:
            return None
        entry_price = event.close
        sl_price = ltf_st.supertrend_value
        if sl_price is None:
            return None
        ltf_target = self._get_swing_target(symbol, config.ltf, is_bullish)
        if ltf_target is None:
            ltf_target = entry_price
        if is_bullish:
            if entry_price <= sl_price:
                return None
            risk = entry_price - sl_price
            if risk <= Decimal("0"):
                return None
            min_tp3_distance = risk * self.min_rr_tp3
            min_tp3_price = entry_price + min_tp3_distance
            tp3 = max(htf_target, min_tp3_price)
            tp2 = mtf_target if mtf_target > entry_price else entry_price + min_tp3_distance * Decimal("0.6")
            tp1 = ltf_target if ltf_target > entry_price else entry_price + min_tp3_distance * Decimal("0.3")
            if not (tp1 > entry_price and tp2 > tp1 and tp3 > tp2):
                tp2 = max(tp2, tp1 + risk * Decimal("0.5"))
                tp3 = max(tp3, tp2 + risk * Decimal("0.5"))
        else:
            if entry_price >= sl_price:
                return None
            risk = sl_price - entry_price
            if risk <= Decimal("0"):
                return None
            min_tp3_distance = risk * self.min_rr_tp3
            min_tp3_price = entry_price - min_tp3_distance
            tp3 = min(htf_target, min_tp3_price)
            tp2 = mtf_target if mtf_target < entry_price else entry_price - min_tp3_distance * Decimal("0.6")
            tp1 = ltf_target if ltf_target < entry_price else entry_price - min_tp3_distance * Decimal("0.3")
            if not (tp1 < entry_price and tp2 < tp1 and tp3 < tp2):
                tp2 = min(tp2, tp1 - risk * Decimal("0.5"))
                tp3 = min(tp3, tp2 - risk * Decimal("0.5"))
        lot_size = self._calculate_lot_size(symbol, risk, entry_price)
        if lot_size is None:
            return None
        intent = self.create_intent(
            symbol=symbol,
            side=OrderSide.BUY if is_bullish else OrderSide.SELL,
            order_type=OrderType.MARKET,
            lot_size=lot_size,
            timestamp=event.timestamp,
            stop_loss=sl_price,
            take_profit=tp3,
            urgency=UrgencyLevel.MEDIUM,
            client_tag=(
                f"ST_STOCH|SET={set_key}|"
                f"{'BUY' if is_bullish else 'SELL'}|"
                f"HTF={config.htf.value}|MTF={config.mtf.value}|LTF={config.ltf.value}|"
                f"TP1={tp1}|TP2={tp2}|TP3={tp3}|SL={sl_price}|"
                f"RR={round((tp3 - entry_price) / risk, 2)}R"
            ),
        )
        trade = ActiveTrade(
            symbol=symbol,
            set_key=set_key,
            side=OrderSide.BUY if is_bullish else OrderSide.SELL,
            entry_price=entry_price,
            entry_time=event.timestamp,
            stop_loss=sl_price,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            units=lot_size.units,
            risk_amount=risk,
            state=TradeState.ACTIVE,
            trailing_timeframe=config.ltf,
            trailing_price=sl_price,
            current_stop=sl_price,
        )
        self._active_trades[(symbol, set_key)] = trade

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_swing_target(self, symbol: str, tf: CanonicalTimeframe,
                          is_bullish: bool) -> Optional[Decimal]:
        """Get swing target from Supertrend bands as proxy for swing high/low."""
        st = self._supertrends[symbol][tf.value]
        if is_bullish:
            return st.final_upper
        else:
            return st.final_lower

    def _detect_kd_crossover(self, stoch: Stochastic, is_bullish: bool) -> bool:
        """Detect Stochastic K/D crossover after candle close."""
        if stoch.k is None or stoch.d is None:
            return False
        if is_bullish:
            return stoch.k > stoch.d
        else:
            return stoch.k < stoch.d

    def _calculate_lot_size(self, symbol: str, risk_per_unit: Decimal,
                            entry_price: Decimal) -> Optional[LotSize]:
        risk_amount = self.account_equity * self.risk_fraction
        pair = CurrencyPair.from_symbol(symbol)
        pip_value_per_lot = pair.pip_size * Decimal(str(pair.standard_lot_units))
        risk_pips = risk_per_unit / pair.pip_size
        if risk_pips <= Decimal("0"):
            return None
        lots = risk_amount / (risk_pips * pip_value_per_lot)
        if lots <= Decimal("0"):
            return None
        return LotSize.from_lots(float(lots))

    # ------------------------------------------------------------------
    # Position management (trailing stops)
    # ------------------------------------------------------------------
    def manage_open_position(
        self,
        event: BarEvent,
        position: object,
        open_intent: OrderIntent,
        current_stop_loss: Optional[Decimal],
        favorable_high: Decimal,
        favorable_low: Decimal,
    ) -> dict[str, object]:
        result: dict[str, object] = {"action": "hold", "new_stop": current_stop_loss}
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        tag = open_intent.client_tag or ""
        set_key = ""
        for part in tag.split("|"):
            if part.startswith("SET="):
                set_key = part.split("=", 1)[1]
                break
        if not set_key or (symbol, set_key) not in self._active_trades:
            return result
        trade = self._active_trades[(symbol, set_key)]
        config = self.SET_CONFIGS[set_key]
        close_price = event.close
        direction = 1 if trade.side == OrderSide.BUY else -1
        # TP hit checks
        if not trade.tp1_hit and direction > 0 and close_price >= trade.take_profit_1:
            trade.tp1_hit = True
            trade.state = TradeState.TRAILING
            trade.trailing_timeframe = config.mtf
            result["action"] = "tp1_hit"
            result["new_stop"] = trade.entry_price
            return result
        if trade.tp1_hit and not trade.tp2_hit and direction > 0 and close_price >= trade.take_profit_2:
            trade.tp2_hit = True
            trade.trailing_timeframe = config.htf
            result["action"] = "tp2_hit"
            return result
        # Trailing stop logic
        if trade.trailing_timeframe == config.ltf:
            st = self._supertrends[symbol][config.ltf.value]
            if st.supertrend_value is not None:
                trail = st.supertrend_value
                if direction > 0 and trail > (current_stop_loss or Decimal("0")):
                    result["new_stop"] = trail
                    trade.current_stop = trail
        elif trade.trailing_timeframe == config.mtf:
            st = self._supertrends[symbol][config.mtf.value]
            if st.supertrend_value is not None:
                trail = st.supertrend_value
                if direction > 0 and trail > (current_stop_loss or Decimal("0")):
                    result["new_stop"] = trail
                    trade.current_stop = trail
        elif trade.trailing_timeframe == config.htf:
            st = self._supertrends[symbol][config.htf.value]
            if st.supertrend_value is not None:
                trail = st.supertrend_value
                if direction > 0 and trail > (current_stop_loss or Decimal("0")):
                    result["new_stop"] = trail
                    trade.current_stop = trail
        return result

