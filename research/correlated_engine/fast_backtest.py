#!/usr/bin/env python3
"""
Fast Correlated MTF Backtest
=============================
Pre-computed / vectorized approach for testing the correlated
multi-timeframe fractal strategy across SET_1..SET_5.

Design notes
------------
- Strategy CONCEPT only. No modification of the frozen InstitutionalFractalStrategy.
- Every timeframe bar is assigned a phase from its own structure + PZ.
- Sets are evaluated on a shared 5M clock with as-of alignment (no lookahead).
- Entry: HTF bias -> MTF setup -> LTF trigger.
- Exit: LTF stop (1.5 ATR), HTF target (RR floor), plus time stop.
- Costs: applied at trade close as a fixed R haircut.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional
from enum import Enum

import numpy as np
import polars as pl

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from indicators import (  # noqa: E402
    compute_all_indicators,
    detect_swings,
    detect_market_structure,
)


# ---------------------------------------------------------------------------
# Phase model
# ---------------------------------------------------------------------------
class Phase(Enum):
    TRENDING_BULL = 1
    TRENDING_BEAR = 2
    PULLBACK_BULL = 3
    PULLBACK_BEAR = 4
    CONTINUATION_BULL = 5
    CONTINUATION_BEAR = 6
    CHOP = 7
    TRANSITION = 8


SET_DEFINITIONS = {
    "SET_1": {"HTF": "1M", "MTF": "1W", "LTF": "1D"},
    "SET_2": {"HTF": "1W", "MTF": "1D", "LTF": "4H"},
    "SET_3": {"HTF": "1D", "MTF": "4H", "LTF": "1H"},
    "SET_4": {"HTF": "4H", "MTF": "1H", "LTF": "15M"},
    "SET_5": {"HTF": "1H", "MTF": "15M", "LTF": "5M"},
}

TIMEFRAME_HIERARCHY = ["1M", "1W", "1D", "4H", "1H", "15M", "5M"]

# Pair-specific cost model (spread + commission + slippage in R)
COST_R_PER_TRADE = {
    "EURUSD": 0.65,   # tightest spread
    "GBPUSD": 0.75,
    "USDJPY": 0.70,
    "AUDUSD": 0.80,
    "DEFAULT": 0.80,
}

# Correlation penalty: 0.05R per additional aligned set
CORRELATION_PENALTY_STEP = 0.05

RR_FLOOR = 4.0
RISK_PER_TRADE_R = 1.0
MAX_BARS_IN_TRADE = 500


def detect_phase(structure: Dict, pz: Optional[float], prev_phase: int) -> int:
    """Assign a phase from own-timeframe structure, PZ and previous phase."""
    if pz is None or pz != pz:  # NaN
        return Phase.CHOP.value

    trend = structure.get("trend", "MIXED")
    stype = structure.get("structure", "CHOP")

    if trend == "BULLISH" and stype == "TRENDING":
        if pz < -0.3:
            return Phase.PULLBACK_BULL.value
        if pz <= 0.3:
            return Phase.TRENDING_BULL.value
        # price at discount after a pullback → continuation
        if prev_phase in (Phase.PULLBACK_BULL.value, Phase.TRANSITION.value):
            return Phase.CONTINUATION_BULL.value
        return Phase.TRENDING_BULL.value

    if trend == "BEARISH" and stype == "TRENDING":
        if pz > 0.3:
            return Phase.PULLBACK_BEAR.value
        if pz >= -0.3:
            return Phase.TRENDING_BEAR.value
        if prev_phase in (Phase.PULLBACK_BEAR.value, Phase.TRANSITION.value):
            return Phase.CONTINUATION_BEAR.value
        return Phase.TRENDING_BEAR.value

    return Phase.CHOP.value


# ---------------------------------------------------------------------------
# Pre-computation
# ---------------------------------------------------------------------------
def precompute_timeframe(df: pl.DataFrame, roll_window: int = 60) -> pl.DataFrame:
    """Pre-compute phase / trend / structure for a timeframe."""
    if df.is_empty():
        return df

    if len(df) < 50:
        return df.with_columns([
            pl.lit("MIXED").alias("trend"),
            pl.lit("CHOP").alias("struct_type"),
            pl.lit(Phase.CHOP.value, dtype=pl.Int32).alias("phase"),
        ])

    n = len(df)
    trend_arr = np.full(n, "MIXED", dtype=object)
    struct_arr = np.full(n, "CHOP", dtype=object)

    # For very small timeframes (1M, 1W), use expanding window from available data
    min_window = max(30, roll_window)
    
    for start in range(0, n, roll_window):
        end = min(start + roll_window + 20, n)
        # Use expanding window for early data
        window_start = max(0, start - min_window)
        window = df.slice(window_start, end - window_start)
        sh, sl = detect_swings(window, lookback=3)
        structure = detect_market_structure(sh, sl)
        trend_arr[start:end] = structure.get("trend", "MIXED")
        struct_arr[start:end] = structure.get("structure", "CHOP")

    df = df.with_columns([
        pl.Series("trend", trend_arr),
        pl.Series("struct_type", struct_arr),
    ])

    # Phase per bar (sequential, uses previous phase)
    phases = np.empty(n, dtype=np.int32)
    prev = Phase.CHOP.value
    pz_col = df["pz"].to_list()
    for i in range(n):
        prev = detect_phase(
            {"trend": trend_arr[i], "structure": struct_arr[i]},
            pz_col[i],
            prev,
        )
        phases[i] = prev

    return df.with_columns(pl.Series("phase", phases))


def load_pair_data(pair: str, data_path: str, verbose: bool = True) -> Dict[str, pl.DataFrame]:
    """Load M1..5M parquet for one pair and compute indicators + phases."""
    mtf_path = Path(data_path) / "mtf"
    out: Dict[str, pl.DataFrame] = {}

    for tf in TIMEFRAME_HIERARCHY:
        fp = mtf_path / f"{pair}_{tf}.parquet"
        if not fp.exists():
            if verbose:
                print(f"  {pair} {tf}: MISSING")
            out[tf] = pl.DataFrame()
            continue

        df = pl.read_parquet(fp)
        if "open_time" in df.columns:
            df = df.drop("timestamp").rename({"open_time": "timestamp"})
        elif df["timestamp"].dtype != pl.Datetime:
            df = df.with_columns(pl.col("timestamp").cast(pl.Datetime("ns", "UTC")))

        df = compute_all_indicators(df)
        df = precompute_timeframe(df)
        out[tf] = df

        if verbose:
            cnt = df["phase"].value_counts().sort("phase")
            dist = {int(r["phase"]): int(r["count"]) for r in cnt.to_dicts()}
            print(f"  {pair} {tf}: {len(df):>7} bars | phases {dist}")

    return out


def align_to_base(tf_data: Dict[str, pl.DataFrame], base_tf: str = "5M") -> pl.DataFrame:
    """As-of (backward) align every timeframe onto the base clock."""
    cols = ["timestamp", "close", "phase", "pz", "vwma_50", "atr_14", "trend", "struct_type"]

    base = tf_data[base_tf].select(cols)
    base = base.rename({
        "close": f"close_{base_tf}",
        "phase": f"phase_{base_tf}",
        "pz": f"pz_{base_tf}",
        "vwma_50": f"vwma_{base_tf}",
        "atr_14": f"atr_{base_tf}",
        "trend": f"trend_{base_tf}",
        "struct_type": f"struct_{base_tf}",
    })

    for tf in TIMEFRAME_HIERARCHY:
        if tf == base_tf or tf_data.get(tf) is None or tf_data[tf].is_empty():
            continue
        df = tf_data[tf].select(cols)
        df = df.rename({
            "close": f"close_{tf}",
            "phase": f"phase_{tf}",
            "pz": f"pz_{tf}",
            "vwma_50": f"vwma_{tf}",
            "atr_14": f"atr_{tf}",
            "trend": f"trend_{tf}",
            "struct_type": f"struct_{tf}",
        })
        # trim to base range for speed
        df = df.filter(
            (pl.col("timestamp") >= base["timestamp"].min())
            & (pl.col("timestamp") <= base["timestamp"].max())
        )
        base = base.join_asof(df, on="timestamp", strategy="backward")

    return base


# ---------------------------------------------------------------------------
# Single-set state machine
# ---------------------------------------------------------------------------
def run_set(set_df: pl.DataFrame, set_key: str, pair: str, debug: bool = False) -> List[dict]:
    """HTF bias -> MTF setup -> LTF trigger with confluence -> manage -> exit."""
    trades: List[dict] = []
    bias: Optional[str] = None
    setup_ready = False
    trade: Optional[dict] = None
    counters = {"bias": 0, "setup": 0, "entry": 0, "rejected": 0}

    ts = set_df["timestamp"].to_list()
    htf_phase = set_df["htf_phase"].to_list()
    mtf_phase = set_df["mtf_phase"].to_list()
    ltf_phase = set_df["ltf_phase"].to_list()
    ltf_close = set_df["ltf_close"].to_list()
    ltf_atr = set_df["ltf_atr"].to_list()
    mtf_pz = set_df["mtf_pz"].to_list()
    htf_pz = set_df["htf_pz"].to_list()
    
    # Confluence data (if available from indicators)
    ltf_vwma = set_df.get("ltf_vwma", [None]*len(set_df)).to_list() if "ltf_vwma" in set_df.columns else [None]*len(set_df)
    ltf_pz_col = set_df.get("ltf_pz", [None]*len(set_df)).to_list() if "ltf_pz" in set_df.columns else [None]*len(set_df)

    n = len(set_df)
    
    if debug:
        print(f"    [DEBUG] {set_key}: n={n}")

    for i in range(n):
        px = ltf_close[i]
        atr = ltf_atr[i]
        if px is None or atr is None or atr <= 0:
            continue

        # ---- manage open trade -------------------------------------------
        if trade is not None:
            bars_held = i - trade["entry_idx"]
            hit_sl = (
                px <= trade["stop_price"] if trade["direction"] == "LONG"
                else px >= trade["stop_price"]
            )
            hit_tp = (
                px >= trade["target_price"] if trade["direction"] == "LONG"
                else px <= trade["target_price"]
            )
            
            # MTF trailing: if MTF structure turns against us, tighten stop
            mtf_against = False
            if trade["direction"] == "LONG" and mtf_phase[i] in (Phase.TRANSITION.value, Phase.PULLBACK_BEAR.value):
                mtf_against = True
            elif trade["direction"] == "SHORT" and mtf_phase[i] in (Phase.TRANSITION.value, Phase.PULLBACK_BULL.value):
                mtf_against = True
            
            hit_sl = (
                px <= trade["stop_price"] if trade["direction"] == "LONG"
                else px >= trade["stop_price"]
            )
            hit_tp = (
                px >= trade["target_price"] if trade["direction"] == "LONG"
                else px <= trade["target_price"]
            )

            exit_px = None
            reason = ""
            if hit_sl:
                exit_px, reason = trade["stop_price"], "STOP"
            elif hit_tp:
                exit_px, reason = trade["target_price"], "TARGET"
            elif mtf_against and bars_held > 20:
                # MTF structure break against us - exit early
                exit_px, reason = px, "MTF_TRAIL"
            elif bars_held >= MAX_BARS_IN_TRADE:
                exit_px, reason = px, "TIME"

            if exit_px is not None:
                risk = abs(trade["entry_price"] - trade["stop_price"])
                move = (
                    exit_px - trade["entry_price"] if trade["direction"] == "LONG"
                    else trade["entry_price"] - exit_px
                )
                gross_r = move / risk if risk > 0 else 0.0
                
                # Apply pair-specific cost
                pair_cost = COST_R_PER_TRADE.get(pair, COST_R_PER_TRADE["DEFAULT"])
                
                # Apply correlation penalty if tracked
                corr_penalty = trade.get("correlation_penalty", 0.0)
                
                net_r = gross_r - pair_cost - corr_penalty
                trades.append({
                    **trade,
                    "exit_idx": i,
                    "exit_time": str(ts[i]),
                    "exit_price": exit_px,
                    "exit_reason": reason,
                    "gross_r": round(gross_r, 4),
                    "pnl_r": round(net_r, 4),
                    "bars_held": bars_held,
                    "cost_r": pair_cost,
                    "corr_penalty_r": corr_penalty,
                })
                trade = None
                bias = None
                setup_ready = False
            continue

        # ---- bias (HTF) ---------------------------------------------------
        if bias is None:
            # Allow TRENDING, PULLBACK, CONTINUATION for bias
            if htf_phase[i] in (Phase.TRENDING_BULL.value, Phase.CONTINUATION_BULL.value, Phase.PULLBACK_BULL.value):
                bias = "LONG"
            elif htf_phase[i] in (Phase.TRENDING_BEAR.value, Phase.CONTINUATION_BEAR.value, Phase.PULLBACK_BEAR.value):
                bias = "SHORT"
            if bias and debug and counters["bias"] < 5:
                counters["bias"] += 1
                print(f"    [{pair} {set_key}] bias={bias} @ {ts[i]} htf_phase={htf_phase[i]}")

        if bias is None:
            continue

        # ---- MTF setup ----------------------------------------------------
        if not setup_ready:
            p = mtf_pz[i]
            if p is None:
                continue
            # MTF must show pullback/contra-trend move in direction of HTF bias
            if bias == "LONG" and mtf_phase[i] in (
                Phase.PULLBACK_BULL.value, Phase.TRENDING_BULL.value, Phase.CONTINUATION_BULL.value
            ) and p >= -0.7:
                setup_ready = True
            elif bias == "SHORT" and mtf_phase[i] in (
                Phase.PULLBACK_BEAR.value, Phase.TRENDING_BEAR.value, Phase.CONTINUATION_BEAR.value
            ) and p <= 0.7:
                setup_ready = True
            if setup_ready and debug and counters["setup"] < 5:
                counters["setup"] += 1
                print(f"    [{pair} {set_key}] setup ok @ {ts[i]} mtf_phase={mtf_phase[i]} pz={p}")

        if not setup_ready:
            continue

        # ---- LTF trigger with CONFLUENCE ----------------------------------
        lf = ltf_phase[i]
        ltf_pz_val = ltf_pz_col[i] if i < len(ltf_pz_col) else None
        vwma = ltf_vwma[i] if i < len(ltf_vwma) else None
        
        # Directional phase filter
        if bias == "LONG":
            phase_ok = lf in (
                Phase.TRENDING_BULL.value,
                Phase.PULLBACK_BULL.value,
                Phase.CONTINUATION_BULL.value,
            )
        else:
            phase_ok = lf in (
                Phase.TRENDING_BEAR.value,
                Phase.PULLBACK_BEAR.value,
                Phase.CONTINUATION_BEAR.value,
            )
        
        if not phase_ok:
            continue
        
        # CONFLUENCE CHECK: LTF PZ must be at discount (LONG) or premium (SHORT)
        # i.e., price pulled back to favorable zone
        confluence_ok = True
        if ltf_pz_val is not None:
            if bias == "LONG" and ltf_pz_val > 0.3:  # Not at discount
                confluence_ok = False
            elif bias == "SHORT" and ltf_pz_val < -0.3:  # Not at premium
                confluence_ok = False
        
        # Additional: price should be near VWMA (fair value) or key zone
        # (simplified: require PZ within reasonable range)
        if ltf_pz_val is not None and abs(ltf_pz_val) > 2.0:
            confluence_ok = False  # Too extreme
        
        if not confluence_ok:
            counters["rejected"] += 1
            continue

        stop_dist = 1.5 * atr
        if bias == "LONG":
            stop_price = px - 1.5 * atr
            target_price = px + 1.5 * atr * RR_FLOOR
        else:
            stop_price = px + 1.5 * atr
            target_price = px - 1.5 * atr * RR_FLOOR

        # Correlation penalty: check active trades in same direction on same pair
        # (We'll compute this in run_pair where we have access to all sets)
        correlation_penalty = 0.0  # placeholder, will be set by run_pair
        
        trade = {
            "trade_id": f"{pair}-{set_key}-{i}",
            "pair": pair,
            "set_key": set_key,
            "direction": bias,
            "entry_idx": i,
            "entry_time": str(ts[i]),
            "entry_price": round(px, 6),
            "stop_price": round(stop_price, 6),
            "target_price": round(target_price, 6),
            "rr_planned": RR_FLOOR,
            "correlation_penalty": correlation_penalty,
        }
        if debug and counters["entry"] < 5:
            counters["entry"] += 1
            print(f"    [{pair} {set_key}] ENTRY {bias} @ {ts[i]} {px:.5f} pz={ltf_pz_val}")

    if debug:
        print(f"    [DEBUG] {set_key} counters: {counters}")

    return trades


# ---------------------------------------------------------------------------
# Portfolio-level runner
# ---------------------------------------------------------------------------
def run_pair(pair: str, tf_data: Dict[str, pl.DataFrame], max_base_bars: int, debug: bool) -> dict:
    """Run all 5 sets in sync on shared 5M clock with correlation tracking."""
    base = align_to_base(tf_data, "5M")
    if debug:
        print(f"  [DEBUG] base columns: {base.columns}")
        print(f"  [DEBUG] base shape: {base.shape}")
    if max_base_bars and len(base) > max_base_bars:
        base = base.tail(max_base_bars)

    # Build set-specific DataFrames
    set_dfs = {}
    for set_key, tfs in SET_DEFINITIONS.items():
        htf, mtf, ltf = tfs["HTF"], tfs["MTF"], tfs["LTF"]
        need = [
            f"phase_{htf}", f"pz_{htf}",
            f"phase_{mtf}", f"pz_{mtf}",
            f"phase_{ltf}", f"atr_{ltf}", f"close_{ltf}",
            f"pz_{ltf}", f"vwma_{ltf}",  # for confluence
        ]
        if debug:
            missing = [c for c in need if c not in base.columns]
            if missing:
                print(f"    {set_key}: MISSING {missing}")
            else:
                print(f"    {set_key}: all columns present")
        if any(c not in base.columns for c in need):
            continue

        set_df = base.select([
            "timestamp",
            f"phase_{htf}", f"pz_{htf}",
            f"phase_{mtf}", f"pz_{mtf}",
            f"phase_{ltf}", f"atr_{ltf}", f"close_{ltf}",
            f"pz_{ltf}", f"vwma_{ltf}",
        ]).rename({
            f"phase_{htf}": "htf_phase", f"pz_{htf}": "htf_pz",
            f"phase_{mtf}": "mtf_phase", f"pz_{mtf}": "mtf_pz",
            f"phase_{ltf}": "ltf_phase", f"atr_{ltf}": "ltf_atr",
            f"close_{ltf}": "ltf_close", f"pz_{ltf}": "ltf_pz",
            f"vwma_{ltf}": "ltf_vwma",
        })
        set_dfs[set_key] = (set_df, tfs["HTF"], tfs["MTF"], tfs["LTF"])

    # Synchronized simulation: step through 5M bars, track all sets
    n_bars = len(base)
    if max_base_bars:
        n_bars = min(n_bars, max_base_bars)

    # State per set
    set_states = {}
    all_trades = []
    
    for set_key in set_dfs:
        set_states[set_key] = {
            "bias": None,
            "setup_ready": False,
            "active_trade": None,
            "trades": [],
            "counters": {"bias": 0, "setup": 0, "entry": 0, "rejected": 0},
        }

    # Main simulation loop
    for i in range(n_bars):
        # First: manage existing trades (check exits)
        for set_key, state in set_states.items():
            trade = state["active_trade"]
            if trade is None:
                continue
            
            # Get LTF data for this set at current bar
            set_df, htf_tf, mtf_tf, ltf_tf = set_dfs[set_key]
            if i >= len(set_df):
                continue
            
            row = set_df.row(i, named=True)
            px = row["ltf_close"]
            atr = row["ltf_atr"]
            mtf_phase_val = row["mtf_phase"]
            
            if px is None or atr is None or atr <= 0:
                continue
            
            bars_held = i - trade["entry_idx"]
            
            # Check exits
            hit_sl = (px <= trade["stop_price"]) if trade["direction"] == "LONG" else (px >= trade["stop_price"])
            hit_tp = (px >= trade["target_price"]) if trade["direction"] == "LONG" else (px <= trade["target_price"])
            
            # MTF trailing
            mtf_against = False
            if trade["direction"] == "LONG" and mtf_phase_val in (Phase.TRANSITION.value, Phase.PULLBACK_BEAR.value):
                mtf_against = True
            elif trade["direction"] == "SHORT" and mtf_phase_val in (Phase.TRANSITION.value, Phase.PULLBACK_BULL.value):
                mtf_against = True
            
            exit_px = None
            reason = ""
            if hit_sl:
                exit_px, reason = trade["stop_price"], "STOP"
            elif hit_tp:
                exit_px, reason = trade["target_price"], "TARGET"
            elif mtf_against and bars_held > 20:
                exit_px, reason = px, "MTF_TRAIL"
            elif bars_held >= MAX_BARS_IN_TRADE:
                exit_px, reason = px, "TIME"
            
            if exit_px is not None:
                risk = abs(trade["entry_price"] - trade["stop_price"])
                move = (exit_px - trade["entry_price"]) if trade["direction"] == "LONG" else (trade["entry_price"] - exit_px)
                gross_r = move / risk if risk > 0 else 0.0
                pair_cost = COST_R_PER_TRADE.get(pair, COST_R_PER_TRADE["DEFAULT"])
                corr_penalty = trade.get("correlation_penalty", 0.0)
                net_r = gross_r - pair_cost - corr_penalty
                
                completed_trade = {
                    **trade,
                    "exit_idx": i,
                    "exit_time": str(set_df["timestamp"][i]),
                    "exit_price": exit_px,
                    "exit_reason": reason,
                    "gross_r": round(gross_r, 4),
                    "pnl_r": round(net_r, 4),
                    "bars_held": bars_held,
                    "cost_r": pair_cost,
                    "corr_penalty_r": corr_penalty,
                }
                state["trades"].append(completed_trade)
                all_trades.append(completed_trade)
                state["active_trade"] = None
                state["bias"] = None
                state["setup_ready"] = False

        # Second: check for new entries (only if no active trade)
        for set_key, state in set_states.items():
            if state["active_trade"] is not None:
                continue
            
            set_df, htf_tf, mtf_tf, ltf_tf = set_dfs[set_key]
            if i >= len(set_df):
                continue
            
            row = set_df.row(i, named=True)
            px = row["ltf_close"]
            atr = row["ltf_atr"]
            if px is None or atr is None or atr <= 0:
                continue
            
            htf_phase = row["htf_phase"]
            mtf_phase = row["mtf_phase"]
            ltf_phase = row["ltf_phase"]
            mtf_pz = row["mtf_pz"]
            htf_pz = row["htf_pz"]
            ltf_pz = row["ltf_pz"]
            
            # ---- bias (HTF) ----
            if state["bias"] is None:
                if htf_phase in (Phase.TRENDING_BULL.value, Phase.CONTINUATION_BULL.value, Phase.PULLBACK_BULL.value):
                    state["bias"] = "LONG"
                elif htf_phase in (Phase.TRENDING_BEAR.value, Phase.CONTINUATION_BEAR.value, Phase.PULLBACK_BEAR.value):
                    state["bias"] = "SHORT"
                if state["bias"] and debug and state["counters"]["bias"] < 5:
                    state["counters"]["bias"] += 1
            
            if state["bias"] is None:
                continue
            
            # ---- MTF setup ----
            if not state["setup_ready"]:
                p = mtf_pz
                if p is not None:
                    if state["bias"] == "LONG" and mtf_phase in (
                        Phase.PULLBACK_BULL.value, Phase.TRENDING_BULL.value, Phase.CONTINUATION_BULL.value
                    ) and p >= -0.7:
                        state["setup_ready"] = True
                    elif state["bias"] == "SHORT" and mtf_phase in (
                        Phase.PULLBACK_BEAR.value, Phase.TRENDING_BEAR.value, Phase.CONTINUATION_BEAR.value
                    ) and p <= 0.7:
                        state["setup_ready"] = True
            
            if not state["setup_ready"]:
                continue
            
            # ---- LTF trigger with CONFLUENCE ----
            lf = ltf_phase
            ltf_pz_val = ltf_pz
            
            if state["bias"] == "LONG":
                phase_ok = lf in (Phase.TRENDING_BULL.value, Phase.PULLBACK_BULL.value, Phase.CONTINUATION_BULL.value)
            else:
                phase_ok = lf in (Phase.TRENDING_BEAR.value, Phase.PULLBACK_BEAR.value, Phase.CONTINUATION_BEAR.value)
            
            if not phase_ok:
                continue
            
            # Confluence: PZ at discount/premium
            confluence_ok = True
            if ltf_pz_val is not None:
                if state["bias"] == "LONG" and ltf_pz_val > 0.3:
                    confluence_ok = False
                elif state["bias"] == "SHORT" and ltf_pz_val < -0.3:
                    confluence_ok = False
            
            if ltf_pz_val is not None and abs(ltf_pz_val) > 2.0:
                confluence_ok = False
            
            if not confluence_ok:
                state["counters"]["rejected"] += 1
                continue
            
            # Calculate correlation penalty: count other active trades in same direction
            aligned_count = 0
            for other_key, other_state in set_states.items():
                if other_key != set_key and other_state["active_trade"] is not None:
                    if other_state["active_trade"]["direction"] == state["bias"]:
                        aligned_count += 1
            
            correlation_penalty = CORRELATION_PENALTY_STEP * aligned_count
            
            stop_dist = 1.5 * atr
            if state["bias"] == "LONG":
                stop_price = px - 1.5 * atr
                target_price = px + 1.5 * atr * RR_FLOOR
            else:
                stop_price = px + 1.5 * atr
                target_price = px - 1.5 * atr * RR_FLOOR
            
            trade = {
                "trade_id": f"{pair}-{set_key}-{i}",
                "pair": pair,
                "set_key": set_key,
                "direction": state["bias"],
                "entry_idx": i,
                "entry_time": str(set_df["timestamp"][i]),
                "entry_price": round(px, 6),
                "stop_price": round(stop_price, 6),
                "target_price": round(target_price, 6),
                "rr_planned": RR_FLOOR,
                "correlation_penalty": correlation_penalty,
            }
            state["active_trade"] = trade
            state["counters"]["entry"] += 1

    # Collect results
    result = {
        "pair": pair,
        "base_bars": n_bars,
        "sets": {},
        "total_trades": len(all_trades),
        "gross_pnl_r": sum(t["gross_r"] for t in all_trades),
        "net_pnl_r": sum(t["pnl_r"] for t in all_trades),
    }
    
    for set_key, state in set_states.items():
        trades = state["trades"]
        if not trades:
            result["sets"][set_key] = {"trades": 0}
            continue
        
        pnls = [t["pnl_r"] for t in trades]
        gross = [t["gross_r"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
        
        result["sets"][set_key] = {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades), 4),
            "gross_pnl_r": round(sum(gross), 4),
            "net_pnl_r": round(sum(pnls), 4),
            "expectancy_r": round(sum(pnls) / len(trades), 4),
            "profit_factor": round(pf, 4) if pf != float("inf") else 999.0,
            "avg_bars_held": round(sum(t["bars_held"] for t in trades) / len(trades), 1),
            "exit_mix": {
                r: sum(1 for t in trades if t["exit_reason"] == r)
                for r in ("STOP", "TARGET", "TIME", "MTF_TRAIL")
            },
            "trades_detail": trades,
        }
    
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Correlated MTF fractal backtest")
    ap.add_argument("--pairs", default="GBPUSD,EURUSD,USDJPY,AUDUSD")
    ap.add_argument("--data-path", default="data/cache/dukascopy")
    ap.add_argument("--output", default="research/results/correlated_fast_backtest.json")
    ap.add_argument("--max-bars", type=int, default=0,
                    help="Limit base 5M bars (0 = all)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",")]
    report = {
        "concept": "correlated_mtf_fractal",
        "cost_model_r_per_trade": COST_R_PER_TRADE,
        "rr_floor": RR_FLOOR,
        "pairs": {},
        "summary": {},
    }

    tot_trades = 0
    tot_net = 0.0

    for pair in pairs:
        print(f"\n[BACKTEST] {pair}")
        tf_data = load_pair_data(pair, args.data_path)
        tf5 = tf_data.get("5M")
        if tf5 is None or tf5.is_empty():
            print(f"  {pair}: no 5M data, skipping")
            continue
        res = run_pair(pair, tf_data, args.max_bars, args.debug)
        report["pairs"][pair] = res
        print(f"  -> {res['total_trades']} trades | net {res['net_pnl_r']:+.2f}R")
        for sk, m in res["sets"].items():
            if m.get("trades"):
                print(f"     {sk}: {m['trades']} tr | WR {m['win_rate']:.1%} | "
                      f"PF {m['profit_factor']:.2f} | net {m['net_pnl_r']:+.2f}R")
        tot_trades += res["total_trades"]
        tot_net += res["net_pnl_r"]

    # Aggregate by set
    by_set: Dict[str, dict] = {}
    for pair_res in report["pairs"].values():
        for sk, m in pair_res["sets"].items():
            if not m.get("trades"):
                continue
            agg = by_set.setdefault(sk, {"trades": 0, "net_pnl_r": 0.0, "wins": 0})
            agg["trades"] += m["trades"]
            agg["net_pnl_r"] += m["net_pnl_r"]
            agg["wins"] += m["wins"]

    for sk, agg in by_set.items():
        agg["win_rate"] = round(agg["wins"] / agg["trades"], 4)
        agg["expectancy_r"] = round(agg["net_pnl_r"] / agg["trades"], 4)

    report["summary"] = {
        "pairs_tested": list(report["pairs"].keys()),
        "total_trades": tot_trades,
        "total_net_pnl_r": round(tot_net, 4),
        "by_set": by_set,
        "verdict": "NO_TRADES" if tot_trades == 0 else (
            "NET_POSITIVE" if tot_net > 0 else "NET_NEGATIVE"
        ),
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"TOTAL: {tot_trades} trades | net {tot_net:+.2f}R | "
          f"{report['summary']['verdict']}")
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
