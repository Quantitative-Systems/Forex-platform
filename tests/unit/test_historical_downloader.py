"""
Unit tests for Historical ECN Data Downloader.
"""

from __future__ import annotations

import gzip
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import polars as pl
import pytest

from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_downloader import (
    HistoricalDownloader,
    download_historical_data,
)
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher


class TestHistoricalDownloader:
    """Tests for HistoricalDownloader class."""

    def test_initialization(self):
        """Test downloader initialization with defaults."""
        downloader = HistoricalDownloader()
        assert downloader.cache_dir == HistoricalECNFetcher.CACHE_DIR
        assert downloader.max_concurrent_downloads == 3
        assert downloader.request_timeout == 60
        assert downloader.retry_attempts == 3
        assert downloader.retry_delay == 2.0

    def test_initialization_custom(self):
        """Test downloader initialization with custom parameters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "custom_cache"
            downloader = HistoricalDownloader(
                cache_dir=cache_dir,
                max_concurrent_downloads=5,
                request_timeout=120,
                retry_attempts=5,
                retry_delay=1.0,
            )
            assert downloader.cache_dir == cache_dir
            assert downloader.max_concurrent_downloads == 5
            assert downloader.request_timeout == 120
            assert downloader.retry_attempts == 5
            assert downloader.retry_delay == 1.0

    def test_build_dukascopy_csv_url(self):
        """Test Dukascopy CSV URL building."""
        downloader = HistoricalDownloader()
        url = downloader._build_dukascopy_csv_url("EURUSD", Timeframe.M15, 2024, 1)
        assert "EURUSD" in url
        assert "2024" in url
        assert "01" in url
        assert "15_M" in url
        assert "Candlestick" in url

    def test_build_dukascopy_csv_url_different_timeframes(self):
        """Test URL building for different timeframes."""
        downloader = HistoricalDownloader()

        # M1
        url_m1 = downloader._build_dukascopy_csv_url("EURUSD", Timeframe.M1, 2024, 1)
        assert "1_M" in url_m1

        # M5
        url_m5 = downloader._build_dukascopy_csv_url("EURUSD", Timeframe.M5, 2024, 1)
        assert "5_M" in url_m5

        # H1
        url_h1 = downloader._build_dukascopy_csv_url("EURUSD", Timeframe.H1, 2024, 1)
        assert "1_H" in url_h1

    def test_last_day_of_month(self):
        """Test last day of month calculation."""
        downloader = HistoricalDownloader()
        assert downloader._last_day_of_month(2024, 1) == 31
        assert downloader._last_day_of_month(2024, 2) == 29  # Leap year
        assert downloader._last_day_of_month(2023, 2) == 28  # Non-leap year
        assert downloader._last_day_of_month(2024, 4) == 30
        assert downloader._last_day_of_month(2024, 12) == 31

    def test_download_file_success(self):
        """Test successful file download."""
        import asyncio

        downloader = HistoricalDownloader()

        # Mock aiohttp session - need to properly mock async context manager
        mock_response = AsyncMock()
        mock_response.status = 200

        # Create async iterator for iter_chunked
        async def mock_iter_chunked(chunk_size):
            yield b"test data"

        mock_response.content.iter_chunked = mock_iter_chunked

        # Create async context manager mock that returns itself on __aenter__
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        # get() should return the context manager directly, not a coroutine
        mock_session.get = Mock(return_value=mock_cm)

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_path = Path(tmpdir) / "test.csv.gz"
            progress_bar = Mock()

            result = asyncio.run(downloader._download_file(mock_session, "http://test.com/file.csv.gz", dest_path, progress_bar))

            assert result is True
            assert dest_path.exists()
            assert dest_path.read_bytes() == b"test data"

    def test_download_file_404(self):
        """Test file not found (404) handling."""
        import asyncio

        downloader = HistoricalDownloader()

        mock_response = AsyncMock()
        mock_response.status = 404

        mock_session = AsyncMock()
        mock_session.get.return_value.__aenter__.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_path = Path(tmpdir) / "test.csv.gz"

            result = asyncio.run(downloader._download_file(mock_session, "http://test.com/file.csv.gz", dest_path))

            assert result is False
            assert not dest_path.exists()

    def test_download_file_retry_on_error(self):
        """Test retry logic on download error."""
        import asyncio

        downloader = HistoricalDownloader(retry_attempts=3, retry_delay=0.01)

        mock_session = AsyncMock()
        mock_session.get.side_effect = Exception("Network error")

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_path = Path(tmpdir) / "test.csv.gz"

            result = asyncio.run(downloader._download_file(mock_session, "http://test.com/file.csv.gz", dest_path))

            assert result is False
            assert mock_session.get.call_count == 3

    def test_batch_download_fails_closed_without_explicit_fallback(self, tmp_path):
        import asyncio

        downloader = HistoricalDownloader(cache_dir=tmp_path)
        with patch.object(
            downloader,
            "download_and_cache",
            new=AsyncMock(side_effect=FileNotFoundError("no network")),
        ):
            with pytest.raises(RuntimeError, match="Historical download failed"):
                asyncio.run(downloader.download_multiple(["EURUSD"], [Timeframe.M15]))

    def test_batch_download_explicit_fallback_is_labeled_synthetic(self, tmp_path):
        import asyncio

        downloader = HistoricalDownloader(cache_dir=tmp_path)
        with patch.object(
            downloader,
            "download_and_cache",
            new=AsyncMock(side_effect=FileNotFoundError("no network")),
        ):
            results = asyncio.run(
                downloader.download_multiple(
                    ["EURUSD"], [Timeframe.M15], years=1,
                    allow_synthetic_fallback=True,
                )
            )
        assert results["EURUSD"][Timeframe.M15].height > 0
        provenance, _ = HistoricalECNFetcher.load_provenance("EURUSD", Timeframe.M15, tmp_path)
        assert provenance.value == "SYNTHETIC"

    def test_all_fx_manifest_has_28_audited_datasets(self, tmp_path):
        import asyncio
        from datetime import timedelta
        from forex_platform.core.instruments import FXInstrumentRegistry

        downloader = HistoricalDownloader(cache_dir=tmp_path)
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        frames = {}
        for symbol in FXInstrumentRegistry.SYMBOLS:
            price = 1.0 if "JPY" not in symbol else 100.0
            rows = []
            for i in range(40):
                ts = start + timedelta(minutes=15 * i)
                close = price + i * 0.00001
                rows.append({
                    "timestamp": ts, "open": price, "high": close + 0.0001,
                    "low": price - 0.0001, "close": close,
                    "volume": 100.0, "spread": 1.0,
                })
                price = close
            frames[symbol] = {Timeframe.M15: pl.DataFrame(rows)}
        with patch.object(downloader, "download_multiple", new=AsyncMock(return_value=frames)):
            asyncio.run(downloader.download_all_fx(Timeframe.M15, years=1))
        manifest_path = tmp_path / "manifest_M15.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert len(manifest["datasets"]) == 28
        assert all(item["provenance"] == "REAL_VENDOR" for item in manifest["datasets"])
        assert all(item["quality_passed"] for item in manifest["datasets"])

    def test_download_historical_data_sync_wrapper(self):
        """Test synchronous wrapper function."""
        # This test uses synthetic data generation as fallback
        with patch.object(HistoricalDownloader, 'download_multiple') as mock_download:
            mock_download.return_value = {
                "EURUSD": {
                    Timeframe.M15: pl.DataFrame({
                        "timestamp": [datetime(2024, 1, 1, tzinfo=timezone.utc)],
                        "open": [1.0850],
                        "high": [1.0855],
                        "low": [1.0845],
                        "close": [1.0852],
                        "volume": [1000],
                        "spread": [1.0],
                    })
                }
            }

            results = download_historical_data(
                symbols=["EURUSD"],
                timeframes=[Timeframe.M15],
                years=1,
            )

            assert "EURUSD" in results
            assert Timeframe.M15 in results["EURUSD"]
            assert results["EURUSD"][Timeframe.M15].height == 1


class TestHistoricalECNFetcherIntegration:
    """Integration tests for HistoricalECNFetcher with downloader."""

    def test_generate_synthetic_ecn_history(self):
        """Test synthetic data generation."""
        df = HistoricalECNFetcher.generate_synthetic_ecn_history(
            symbol="EURUSD",
            timeframe=Timeframe.M15,
            num_bars=100,
        )

        assert df.height == 100
        assert "timestamp" in df.columns
        assert "open" in df.columns
        assert "high" in df.columns
        assert "low" in df.columns
        assert "close" in df.columns
        assert "volume" in df.columns
        assert "spread" in df.columns

        # Check timestamp monotonicity
        timestamps = df["timestamp"].to_list()
        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i-1], "Timestamps must be monotonic"

        # Check OHLC validity
        for row in df.iter_rows(named=True):
            assert row["high"] >= max(row["open"], row["close"])
            assert row["low"] <= min(row["open"], row["close"])

    def test_generate_synthetic_ecn_history_jpy(self):
        """Test synthetic data generation for JPY pairs."""
        df = HistoricalECNFetcher.generate_synthetic_ecn_history(
            symbol="USDJPY",
            timeframe=Timeframe.M15,
            num_bars=50,
        )

        assert df.height == 50
        # JPY pairs have different price precision
        assert all(row["close"] > 100 for row in df.iter_rows(named=True))

    def test_cache_to_parquet_and_load(self):
        """Test Parquet caching and loading."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)

            df = HistoricalECNFetcher.generate_synthetic_ecn_history(
                symbol="EURUSD",
                timeframe=Timeframe.M15,
                num_bars=10,
            )

            # Cache to parquet
            cache_path = HistoricalECNFetcher.cache_to_parquet(df, "EURUSD", Timeframe.M15, cache_dir)
            assert cache_path.exists()
            assert cache_path.suffix == ".parquet"

            # Load from parquet
            loaded_df = HistoricalECNFetcher.load_from_parquet("EURUSD", Timeframe.M15, cache_dir)
            assert loaded_df is not None
            assert loaded_df.height == 10
            assert loaded_df.columns == df.columns

    def test_parse_external_csv_standard(self):
        """Test parsing standard CSV format."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("timestamp,open,high,low,close,volume,spread\n")
            f.write("2024-01-01 00:00:00,1.0850,1.0855,1.0845,1.0852,1000,1.0\n")
            f.write("2024-01-01 00:15:00,1.0852,1.0858,1.0848,1.0855,1200,1.0\n")
            csv_path = Path(f.name)

        try:
            df = HistoricalECNFetcher.parse_external_csv(csv_path)
            assert df.height == 2
            assert "timestamp" in df.columns
            assert df["close"][0] == 1.0852
        finally:
            csv_path.unlink()

    def test_parse_external_csv_dukascopy(self):
        """Test parsing Dukascopy CSV format."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("Gmt time,Open,High,Low,Close,Volume\n")
            f.write("01.01.2024 00:00:00,1.0850,1.0855,1.0845,1.0852,1000\n")
            f.write("01.01.2024 00:15:00,1.0852,1.0858,1.0848,1.0855,1200\n")
            csv_path = Path(f.name)

        try:
            df = HistoricalECNFetcher.parse_external_csv(csv_path)
            assert df.height == 2
            assert "timestamp" in df.columns
            assert "spread" in df.columns  # Should be added with default
        finally:
            csv_path.unlink()

    def test_parse_external_csv_histdata(self):
        """Test parsing HistData headerless format."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write("20240101 000000;1.0850;1.0855;1.0845;1.0852;1000\n")
            f.write("20240101 001500;1.0852;1.0858;1.0848;1.0855;1200\n")
            csv_path = Path(f.name)

        try:
            df = HistoricalECNFetcher.parse_external_csv(csv_path)
            assert df.height == 2
            assert "timestamp" in df.columns
            assert "spread" in df.columns
        finally:
            csv_path.unlink()

    def test_fetch_or_load_cache_hit(self):
        """Test fetch_or_load with cache hit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)

            # Create and cache data
            df = HistoricalECNFetcher.generate_synthetic_ecn_history("EURUSD", Timeframe.M15, 10)
            HistoricalECNFetcher.cache_to_parquet(df, "EURUSD", Timeframe.M15, cache_dir)

            # Fetch should load from cache
            loaded = HistoricalECNFetcher.fetch_or_load(
                "EURUSD", Timeframe.M15, cache_dir=cache_dir, auto_generate_synthetic_if_missing=False
            )
            assert loaded is not None
            assert loaded.height == 10

    def test_fetch_or_load_synthetic_fallback(self):
        """Test fetch_or_load falls back to synthetic generation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)

            # No cache, no source file, should generate synthetic
            loaded = HistoricalECNFetcher.fetch_or_load(
                "EURUSD", Timeframe.M15, cache_dir=cache_dir, auto_generate_synthetic_if_missing=True, synthetic_bars=20
            )
            assert loaded is not None
            assert loaded.height == 20


class TestDownloadHistoricalDataCLI:
    """Tests for CLI integration."""

    def test_add_download_history_parser(self):
        """Test that parser is added correctly."""
        import argparse
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        from forex_platform.market_data.historical_downloader import add_download_history_parser
        add_download_history_parser(subparsers)

        # Parse args
        args = parser.parse_args(["download-history", "--symbols", "EURUSD,GBPUSD", "--years", "3"])
        assert args.command == "download-history"
        assert args.symbols == "EURUSD,GBPUSD"
        assert args.years == 3

    def test_cmd_download_history_dry_run(self):
        """Test CLI command with mocked downloader."""
        import argparse
        from unittest.mock import MagicMock

        # Create mock args
        args = argparse.Namespace(
            symbols="EURUSD",
            timeframes="M15",
            years=1,
            cache_dir="data/cache",
            max_concurrent=1,
        )

        with patch('forex_platform.market_data.historical_downloader.download_historical_data') as mock_download:
            mock_download.return_value = {
                "EURUSD": {
                    Timeframe.M15: pl.DataFrame({
                        "timestamp": [datetime(2024, 1, 1, tzinfo=timezone.utc)],
                        "open": [1.0850],
                        "high": [1.0855],
                        "low": [1.0845],
                        "close": [1.0852],
                        "volume": [1000],
                        "spread": [1.0],
                    })
                }
            }

            from forex_platform.market_data.historical_downloader import cmd_download_history
            result = cmd_download_history(args)
            assert result == 0
            mock_download.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])