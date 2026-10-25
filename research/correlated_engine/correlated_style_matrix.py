#!/usr/bin/env python3
"""
Correlated Multi-Timeframe Fractal Simulation Engine
======================================================
Additive simulation mode for the 5-set correlated fractal strategy.
Does NOT modify existing strategy code or parameters.

Architecture:
- Reads frozen audit configs for each pair/set/style
- Builds correlated state across SET_1 through SET_5
- Shared M1→MTF aggregation via multi_tick_aggregator
- Outputs correlated results alongside independent baseline

Usage:
    python correlated_style_matrix.py --correlated --pairs GBPUSD,EURUSD --sets SET_1,SET_2,SET_3,SET_4,SET_5
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from decimal import Decimal
import uuid

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import existing validated components (read-only)
from forex_platform.strategy_engine.fractal_institutional import InstitutionalFractalStrategy
from forex_platform.strategy_engine.base import BarEvent
import polars as pl
from pathlib import Path
from typing import Optional

# Import our indicators
from indicators import (
    compute_all_indicators,
    detect_swings,
    detect_market_structure,
    detect_key_zones,
    detect_order_blocks,
    detect_fvgs,
    calculate_pz,
)


class Phase(Enum):
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    PULLBACK_BULL = "PULLBACK_BULL"
    PULLBACK_BEAR = "PULLBACK_BEAR"
    CONTINUATION_BULL = "CONTINUATION_BULL"
    CONTINUATION_BEAR = "CONTINUATION_BEAR"
    CHOP = "CHOP"
    TRANSITION = "TRANSITION"


@dataclass
class TimeframeState:
    """State for a single timeframe within a set."""
    timeframe: str  # "1M", "1W", "1D", "4H", "1H", "15M", "5M"
    role: str       # "HTF", "MTF", "LTF"
    phase: Phase = Phase.CHOP
    pz: float = 0.0          # Premium/Discount position
    key_zones: List[dict] = field(default_factory=list)
    order_blocks: List[dict] = field(default_factory=list)
    fvgs: List[dict] = field(default_factory=list)
    vwma_50: float = 0.0
    atr_14: float = 0.0
    swing_highs: List = field(default_factory=list)
    swing_lows: List = field(default_factory=list)
    structure: Dict = field(default_factory=dict)
    confirmation_bars: int = 0
    last_update_idx: int = -1


@dataclass
class TradeRecord:
    """Record of a completed trade."""
    trade_id: str
    pair: str
    set_key: str
    direction: str
    entry_time: str
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    pnl_r: float = 0.0
    exit_reason: str = ""
    rr_ratio: float = 0.0
    risk_r: float = 1.0


@dataclass
class SetState:
    """Complete state for one correlated set."""
    set_key: str              # "SET_1" through "SET_5"
    pair: str
    htf: TimeframeState
    mtf: TimeframeState
    ltf: TimeframeState
    bias: Optional[str] = None      # "LONG", "SHORT", None
    setup_ready: bool = False
    entry_armed: bool = False
    correlation_weight: float = 1.0
    active_trade: Optional[TradeRecord] = None
    completed_trades: List[TradeRecord] = field(default_factory=list)


# Set definitions from design specs
SET_DEFINITIONS = {
    "SET_1": {"HTF": "1M", "MTF": "1W", "LTF": "1D"},
    "SET_2": {"HTF": "1W", "MTF": "1D", "LTF": "4H"},
    "SET_3": {"HTF": "1D", "MTF": "4H", "LTF": "1H"},
    "SET_4": {"HTF": "4H", "MTF": "1H", "LTF": "15M"},
    "SET_5": {"HTF": "1H", "MTF": "15M", "LTF": "5M"},
}

TIMEFRAME_HIERARCHY = ["1M", "1W", "1D", "4H", "1H", "15M", "5M"]

# Risk parameters
RISK_PER_TRADE = 0.01      # 1% per trade
RR_FLOOR = 4.0             # Minimum 1:4 RR
CORRELATION_PENALTY = 0.05 # 0.05R per additional aligned set
MAX_DAILY_DD = 0.03        # 3% daily drawdown circuit


class CorrelatedEngine:
    def __init__(self, pairs: List[str], sets: List[str], data_path: str):
        self.pairs = pairs
        self.sets = sets
        self.data_path = data_path
        self.max_bars = 50000  # Default, can be overridden
        self.set_states: Dict[str, Dict[str, SetState]] = {}  # pair -> set_key -> SetState
        self.tf_data: Dict[str, Dict[str, pl.DataFrame]] = {}  # pair -> timeframe -> DataFrame with indicators
        self.daily_pnl_r: Dict[str, float] = {}  # date -> pnl_r
        self.trading_halted: Dict[str, bool] = {}  # date -> halted

    def load_all_data(self):
        """Load pre-aggregated MTF data and compute indicators for all timeframes."""
        print(f"[CORRELATED] Loading data for {len(self.pairs)} pairs, {len(self.sets)} sets...")
        mtf_path = Path(self.data_path) / "mtf"
        for pair in self.pairs:
            self.tf_data[pair] = {}
            for tf in TIMEFRAME_HIERARCHY:
                try:
                    file_path = mtf_path / f"{pair}_{tf}.parquet"
                    if file_path.exists():
                        df = pl.read_parquet(file_path)
                        # Use open_time as the datetime column, drop int64 timestamp
                        if "open_time" in df.columns:
                            df = df.drop("timestamp").rename({"open_time": "timestamp"})
                        elif "timestamp" in df.columns and df["timestamp"].dtype != pl.Datetime:
                            # Convert int64 nanoseconds to datetime
                            df = df.with_columns(pl.col("timestamp").cast(pl.Datetime("ns", "UTC")))
                        # Compute all indicators
                        df = compute_all_indicators(df)
                        # Pre-compute structural data for efficiency
                        if len(df) >= 50:
                            swings_h, swings_l = detect_swings(df)
                            df = df.with_columns([
                                pl.lit(0).alias("_swing_high"),
                                pl.lit(0).alias("_swing_low"),
                            ])
                            # Mark swing points
                            for idx, _ in swings_h:
                                if idx < len(df):
                                    df = df.with_columns(
                                        pl.when(pl.int_range(pl.len()) == idx)
                                        .then(1).otherwise(pl.col("_swing_high"))
                                        .alias("_swing_high")
                                    )
                            for idx, _ in swings_l:
                                if idx < len(df):
                                    df = df.with_columns(
                                        pl.when(pl.int_range(pl.len()) == idx)
                                        .then(1).otherwise(pl.col("_swing_low"))
                                        .alias("_swing_low")
                                    )
                        self.tf_data[pair][tf] = df
                        print(f"  {pair} {tf}: {len(df)} bars (indicators computed)")
                    else:
                        print(f"  {pair} {tf}: FILE NOT FOUND - {file_path}")
                        self.tf_data[pair][tf] = pl.DataFrame()
                except Exception as e:
                    print(f"  {pair} {tf}: FAILED - {e}")
                    self.tf_data[pair][tf] = pl.DataFrame()

    def initialize_set_states(self):
        """Initialize state objects for all pair/set combinations."""
        for pair in self.pairs:
            self.set_states[pair] = {}
            for set_key in self.sets:
                if set_key not in SET_DEFINITIONS:
                    continue
                tf_def = SET_DEFINITIONS[set_key]
                self.set_states[pair][set_key] = SetState(
                    set_key=set_key,
                    pair=pair,
                    htf=TimeframeState(tf_def["HTF"], "HTF"),
                    mtf=TimeframeState(tf_def["MTF"], "MTF"),
                    ltf=TimeframeState(tf_def["LTF"], "LTF"),
                )

    def update_timeframe_state(self, tf_state: TimeframeState, df: pl.DataFrame, current_idx: int):
        """Update timeframe state with latest bar data."""
        if df.is_empty() or current_idx >= len(df):
            return
        
        row = df.row(current_idx, named=True)
        close = row["close"]
        vwma = row.get("vwma_50", 0.0)
        atr = row.get("atr_14", 0.0)
        pz = row.get("pz", 0.0)
        
        tf_state.vwma_50 = vwma
        tf_state.atr_14 = atr
        tf_state.pz = pz
        tf_state.last_update_idx = current_idx
        
        # Update swings, structure, zones periodically (every 20 bars or first time)
        if current_idx % 20 == 0 or tf_state.confirmation_bars == 0:
            if current_idx >= 50:  # Need enough data
                # Get data up to current index for swing detection
                hist_df = df.slice(0, current_idx + 1)
                swings_h, swings_l = detect_swings(hist_df)
                tf_state.swing_highs = swings_h[-20:]  # Keep last 20
                tf_state.swing_lows = swings_l[-20:]
                
                # Detect structure
                tf_state.structure = detect_market_structure(swings_h, swings_l)
                
                # Detect key zones, OBs, FVGs
                atr_val = atr if atr > 0 else 0.001
                tf_state.key_zones = detect_key_zones(hist_df, swings_h, swings_l, atr_val)
                tf_state.order_blocks = detect_order_blocks(hist_df, swings_h, swings_l, atr_val)
                tf_state.fvgs = detect_fvgs(hist_df, atr_val)
        
        # Detect phase
        tf_state.phase = self.detect_phase(tf_state)

    def detect_phase(self, tf_state: TimeframeState) -> Phase:
        """
        Phase detection based on market structure, PZ, and confirmation bars.
        """
        structure = tf_state.structure
        pz = tf_state.pz
        trend = structure.get("trend", "MIXED")
        struct_type = structure.get("structure", "CHOP")
        
        # Insufficient data
        if struct_type == "CHOP" and trend == "UNDEFINED":
            return Phase.CHOP
        
        # Get phase from previous state
        prev_phase = tf_state.phase
        
        if trend == "BULLISH" and struct_type == "TRENDING":
            # Bullish trend
            if pz < -0.7:
                # Extreme premium - expect pullback
                return Phase.PULLBACK_BULL
            elif pz < -0.3:
                # Premium zone - pullback likely
                return Phase.PULLBACK_BULL
            elif -0.3 <= pz <= 0.3:
                # Neutral - trending
                return Phase.TRENDING_BULL
            elif pz > 0.3:
                # Discount - continuation likely
                if prev_phase in [Phase.PULLBACK_BULL, Phase.TRANSITION]:
                    return Phase.CONTINUATION_BULL
                return Phase.TRENDING_BULL
            else:
                return Phase.TRENDING_BULL
                
        elif trend == "BEARISH" and struct_type == "TRENDING":
            # Bearish trend
            if pz > 0.7:
                # Extreme discount - expect pullback
                return Phase.PULLBACK_BEAR
            elif pz > 0.3:
                # Discount zone - pullback likely
                return Phase.PULLBACK_BEAR
            elif -0.3 <= pz <= 0.3:
                # Neutral - trending
                return Phase.TRENDING_BEAR
            elif pz < -0.3:
                # Premium - continuation likely
                if prev_phase in [Phase.PULLBACK_BEAR, Phase.TRANSITION]:
                    return Phase.CONTINUATION_BEAR
                return Phase.TRENDING_BEAR
            else:
                return Phase.TRENDING_BEAR
        
        return Phase.CHOP

    def determine_bias(self, htf_state: TimeframeState) -> Optional[str]:
        """Determine trade bias from HTF phase."""
        if htf_state.phase in [Phase.TRENDING_BULL, Phase.CONTINUATION_BULL]:
            return "LONG"
        elif htf_state.phase in [Phase.TRENDING_BEAR, Phase.CONTINUATION_BEAR]:
            return "SHORT"
        return None

    def check_mtf_setup(self, set_state: SetState) -> bool:
        """
        Check if MTF confirms HTF bias expectation.
        For LONG bias: MTF should show PULLBACK_BULL forming at discount/premium appropriately
        For SHORT bias: MTF should show PULLBACK_BEAR
        """
        htf = set_state.htf
        mtf = set_state.mtf
        bias = set_state.bias
        
        if bias is None:
            return False
        
        if bias == "LONG":
            # Expect MTF to show pullback in bullish trend
            if mtf.phase in [Phase.PULLBACK_BULL]:
                # Additional confirmation: PZ in discount or neutral for long entry
                if mtf.pz >= -0.3:  # Not deeply overbought
                    return True
        elif bias == "SHORT":
            # Expect MTF to show pullback in bearish trend
            if mtf.phase in [Phase.PULLBACK_BEAR]:
                if mtf.pz <= 0.3:  # Not deeply oversold
                    return True
        
        return False

    def check_ltf_entry(self, set_state: SetState) -> Optional[dict]:
        """
        Check LTF for entry trigger.
        Requires: bias set, MTF setup ready, LTF phase favorable, key zone/OB/FVG confluence
        """
        if set_state.bias is None or not set_state.setup_ready:
            return None
        
        ltf = set_state.ltf
        mtf = set_state.mtf
        htf = set_state.htf
        
        if set_state.bias == "LONG":
            # Look for CONTINUATION_BULL or TRENDING_BULL on LTF
            if ltf.phase not in [Phase.CONTINUATION_BULL, Phase.TRENDING_BULL, Phase.PULLBACK_BULL]:
                return None
            
            # Entry zone: price near key zone, OB, or FVG with PZ favorable
            entry_price = ltf.vwma_50  # Default to VWMA
            stop_price = entry_price - ltf.atr_14 * 1.5
            target_price = entry_price + (entry_price - stop_price) * RR_FLOOR
            
            # Check for confluence
            confluence = self.check_confluence_long(ltf)
            if not confluence:
                return None
            
            return {
                "direction": "LONG",
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "confluence": confluence
            }
            
        elif set_state.bias == "SHORT":
            if ltf.phase not in [Phase.CONTINUATION_BEAR, Phase.TRENDING_BEAR, Phase.PULLBACK_BEAR]:
                return None
            
            entry_price = ltf.vwma_50
            stop_price = entry_price + ltf.atr_14 * 1.5
            target_price = entry_price - (stop_price - entry_price) * RR_FLOOR
            
            confluence = self.check_confluence_short(ltf)
            if not confluence:
                return None
            
            return {
                "direction": "SHORT",
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "confluence": confluence
            }
        
        return None

    def check_confluence_long(self, ltf: TimeframeState) -> List[str]:
        """Check for bullish confluence factors."""
        factors = []
        
        # Price at or below VWMA (discount/neutral)
        if ltf.pz >= -0.3:
            factors.append("pz_neutral_discount")
        
        # Key zone support nearby
        for zone in ltf.key_zones:
            if zone["low"] <= ltf.vwma_50 <= zone["high"]:
                factors.append(f"key_zone_support_strength_{zone['strength']}")
                break
        
        # Bullish order block
        for ob in ltf.order_blocks:
            if ob["type"] == "bullish_ob" and ob["price_low"] <= ltf.vwma_50 <= ob["price_high"]:
                factors.append("bullish_ob")
                break
        
        # Bullish FVG
        for fvg in ltf.fvgs:
            if fvg["type"] == "bullish_fvg" and not fvg["filled"]:
                factors.append("bullish_fvg")
                break
        
        return factors

    def check_confluence_short(self, ltf: TimeframeState) -> List[str]:
        """Check for bearish confluence factors."""
        factors = []
        
        # Price at or above VWMA (premium/neutral)
        if ltf.pz <= 0.3:
            factors.append("pz_neutral_premium")
        
        # Key zone resistance nearby
        for zone in ltf.key_zones:
            if zone["low"] <= ltf.vwma_50 <= zone["high"]:
                factors.append(f"key_zone_resistance_strength_{zone['strength']}")
                break
        
        # Bearish order block
        for ob in ltf.order_blocks:
            if ob["type"] == "bearish_ob" and ob["price_low"] <= ltf.vwma_50 <= ob["price_high"]:
                factors.append("bearish_ob")
                break
        
        # Bearish FVG
        for fvg in ltf.fvgs:
            if fvg["type"] == "bearish_fvg" and not fvg["filled"]:
                factors.append("bearish_fvg")
                break
        
        return factors

    def check_correlation_alignment(self, pair: str, set_key: str, direction: str) -> Tuple[int, List[str]]:
        """Check how many other sets are aligned in same direction."""
        aligned_sets = []
        for other_set, state in self.set_states[pair].items():
            if other_set != set_key and state.bias == direction and state.active_trade is not None:
                aligned_sets.append(other_set)
        return len(aligned_sets), aligned_sets

    def apply_correlation_penalty(self, aligned_count: int) -> float:
        """Calculate correlation penalty."""
        if aligned_count <= 1:
            return 0.0
        return CORRELATION_PENALTY * (aligned_count - 1)

    def check_daily_dd(self, pair: str, current_date: str) -> bool:
        """Check if daily drawdown circuit breaker is triggered."""
        if current_date not in self.daily_pnl_r:
            self.daily_pnl_r[current_date] = 0.0
            self.trading_halted[current_date] = False
        
        if self.daily_pnl_r[current_date] <= -MAX_DAILY_DD:
            self.trading_halted[current_date] = True
            return True
        return False

    def simulate_pair(self, pair: str) -> dict:
        """Simulate one pair across all correlated sets bar by bar."""
        # Get the LTF data (highest resolution) to drive the simulation
        # Use SET_5 LTF (5M) as the base clock
        ltf_tf = "5M"
        if ltf_tf not in self.tf_data[pair] or self.tf_data[pair][ltf_tf].is_empty():
            return {"pair": pair, "error": "No LTF data"}
        
        ltf_df = self.tf_data[pair][ltf_tf]
        total_bars = min(len(ltf_df), getattr(self, 'max_bars', 50000))
        
        if total_bars < 100:
            return {"pair": pair, "error": "Insufficient data"}
        
        print(f"  {pair}: Simulating {total_bars} bars on {ltf_tf}...")
        
        # For each set, we need to track its own timeframe index
        # Map each set's timeframes to indices in the LTF clock
        tf_to_multiplier = {
            "1M": 43200,   # ~30 days * 24h * 60m / 5m (approx)
            "1W": 10080,   # 7 days * 24h * 60m / 5m
            "1D": 288,     # 24h * 60m / 5m
            "4H": 48,      # 4h * 60m / 5m
            "1H": 12,      # 1h * 60m / 5m
            "15M": 3,      # 15m / 5m
            "5M": 1,
        }
        
        # We'll process bar by bar on the LTF (5M) clock
        # For higher timeframes, we update state when a new bar closes
        tf_indices = {}
        for set_key in self.sets:
            if set_key not in SET_DEFINITIONS:
                continue
            tf_def = SET_DEFINITIONS[set_key]
            tf_indices[set_key] = {
                "HTF": 0,
                "MTF": 0,
                "LTF": 0,
                "htf_tf": tf_def["HTF"],
                "mtf_tf": tf_def["MTF"],
                "ltf_tf": tf_def["LTF"],
            }
        
        # Main simulation loop
        for i in range(50, total_bars):  # Start after warmup
            # Get current date for daily DD check
            current_row = ltf_df.row(i, named=True)
            current_ts = current_row["timestamp"]
            current_date = str(current_ts.date()) if hasattr(current_ts, 'date') else str(current_ts)[:10]
            
            # Check daily DD circuit
            if self.check_daily_dd(pair, current_date):
                continue
            
            # Update each set's timeframe states
            for set_key in self.sets:
                if set_key not in self.set_states[pair]:
                    continue
                
                set_state = self.set_states[pair][set_key]
                tf_idx = tf_indices[set_key]
                
                # Update HTF
                htf_tf = tf_idx["htf_tf"]
                htf_multiplier = tf_to_multiplier.get(htf_tf, 1)
                htf_idx = i // htf_multiplier
                if htf_idx < len(self.tf_data[pair].get(htf_tf, pl.DataFrame())):
                    htf_df = self.tf_data[pair][htf_tf]
                    if not htf_df.is_empty() and htf_idx != tf_indices[set_key].get("last_htf_idx", -1):
                        tf_indices[set_key]["last_htf_idx"] = htf_idx
                        self.update_timeframe_state(set_state.htf, htf_df, htf_idx)
                
                # Update MTF
                mtf_tf = tf_idx["mtf_tf"]
                mtf_multiplier = tf_to_multiplier.get(mtf_tf, 1)
                mtf_idx = i // mtf_multiplier
                if mtf_idx < len(self.tf_data[pair].get(mtf_tf, pl.DataFrame())):
                    mtf_df = self.tf_data[pair][mtf_tf]
                    if not mtf_df.is_empty() and mtf_idx != tf_indices[set_key].get("last_mtf_idx", -1):
                        tf_indices[set_key]["last_mtf_idx"] = mtf_idx
                        self.update_timeframe_state(set_state.mtf, mtf_df, mtf_idx)
                
                # Update LTF (every bar on the base clock for SET_5, or per LTF bar for others)
                ltf_tf_set = tf_idx["ltf_tf"]
                ltf_multiplier = tf_to_multiplier.get(ltf_tf_set, 1)
                ltf_idx = i // ltf_multiplier
                if ltf_idx < len(self.tf_data[pair].get(ltf_tf_set, pl.DataFrame())):
                    ltf_df_set = self.tf_data[pair][ltf_tf_set]
                    if not ltf_df_set.is_empty() and ltf_idx != tf_indices[set_key].get("last_ltf_idx", -1):
                        tf_indices[set_key]["last_ltf_idx"] = ltf_idx
                        self.update_timeframe_state(set_state.ltf, ltf_df_set, ltf_idx)
                
                # Now run the set logic
                self.run_set_logic(pair, set_key, current_ts, current_row["close"])
        
        # Collect results
        return self.collect_results(pair)

    def run_set_logic(self, pair: str, set_key: str, current_ts, current_price: float):
        """Run the core logic for one set at current timestamp."""
        set_state = self.set_states[pair][set_key]
        
        # Skip if trading halted for today
        current_date = str(current_ts.date()) if hasattr(current_ts, 'date') else str(current_ts)[:10]
        if self.trading_halted.get(current_date, False):
            return
        
        # Skip if already in a trade
        if set_state.active_trade is not None:
            self.manage_active_trade(pair, set_key, current_ts, current_price)
            return
        
        # Step 1: Determine bias from HTF
        if set_state.bias is None:
            set_state.bias = self.determine_bias(set_state.htf)
            if set_state.bias:
                print(f"    [{pair} {set_key}] Bias set: {set_state.bias} (HTF phase: {set_state.htf.phase.value}, PZ: {set_state.htf.pz:.2f})")
        
        # Step 2: Check MTF setup confirmation
        if set_state.bias and not set_state.setup_ready:
            if self.check_mtf_setup(set_state):
                set_state.setup_ready = True
                print(f"    [{pair} {set_key}] MTF setup confirmed (MTF phase: {set_state.mtf.phase.value}, PZ: {set_state.mtf.pz:.2f})")
        
        # Step 3: Check LTF entry trigger
        if set_state.bias and set_state.setup_ready and not set_state.entry_armed:
            entry_signal = self.check_ltf_entry(set_state)
            if entry_signal:
                set_state.entry_armed = True
                self.execute_entry(pair, set_key, entry_signal, current_ts)

    def execute_entry(self, pair: str, set_key: str, signal: dict, entry_ts):
        """Execute trade entry with correlation penalty."""
        set_state = self.set_states[pair][set_key]
        
        # Check correlation alignment
        aligned_count, aligned_sets = self.check_correlation_alignment(pair, set_key, signal["direction"])
        penalty = self.apply_correlation_penalty(aligned_count)
        effective_rr = RR_FLOOR * (1 - penalty)
        
        if effective_rr < 0.95 * RR_FLOOR:
            # Penalty too high, reduce position but don't reject
            pass
        
        # Adjust target for penalty
        entry_price = signal["entry_price"]
        stop_price = signal["stop_price"]
        risk_per_unit = abs(entry_price - stop_price)
        target_price = entry_price + risk_per_unit * effective_rr * (1 if signal["direction"] == "LONG" else -1)
        
        # Create trade record
        trade = TradeRecord(
            trade_id=str(uuid.uuid4())[:8],
            pair=pair,
            set_key=set_key,
            direction=signal["direction"],
            entry_time=str(entry_ts),
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            rr_ratio=effective_rr,
            risk_r=RISK_PER_TRADE * (1 - penalty),
        )
        
        set_state.active_trade = trade
        set_state.entry_armed = False  # Reset for next trade
        
        print(f"    [{pair} {set_key}] ENTRY {signal['direction']}: {entry_price:.5f} SL:{stop_price:.5f} TP:{target_price:.5f} RR:{effective_rr:.2f} (penalty:{penalty:.2f}, aligned:{aligned_sets})")

    def manage_active_trade(self, pair: str, set_key: str, current_ts, current_price: float):
        """Manage active trade: check SL, TP, trailing."""
        set_state = self.set_states[pair][set_key]
        trade = set_state.active_trade
        
        if trade is None:
            return
        
        # Check stop loss
        if trade.direction == "LONG":
            if current_price <= trade.stop_price:
                self.exit_trade(pair, set_key, trade.stop_price, current_ts, "STOP_LOSS")
                return
            # Check take profit
            if current_price >= trade.target_price:
                self.exit_trade(pair, set_key, trade.target_price, current_ts, "TAKE_PROFIT")
                return
            # MTF trailing: if MTF structure breaks against us, tighten stop
            if set_state.mtf.phase in [Phase.TRANSITION, Phase.PULLBACK_BEAR]:
                # Trail stop to breakeven or recent swing
                pass
        else:  # SHORT
            if current_price >= trade.stop_price:
                self.exit_trade(pair, set_key, trade.stop_price, current_ts, "STOP_LOSS")
                return
            if current_price <= trade.target_price:
                self.exit_trade(pair, set_key, trade.target_price, current_ts, "TAKE_PROFIT")
                return

    def exit_trade(self, pair: str, set_key: str, exit_price: float, exit_ts, reason: str):
        """Exit active trade and record P&L."""
        set_state = self.set_states[pair][set_key]
        trade = set_state.active_trade
        
        if trade is None:
            return
        
        # Calculate P&L in R
        if trade.direction == "LONG":
            pnl_units = exit_price - trade.entry_price
        else:
            pnl_units = trade.entry_price - exit_price
        
        risk_units = abs(trade.entry_price - trade.stop_price)
        pnl_r = (pnl_units / risk_units) * trade.risk_r if risk_units > 0 else 0
        
        trade.exit_time = str(exit_ts)
        trade.exit_price = exit_price
        trade.pnl_r = pnl_r
        trade.exit_reason = reason
        
        set_state.completed_trades.append(trade)
        set_state.active_trade = None
        set_state.bias = None  # Reset bias after trade
        set_state.setup_ready = False
        set_state.entry_armed = False
        
        # Update daily P&L
        current_date = str(exit_ts.date()) if hasattr(exit_ts, 'date') else str(exit_ts)[:10]
        self.daily_pnl_r[current_date] = self.daily_pnl_r.get(current_date, 0.0) + pnl_r
        
        print(f"    [{pair} {set_key}] EXIT {reason}: {exit_price:.5f} PnL:{pnl_r:.2f}R")

    def collect_results(self, pair: str) -> dict:
        """Collect simulation results for a pair."""
        pair_result = {
            "pair": pair,
            "sets_simulated": [],
            "total_trades": 0,
            "total_pnl_r": 0.0,
            "per_set_metrics": {},
            "correlated_metrics": {
                "avg_correlation": 0.0,
                "conflict_rate": 0.0,
                "penalty_applied_r": 0.0
            }
        }
        
        all_trades = []
        for set_key in self.sets:
            if set_key not in self.set_states[pair]:
                continue
            set_state = self.set_states[pair][set_key]
            trades = set_state.completed_trades
            
            if trades:
                pair_result["sets_simulated"].append(set_key)
                pnls = [t.pnl_r for t in trades]
                wins = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p <= 0]
                
                pair_result["per_set_metrics"][set_key] = {
                    "trades": len(trades),
                    "wins": len(wins),
                    "losses": len(losses),
                    "win_rate": len(wins) / len(trades) if trades else 0,
                    "net_pnl_r": sum(pnls),
                    "avg_win_r": sum(wins) / len(wins) if wins else 0,
                    "avg_loss_r": sum(losses) / len(losses) if losses else 0,
                    "profit_factor": sum(wins) / abs(sum(losses)) if losses else float('inf'),
                    "avg_rr": sum(t.rr_ratio for t in trades) / len(trades),
                    "avg_penalty": sum(RR_FLOOR - t.rr_ratio for t in trades) / len(trades),
                }
                
                all_trades.extend(trades)
        
        if all_trades:
            all_pnls = [t.pnl_r for t in all_trades]
            pair_result["total_trades"] = len(all_trades)
            pair_result["total_pnl_r"] = sum(all_pnls)
            
            wins = [p for p in all_pnls if p > 0]
            losses = [p for p in all_pnls if p <= 0]
            pair_result["overall_win_rate"] = len(wins) / len(all_trades)
            pair_result["overall_profit_factor"] = sum(wins) / abs(sum(losses)) if losses else float('inf')
            pair_result["overall_expectancy"] = sum(all_pnls) / len(all_trades)
        
        return pair_result

    def run_correlated_simulation(self) -> dict:
        """Main simulation loop across all sets with correlation."""
        self.load_all_data()
        self.initialize_set_states()
        
        results = {
            "mode": "CORRELATED",
            "pairs": self.pairs,
            "sets": self.sets,
            "per_pair_results": {},
            "correlation_metrics": {}
        }
        
        for pair in self.pairs:
            print(f"[CORRELATED] Simulating {pair}...")
            pair_result = self.simulate_pair(pair)
            results["per_pair_results"][pair] = pair_result
        
        return results


def main():
    parser = argparse.ArgumentParser(description="Correlated MTF Fractal Simulation")
    parser.add_argument("--correlated", action="store_true", required=True,
                        help="Enable correlated mode (required)")
    parser.add_argument("--pairs", type=str, default="GBPUSD",
                        help="Comma-separated pairs")
    parser.add_argument("--sets", type=str, default="SET_1,SET_2,SET_3,SET_4,SET_5",
                        help="Comma-separated sets")
    parser.add_argument("--data-path", type=str, default="data/cache/dukascopy",
                        help="Path to M1 parquet data")
    parser.add_argument("--output", type=str, default="research/results/correlated_simulation.json",
                        help="Output file for results")
    parser.add_argument("--max-bars", type=int, default=50000,
                        help="Maximum bars to simulate per pair (for testing)")

    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",")]
    sets = [s.strip() for s in args.sets.split(",")]

    print("=" * 60)
    print("CORRELATED MULTI-TIMEFRAME FRACTAL SIMULATION")
    print("=" * 60)
    print(f"Pairs: {pairs}")
    print(f"Sets:  {sets}")
    print(f"Data:  {args.data_path}")
    print(f"Max bars: {args.max_bars}")
    print(f"Mode:  CORRELATED (additive, no strategy modification)")
    print()

    engine = CorrelatedEngine(pairs, sets, args.data_path)
    engine.max_bars = args.max_bars
    results = engine.run_correlated_simulation()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\n[CORRELATED] Results written to {output_path}")
    print("[CORRELATED] Simulation complete.")


if __name__ == "__main__":
    main()