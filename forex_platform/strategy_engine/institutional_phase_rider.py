"""
Institutional Phase Rider Strategy - Comprehensive Multi-Timeframe Market Structure Strategy.

This strategy implements institutional-grade trading using:
1. Market Structure: Swings, BOS/CHOCH/MSS breaks across HTF/MTF/LTF
2. Key Zones: Order Blocks, Fair Value Gaps, Liquidity Pools, Session Levels
3. Phases: Pullback vs Continuation classification (HTF bias)
4. HTF Bias: Determines directional edge from highest timeframe structure
5. MTF Setup: Confirms structural shift from HTF key zones
6. LTF Entry: Precise entry at discount (longs) / premium (shorts) on LTF
7. LTF Stop Loss: Initial risk defined on LTF structure
8. MTF Trailing: Trail stop on MTF structure after +2R excursion
9. HTF Take Profit: Structural targets at HTF destinations

Works across all 5 canonical timeframe sets (SET_1 through SET_5)
Portable across all forex assets with 24/7/365 operation.
Self-improving via regime detection and adaptive parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import polars as pl
from pydantic import BaseModel, ConfigDict

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    UrgencyLevel,
    to_decimal,
)
from forex_platform.market_data.causal_aligner import CausalAligner, Timeframe
from forex_platform.market_model.contracts import (
    BreakType,
    MarketPhase,
    MarketZone,
    MarketState,
    StructureBreak,
    SwingPoint,
    SwingScope,
    SwingType,
    TimeframeState,
    ZoneScope,
    ZoneType,
)
from forex_platform.market_model.phases import MarketPhaseClassifier, PhaseAssessment, classify_phase
from forex_platform.market_model.structure import (
    detect_internal_swings,
    detect_structure_breaks,
    detect_swings,
    get_trend_direction,
    is_trend_invalidated,
    mark_protected_weak_swings,
    split_external_internal,
)
from forex_platform.market_model.zones import (
    FVGDirection,
    FairValueGap,
    LiquidityKind,
    LiquidityLevel,
    LiquidityLevelType,
    LiquidityPool,
    LiquiditySweep,
    OBDirection,
    OrderBlock,
    detect_fair_value_gaps,
    detect_liquidity_pools,
    detect_liquidity_sweeps,
    detect_order_blocks,
    extract_session_levels,
    fvgs_to_zones,
    latest_levels,
    levels_to_zones,
    liquidity_pools_to_zones,
    order_blocks_to_zones,
)
from forex_platform.core.sessions import ForexSession, get_session_at
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class TradeState(str, Enum):
    WAITING = "WAITING"
    SETUP = "SETUP"
    ACTIVE = "ACTIVE"
    TRAILING = "TRAILING"
    CLOSED = "CLOSED"


class MarketRegime(str, Enum):
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TradeSetup:
    symbol: str
    direction: OrderSide
    htf_bias: int
    mtf_setup_type: str
    ltf_entry_zone: MarketZone
    ltf_stop_loss: Decimal
    htf_take_profit: Decimal
    mtf_trail_level: Decimal
    risk_reward: Decimal
    confidence: Decimal
    timestamp: datetime
    htf_timeframe: Timeframe
    mtf_timeframe: Timeframe
    ltf_timeframe: Timeframe
    regime: MarketRegime = MarketRegime.UNKNOWN
    session: str = ""
    volatility_percentile: Decimal = Decimal("0.5")


@dataclass
class ActiveTrade:
    setup: TradeSetup
    entry_price: Decimal
    entry_time: datetime
    current_sl: Decimal
    current_tp: Decimal
    max_favorable_excursion: Decimal = Decimal("0")
    max_adverse_excursion: Decimal = Decimal("0")
    trailing_activated: bool = False
    breakeven_moved: bool = False
    state: TradeState = TradeState.ACTIVE
    trade_id: str = ""
    partial_exits: List[Tuple[Decimal, Decimal, datetime]] = field(default_factory=list)


class RegimeDetector:
    def __init__(self, lookback: int = 100):
        self.lookback = lookback
        self._regime_cache: Dict[str, MarketRegime] = {}
    
    def detect(self, symbol: str, history: List[BarEvent]) -> MarketRegime:
        if len(history) < 50:
            return MarketRegime.UNKNOWN
        
        closes = [b.close for b in history[-self.lookback:]]
        highs = [b.high for b in history[-self.lookback:]]
        lows = [b.low for b in history[-self.lookback:]]
        
        tr_sum = Decimal("0")
        dm_plus_sum = Decimal("0")
        dm_minus_sum = Decimal("0")
        
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            tr_sum += tr
            
            dm_plus = highs[i] - highs[i-1] if highs[i] > highs[i-1] else Decimal("0")
            dm_minus = lows[i-1] - lows[i] if lows[i] < lows[i-1] else Decimal("0")
            
            if dm_plus > dm_minus:
                dm_plus_sum += dm_plus
                dm_minus_sum += Decimal("0")
            else:
                dm_plus_sum += Decimal("0")
                dm_minus_sum += dm_minus
        
        if tr_sum == 0:
            return MarketRegime.UNKNOWN
        
        di_plus = (dm_plus_sum / tr_sum) * 100
        di_minus = (dm_minus_sum / tr_sum) * 100
        adx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else Decimal("0")
        
        atr_values = []
        for i in range(14, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            atr_values.append(tr)
        
        if atr_values:
            current_atr = sum(atr_values[-14:]) / 14
            sorted_atr = sorted(atr_values)
            percentile = sum(1 for v in sorted_atr if v <= current_atr) / len(sorted_atr) * 100
        else:
            percentile = Decimal("50")
        
        if adx > 25:
            if di_plus > di_minus:
                return MarketRegime.TRENDING_BULL
            else:
                return MarketRegime.TRENDING_BEAR
        elif percentile > 80:
            return MarketRegime.VOLATILE
        else:
            return MarketRegime.RANGING


class AdaptiveParameterEngine:
    def __init__(self):
        self.performance_history: Dict[str, List[Dict]] = {}
        self.regime_params: Dict[MarketRegime, Dict[str, Decimal]] = {
            MarketRegime.TRENDING_BULL: {
                "min_rr": Decimal("4.0"),
                "trail_activation_r": Decimal("2.0"),
                "trail_distance_atr_mult": Decimal("1.5"),
                "position_size_mult": Decimal("1.0"),
                "entry_aggression": Decimal("0.5"),
            },
            MarketRegime.TRENDING_BEAR: {
                "min_rr": Decimal("4.0"),
                "trail_activation_r": Decimal("2.0"),
                "trail_distance_atr_mult": Decimal("1.5"),
                "position_size_mult": Decimal("1.0"),
                "entry_aggression": Decimal("0.5"),
            },
            MarketRegime.RANGING: {
                "min_rr": Decimal("3.0"),
                "trail_activation_r": Decimal("1.5"),
                "trail_distance_atr_mult": Decimal("1.0"),
                "position_size_mult": Decimal("0.7"),
                "entry_aggression": Decimal("0.3"),
            },
            MarketRegime.VOLATILE: {
                "min_rr": Decimal("5.0"),
                "trail_activation_r": Decimal("3.0"),
                "trail_distance_atr_mult": Decimal("2.0"),
                "position_size_mult": Decimal("0.5"),
                "entry_aggression": Decimal("0.7"),
            },
            MarketRegime.UNKNOWN: {
                "min_rr": Decimal("4.0"),
                "trail_activation_r": Decimal("2.0"),
                "trail_distance_atr_mult": Decimal("1.5"),
                "position_size_mult": Decimal("0.5"),
                "entry_aggression": Decimal("0.5"),
            },
        }
    
    def get_params(self, regime: MarketRegime) -> Dict[str, Decimal]:
        return self.regime_params.get(regime, self.regime_params[MarketRegime.UNKNOWN])
    
    def record_trade(self, symbol: str, setup: TradeSetup, outcome: Dict):
        if symbol not in self.performance_history:
            self.performance_history[symbol] = []
        
        record = {
            "timestamp": setup.timestamp,
            "regime": setup.regime,
            "direction": setup.direction,
            "rr_achieved": outcome.get("rr_achieved", Decimal("0")),
            "win": outcome.get("win", False),
            "duration_hours": outcome.get("duration_hours", 0),
            "session": setup.session,
        }
        self.performance_history[symbol].append(record)
        
        if len(self.performance_history[symbol]) > 200:
            self.performance_history[symbol] = self.performance_history[symbol][-200:]
    
    def optimize_for_symbol(self, symbol: str) -> Dict[MarketRegime, Dict[str, Decimal]]:
        if symbol not in self.performance_history or len(self.performance_history[symbol]) < 30:
            return self.regime_params
        
        history = self.performance_history[symbol]
        optimized = {}
        
        for regime in MarketRegime:
            regime_trades = [t for t in history if t["regime"] == regime]
            if len(regime_trades) < 10:
                optimized[regime] = self.regime_params[regime]
                continue
            
            win_rate = sum(1 for t in regime_trades if t["win"]) / len(regime_trades)
            avg_rr = sum(t["rr_achieved"] for t in regime_trades) / len(regime_trades)
            
            base = self.regime_params[regime].copy()
            
            if win_rate > 0.55 and avg_rr > 3:
                base["position_size_mult"] = min(base["position_size_mult"] * Decimal("1.1"), Decimal("1.5"))
                base["min_rr"] = max(base["min_rr"] * Decimal("0.95"), Decimal("3.0"))
            elif win_rate < 0.4 or avg_rr < 1.5:
                base["position_size_mult"] = max(base["position_size_mult"] * Decimal("0.9"), Decimal("0.3"))
                base["min_rr"] = min(base["min_rr"] * Decimal("1.1"), Decimal("6.0"))
                base["trail_activation_r"] = min(base["trail_activation_r"] * Decimal("1.1"), Decimal("4.0"))
            
            optimized[regime] = base
        
        return optimized


class InstitutionalPhaseRider(BaseStrategy):
    TIMEFRAME_SET_CONFIG = {
        "SET_1": {"htf": Timeframe.MO1, "mtf": Timeframe.W1, "ltf": Timeframe.D1, "gen_tf": Timeframe.H1},
        "SET_2": {"htf": Timeframe.W1, "mtf": Timeframe.D1, "ltf": Timeframe.H4, "gen_tf": Timeframe.H1},
        "SET_3": {"htf": Timeframe.D1, "mtf": Timeframe.H4, "ltf": Timeframe.H1, "gen_tf": Timeframe.H1},
        "SET_4": {"htf": Timeframe.H4, "mtf": Timeframe.H1, "ltf": Timeframe.M15, "gen_tf": Timeframe.M15},
        "SET_5": {"htf": Timeframe.H1, "mtf": Timeframe.M15, "ltf": Timeframe.M5, "gen_tf": Timeframe.M5},
    }
    
    def __init__(
        self,
        strategy_id: str = "institutional_phase_rider",
        symbols: Optional[List[str]] = None,
        timeframe_set: str = "SET_4",
        lookback_swings: int = 5,
        lookback_breaks: int = 3,
        min_rr: float = 4.0,
        trail_activation_r: float = 2.0,
        trail_distance_atr_mult: float = 1.5,
        max_risk_per_trade: float = 0.01,
        max_daily_risk: float = 0.03,
        max_correlation_exposure: float = 0.05,
        use_adaptive_params: bool = True,
        min_bars_for_structure: int = 100,
        session_filter: bool = True,
        news_filter: bool = False,
    ):
        target_symbols = symbols or [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
            "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "EURAUD", "GBPAUD", "AUDCAD"
        ]
        
        tf_config = self.TIMEFRAME_SET_CONFIG.get(timeframe_set, self.TIMEFRAME_SET_CONFIG["SET_4"])
        timeframes = [tf_config["ltf"], tf_config["mtf"], tf_config["htf"]]
        
        super().__init__(
            strategy_id=strategy_id,
            name=f"Institutional Phase Rider ({timeframe_set})",
            symbols=target_symbols,
            timeframes=timeframes,
            parameters={
                "timeframe_set": timeframe_set,
                "lookback_swings": lookback_swings,
                "lookback_breaks": lookback_breaks,
                "min_rr": min_rr,
                "trail_activation_r": trail_activation_r,
                "trail_distance_atr_mult": trail_distance_atr_mult,
                "max_risk_per_trade": max_risk_per_trade,
                "max_daily_risk": max_daily_risk,
                "max_correlation_exposure": max_correlation_exposure,
                "use_adaptive_params": use_adaptive_params,
                "min_bars_for_structure": min_bars_for_structure,
                "session_filter": session_filter,
                "news_filter": news_filter,
            },
        )
        
        self.tf_config = tf_config
        self.htf_tf = tf_config["htf"]
        self.mtf_tf = tf_config["mtf"]
        self.ltf_tf = tf_config["ltf"]
        self.gen_tf = tf_config["gen_tf"]
        
        self.lookback_swings = lookback_swings
        self.lookback_breaks = lookback_breaks
        self.base_min_rr = Decimal(str(min_rr))
        self.base_trail_activation_r = Decimal(str(trail_activation_r))
        self.base_trail_distance_atr_mult = Decimal(str(trail_distance_atr_mult))
        self.max_risk_per_trade = Decimal(str(max_risk_per_trade))
        self.max_daily_risk = Decimal(str(max_daily_risk))
        self.max_correlation_exposure = Decimal(str(max_correlation_exposure))
        self.use_adaptive_params = use_adaptive_params
        self.min_bars_for_structure = min_bars_for_structure
        self.session_filter_enabled = session_filter
        self.news_filter_enabled = news_filter
        
        self._market_states: Dict[str, MarketState] = {}
        self._active_trades: Dict[str, ActiveTrade] = {}
        self._pending_setups: Dict[str, TradeSetup] = {}
        self._daily_pnl: Dict[str, Decimal] = {}
        self._daily_trade_count: Dict[str, int] = {}
        
        self.regime_detector = RegimeDetector()
        self.adaptive_engine = AdaptiveParameterEngine()
        self._causal_aligner = CausalAligner()
        
        self._total_trades = 0
        self._winning_trades = 0
        self._total_rr = Decimal("0")
    
    def _analyze_market_structure(self, symbol: str) -> Optional[MarketState]:
        tf_states = {}
        
        for tf_name, tf in [("htf", self.htf_tf), ("mtf", self.mtf_tf), ("ltf", self.ltf_tf)]:
            df = self._get_history_df(symbol, tf)
            if df is None:
                return None
            
            swings = detect_swings(df, lookback=self.lookback_swings)
            if not swings:
                return None
            
            swings = mark_protected_weak_swings(swings, df)
            external, internal = split_external_internal(swings)
            breaks = detect_structure_breaks(df, swings, confirmation_bars=self.lookback_breaks)
            
            order_blocks = detect_order_blocks(df, swings)
            fvgs = detect_fair_value_gaps(df)
            liquidity_pools = detect_liquidity_pools(df, swings)
            session_levels = extract_session_levels(df)
            
            ob_zones = order_blocks_to_zones(order_blocks)
            fvg_zones = fvgs_to_zones(fvgs)
            liq_zones = liquidity_pools_to_zones(liquidity_pools)
            level_zones = levels_to_zones(session_levels)
            
            all_zones = ob_zones + fvg_zones + liq_zones + level_zones
            
            external_trend = get_trend_direction(external)
            internal_trend = get_trend_direction(internal)
            
            phase = MarketPhase.PULLBACK
            if breaks:
                causal_breaks = [b for b in breaks if b.index <= len(df) - 1]
                if causal_breaks:
                    classifier = MarketPhaseClassifier(swings, causal_breaks)
                    assessment = classifier.classify(df, index=len(df) - 1)
                    phase = assessment.phase
            
            tf_states[tf_name] = TimeframeState(
                timeframe=tf.value,
                symbol=symbol,
                dataframe=df,
                swings=swings,
                breaks=breaks,
                zones=all_zones,
                phase=phase,
                external_trend_direction=external_trend,
                internal_trend_direction=internal_trend,
                protected_swing_high=next((s for s in reversed(external) if s.swing_type == SwingType.HIGH and s.is_protected), None),
                protected_swing_low=next((s for s in reversed(external) if s.swing_type == SwingType.LOW and s.is_protected), None),
                weak_swing_high=next((s for s in reversed(external) if s.swing_type == SwingType.HIGH and s.is_weak), None),
                weak_swing_low=next((s for s in reversed(external) if s.swing_type == SwingType.LOW and s.is_weak), None),
            )
        
        return MarketState(symbol=symbol, timeframes=tf_states)
    
    def _get_htf_bias(self, state: MarketState) -> Tuple[int, Decimal]:
        htf = state.timeframes.get("htf")
        if not htf:
            return 0, Decimal("0")
        
        if htf.external_trend_direction is not None:
            trend = htf.external_trend_direction
            has_protected_high = htf.protected_swing_high is not None
            has_protected_low = htf.protected_swing_low is not None
            confidence = Decimal("0.8") if (has_protected_high or has_protected_low) else Decimal("0.5")
            return trend, confidence
        
        if htf.breaks:
            latest_break = htf.breaks[-1]
            return latest_break.direction, Decimal("0.6")
        
        return 0, Decimal("0")
    
    def _find_mtf_setup(self, state: MarketState, htf_bias: int) -> Optional[Dict]:
        mtf = state.timeframes.get("mtf")
        htf = state.timeframes.get("htf")
        
        if not mtf or not htf:
            return None
        
        htf_key_zones = [
            z for z in htf.zones 
            if z.zone_type in (ZoneType.ORDER_BLOCK, ZoneType.FVG, ZoneType.LIQUIDITY_POOL)
            and z.scope == ZoneScope.EXTERNAL
        ]
        
        if not htf_key_zones:
            return None
        
        for brk in mtf.breaks[-self.lookback_breaks:]:
            if brk.direction != htf_bias:
                continue
            
            for zone in htf_key_zones:
                if brk.direction > 0:
                    if zone.price_low <= brk.body_close <= zone.price_high:
                        return {
                            "type": "CHOCH_FROM_OB" if brk.break_type == BreakType.CHOCH else "BOS_FROM_OB",
                            "htf_zone": zone,
                            "mtf_break": brk,
                            "entry_zone_price": (zone.price_low + zone.price_high) / 2,
                        }
                else:
                    if zone.price_low <= brk.body_close <= zone.price_high:
                        return {
                            "type": "CHOCH_FROM_OB" if brk.break_type == BreakType.CHOCH else "BOS_FROM_OB",
                            "htf_zone": zone,
                            "mtf_break": brk,
                            "entry_zone_price": (zone.price_low + zone.price_high) / 2,
                        }
        
        liq_sweeps = detect_liquidity_sweeps(mtf.dataframe, htf_key_zones)
        for sweep in liq_sweeps:
            if sweep.direction == -htf_bias:
                return {
                    "type": "LIQUIDITY_SWEEP_REVERSAL",
                    "htf_zone": sweep.zone,
                    "mtf_break": None,
                    "entry_zone_price": (sweep.zone.price_low + sweep.zone.price_high) / 2,
                }
        
        return None
    
    def _find_ltf_entry_zone(self, state: MarketState, mtf_setup: Dict, htf_bias: int) -> Optional[MarketZone]:
        ltf = state.timeframes.get("ltf")
        if not ltf:
            return None
        
        ltf_zones = [
            z for z in ltf.zones
            if z.scope == ZoneScope.INTERNAL
            and z.zone_type in (ZoneType.ORDER_BLOCK, ZoneType.FVG)
        ]
        
        if not ltf_zones:
            return None
        
        current_price = Decimal(str(ltf.dataframe["close"][-1]))
        entry_zone_price = mtf_setup["entry_zone_price"]
        
        if htf_bias > 0:
            candidates = [z for z in ltf_zones if z.price_high < current_price and z.price_low > entry_zone_price * Decimal("0.999")]
            if candidates:
                fvgs = [z for z in candidates if z.zone_type == ZoneType.FVG]
                if fvgs:
                    return max(fvgs, key=lambda z: z.price_high)
                return max(candidates, key=lambda z: z.price_high)
        else:
            candidates = [z for z in ltf_zones if z.price_low > current_price and z.price_high < entry_zone_price * Decimal("1.001")]
            if candidates:
                fvgs = [z for z in candidates if z.zone_type == ZoneType.FVG]
                if fvgs:
                    return min(fvgs, key=lambda z: z.price_low)
                return min(candidates, key=lambda z: z.price_low)
        
        return None
return None
    
    def _calculate_levels(
        self,
        symbol: str,
        direction: OrderSide,
        ltf_zone: MarketZone,
        state: MarketState,
        regime_params: Dict[str, Decimal],
    ) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
        pair = CurrencyPair.from_symbol(symbol)
        ltf = state.timeframes.get("ltf")
        mtf = state.timeframes.get("mtf")
        htf = state.timeframes.get("htf")
        
        if not ltf or not mtf or not htf:
            raise ValueError("Missing timeframe state")
        
        current_price = Decimal(str(ltf.dataframe["close"][-1]))
        
        if direction == OrderSide.BUY:
            ltf_swing_lows = [s for s in ltf.swings if s.swing_type == SwingType.LOW and s.index > len(ltf.swings) - 10]
            if ltf_swing_lows:
                sl = min(s.price for s in ltf_swing_lows)
            else:
                sl = ltf_zone.price_low
            sl = min(sl, current_price - pair.to_price(Decimal("10")))
        else:
            ltf_swing_highs = [s for s in ltf.swings if s.swing_type == SwingType.HIGH and s.index > len(ltf.swings) - 10]
            if ltf_swing_highs:
                sl = max(s.price for s in ltf_swing_highs)
            else:
                sl = ltf_zone.price_high
            sl = max(sl, current_price + pair.to_price(Decimal("10")))
        
        aggression = regime_params["entry_aggression"]
        if direction == OrderSide.BUY:
            entry = ltf_zone.price_low + (ltf_zone.price_high - ltf_zone.price_low) * aggression
            entry = min(entry, current_price)
        else:
            entry = ltf_zone.price_high - (ltf_zone.price_high - ltf_zone.price_low) * aggression
            entry = max(entry, current_price)
        
        risk = abs(entry - sl)
        if risk == 0:
            raise ValueError("Zero risk")
        
        if direction == OrderSide.BUY:
            htf_targets = []
            if htf.protected_swing_high:
                htf_targets.append(htf.protected_swing_high.price)
            for z in htf.zones:
                if z.zone_type in (ZoneType.ORDER_BLOCK, ZoneType.PREVIOUS_DAY_HIGH_LOW, ZoneType.PREVIOUS_WEEK_HIGH_LOW):
                    if z.price_low > entry:
                        htf_targets.append(z.price_high)
            
            if htf_targets:
                tp = min(htf_targets)
            else:
                tp = entry + risk * Decimal("4")
        else:
            htf_targets = []
            if htf.protected_swing_low:
                htf_targets.append(htf.protected_swing_low.price)
            for z in htf.zones:
                if z.zone_type in (ZoneType.ORDER_BLOCK, ZoneType.PREVIOUS_DAY_HIGH_LOW, ZoneType.PREVIOUS_WEEK_HIGH_LOW):
                    if z.price_high < entry:
                        htf_targets.append(z.price_low)
            
            if htf_targets:
                tp = max(htf_targets)
            else:
                tp = entry - risk * Decimal("4")
        
        min_rr = regime_params["min_rr"]
        actual_rr = abs(tp - entry) / risk
        if actual_rr < min_rr:
            if direction == OrderSide.BUY:
return True
    
    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        self.update_history(event)
        symbol = event.symbol
        
        if not self._check_session_filter(event.timestamp):
            return []
        
        state = self._analyze_market_structure(symbol)
        if not state:
            return []
        
        self._market_states[symbol] = state
        
        intents = self._manage_active_trades(symbol, event, state)
        
        if symbol not in self._active_trades:
            setup = self._find_new_setup(symbol, event, state)
            if setup:
                self._pending_setups[symbol] = setup
                entry_intent = self._check_ltf_entry(symbol, event, setup)
                if entry_intent:
                    intents.append(entry_intent)
                    self._activate_trade(symbol, setup, event, entry_intent)
                    del self._pending_setups[symbol]
        
        return intents
                tp = entry + risk * min_rr
            else:
                tp = entry - risk * min_rr
        
        if direction == OrderSide.BUY:
            mtf_swing_lows = [s for s in mtf.swings if s.swing_type == SwingType.LOW]
            if mtf_swing_lows:
                trail = mtf_swing_lows[-1].price
            else:
                trail = entry + risk * Decimal("1")
        else:
            mtf_swing_highs = [s for s in mtf.swings if s.swing_type == SwingType.HIGH]
return intents
    
    def _find_new_setup(self, symbol: str, event: BarEvent, state: MarketState) -> Optional[TradeSetup]:
        htf_bias, htf_confidence = self._get_htf_bias(state)
        if htf_bias == 0 or htf_confidence < Decimal("0.5"):
            return None
        
        mtf_setup = self._find_mtf_setup(state, htf_bias)
        if not mtf_setup:
            return None
        
        mtf = state.timeframes.get("mtf")
        if mtf and mtf.phase == MarketPhase.CONTINUATION and mtf.external_trend_direction != htf_bias:
            return None
        
        ltf_zone = self._find_ltf_entry_zone(state, mtf_setup, htf_bias)
        if not ltf_zone:
            return None
        
        ltf_history = self.get_history(symbol)
        regime = self.regime_detector.detect(symbol, ltf_history)
        
        if self.use_adaptive_params:
            regime_params = self.adaptive_engine.get_params(regime)
        else:
            regime_params = self.adaptive_engine.regime_params[MarketRegime.UNKNOWN]
        
        direction = OrderSide.BUY if htf_bias > 0 else OrderSide.SELL
        try:
            entry, sl, tp, trail = self._calculate_levels(
                symbol, direction, ltf_zone, state, regime_params
            )
        except ValueError:
            return None
        
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else Decimal("0")
        
        if rr < regime_params["min_rr"]:
            return None
        
        session = get_session_at(event.timestamp).value
        volatility_percentile = Decimal("0.5")
        
        return TradeSetup(
            symbol=symbol,
            direction=direction,
            htf_bias=htf_bias,
            mtf_setup_type=mtf_setup["type"],
            ltf_entry_zone=ltf_zone,
            ltf_stop_loss=sl,
            htf_take_profit=tp,
            mtf_trail_level=trail,
            risk_reward=rr,
            confidence=htf_confidence,
            timestamp=event.timestamp,
            htf_timeframe=self.htf_tf,
            mtf_timeframe=self.mtf_tf,
            ltf_timeframe=self.ltf_tf,
            regime=regime,
            session=session,
            volatility_percentile=volatility_percentile,
        )
    
    def _check_ltf_entry(self, symbol: str, event: BarEvent, setup: TradeSetup) -> Optional[OrderIntent]:
        ltf_history = self.get_history(symbol)
        if not ltf_history:
            return None
        
        current_price = event.close
        
        if self.use_adaptive_params:
            regime_params = self.adaptive_engine.get_params(setup.regime)
        else:
            regime_params = self.adaptive_engine.regime_params[MarketRegime.UNKNOWN]
        
        if setup.direction == OrderSide.BUY:
            if current_price <= setup.ltf_entry_zone.price_high and current_price >= setup.ltf_entry_zone.price_low:
                if event.close >= setup.ltf_entry_zone.price_low:
                    pair = CurrencyPair.from_symbol(symbol)
today = event.timestamp.date().isoformat()
        self._daily_trade_count[today] = self._daily_trade_count.get(today, 0) + 1
    
    def _manage_active_trades(self, symbol: str, event: BarEvent, state: MarketState) -> List[OrderIntent]:
        intents = []
        
        if symbol not in self._active_trades:
            return intents
        
        trade = self._active_trades[symbol]
        current_price = event.close
        direction = trade.setup.direction
        
        if direction == OrderSide.BUY:
            trade.max_favorable_excursion = max(trade.max_favorable_excursion, current_price - trade.entry_price)
            trade.max_adverse_excursion = max(trade.max_adverse_excursion, trade.entry_price - current_price)
        else:
            trade.max_favorable_excursion = max(trade.max_favorable_excursion, trade.entry_price - current_price)
            trade.max_adverse_excursion = max(trade.max_adverse_excursion, current_price - trade.entry_price)
        
        regime_params = self.adaptive_engine.get_params(trade.setup.regime)
        trail_activation_r = regime_params["trail_activation_r"]
        
        risk = abs(trade.entry_price - trade.current_sl)
        if risk == 0:
            return intents
        
        current_r = trade.max_favorable_excursion / risk
        
        if not trade.breakeven_moved and current_r >= Decimal("1.0"):
            trade.current_sl = trade.entry_price
            trade.breakeven_moved = True
            trade.state = TradeState.TRAILING
        
        if not trade.trailing_activated and current_r >= trail_activation_r:
            trade.trailing_activated = True
            trade.state = TradeState.TRAILING
        
        if trade.trailing_activated:
            mtf = state.timeframes.get("mtf")
            if mtf:
                if direction == OrderSide.BUY:
                    mtf_swing_lows = [s for s in mtf.swings if s.swing_type == SwingType.LOW and s.index < len(mtf.dataframe) - 1]
                    if mtf_swing_lows:
                        new_trail = mtf_swing_lows[-1].price
                        if new_trail > trade.current_sl:
                            trade.current_sl = new_trail
                else:
                    mtf_swing_highs = [s for s in mtf.swings if s.swing_type == SwingType.HIGH and s.index < len(mtf.dataframe) - 1]
                    if mtf_swing_highs:
                        new_trail = mtf_swing_highs[-1].price
                        if new_trail < trade.current_sl:
                            trade.current_sl = new_trail
        
        sl_hit = False
        if direction == OrderSide.BUY and event.low <= trade.current_sl:
            sl_hit = True
        elif direction == OrderSide.SELL and event.high >= trade.current_sl:
            sl_hit = True
        
        tp_hit = False
        if direction == OrderSide.BUY and event.high >= trade.current_tp:
            tp_hit = True
        elif direction == OrderSide.SELL and event.low <= trade.current_tp:
            tp_hit = True
        
        if sl_hit or tp_hit:
            exit_price = trade.current_sl if sl_hit else trade.current_tp
            exit_intent = self.create_intent(
                symbol=symbol,
                side=OrderSide.SELL if direction == OrderSide.BUY else OrderSide.BUY,
                order_type=OrderType.MARKET,
                lot_size=LotSize.from_lots(1.0),
                timestamp=event.timestamp,
                stop_loss=None,
                take_profit=None,
                urgency=UrgencyLevel.HIGH,
                client_tag=f"PHASE_RIDER_EXIT_{'SL' if sl_hit else 'TP'}",
            )
            intents.append(exit_intent)
            
            rr_achieved = abs(exit_price - trade.entry_price) / risk
            win = tp_hit
            self._record_trade_outcome(trade, rr_achieved, win, event.timestamp)
            
            trade.state = TradeState.CLOSED
            del self._active_trades[symbol]
        
        return intents
    
    def _record_trade_outcome(self, trade: ActiveTrade, rr_achieved: Decimal, win: bool, exit_time: datetime):
        duration_hours = (exit_time - trade.entry_time).total_seconds() / 3600
        
        self.adaptive_engine.record_trade(trade.setup.symbol, trade.setup, {
            "rr_achieved": rr_achieved,
            "win": win,
            "duration_hours": duration_hours,
        })
        
        self._total_trades += 1
        if win:
            self._winning_trades += 1
        self._total_rr += rr_achieved
        
        today = exit_time.date().isoformat()
        pnl = rr_achieved * abs(trade.entry_price - trade.setup.ltf_stop_loss)
        self._daily_pnl[today] = self._daily_pnl.get(today, Decimal("0")) + pnl
    
    def get_performance_stats(self) -> Dict[str, Any]:
        win_rate = self._winning_trades / self._total_trades if self._total_trades > 0 else 0
        avg_rr = self._total_rr / self._total_trades if self._total_trades > 0 else 0
        expectancy = (win_rate * avg_rr) - ((1 - win_rate) * 1)
        
        return {
            "total_trades": self._total_trades,
            "winning_trades": self._winning_trades,
            "win_rate": win_rate,
            "avg_rr": float(avg_rr),
            "expectancy": float(expectancy),
            "active_trades": len(self._active_trades),
            "pending_setups": len(self._pending_setups),
        }


__all__ = ["InstitutionalPhaseRider", "TradeSetup", "ActiveTrade", "TradeState", "MarketRegime"]
                    lot_size = LotSize.from_lots(float(setup.confidence * regime_params["position_size_mult"]))
                    return self.create_intent(
                        symbol=symbol,
                        side=OrderSide.BUY,
                        order_type=OrderType.LIMIT,
                        lot_size=lot_size,
                        timestamp=event.timestamp,
                        limit_price=pair.round_price(current_price),
                        stop_loss=setup.ltf_stop_loss,
                        take_profit=setup.htf_take_profit,
                        urgency=UrgencyLevel.MEDIUM,
                        client_tag=f"PHASE_RIDER_{setup.mtf_setup_type}_BUY",
                    )
        else:
            if current_price >= setup.ltf_entry_zone.price_low and current_price <= setup.ltf_entry_zone.price_high:
                if event.close <= setup.ltf_entry_zone.price_high:
                    pair = CurrencyPair.from_symbol(symbol)
                    lot_size = LotSize.from_lots(float(setup.confidence * regime_params["position_size_mult"]))
                    return self.create_intent(
                        symbol=symbol,
                        side=OrderSide.SELL,
                        order_type=OrderType.LIMIT,
                        lot_size=lot_size,
                        timestamp=event.timestamp,
                        limit_price=pair.round_price(current_price),
                        stop_loss=setup.ltf_stop_loss,
                        take_profit=setup.htf_take_profit,
                        urgency=UrgencyLevel.MEDIUM,
                        client_tag=f"PHASE_RIDER_{setup.mtf_setup_type}_SELL",
                    )
        
        return None
    
    def _activate_trade(self, symbol: str, setup: TradeSetup, event: BarEvent, intent: OrderIntent):
        trade_id = f"{setup.symbol}_{setup.direction.value}_{int(event.timestamp.timestamp())}"
        
        self._active_trades[symbol] = ActiveTrade(
            setup=setup,
            entry_price=intent.limit_price or event.close,
            entry_time=event.timestamp,
            current_sl=setup.ltf_stop_loss,
            current_tp=setup.htf_take_profit,
            trade_id=trade_id,
            state=TradeState.ACTIVE,
        )
        
        today = event.timestamp.date().isoformat()
        self._daily_trade_count[today] = self._daily_trade_count.get(today, 0) + 1
            if mtf_swing_highs:
                trail = mtf_swing_highs[-1].price
            else:
                trail = entry - risk * Decimal("1")
        
        return pair.round_price(entry), pair.round_price(sl), pair.round_price(tp), pair.round_price(trail)
    
    def _check_session_filter(self, timestamp: datetime) -> bool:
        if not self.session_filter_enabled:
            return True
        
        session = get_session_at(timestamp)
        avoided = {ForexSession.CLOSED, ForexSession.LATE_NY}
        return session not in avoided
    
    def _check_risk_limits(self, symbol: str, risk_amount: Decimal) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        daily_pnl = self._daily_pnl.get(today, Decimal("0"))
        daily_trades = self._daily_trade_count.get(today, 0)
        
        account_equity = Decimal("100000")
        max_daily_loss = account_equity * self.max_daily_risk
        
        if daily_pnl <= -max_daily_loss:
            return False
        
        if daily_trades >= 10:
            return False
        
        trade_risk = risk_amount
        if trade_risk > account_equity * self.max_risk_per_trade:
            return False
        
        return True