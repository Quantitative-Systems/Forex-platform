"""
Unit tests for Historical ECN Fetcher, Parquet Caching, Format Normalization,
and Data Quality Auditing.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import polars as pl
import pytest

from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.quality import DataQualityAuditor


class TestHistoricalECNFetcher:

    def test_parse_dukascopy_format(self, tmp_path):
        """Verify parsing of Dukascopy historical bar export."""
        csv_file = tmp_path / "EURUSD_dukascopy.csv"
        content = (
            "Gmt time,Open,High,Low,Close,Volume\n"
            "05.01.2026 00:00:00.000,1.08500,1.08550,1.08480,1.08520,150.5\n"
            "05.01.2026 00:15:00.000,1.08520,1.08580,1.08510,1.08560,200.0\n"
        )
        csv_file.write_text(content)

        df = HistoricalECNFetcher.parse_external_csv(csv_file)
        assert df.height == 2
        assert "timestamp" in df.columns
        assert "spread" in df.columns
        assert df["close"][0] == 1.08520
        assert df["timestamp"][0] == datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)

    def test_parse_histdata_format(self, tmp_path):
        """Verify parsing of HistData semicolon-delimited headerless export."""
        csv_file = tmp_path / "EURUSD_histdata.csv"
        content = (
            "20260105 000000;1.08500;1.08550;1.08480;1.08520;120\n"
            "20260105 001500;1.08520;1.08580;1.08510;1.08560;140\n"
        )
        csv_file.write_text(content)

        df = HistoricalECNFetcher.parse_external_csv(csv_file)
        assert df.height == 2
        assert df["open"][0] == 1.08500
        assert df["close"][1] == 1.08560
        assert df["timestamp"][1] == datetime(2026, 1, 5, 0, 15, 0, tzinfo=timezone.utc)

    def test_parse_metatrader_format(self, tmp_path):
        """Verify parsing of MetaTrader MT5 format export."""
        csv_file = tmp_path / "EURUSD_mt5.csv"
        content = (
            "<DATE>	<TIME>	<OPEN>	<HIGH>	<LOW>	<CLOSE>	<TICKVOL>	<VOL>	<SPREAD>\n"
            "2026.01.05	00:00:00	1.08500	1.08550	1.08480	1.08520	120	0	8\n"
            "2026.01.05	00:15:00	1.08520	1.08580	1.08510	1.08560	140	0	9\n"
        )
        csv_file.write_text(content)

        df = HistoricalECNFetcher.parse_external_csv(csv_file)
        assert df.height == 2
        assert df["volume"][0] == 120.0
        assert df["spread"][0] == 8.0

    def test_parquet_caching_and_reload(self, tmp_path):
        """Verify saving to Parquet cache and subsequent high-speed reload."""
        df = HistoricalECNFetcher.generate_synthetic_ecn_history(
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            num_bars=100,
        )

        cache_path = HistoricalECNFetcher.cache_to_parquet(
            df=df,
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            cache_dir=tmp_path,
        )
        assert cache_path.exists()
        assert cache_path.name == "EURUSD_M15.parquet"

        reloaded = HistoricalECNFetcher.load_from_parquet(
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            cache_dir=tmp_path,
        )
        assert reloaded is not None
        assert reloaded.height == 100
        assert reloaded.schema == df.schema

    def test_fetch_or_load_generates_and_caches_if_missing(self, tmp_path):
        """Verify fetch_or_load populates cache transparently if missing."""
        df = HistoricalECNFetcher.fetch_or_load(
            symbol="GBPUSD",
            timeframe=Timeframe.M15,
            cache_dir=tmp_path,
            synthetic_bars=50,
        )
        assert df.height == 50
        cache_file = tmp_path / "GBPUSD_M15.parquet"
        assert cache_file.exists()


class TestDataQualityAuditor:

    def test_audit_clean_data(self):
        """Verify clean synthetic history passes quality audit."""
        # 10 days of M15 data spanning across a weekend
        t0 = datetime(2026, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
        df = HistoricalECNFetcher.generate_synthetic_ecn_history(
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            num_bars=300,
            start_dt=t0,
        )

        report = DataQualityAuditor.audit(df, symbol="EURUSD", expected_interval=timedelta(minutes=15))
        assert report.symbol == "EURUSD"
        assert report.total_bars == 300
        assert report.anomalies_count == 0
        assert report.is_valid is True
        assert "LONDON" in report.session_median_spreads
        assert "NEW_YORK" in report.session_median_spreads
        assert report.weekend_gaps_count >= 1

    def test_audit_detects_price_anomaly(self):
        """Verify auditor flags high < low or negative prices."""
        t0 = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
        bad_df = pl.DataFrame({
            "timestamp": [t0, t0 + timedelta(minutes=15)],
            "open": [1.0850, 1.0855],
            "high": [1.0840, 1.0860],  # High < Open anomaly on bar 0
            "low": [1.0845, 1.0850],
            "close": [1.0848, 1.0858],
            "volume": [100.0, 100.0],
            "spread": [1.0, 1.0],
        })

        report = DataQualityAuditor.audit(bad_df, symbol="EURUSD", expected_interval=timedelta(minutes=15))
        assert report.is_valid is False
        assert report.anomalies_count > 0
        assert any(a.anomaly_type == "HIGH_LESS_THAN_BODY" for a in report.anomalies)

    def test_audit_detects_midweek_feed_drop(self):
        """Verify mid-week gap is flagged as a feed drop rather than weekend closure."""
        # Gap on Tuesday
        t1 = datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)  # 4-hour drop on Tuesday

        df_gap = pl.DataFrame({
            "timestamp": [t1, t2],
            "open": [1.0850, 1.0860],
            "high": [1.0855, 1.0865],
            "low": [1.0845, 1.0855],
            "close": [1.0852, 1.0862],
            "volume": [100.0, 100.0],
            "spread": [1.0, 1.0],
        })

        report = DataQualityAuditor.audit(df_gap, symbol="EURUSD", expected_interval=timedelta(minutes=15))
        assert report.midweek_drops_count == 1
        assert report.is_valid is False
