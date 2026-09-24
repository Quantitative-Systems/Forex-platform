"""
High-speed causal market data loader with strict monotonicity and weekend gap detection.
Built on Polars for vectorized parsing and sub-millisecond execution.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Union
from pydantic import BaseModel, ConfigDict
import polars as pl

from forex_platform.core.sessions import ForexSessionEngine


class MarketDataError(Exception):
    """Base exception for market data errors."""
    pass


class MonotonicityError(MarketDataError):
    """Raised when timestamp sequence is not strictly increasing."""
    pass


class DuplicateTimestampError(MonotonicityError):
    """Raised when duplicate timestamps are encountered."""
    pass


class SchemaError(MarketDataError):
    """Raised when required OHLCV columns are missing or malformed."""
    pass


class DataGap(BaseModel):
    """
    Structure representing a discontinuity in historical bar data.
    """
    model_config = ConfigDict(frozen=True)

    start_time: datetime
    end_time: datetime
    duration: timedelta
    missing_bars_count: int
    is_weekend_gap: bool


class MarketDataLoader:
    """
    Production-grade historical bar data loader.
    Loads and normalizes CSV/JSON historical Forex bars into canonical Polars DataFrames.
    Guarantees strict monotonicity and classifies gaps into weekend closures vs feed outages.
    """

    REQUIRED_COLS = ["timestamp", "open", "high", "low", "close", "volume"]
    CANONICAL_SCHEMA = {
        "timestamp": pl.Datetime("us", "UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "spread": pl.Float64,
    }

    COLUMN_ALIASES = {
        "date": "timestamp",
        "time": "timestamp",
        "datetime": "timestamp",
        "ts": "timestamp",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "vol": "volume",
        "sp": "spread",
    }

    @classmethod
    def load_csv(
        cls,
        source: Union[str, Path],
        expected_interval: timedelta = timedelta(minutes=1),
        enforce_monotonicity: bool = True,
        default_spread: float = 1.0,
    ) -> pl.DataFrame:
        """
        Load historical OHLCV data from CSV file.
        """
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Market data file not found at {path}")

        try:
            df = pl.read_csv(path, try_parse_dates=True)
        except Exception as e:
            raise MarketDataError(f"Failed to parse CSV at {path}: {e}") from e

        return cls.normalize_and_validate(
            df,
            expected_interval=expected_interval,
            enforce_monotonicity=enforce_monotonicity,
            default_spread=default_spread,
        )

    @classmethod
    def load_json(
        cls,
        source: Union[str, Path],
        expected_interval: timedelta = timedelta(minutes=1),
        enforce_monotonicity: bool = True,
        default_spread: float = 1.0,
    ) -> pl.DataFrame:
        """
        Load historical OHLCV data from JSON file.
        """
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Market data file not found at {path}")

        try:
            df = pl.read_json(path)
        except Exception as e:
            raise MarketDataError(f"Failed to parse JSON at {path}: {e}") from e

        return cls.normalize_and_validate(
            df,
            expected_interval=expected_interval,
            enforce_monotonicity=enforce_monotonicity,
            default_spread=default_spread,
        )

    @classmethod
    def from_records(
        cls,
        records: list[dict],
        expected_interval: timedelta = timedelta(minutes=1),
        enforce_monotonicity: bool = True,
        default_spread: float = 1.0,
    ) -> pl.DataFrame:
        """
        Create DataFrame from Python dictionaries.
        """
        if not records:
            raise MarketDataError("Cannot create market data DataFrame from empty records.")
        df = pl.DataFrame(records)
        return cls.normalize_and_validate(
            df,
            expected_interval=expected_interval,
            enforce_monotonicity=enforce_monotonicity,
            default_spread=default_spread,
        )

    @classmethod
    def normalize_and_validate(
        cls,
        df: pl.DataFrame,
        expected_interval: timedelta = timedelta(minutes=1),
        enforce_monotonicity: bool = True,
        default_spread: float = 1.0,
    ) -> pl.DataFrame:
        """
        Normalize column names, ensure UTC datetime, check monotonicity and cast to schema.
        """
        # Map lower-cased column names
        rename_map = {}
        for col in df.columns:
            low = col.lower()
            if low in cls.COLUMN_ALIASES:
                rename_map[col] = cls.COLUMN_ALIASES[low]
            elif low in cls.REQUIRED_COLS or low == "spread":
                rename_map[col] = low

        df = df.rename(rename_map)

        # Verify required columns exist
        missing = [col for col in cls.REQUIRED_COLS if col not in df.columns]
        if missing:
            raise SchemaError(f"Missing required market data columns: {missing}. Present: {df.columns}")

        # Add spread if absent
        if "spread" not in df.columns:
            df = df.with_columns(pl.lit(default_spread, dtype=pl.Float64).alias("spread"))

        # Normalize timestamp to UTC Datetime
        ts_type = df.schema["timestamp"]
        if ts_type == pl.String:
            # Try parsing ISO 8601 strings
            df = df.with_columns(
                pl.col("timestamp").str.to_datetime(time_zone="UTC").alias("timestamp")
            )
        elif isinstance(ts_type, pl.Datetime):
            if ts_type.time_zone is None:
                df = df.with_columns(
                    pl.col("timestamp").dt.replace_time_zone("UTC").alias("timestamp")
                )
            elif ts_type.time_zone != "UTC":
                df = df.with_columns(
                    pl.col("timestamp").dt.convert_time_zone("UTC").alias("timestamp")
                )

        # Cast numeric fields
        df = df.with_columns([
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
            pl.col("spread").cast(pl.Float64),
        ])

        # Select only standard columns in canonical order
        df = df.select(["timestamp", "open", "high", "low", "close", "volume", "spread"])

        if enforce_monotonicity:
            cls.validate_monotonicity(df)

        return df

    @classmethod
    def validate_monotonicity(cls, df: pl.DataFrame) -> None:
        """
        Enforce strict timestamp monotonicity: t[i] > t[i-1].
        Raises DuplicateTimestampError or MonotonicityError if violated.
        """
        if df.height <= 1:
            return

        ts_series = df["timestamp"]
        # Calculate diff in microseconds
        diffs = ts_series.diff()

        # Check for duplicates (diff == 0)
        duplicates = df.filter(diffs == timedelta(0))
        if duplicates.height > 0:
            dup_ts = duplicates["timestamp"][0]
            raise DuplicateTimestampError(
                f"Duplicate timestamp detected in market data at {dup_ts}. Found {duplicates.height} occurrences."
            )

        # Check for backwards jumps (diff < 0)
        backwards = df.filter(diffs < timedelta(0))
        if backwards.height > 0:
            violating_ts = backwards["timestamp"][0]
            raise MonotonicityError(
                f"Non-monotonic out-of-order timestamp detected in market data at {violating_ts}."
            )

    @classmethod
    def detect_gaps(
        cls,
        df: pl.DataFrame,
        expected_interval: timedelta = timedelta(minutes=1),
    ) -> List[DataGap]:
        """
        Identifies time discontinuities in bar sequence and classifies them into:
        - Structural weekend closure gaps (Friday 21:00 UTC -> Sunday 21:00 UTC)
        - Data drop / feed interruption gaps (unexpected missing intra-week bars)
        """
        if df.height <= 1:
            return []

        # Find rows where diff exceeds expected interval
        diffs = df["timestamp"].diff()
        gap_indices = []
        for i in range(1, df.height):
            d = diffs[i]
            if d is not None and d > expected_interval:
                gap_indices.append(i)

        gaps: list[DataGap] = []
        for idx in gap_indices:
            start_ts = df["timestamp"][idx - 1]
            end_ts = df["timestamp"][idx]

            # Convert to python datetime in UTC
            if isinstance(start_ts, datetime):
                dt_start = start_ts if start_ts.tzinfo else start_ts.replace(tzinfo=timezone.utc)
            else:
                dt_start = datetime.fromisoformat(str(start_ts)).replace(tzinfo=timezone.utc)

            if isinstance(end_ts, datetime):
                dt_end = end_ts if end_ts.tzinfo else end_ts.replace(tzinfo=timezone.utc)
            else:
                dt_end = datetime.fromisoformat(str(end_ts)).replace(tzinfo=timezone.utc)

            duration = dt_end - dt_start
            missing_bars = int(duration / expected_interval) - 1

            # Determine whether this gap is a legitimate weekend closure
            is_weekend = cls._is_weekend_closure_gap(dt_start, dt_end, expected_interval)

            gaps.append(
                DataGap(
                    start_time=dt_start,
                    end_time=dt_end,
                    duration=duration,
                    missing_bars_count=missing_bars,
                    is_weekend_gap=is_weekend,
                )
            )

        return gaps

    @classmethod
    def _is_weekend_closure_gap(
        cls,
        start_dt: datetime,
        end_dt: datetime,
        expected_interval: timedelta,
    ) -> bool:
        """
        Check if a gap represents standard Friday close to Sunday open market closure.
        Friday market closes at 21:00 UTC, Sunday market opens at 21:00 UTC.
        """
        # Start should be on Friday near or after 20:55 UTC
        # End should be on Sunday near or after 21:00 UTC, or Monday early
        # Also verify that during the gap, non-weekend market hours were not missed
        if start_dt.weekday() == 4:  # Friday
            # Last bar Friday was within 15 minutes of 21:00 UTC close
            friday_close = start_dt.replace(hour=21, minute=0, second=0, microsecond=0)
            if abs(start_dt - friday_close) <= timedelta(minutes=15) or start_dt >= friday_close:
                # Reopen on Sunday near 21:00 UTC
                if end_dt.weekday() == 6:  # Sunday
                    sunday_open = end_dt.replace(hour=21, minute=0, second=0, microsecond=0)
                    if abs(end_dt - sunday_open) <= timedelta(minutes=15) or end_dt >= sunday_open:
                        return True
                elif end_dt.weekday() == 0:  # Monday early opening
                    if end_dt.hour == 0 and end_dt.minute <= 10:
                        return True

        # Check if the entire interval between start + interval and end - interval is weekend
        probe = start_dt + expected_interval
        all_weekend = True
        step = timedelta(hours=1)
        while probe < end_dt:
            if not ForexSessionEngine.is_weekend(probe):
                all_weekend = False
                break
            probe += step

        return all_weekend
