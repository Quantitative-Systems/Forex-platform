#!/usr/bin/env python3
"""
Full backtest for all Concord combinations using PrecomputedPerSetStrategy.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import the strategy and backtest function
from research.bt_precomputed3 import run_backtest, PrecomputedPerSetStrategy

CONCORD_COMBINATIONS = [
    ("EURUSD", "SET_5"),
    ("GBPUSD", "SET_4"),
    ("GBPUSD", "SET_5"),
    ("USDJPY", "SET_3"),
    ("USDJPY", "SET_4"),
    ("USDJPY", "SET_5"),
    ("AUDUSD", "SET_3"),
    ("AUDUSD", "SET_4"),
    ("AUDUSD", "SET_5"),
]

def run_all():
    results = {}
    total_trades = 0
    total_pnl = 0.0
    
    print("=" * 70)
    print("CONCORD FULL BACKTEST")
    print("=" * 70)
    
    for pair, set_key in CONCORD_COMBINATIONS:
        try:
            # Use full dataset for key pairs, 100k for others
            if pair in ["EURUSD", "GBPUSD"] and set_key == "SET_5":
                max_bars = 207015  # Full dataset
            elif pair == "GBPUSD" and set_key == "SET_4":
                max_bars = 207015
            elif pair == "AUDUSD" and set_key in ["SET_4", "SET_5"]:
                max_bars = 207015
            else:
                max_bars = 100000
            res, strat = run_backtest(pair, set_key, max_bars=max_bars)
            results[f"{pair}_{set_key}"] = {
                "trades": res.total_trades,
                "net_pnl": float(res.total_net_pnl),
                "win_rate": res.win_rate,
                "pf": res.profit_factor,
            }
            total_trades += res.total_trades
            total_pnl += float(res.total_net_pnl)
            print(f"  {pair} {set_key}: {res.total_trades} trades, ${float(res.total_net_pnl):,.2f}, WR={res.win_rate:.1%}, PF={res.profit_factor:.2f}")
        except Exception as e:
            print(f"  {pair} {set_key}: ERROR - {e}")
            results[f"{pair}_{set_key}"] = {"error": str(e)}
        print()
    
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for key, r in results.items():
        if "error" not in r:
            print(f"  {key}: {r['trades']} trades, ${r['net_pnl']:,.0f}, WR={r['win_rate']:.1%}, PF={r['pf']:.2f}")
        else:
            print(f"  {key}: ERROR - {r['error']}")
    
    print(f"\n  TOTAL: {total_trades} trades, ${total_pnl:,.0f}")
    
    # Compare with per_set_native.py baseline
    print("\n" + "=" * 70)
    print("COMPARISON WITH per_set_native.py BASELINE")
    print("=" * 70)
    
    baseline = {
        ("EURUSD", "SET_5"): 19.76,
        ("GBPUSD", "SET_4"): 3.26,
        ("GBPUSD", "SET_5"): 50.16,
        ("USDJPY", "SET_3"): 8.20,
        ("USDJPY", "SET_4"): 12.80,
        ("USDJPY", "SET_5"): 13.12,
        ("AUDUSD", "SET_3"): 6.40,
        ("AUDUSD", "SET_4"): 14.80,
        ("AUDUSD", "SET_5"): 60.29,
    }
    
    for (pair, set_key), base_r in baseline.items():
        key = f"{pair}_{set_key}"
        if key in results and "error" not in results[key]:
            our_r = results[key]["net_pnl"] / 1000  # Convert $ to R (assuming 1% risk = $1000)
            diff = our_r - base_r
            print(f"  {key}: baseline={base_r:.2f}R, ours={our_r:.2f}R, diff={diff:+.2f}R")

if __name__ == "__main__":
    run_all()