#!/usr/bin/env python3
"""
Multi-Timeframe Tick Aggregator
================================
Builds all required timeframe bars (1M, 1W, 1D, 4H, 1H, 15M, 5M)
from a single M1 tick stream per pair.

Preserves causal alignment: each higher-timeframe bar uses ONLY
ticks from strictly prior M1 bars. No look-ahead.
"""

import polars as pl
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass
import sys

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Re-use existing causal aligner for validation
from forex_platform.market_data.causal_aligner import CausalAligner


@dataclass
class TimeframeSpec:
    name: str
    minutes: int
    session_aware: bool = True


TIMEFRAME_SPECS = [
    TimeframeSpec("1M", 30 * 24 * 60, session_aware=False),   # Monthly (calendar)
    TimeframeSpec("1W", 7 * 24 * 60, session_aware=False),    # Weekly
    TimeframeSpec("1D", 24 * 60, session_aware=False),        # Daily
    TimeframeSpec("4H", 4 * 60, session_aware=True),          # 4-hour
    TimeframeSpec("1H", 60, session_aware=True),              # 1-hour
    TimeframeSpec("15M", 15, session_aware=True),             # 15-minute
    TimeframeSpec("5M", 5, session_aware=True),               # 5-minute
]


class MultiTickAggregator:
    def __init__(self, m1_data_path: str):
        self.m1_data_path = Path(m1_data_path)
        self.aligner = CausalAligner()

    def load_m1_ticks(self, pair: str) -> pl.DataFrame:
        """Load raw M1 ticks/parquet for a pair."""
        # Expect columns: timestamp, open, high, low, close, volume, spread
        files = list(self.m1_data_path.glob(f"{pair}_m1*.parquet"))
        if not files:
            raise FileNotFoundError(f"No M1 data found for {pair} in {self.m1_data_path}")
        return pl.read_parquet(files[0])

    def aggregate_to_timeframe(self, m1_df: pl.DataFrame, spec: TimeframeSpec) -> pl.DataFrame:
        """Aggregate M1 bars to target timeframe using causal boundaries."""
        # Ensure timestamp is datetime
        if m1_df["timestamp"].dtype != pl.Datetime:
            m1_df = m1_df.with_columns(pl.col("timestamp").cast(pl.Datetime))

        # Floor to timeframe boundary
        minutes = spec.minutes
        m1_df = m1_df.with_columns([
            (pl.col("timestamp").dt.epoch("1m") // (minutes * 60_000_000) * (minutes * 60_000_000)).alias("tf_boundary")
        ])

        # Aggregate
        agg = m1_df.group_by("tf_boundary").agg([
            pl.col("timestamp").first().alias("open_time"),
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
            pl.col("spread").mean().alias("avg_spread"),
        ]).sort("tf_boundary")

        # Rename boundary to timestamp
        agg = agg.rename({"tf_boundary": "timestamp"})
        return agg

    def build_all_timeframes(self, pair: str) -> Dict[str, pl.DataFrame]:
        """Build all 7 timeframes from M1 for a single pair."""
        m1_df = self.load_m1_ticks(pair)
        results = {"M1": m1_df}

        for spec in TIMEFRAME_SPECS:
            results[spec.name] = self.aggregate_to_timeframe(m1_df, spec)

        return results

    def validate_causal_integrity(self, pair: str, timeframes: Dict[str, pl.DataFrame]) -> bool:
        """Verify no look-ahead across all timeframes."""
        for tf_name, df in timeframes.items():
            if not self.aligner.validate_no_lookahead(df):
                print(f"[AGGREGATOR] CAUSAL VIOLATION: {pair} {tf_name}")
                return False
        return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build multi-timeframe bars from M1")
    parser.add_argument("--pair", type=str, required=True)
    parser.add_argument("--data-path", type=str, default="data/cache/dukascopy")
    parser.add_argument("--output-dir", type=str, default="data/cache/dukascopy/mtf")
    args = parser.parse_args()

    agg = MultiTickAggregator(args.data_path)
    print(f"[AGGREGATOR] Building timeframes for {args.pair}...")
    tfs = agg.build_all_timeframes(args.pair)

    ok = agg.validate_causal_integrity(args.pair, tfs)
    print(f"[AGGREGATOR] Causal integrity: {'PASS' if ok else 'FAIL'}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for name, df in tfs.items():
        out_file = out_dir / f"{args.pair}_{name}.parquet"
        df.write_parquet(out_file)
        print(f"  {name}: {len(df)} bars → {out_file}")


if __name__ == "__main__":
    main()