#!/usr/bin/env python3
"""
Per-Set Native LTF Backtest
===========================
Tests each SET independently on its OWN LTF timeframe:
  SET_1 on 1D (868 bars/yr)  -- 868 bars total
  SET_2 on 4H (4462 bars/yr) -- 4462 bars total
  SET_3 on 1H (17257 bars/yr)-- 17257 bars total
  SET_4 on 15M (69013 bars/yr)-- 69013 bars total
  SET_5 on 5M (206960 bars/yr)-- 206960 bars total

Same logic for all sets: HTF bias -> MTF setup -> LTF trigger -> confluence ->
                        HTF target (RR=4), 1.5 ATR stop, MTF trailing.
Costs applied per pair.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import polars as pl

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from indicators import compute_all_indicators, detect_swings, detect_market_structure


class Phase:
    TB = 1; TRB = 2; PB = 3; PRB = 4; CB = 5; CRB = 6; CH = 7; TR = 8

PHASE = Phase()

SET_DEFINITIONS = {
    "SET_1": {"HTF": "1M", "MTF": "1W", "LTF": "1D"},
    "SET_2": {"HTF": "1W", "MTF": "1D", "LTF": "4H"},
    "SET_3": {"HTF": "1D", "MTF": "4H", "LTF": "1H"},
    "SET_4": {"HTF": "4H", "MTF": "1H", "LTF": "15M"},
    "SET_5": {"HTF": "1H", "MTF": "15M", "LTF": "5M"},
}

COSTS = {"EURUSD": 0.65, "GBPUSD": 0.75, "USDJPY": 0.70, "AUDUSD": 0.80, "DEFAULT": 0.80}
RR = 4.0
MAX_BARS_IN_TRADE = 500


def detect_phase(structure, pz, prev):
    if pz is None or pz != pz:
        return Phase.CH
    trend = structure.get("trend", "MIXED")
    stype = structure.get("structure", "CHOP")
    if trend == "BULLISH" and stype == "TRENDING":
        return (Phase.PB if pz < -0.3 else Phase.TB if pz <= 0.3 else
                Phase.CB if prev in (Phase.PB, Phase.TR) else Phase.TB)
    if trend == "BEARISH" and stype == "TRENDING":
        return (Phase.PRB if pz > 0.3 else Phase.TRB if pz >= -0.3 else
                Phase.CRB if prev in (Phase.PRB, Phase.TR) else Phase.TRB)
    return Phase.CH


def precompute(df, roll_window=60):
    if df.is_empty() or len(df) < 50:
        return df.with_columns([pl.lit("MIXED").alias("trend"),
                               pl.lit("CHOP").alias("struct_type"),
                               pl.lit(Phase.CH, dtype=pl.Int32).alias("phase")])
    n = len(df)
    arr_t = np.full(n, "MIXED", dtype=object)
    arr_s = np.full(n, "CHOP", dtype=object)
    for start in range(0, n, roll_window):
        end = min(start + roll_window + 20, n)
        w = df.slice(max(0, start - roll_window), end - max(0, start - roll_window))
        sh, sl = detect_swings(w, lookback=3)
        s = detect_market_structure(sh, sl)
        arr_t[start:end] = s.get("trend", "MIXED")
        arr_s[start:end] = s.get("structure", "CHOP")
    df = df.with_columns([pl.Series("trend", arr_t), pl.Series("struct_type", arr_s)])
    phases = np.empty(n, dtype=np.int32)
    prev = Phase.CH
    for i in range(n):
        prev = detect_phase({"trend": arr_t[i], "structure": arr_s[i]}, df["pz"][i], prev)
        phases[i] = prev
    return df.with_columns(pl.Series("phase", phases))


def load_tfs(pair, data_path):
    mtf = Path(data_path) / "mtf"
    out = {}
    for tf in ["1M", "1W", "1D", "4H", "1H", "15M", "5M"]:
        fp = mtf / f"{pair}_{tf}.parquet"
        if not fp.exists():
            out[tf] = pl.DataFrame()
            continue
        df = pl.read_parquet(fp)
        if "open_time" in df.columns:
            df = df.drop("timestamp").rename({"open_time": "timestamp"})
        elif df["timestamp"].dtype != pl.Datetime:
            df = df.with_columns(pl.col("timestamp").cast(pl.Datetime("ns", "UTC")))
        df = compute_all_indicators(df)
        out[tf] = precompute(df)
    return out


def align(tf_data, htf, mtf, ltf):
    cols = ["timestamp", "phase", "pz", "atr_14", "close", "vwma_50"]
    ltf_df = tf_data[ltf].select(cols).rename(
        {"phase": "ltf_phase", "pz": "ltf_pz", "atr_14": "ltf_atr",
         "close": "ltf_close", "vwma_50": "ltf_vwma"})
    base = ltf_df.clone()
    for label, tf in (("htf", htf), ("mtf", mtf)):
        if tf_data.get(tf) is None or tf_data[tf].is_empty():
            continue
        df = tf_data[tf].select(cols)
        df = df.rename({"phase": f"{label}_phase", "pz": f"{label}_pz",
                        "atr_14": f"{label}_atr", "close": f"{label}_close",
                        "vwma_50": f"{label}_vwma"})
        base = base.join_asof(df, on="timestamp", strategy="backward")
    return base


def run_set(set_key, pair, tf_data, debug=False):
    spec = SET_DEFINITIONS[set_key]
    htf, mtf, ltf = spec["HTF"], spec["MTF"], spec["LTF"]
    df = align(tf_data, htf, mtf, ltf)
    cost = COSTS.get(pair, COSTS["DEFAULT"])

    ts = df["timestamp"].to_list()
    hph = (df["htf_phase"].to_list() if "htf_phase" in df.columns else [Phase.CH] * len(df))
    hpz = (df["htf_pz"].to_list() if "htf_pz" in df.columns else [0.0] * len(df))
    mph = (df["mtf_phase"].to_list() if "mtf_phase" in df.columns else [Phase.CH] * len(df))
    mpz = (df["mtf_pz"].to_list() if "mtf_pz" in df.columns else [0.0] * len(df))
    lph = df["ltf_phase"].to_list()
    lcl = df["ltf_close"].to_list()
    lat = df["ltf_atr"].to_list()
    lpz = df["ltf_pz"].to_list()

    n = len(df)
    trades = []
    bias = setup = active = None
    counters = {"bias": 0, "setup": 0, "entry": 0, "rej": 0}

    for i in range(n):
        px, atr = lcl[i], lat[i]
        if px is None or atr is None or atr <= 0:
            continue

        # Manage open trade
        if active is not None:
            bh = i - active["ei"]
            sl = px <= active["sl"] if active["direction"] == "LONG" else px >= active["sl"]
            tp = px >= active["tp"] if active["direction"] == "LONG" else px <= active["tp"]
            mtf_a = ((active["direction"] == "LONG" and mph[i] in (Phase.TR, Phase.PRB)) or
                     (active["direction"] == "SHORT" and mph[i] in (Phase.TR, Phase.PB)))
            exit_px, reason = None, None
            if sl: exit_px, reason = active["sl"], "STOP"
            elif tp: exit_px, reason = active["tp"], "TARGET"
            elif mtf_a and bh > 20:
                exit_px, reason = px, "MTF_TRAIL"
            elif bh >= MAX_BARS_IN_TRADE:
                exit_px, reason = px, "TIME"
            if exit_px is not None:
                risk = abs(active["entry"] - active["sl"])
                move = exit_px - active["entry"] if active["direction"] == "LONG" else active["entry"] - exit_px
                gross = move / risk if risk > 0 else 0.0
                net = gross - cost
                trades.append({**active, "exit_idx": i, "exit_time": str(ts[i]), "exit_price": exit_px,
                               "exit_reason": reason, "gross_r": round(gross, 4), "pnl_r": round(net, 4),
                               "bars_held": bh})
                active = bias = setup = None
            continue

        # Bias
        if bias is None:
            if hph[i] in (Phase.TB, Phase.CB, Phase.PB):
                bias = "LONG"
            elif hph[i] in (Phase.TRB, Phase.CRB, Phase.PRB):
                bias = "SHORT"
            if bias and counters["bias"] < 5 and debug:
                counters["bias"] += 1
                print(f"    {pair} {set_key}: bias={bias} @ {ts[i]}  htf={hph[i]} pz={hpz[i]:.2f}")

        if bias is None:
            continue

        # MTF Setup
        if setup is None:
            p = mpz[i]
            if p is not None:
                if bias == "LONG" and mph[i] in (Phase.PB, Phase.TB, Phase.CB) and p >= -0.7:
                    setup = True
                elif bias == "SHORT" and mph[i] in (Phase.PRB, Phase.TRB, Phase.CRB) and p <= 0.7:
                    setup = True
            if setup is True and counters["setup"] < 5 and debug:
                counters["setup"] += 1
                print(f"    {pair} {set_key}: setup OK @ {ts[i]}  mtf={mph[i]} pz={p}")

        if setup is not True:
            continue

        # LTF Trigger + Confluence
        lf = lph[i]
        if bias == "LONG":
            phase_ok = lf in (Phase.TB, Phase.PB, Phase.CB)
        else:
            phase_ok = lf in (Phase.TRB, Phase.PRB, Phase.CRB)
        if not phase_ok:
            continue

        lp = lpz[i]
        conf_ok = True
        if lp is not None:
            if bias == "LONG" and (lp > 0.3 or abs(lp) > 2.0):
                conf_ok = False
            elif bias == "SHORT" and (lp < -0.3 or abs(lp) > 2.0):
                conf_ok = False
        if not conf_ok:
            counters["rej"] += 1
            continue

        # Entry
        stop = px - 1.5 * atr if bias == "LONG" else px + 1.5 * atr
        target = px + 1.5 * atr * RR if bias == "LONG" else px - 1.5 * atr * RR

        active = {"trade_id": f"{pair}-{set_key}-{i}", "pair": pair, "set_key": set_key,
                  "direction": bias, "entry_idx": i, "entry_time": str(ts[i]),
                  "entry": px, "sl": stop, "tp": target, "rr_planned": RR, "ei": i}
        if counters["entry"] < 3 and debug:
            counters["entry"] += 1
            print(f"    {pair} {set_key}: ENTRY {bias} @ {ts[i]} {px:.5f} pz={lp}")

    return trades, counters


def main():
    ap = argparse.ArgumentParser(description="Per-set native LTF backtest")
    ap.add_argument("--pairs", default="GBPUSD,EURUSD,USDJPY,AUDUSD")
    ap.add_argument("--sets", default="SET_1,SET_2,SET_3,SET_4,SET_5")
    ap.add_argument("--data-path", default="data/cache/dukascopy")
    ap.add_argument("--output", default="research/results/per_set_native_backtest.json")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",")]
    sets = [s.strip() for s in args.sets.split(",")]
    report = {"pairs": {}, "by_set": {}, "summary": {}}
    grand_trades, grand_net = 0, 0.0

    for pair in pairs:
        print(f"\n[PER-SET] {pair}")
        tfs = load_tfs(pair, args.data_path)
        report["pairs"].setdefault(pair, {})
        for sk in sets:
            tf = SET_DEFINITIONS[sk]["LTF"]
            bars = len(tfs[tf]) if tf in tfs and not tfs[tf].is_empty() else 0
            print(f"  -> {sk} (LTF={tf}, {bars} bars)")
            trades, counters = run_set(sk, pair, tfs, debug=args.debug)
            print(f"     counters: {counters}")
            if trades:
                pnls = [t["pnl_r"] for t in trades]
                wins = [p for p in pnls if p > 0]
                loss = [p for p in pnls if p <= 0]
                pf = sum(wins) / abs(sum(loss)) if loss else float("inf")
                m = {"trades": len(trades), "wins": len(wins), "losses": len(loss),
                     "win_rate": round(len(wins) / len(trades), 4),
                     "gross_pnl_r": round(sum(t["gross_r"] for t in trades), 4),
                     "net_pnl_r": round(sum(pnls), 4),
                     "expectancy_r": round(sum(pnls) / len(trades), 4),
                     "profit_factor": round(pf, 4) if pf != float("inf") else 999.0,
                     "avg_bars_held": round(sum(t["bars_held"] for t in trades) / len(trades), 1),
                     "exit_mix": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in
                                  {"STOP", "TARGET", "TIME", "MTF_TRAIL"}},
                     "trades_detail": trades, "counters": counters}
            else:
                m = {"trades": 0, "counters": counters}
            report["pairs"][pair][sk] = m
            if m.get("trades"):
                print(f"     {m['trades']} tr | WR {m['win_rate']:.1%} | "
                      f"PF {m['profit_factor']:.2f} | net {m['net_pnl_r']:+.2f}R")
                grand_trades += m["trades"]
                grand_net += m["net_pnl_r"]

    by_set = report["by_set"]
    for pair, d in report["pairs"].items():
        for sk, m in d.items():
            if not m.get("trades"):
                continue
            agg = by_set.setdefault(sk, {"trades": 0, "net_pnl_r": 0.0, "wins": 0})
            agg["trades"] += m["trades"]
            agg["net_pnl_r"] += m["net_pnl_r"]
            agg["wins"] += m["wins"]

    for sk, agg in by_set.items():
        agg["win_rate"] = round(agg["wins"] / agg["trades"], 4)
        agg["expectancy_r"] = round(agg["net_pnl_r"] / agg["trades"], 4)
        agg["verdict"] = "POSITIVE" if agg["net_pnl_r"] > 0 else "NEGATIVE"

    report["summary"] = {"pairs_tested": pairs, "sets_tested": sets,
                          "total_trades": grand_trades, "total_net_pnl_r": round(grand_net, 4),
                          "by_set": by_set,
                          "verdict": "NET_POSITIVE" if grand_net > 0 else "NET_NEGATIVE"}
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("\n" + "=" * 60)
    print("PER-SET NATIVE LTF BACKTEST")
    print("=" * 60)
    for sk in sets:
        agg = by_set.get(sk, {"trades": 0, "net_pnl_r": 0, "win_rate": 0, "verdict": "NO_TRADES"})
        if agg["trades"] > 0:
            print(f"  {sk}: {agg['trades']:>4} tr | WR {agg['win_rate']:.1%} | "
                  f"exp {agg['expectancy_r']:+.3f}R | net {agg['net_pnl_r']:+8.2f}R | {agg['verdict']}")
        else:
            print(f"  {sk}: no trades")
    print(f"\nTOTAL: {grand_trades} trades | net {grand_net:+.2f}R | {report['summary']['verdict']}")
    print(f"Written: {out}")


if __name__ == "__main__":
    main()
