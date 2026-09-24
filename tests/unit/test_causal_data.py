"""
Unit tests for MarketDataLoader and CausalAligner:
Timestamp monotonicity, weekend gap distinction, multi-timeframe downsampling,
and zero-lookahead causal alignment guarantees.
"""

from datetime import datetime, timedelta, timezone
import polars as pl
import pytest

from forex_platform.market_data.loader import (
    DataGap,
    DuplicateTimestampError,
    MarketDataLoader,
    MonotonicityError,
    SchemaError,
)
from forex_platform.market_data.causal_aligner import (
    CausalAligner,
    CausalLookaheadViolationError,
    Timeframe,
)


class TestMarketDataLoader:
    """Test data loading, monotonicity enforcement, and gap classification."""

    def test_valid_monotonic_data_passes(self):
        records = [
            {"timestamp": "2026-01-06T10:00:00Z", "open": 1.0850, "high": 1.0855, "low": 1.0849, "close": 1.0853, "volume": 120},
            {"timestamp": "2026-01-06T10:01:00Z", "open": 1.0853, "high": 1.0857, "low": 1.0851, "close": 1.0856, "volume": 140},
            {"timestamp": "2026-01-06T10:02:00Z", "open": 1.0856, "high": 1.0860, "low": 1.0854, "close": 1.0858, "volume": 110},
        ]
        df = MarketDataLoader.from_records(records)
        assert df.height == 3
        assert "spread" in df.columns
        assert df["close"][1] == 1.0856

    def test_duplicate_timestamp_raises(self):
        records = [
            {"timestamp": "2026-01-06T10:00:00Z", "open": 1.0850, "high": 1.0855, "low": 1.0849, "close": 1.0853, "volume": 120},
            {"timestamp": "2026-01-06T10:01:00Z", "open": 1.0853, "high": 1.0857, "low": 1.0851, "close": 1.0856, "volume": 140},
            {"timestamp": "2026-01-06T10:01:00Z", "open": 1.0854, "high": 1.0858, "low": 1.0852, "close": 1.0857, "volume": 130},  # Duplicate
        ]
        with pytest.raises(DuplicateTimestampError):
            MarketDataLoader.from_records(records)

    def test_backwards_timestamp_raises_monotonicity_error(self):
        records = [
            {"timestamp": "2026-01-06T10:02:00Z", "open": 1.0856, "high": 1.0860, "low": 1.0854, "close": 1.0858, "volume": 110},
            {"timestamp": "2026-01-06T10:01:00Z", "open": 1.0853, "high": 1.0857, "low": 1.0851, "close": 1.0856, "volume": 140},  # Backwards
        ]
        with pytest.raises(MonotonicityError):
            MarketDataLoader.from_records(records)

    def test_missing_required_column_raises_schema_error(self):
        records = [
            {"timestamp": "2026-01-06T10:00:00Z", "open": 1.0850, "high": 1.0855, "close": 1.0853, "volume": 120},  # missing "low"
        ]
        with pytest.raises(SchemaError):
            MarketDataLoader.from_records(records)

    def test_weekend_gap_vs_intraweek_drop(self):
        # 1. Intra-week unexpected drop: Tuesday 10:00 to Tuesday 10:30 (30 mins missing)
        intraweek_records = [
            {"timestamp": "2026-01-06T10:00:00Z", "open": 1.0850, "high": 1.0855, "low": 1.0849, "close": 1.0853, "volume": 120},
            {"timestamp": "2026-01-06T10:30:00Z", "open": 1.0855, "high": 1.0860, "low": 1.0852, "close": 1.0858, "volume": 150},
        ]
        df_intra = MarketDataLoader.from_records(intraweek_records)
        gaps_intra = MarketDataLoader.detect_gaps(df_intra, expected_interval=timedelta(minutes=1))
        assert len(gaps_intra) == 1
        assert not gaps_intra[0].is_weekend_gap
        assert gaps_intra[0].missing_bars_count == 29

        # 2. Weekend closure: Friday 2026-01-09 20:59:00Z to Sunday 2026-01-11 21:01:00Z
        weekend_records = [
            {"timestamp": "2026-01-09T20:59:00Z", "open": 1.0850, "high": 1.0855, "low": 1.0849, "close": 1.0853, "volume": 100},
            {"timestamp": "2026-01-11T21:01:00Z", "open": 1.0852, "high": 1.0856, "low": 1.0850, "close": 1.0854, "volume": 110},
        ]
        df_weekend = MarketDataLoader.from_records(weekend_records)
        gaps_weekend = MarketDataLoader.detect_gaps(df_weekend, expected_interval=timedelta(minutes=1))
        assert len(gaps_weekend) == 1
        assert gaps_weekend[0].is_weekend_gap

    def test_csv_and_json_loading(self, tmp_path):
        csv_file = tmp_path / "test_bars.csv"
        csv_file.write_text(
            "time,open,high,low,close,volume,spread\n"
            "2026-01-06T12:00:00Z,1.1000,1.1010,1.0990,1.1005,500,0.8\n"
            "2026-01-06T12:01:00Z,1.1005,1.1015,1.1000,1.1012,450,0.8\n"
        )
        loaded_csv = MarketDataLoader.load_csv(csv_file)
        assert loaded_csv.height == 2
        assert loaded_csv["open"][0] == 1.1000

        json_file = tmp_path / "test_bars.json"
        json_file.write_text(
            '[{"timestamp":"2026-01-06T12:00:00Z","open":1.1000,"high":1.1010,"low":1.0990,"close":1.1005,"volume":500,"spread":0.8},'
            '{"timestamp":"2026-01-06T12:01:00Z","open":1.1005,"high":1.1015,"low":1.1000,"close":1.1012,"volume":450,"spread":0.8}]'
        )
        loaded_json = MarketDataLoader.load_json(json_file)
        assert loaded_json.height == 2
        assert loaded_json["close"][1] == 1.1012


