"""
Causal multi-timeframe downsampler and feature aligner.
Guarantees zero-lookahead bias by strictly aligning higher-timeframe (HTF)
bars/indicators using only already-closed bars.
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
import polars as pl


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        mapping = {
            Timeframe.M1: timedelta(minutes=1),
            Timeframe.M5: timedelta(minutes=5),
            Timeframe.M15: timedelta(minutes=15),
            Timeframe.M30: timedelta(minutes=30),
            Timeframe.H1: timedelta(hours=1),
            Timeframe.H4: timedelta(hours=4),
            Timeframe.D1: timedelta(days=1),
        }
        return mapping[self]

    @property
    def polars_duration(self) -> str:
        mapping = {
            Timeframe.M1: "1m",
            Timeframe.M5: "5m",
            Timeframe.M15: "15m",
            Timeframe.M30: "30m",
            Timeframe.H1: "1h",
            Timeframe.H4: "4h",
            Timeframe.D1: "1d",
        }
        return mapping[self]


class CausalLookaheadViolationError(Exception):
    """Raised when causal invariant is broken (future data leaked into past timestamp)."""
    pass


class CausalAligner:
    """
    Downsamples base timeframe bars into higher timeframes and causally aligns them.

    Causal Invariant:
    A higher timeframe bar covering [T_start, T_end) is closed at T_end.
    Any base bar at timestamp t can ONLY observe HTF data from bars where T_end <= t.
    """

    @classmethod
    def downsample(
        cls,
        df: pl.DataFrame,
        target_tf: Timeframe,
    ) -> pl.DataFrame:
        """
        Downsample M1 (or lower TF) OHLCV data into target timeframe.
        Computes canonical bar aggregations:
        - open: first bar open
        - high: max high
        - low: min low
        - close: last bar close
        - volume: sum volume
        - spread: mean spread
        - close_time: exact timestamp when the bar closes (open_time + duration)
        """
        if df.is_empty():
            return df

        # Group dynamically using Polars dynamic windowing
        every_str = target_tf.polars_duration
        duration = target_tf.duration

        htf = (
            df.sort("timestamp")
            .group_by_dynamic(
                "timestamp",
                every=every_str,
                period=every_str,
                closed="left",
                label="left",
            )
            .agg([
                pl.col("open").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").last().alias("close"),
                pl.col("volume").sum().alias("volume"),
                pl.col("spread").mean().alias("spread"),
                pl.len().alias("bar_count"),
            ])
            .with_columns([
                # close_time = timestamp (open of window) + duration
                (pl.col("timestamp") + duration).alias("close_time"),
            ])
            .sort("close_time")
        )

        return htf

    @classmethod
    def align_higher_timeframe(
        cls,
        base_df: pl.DataFrame,
        htf_df: pl.DataFrame,
        prefix: str | None = None,
        include_cols: list[str] | None = None,
    ) -> pl.DataFrame:
        """
        Causally align HTF bars onto base timeframe bars.

        For each row in base_df at `timestamp`:
        Matches the latest HTF bar whose `close_time <= base_df.timestamp`.
        Zero lookahead bias guaranteed via backward asof-join on close_time.
        """
        if base_df.is_empty() or htf_df.is_empty():
            return base_df

        # Default prefix if not provided
        pfx = prefix or "htf_"

        # Select columns from htf_df
        if include_cols is None:
            # Include all standard OHLCV plus close_time
            htf_cols = [c for c in htf_df.columns if c not in ("timestamp",)]
        else:
            htf_cols = [c for c in include_cols if c in htf_df.columns]
            if "close_time" not in htf_cols and "close_time" in htf_df.columns:
                htf_cols.append("close_time")

        # Rename HTF columns with prefix to avoid collision
        rename_map = {}
        for c in htf_cols:
            if c == "close_time":
                rename_map[c] = f"{pfx}close_time"
            else:
                rename_map[c] = f"{pfx}{c}"

        htf_prefixed = htf_df.select(["close_time"] + [c for c in htf_cols if c != "close_time"]).rename(rename_map)

        # Sort both datasets to ensure strict monotonic ordering for asof join
        base_sorted = base_df.sort("timestamp")
        htf_prefixed_sorted = htf_prefixed.sort(f"{pfx}close_time")

        # Perform backward asof join: base.timestamp >= htf.close_time
        aligned = base_sorted.join_asof(
            htf_prefixed_sorted,
            left_on="timestamp",
            right_on=f"{pfx}close_time",
            strategy="backward",
        )

        # Verify causal invariant
        cls.verify_causal_invariant(aligned, base_ts_col="timestamp", htf_close_col=f"{pfx}close_time")

        return aligned

    @classmethod
    def verify_causal_invariant(
        cls,
        df: pl.DataFrame,
        base_ts_col: str = "timestamp",
        htf_close_col: str = "htf_close_time",
    ) -> None:
        """
        Asserts that no base bar accesses higher-timeframe data before it has closed.
        Raises CausalLookaheadViolationError if any row has htf_close_time > base_timestamp.
        """
        if htf_close_col not in df.columns:
            return

        # Filter rows where htf_close_time is not null
        valid_rows = df.filter(pl.col(htf_close_col).is_not_null())
        if valid_rows.is_empty():
            return

        violations = valid_rows.filter(pl.col(htf_close_col) > pl.col(base_ts_col))
        if violations.height > 0:
            first_viol = violations[0]
            raise CausalLookaheadViolationError(
                f"Causal lookahead violation detected! Base timestamp {first_viol[base_ts_col][0]} "
                f"has access to HTF bar closed at {first_viol[htf_close_col][0]}."
            )
