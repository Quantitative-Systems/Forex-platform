"""Style x Set Matrix Experiment — is Chintu right?

Hypothesis under test: trading *style* is a function of candle count and
structure depth per set, not clock-time. We therefore test ALL 25
combinations of {set} x {style} on the same concept stack
(structure -> keyzone -> phase / HTF bias -> MTF setup -> LTF entry),
and let the data decide which style fits which set.

Styles are parameterized as (target floor, trailing behavior, location
discipline) variations sharing identical doctrine (Law 1-4 of the
Knowledge Core). The matrix reports, per cell: trades, win rate, mean R,
expectancy, PF, max DD, and PASS/FAIL against engine gates.

This produces the scientific answer: "which style actually works on which
set" - anything else is guessing.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path
from typing import Optional

import polars as pl

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.strategy_engine.base import BarEvent
from forex_platform.strategy_engine.fractal_institutional import InstitutionalFractalStrategy
from forex_platform.fractal_engine.timeframes import TIMEFRAME_SETS


# ---------------------------------------------------------------------------
# Style parameterizations — SAME doctrine, different execution flavor
# ---------------------------------------------------------------------------

STYLES: dict[str, dict] = {
    # Few candles, deep structure: wide stops/targets, patient persistence
    "SCALPING": dict(
        minimum_net_reward_risk=Decimal("2.0"),
        setup_persistence_bars=3,
        stop_buffer_pips=Decimal("1.0"),
        minimum_stop_pips=Decimal("2"),
        maximum_stop_pips=Decimal("15"),
        max_bars_in_trade=24,          # ~1.2h on 3M
    ),
    "INTRADAY": dict(
        minimum_net_reward_risk=Decimal("3.0"),
        setup_persistence_bars=4,
        stop_buffer_pips=Decimal("1.5"),
        minimum_stop_pips=Decimal("2"),
        maximum_stop_pips=Decimal("30"),
        max_bars_in_trade=80,          # ~4h on 3M
    ),
    "SWING": dict(
        minimum_net_reward_risk=Decimal("4.0"),
        setup_persistence_bars=6,
        stop_buffer_pips=Decimal("2.0"),
        minimum_stop_pips=Decimal("3"),
        maximum_stop_pips=Decimal("60"),
        max_bars_in_trade=320,         # ~16h on 3M
    ),
    "POSITIONAL": dict(
        minimum_net_reward_risk=Decimal("5.0"),
        setup_persistence_bars=8,
        stop_buffer_pips=Decimal("3.0"),
        minimum_stop_pips=Decimal("5"),
        maximum_stop_pips=Decimal("120"),
        max_bars_in_trade=1440,        # ~3 days on 3M
    ),
    "INVESTING": dict(
        minimum_net_reward_risk=Decimal("6.0"),
        setup_persistence_bars=12,
        stop_buffer_pips=Decimal("4.0"),
        minimum_stop_pips=Decimal("8"),
        maximum_stop_pips=Decimal("250"),
        max_bars_in_trade=6720,        # ~2 weeks on 3M
    ),
}

GATES = {"min_trades": 30, "min_expectancy": 0.0, "min_pf": 1.2, "max_dd": 0.20}


# ---------------------------------------------------------------------------
# Data helpers (reused from batch harness)
# ---------------------------------------------------------------------------

def aggregate_execution_m3(m1: pl.DataFrame) -> pl.DataFrame:
    if m1.is_empty():
        return m1
    known_through = m1["timestamp"][-1] + Timeframe.M1.duration
    m3 = (
        m1.sort("timestamp")
        .group_by_dynamic("timestamp", every="3m", period="3m", closed="left", label="left")
        .agg([
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.col("volume").sum().alias("volume"),
            pl.col("spread").mean().alias("spread"),
        ])
        .filter(pl.col("timestamp") + pl.duration(minutes=3) <= known_through)
        .sort("timestamp")
    )
    return m3.with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))


def load_m3(symbol: str, cache_dir: Path, max_m1_bars: Optional[int] = None) -> pl.DataFrame:
    path = HistoricalECNFetcher.get_cache_path(symbol, Timeframe.M1, cache_dir)
    if not path.exists():
        return pl.DataFrame()
    bars = MarketDataLoader.normalize_and_validate(
        pl.read_parquet(path), expected_interval=Timeframe.M1.duration,
        enforce_monotonicity=True,
    ).sort("timestamp")
    if max_m1_bars:
        bars = bars[-max_m1_bars:]
    return aggregate_execution_m3(bars)


# ---------------------------------------------------------------------------
# Experiment cell runner
# ---------------------------------------------------------------------------

@dataclass
class CellResult:
    pair: str
    set_key: str
    style: str
    trades: int = 0
    win_rate: Optional[float] = None
    mean_r: Optional[float] = None
    expectancy_r: Optional[float] = None
    profit_factor: Optional[float] = None
    max_dd: Optional[float] = None
    verdict: str = "UNTESTED"
    fail_reasons: list = None


def run_cell(pair: str, set_key: str, style: str, m3: pl.DataFrame) -> CellResult:
    cfg = STYLES[style]
    strategy = InstitutionalFractalStrategy(
        strategy_id=f"style_matrix_{style}_{set_key}",
        symbols=[pair],
        account_equity=Decimal("100000"),
        risk_fraction=Decimal("0.01"),
        session_filter=True,
        selected_set=set_key,
        source_timeframe=Timeframe.M3,
        minimum_net_reward_risk=cfg["minimum_net_reward_risk"],
        setup_persistence_bars=cfg["setup_persistence_bars"],
        stop_buffer_pips=cfg["stop_buffer_pips"],
        minimum_stop_pips=cfg["minimum_stop_pips"],
        maximum_stop_pips=cfg["maximum_stop_pips"],
    )

    # Simple equity-curve simulator over strategy intents
    equity = Decimal("100000")
    peak = equity
    max_dd = Decimal("0")
    rs: list[float] = []
    position = None  # (direction, entry, stop, target, bars_open)
    prev_close: Optional[Decimal] = None

    for row in m3.iter_rows(named=True):
        event = BarEvent(
            symbol=pair, timeframe=Timeframe.M3, timestamp=row["timestamp"],
            open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])), spread=Decimal(str(row["spread"])),
        )
        # manage open position first (causal: uses current bar only)
        if position is not None:
            d, entry, stop, target, bars = position
            bars += 1
            exit_price = None
            hi, lo = Decimal(str(row["high"])), Decimal(str(row["low"]))
            if d > 0:
                if lo <= stop: exit_price = stop
                elif hi >= target: exit_price = target
            else:
                if hi >= stop: exit_price = stop
                elif lo <= target: exit_price = target
            if exit_price is None and bars >= cfg["max_bars_in_trade"]:
                exit_price = Decimal(str(row["close"]))
            if exit_price is not None:
                risk = abs(entry - stop)
                r = float((exit_price - entry) / risk if d > 0 else (entry - exit_price) / risk)
                rs.append(r)
                equity *= (Decimal("1") + Decimal(str(r)) * Decimal("0.01"))
                if equity > peak: peak = equity
                dd = (peak - equity) / peak
                if dd > max_dd: max_dd = dd
                position = None

        intents = strategy.on_bar(event)
        if intents and position is None:
            it = intents[0]
            d = 1 if it.side.value == "BUY" else -1
            entry = Decimal(str(it.limit_price)) if it.limit_price else Decimal(str(row["close"]))
            stop = Decimal(str(it.stop_loss))
            target = Decimal(str(it.take_profit))
            position = (d, entry, stop, target, 0)
        prev_close = Decimal(str(row["close"]))

    result = CellResult(pair=pair, set_key=set_key, style=style, trades=len(rs), fail_reasons=[])
    if rs:
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        result.win_rate = len(wins) / len(rs)
        result.mean_r = sum(rs) / len(rs)
        result.expectancy_r = result.mean_r
        gp = sum(wins)
        gl = abs(sum(losses))
        result.profit_factor = (gp / gl) if gl > 0 else float("inf")
        result.max_dd = float(max_dd)
        fail = []
        if result.trades < GATES["min_trades"]: fail.append("TRADES")
        if result.expectancy_r <= GATES["min_expectancy"]: fail.append("EXPECTANCY")
        if (result.profit_factor or 0) < GATES["min_pf"]: fail.append("PF")
        if (result.max_dd or 1) > GATES["max_dd"]: fail.append("DD")
        result.fail_reasons = fail
        result.verdict = "PASS" if not fail else "FAIL"
    else:
        result.verdict = "NO_DATA"
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="+", default=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])
    ap.add_argument("--max-m1-bars", type=int, default=200_000, help="~140 days of M1")
    ap.add_argument("--styles", nargs="+", default=list(STYLES.keys()))
    ap.add_argument("--out", default="research/results/style_matrix.json")
    args = ap.parse_args()

    cache_dir = Path("data/cache/dukascopy")
    all_results: list[dict] = []
    for pair in args.pairs:
        print(f"[{pair}] loading M3...", flush=True)
        m3 = load_m3(pair, cache_dir, args.max_m1_bars)
        if m3.is_empty():
            print(f"[{pair}] no data; skipping")
            continue
        for set_key in TIMEFRAME_SETS:
            for style in args.styles:
                print(f"[{pair}] {set_key} x {style}...", flush=True)
                res = run_cell(pair, set_key, style, m3)
                print(f"    -> trades={res.trades} exp={res.expectancy_r} "
                      f"pf={res.profit_factor} verdict={res.verdict} {res.fail_reasons or ''}",
                      flush=True)
                all_results.append(asdict(res))
                # incremental save so nothing is lost on interruption
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(all_results, indent=2, default=str))

    # --- matrix summary ---
    passed = [r for r in all_results if r["verdict"] == "PASS"]
    print("\n=== STYLE x SET MATRIX — PASSING CELLS ===")
    for r in passed:
        print(f"  {r['pair']:7s} {r['set_key']} x {r['style']:11s} "
              f"trades={r['trades']} exp={r['expectancy_r']:.3f}R pf={r['profit_factor']:.2f}")
    print(f"\nTotal cells: {len(all_results)} | PASS: {len(passed)}")
    print(f"Results: {args.out}")


if __name__ == "__main__":
    main()