class TestCausalAligner:
    """Test multi-timeframe downsampling and zero-lookahead causal alignment."""

    def _generate_m1_bars(self, start_dt: datetime, count: int) -> pl.DataFrame:
        """Helper to generate sequential M1 bars."""
        records = []
        curr = start_dt
        price = 1.0800
        for i in range(count):
            records.append({
                "timestamp": curr.isoformat(),
                "open": price,
                "high": price + 0.0005,
                "low": price - 0.0003,
                "close": price + 0.0002,
                "volume": 100.0 + i,
                "spread": 1.0,
            })
            curr += timedelta(minutes=1)
            price += 0.0002
        return MarketDataLoader.from_records(records)

    def test_downsample_m1_to_m5(self):
        # 10 minutes of M1 bars from 10:00 to 10:09 -> 2 M5 bars
        start = datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
        m1_df = self._generate_m1_bars(start, count=10)

        m5_df = CausalAligner.downsample(m1_df, Timeframe.M5)
        assert m5_df.height == 2

        # First M5 bar: 10:00 to 10:05
        # Open is first bar open (1.0800)
        assert m5_df["open"][0] == 1.0800
        # Close is last bar close (M1 bar at 10:04)
        assert m5_df["close"][0] == m1_df["close"][4]
        # High is max of first 5 bars
        assert m5_df["high"][0] == m1_df["high"][:5].max()
        # Low is min of first 5 bars
        assert m5_df["low"][0] == m1_df["low"][:5].min()
        # Close time is 10:05:00 UTC
        assert m5_df["close_time"][0] == datetime(2026, 1, 6, 10, 5, 0, tzinfo=timezone.utc)

        # Second M5 bar: 10:05 to 10:10
        assert m5_df["close_time"][1] == datetime(2026, 1, 6, 10, 10, 0, tzinfo=timezone.utc)

    def test_causal_invariant_zero_lookahead(self):
        """
        Verify that base M1 bar at time t can only see HTF bars closed at or before t.
        E.g. at 10:04, the 10:00-10:05 M5 bar is NOT closed yet, so it cannot be observed!
        """
        # Generate 15 minutes of M1 bars: 09:55 to 10:09
        start = datetime(2026, 1, 6, 9, 55, 0, tzinfo=timezone.utc)
        m1_df = self._generate_m1_bars(start, count=15)

        # Downsample to M5: will produce bars closing at 10:00, 10:05, 10:10
        m5_df = CausalAligner.downsample(m1_df, Timeframe.M5)
        assert m5_df.height == 3
        # M5 bar 0 closes at 10:00:00
        # M5 bar 1 closes at 10:05:00
        # M5 bar 2 closes at 10:10:00

        aligned = CausalAligner.align_higher_timeframe(m1_df, m5_df, prefix="m5_")

        # Let's inspect rows around 10:04 and 10:05:
        # Index 9 is M1 bar at 10:04:00
        row_10_04 = aligned.filter(pl.col("timestamp") == datetime(2026, 1, 6, 10, 4, 0, tzinfo=timezone.utc))
        assert row_10_04["m5_close_time"][0] == datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
        # At 10:04, the M5 bar is the one that closed at 10:00! NOT the one closing at 10:05!

        # Index 10 is M1 bar at 10:05:00
        row_10_05 = aligned.filter(pl.col("timestamp") == datetime(2026, 1, 6, 10, 5, 0, tzinfo=timezone.utc))
        assert row_10_05["m5_close_time"][0] == datetime(2026, 1, 6, 10, 5, 0, tzinfo=timezone.utc)
        # At 10:05, the M5 bar closing at 10:05 now becomes available!

        # Universal verification passes
        CausalAligner.verify_causal_invariant(aligned, base_ts_col="timestamp", htf_close_col="m5_close_time")

    def test_causal_invariant_violation_raises(self):
        """
        Manually inject a future close_time and ensure verify_causal_invariant raises.
        """
        bad_df = pl.DataFrame({
            "timestamp": [datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)],
            "htf_close_time": [datetime(2026, 1, 6, 10, 5, 0, tzinfo=timezone.utc)],  # 5 minutes in future!
        })
        with pytest.raises(CausalLookaheadViolationError):
            CausalAligner.verify_causal_invariant(bad_df, base_ts_col="timestamp", htf_close_col="htf_close_time")
