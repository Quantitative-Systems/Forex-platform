#!/usr/bin/env python3
"""
Paper Trading Validation for Concord Strategy.
==============================================
Runs the PrecomputedPerSetStrategy through the ForwardPaperTradingDaemon
with simulated live data feed.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "research" / "correlated_engine"))

import polars as pl
import numpy as np

from forex_platform.core.domain import CurrencyPair, OrderSide
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.order_management.router import SmartOrderRouter
from forex_platform.paper_trading.daemon import ForwardPaperTradingDaemon
from forex_platform.paper_trading.persistence import SQLitePaperLedger
from forex_platform.paper_trading.simulator import MicrostructurePaperSimulator
from forex_platform.research_engine.backtester import EventDrivenBacktester
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import MarketTelemetry, PreTradeRiskFirewall
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch
from forex_platform.strategy_engine.base import BarEvent

# Import our strategy
from research.bt_precomputed3 import PrecomputedPerSetStrategy, run_backtest

# Concord combinations
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


def load_and_precompute(pair: str, set_key: str, data_path: str = "data/cache/dukascopy"):
    """Load and precompute aligned data for a pair/set."""
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
    
    mtf_path = Path(data_path) / "mtf"
    tf_data = {}
    for tf in ["1M", "1W", "1D", "4H", "1H", "15M", "5M"]:
        fp = mtf_path / f"{pair}_{tf}.parquet"
        if not fp.exists():
            tf_data[tf] = pl.DataFrame()
            continue
        df = pl.read_parquet(fp)
        if "open_time" in df.columns:
            df = df.drop("timestamp").rename({"open_time": "timestamp"})
        elif df["timestamp"].dtype != pl.Datetime:
            df = df.with_columns(pl.col("timestamp").cast(pl.Datetime("ns", "UTC")))
        df = compute_all_indicators(df)
        tf_data[tf] = precompute(df)
    
    spec = SET_DEFINITIONS[set_key]
    htf, mtf, ltf = spec["HTF"], spec["MTF"], spec["LTF"]
    
    cols = ["timestamp", "open", "high", "low", "close", "volume", "avg_spread", "phase", "pz", "atr_14", "vwma_50"]
    ltf_df = tf_data[ltf].select(cols).rename(
        {"avg_spread": "spread", "phase": "ltf_phase", "pz": "ltf_pz", "atr_14": "ltf_atr",
         "vwma_50": "ltf_vwma"})
    base = ltf_df.clone()
    for label, tf in (("htf", htf), ("mtf", mtf)):
        if tf_data.get(tf) is None or tf_data[tf].is_empty():
            continue
        df = tf_data[tf].select(["timestamp", "phase", "pz", "atr_14", "close", "vwma_50"])
        df = df.rename({"phase": f"{label}_phase", "pz": f"{label}_pz",
                        "atr_14": f"{label}_atr", "close": f"{label}_close",
                        "vwma_50": f"{label}_vwma"})
        base = base.join_asof(df, on="timestamp", strategy="backward")
    
    return base


def run_paper_validation(pair: str, set_key: str, max_bars: int = 20000):
    """Run paper trading validation for a pair/set combination."""
    print(f"\n{'='*70}")
    print(f" PAPER VALIDATION: {pair} {set_key}")
    print(f"{'='*70}")
    
    # Load precomputed data
    print(f"Loading and precomputing data...")
    aligned = load_and_precompute(pair, set_key)
    aligned = aligned.head(max_bars) if max_bars else aligned
    print(f"Aligned bars: {len(aligned)}")
    
    # Create strategy
    strat = PrecomputedPerSetStrategy(
        symbols=[pair],
        selected_set=set_key,
        account_equity=100000,
        risk_fraction=0.01,
        minimum_net_reward_risk=4.0,
        session_filter=False,
    )
    strat.set_precomputed_data(aligned, pair)
    
    # Create paper trading components
    kill_switch = HierarchicalKillSwitch()
    cb = CircuitBreakerEngine(initial_equity=Decimal("100000"))
    firewall = PreTradeRiskFirewall(kill_switch=kill_switch, circuit_breaker=cb)
    router = SmartOrderRouter(allow_live_routing=False)
    oms = OrderManagementSystem(
        firewall=firewall,
        router=router,
        kill_switch=kill_switch,
        initial_balance=Decimal("100000"),
    )
    sim = MicrostructurePaperSimulator()
    ledger = SQLitePaperLedger(db_path=Path(f"results/paper_{pair}_{set_key}.db"))
    daemon = ForwardPaperTradingDaemon(
        strategy=strat,
        oms=oms,
        simulator=sim,
        ledger=ledger,
        currency_pair=CurrencyPair.from_symbol(pair),
    )
    
    # Run paper trading simulation
    print(f"Running paper trading simulation ({len(aligned)} bars)...")
    
    trades_executed = 0
    for i, row in enumerate(aligned.iter_rows(named=True)):
        ts = row["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        
        bar = BarEvent(
            symbol=pair,
            timeframe=Timeframe.M1,  # Source timeframe
            timestamp=ts,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row.get("volume", 0))),
        )
        
        spread_val = row.get("spread", row.get("avg_spread", 1.0))
        bid = Decimal(str(row["close"])) - Decimal(str(spread_val)) / Decimal("20000")
        ask = Decimal(str(row["close"])) + Decimal(str(spread_val)) / Decimal("20000")
        telemetry = MarketTelemetry(
            symbol=pair, bid=bid, ask=ask, quote_timestamp=ts,
            recent_spreads_pips=[Decimal(str(spread_val))] * 20
        )
        
        fills = daemon.on_tick_or_bar(bar, telemetry)
        trades_executed += len(fills)
        
        if i % 5000 == 0 and i > 0:
            print(f"  Processed {i}/{len(aligned)} bars...")
    
    # Results
    account = oms.get_account_summary()
    fills = list(oms._fills.values()) if hasattr(oms, '_fills') else []
    orders = list(oms._orders.values())
    
    print(f"\n  Results:")
    print(f"  Orders: {len(orders)} | Fills: {len(fills)}")
    print(f"  Equity: ${float(account.equity):,.2f} | Realized PnL: ${float(account.realized_pnl):,.2f}")
    print(f"  Strategy dispatched: {strat.count_orders_dispatched}")
    print(f"  Telemetry: htf_bias={strat.count_htf_bias_active}, mtf_setup={strat.count_mtf_setup_aligned}, ltf_trigger={strat.count_ltf_trigger_fired}")
    
    # Recover and verify
    print(f"\n  Testing crash recovery...")
    new_ledger = SQLitePaperLedger(db_path=Path(f"results/paper_{pair}_{set_key}.db"))
    recovered_orders, recovered_positions, balance = new_ledger.recover_state()
    print(f"  Recovered: {len(recovered_orders)} orders, {len(recovered_positions)} positions, balance=${float(balance):,.2f}")
    
    return {
        "pair": pair,
        "set": set_key,
        "bars": len(aligned),
        "orders": len(orders),
        "fills": len(fills),
        "equity": float(account.equity),
        "realized_pnl": float(account.realized_pnl),
        "strategy_dispatched": strat.count_orders_dispatched,
    }


def main():
    print("=" * 70)
    print(" CONCORD STRATEGY - PAPER TRADING VALIDATION")
    print("=" * 70)
    
    results = []
    for pair, set_key in CONCORD_COMBINATIONS:
        try:
            # Use smaller sample for faster validation
            max_bars = 5000 if pair in ["EURUSD", "GBPUSD"] else 10000
            result = run_paper_validation(pair, set_key, max_bars)
            results.append(result)
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"pair": pair, "set": set_key, "error": str(e)})
    
    print("\n" + "=" * 70)
    print(" PAPER VALIDATION SUMMARY")
    print("=" * 70)
    for r in results:
        if "error" not in r:
            print(f"  {r['pair']} {r['set']}: {r['fills']} fills, ${r['realized_pnl']:,.2f} PnL, {r['strategy_dispatched']} signals")
        else:
            print(f"  {r['pair']} {r['set']}: ERROR - {r['error']}")


if __name__ == "__main__":
    main()