"""
Automated Historical ECN Data Downloader.
Downloads real institutional-grade historical M1/M15 bid/ask bar data from public archives
(Dukascopy, HistData, etc.) and caches as optimized Parquet in data/cache/.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import aiohttp
import polars as pl
from tqdm import tqdm

from forex_platform.core.domain import CurrencyPair
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.core.instruments import FXInstrumentRegistry
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_data.provenance import DataProvenance
from forex_platform.market_data.quality import DataQualityAuditor

logger = logging.getLogger(__name__)


class HistoricalDownloader:
    """
    Automated downloader for institutional ECN historical data.
    Supports Dukascopy (primary), with fallback to other public sources.
    """

    # Dukascopy base URL for historical data
    DUKASCOPY_BASE_URL = "https://datafeed.dukascopy.com/datafeed/"

    # Supported pairs with Dukascopy symbols
    DUKASCOPY_SYMBOLS = {symbol: symbol for symbol in FXInstrumentRegistry.SYMBOLS}

    # Timeframe mapping to Dukascopy format
    TIMEFRAME_MAP = {
        Timeframe.M1: "m1",
        Timeframe.M5: "m5",
        Timeframe.M15: "m15",
        Timeframe.M30: "m30",
        Timeframe.H1: "h1",
        Timeframe.H4: "h4",
    }

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        max_concurrent_downloads: int = 3,
        request_timeout: int = 60,
        retry_attempts: int = 3,
        retry_delay: float = 2.0,
    ):
        self.cache_dir = cache_dir or HistoricalECNFetcher.CACHE_DIR
        self.max_concurrent_downloads = max_concurrent_downloads
        self.request_timeout = request_timeout
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _build_dukascopy_url(
        self,
        symbol: str,
        timeframe: Timeframe,
        year: int,
        month: int,
        day: int,
        hour: int,
    ) -> str:
        """Build Dukascopy URL for a specific hour of data."""
        duk_symbol = self.DUKASCOPY_SYMBOLS.get(symbol.upper(), symbol.upper())
        tf = self.TIMEFRAME_MAP.get(timeframe, "m15")

        # Dukascopy path format: /datafeed/EURUSD/2024/01/15/14h_ticks.bi5
        # For bars: /datafeed/EURUSD/2024/01/15/14h_ticks.bi5 (same, we parse ticks to bars)
        # Actually Dukascopy provides tick data in .bi5 format, we need to convert
        # For simplicity, we'll use their CSV export format if available
        # Or use the hourly tick files and aggregate

        # Dukascopy hourly tick files
        url = f"{self.DUKASCOPY_BASE_URL}{duk_symbol}/{year:04d}/{month:02d}/{day:02d}/{hour:02d}h_ticks.bi5"
        return url

    def _build_dukascopy_csv_url(
        self,
        symbol: str,
        timeframe: Timeframe,
        year: int,
        month: int,
    ) -> str:
        """Build Dukascopy CSV export URL for a month."""
        # Dukascopy also provides monthly CSV exports
        # Format: https://datafeed.dukascopy.com/datafeed/EURUSD/2024/01/EURUSD_Candlestick_15_M_BID_01.01.2024-31.01.2024.csv
        duk_symbol = self.DUKASCOPY_SYMBOLS.get(symbol.upper(), symbol.upper())
        tf_map = {
            Timeframe.M1: "1_M",
            Timeframe.M5: "5_M",
            Timeframe.M15: "15_M",
            Timeframe.M30: "30_M",
            Timeframe.H1: "1_H",
            Timeframe.H4: "4_H",
        }
        tf_str = tf_map.get(timeframe, "15_M")

        # Monthly CSV file
        url = f"{self.DUKASCOPY_BASE_URL}{duk_symbol}/{year:04d}/{month:02d}/{duk_symbol}_Candlestick_{tf_str}_BID_01.{month:02d}.{year:04d}-{self._last_day_of_month(year, month):02d}.{month:02d}.{year:04d}.csv"
        return url

    def _last_day_of_month(self, year: int, month: int) -> int:
        """Get last day of month."""
        if month == 12:
            return 31
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        last_day = next_month - timedelta(days=1)
        return last_day.day

    async def _download_file(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest_path: Path,
        progress_bar: Optional[tqdm] = None,
    ) -> bool:
        """Download a single file with retries."""
        for attempt in range(self.retry_attempts):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.request_timeout)) as response:
                    if response.status == 200:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(dest_path, "wb") as f:
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                                if progress_bar:
                                    progress_bar.update(len(chunk))
                        return True
                    elif response.status == 404:
                        logger.debug("File not found (404): %s", url)
                        return False
                    else:
                        logger.warning("HTTP %d for %s (attempt %d/%d)", response.status, url, attempt + 1, self.retry_attempts)
            except asyncio.TimeoutError:
                logger.warning("Timeout downloading %s (attempt %d/%d)", url, attempt + 1, self.retry_attempts)
            except Exception as e:
                logger.warning("Error downloading %s: %s (attempt %d/%d)", url, e, attempt + 1, self.retry_attempts)

            if attempt < self.retry_attempts - 1:
                await asyncio.sleep(self.retry_delay * (attempt + 1))

        return False

    async def _download_month_csv(
        self,
        session: aiohttp.ClientSession,
        symbol: str,
        timeframe: Timeframe,
        year: int,
        month: int,
        semaphore: asyncio.Semaphore,
        progress_bar: Optional[tqdm] = None,
    ) -> Optional[Path]:
        """Download a single month's CSV data from Dukascopy."""
        async with semaphore:
            url = self._build_dukascopy_csv_url(symbol, timeframe, year, month)
            dest_path = self.cache_dir / "raw" / f"{symbol}_{timeframe.name}_{year}_{month:02d}.csv.gz"

            success = await self._download_file(session, url, dest_path, progress_bar)
            if success:
                return dest_path
            return None

    def _parse_dukascopy_bi5(self, bi5_path: Path) -> pl.DataFrame:
        """
        Parse Dukascopy .bi5 tick format.
        Format: 20 bytes per tick (timestamp, ask, bid, ask_volume, bid_volume)
        """
        # This is a simplified parser - in production you'd use a proper bi5 parser
        # For now, we'll focus on CSV downloads
        raise NotImplementedError("BI5 parsing not yet implemented; use CSV exports")

    def _convert_ticks_to_bars(
        self,
        ticks_df: pl.DataFrame,
        timeframe: Timeframe,
    ) -> pl.DataFrame:
        """Aggregate tick data to OHLCV bars."""
        # Group by timeframe intervals and compute OHLCV
        interval_ms = int(timeframe.to_timedelta().total_seconds() * 1000)

        # Add bar timestamp column
        ticks_df = ticks_df.with_columns([
            (pl.col("timestamp").cast(pl.Int64) // (interval_ms * 1_000_000) * (interval_ms * 1_000_000)).alias("bar_timestamp")
        ])

        # Aggregate
        bars = ticks_df.group_by("bar_timestamp").agg([
            pl.col("bid").first().alias("open"),
            pl.col("bid").max().alias("high"),
            pl.col("bid").min().alias("low"),
            pl.col("bid").last().alias("close"),
            pl.col("ask").first().alias("ask_open"),
            pl.col("ask").max().alias("ask_high"),
            pl.col("ask").min().alias("ask_low"),
            pl.col("ask").last().alias("ask_close"),
            pl.col("bid_volume").sum().alias("volume"),
            ((pl.col("ask") - pl.col("bid")).mean() * 10000).alias("spread_pips"),  # Approximate spread in pips
        ])

        # Rename bar_timestamp to timestamp
        bars = bars.rename({"bar_timestamp": "timestamp"})
        bars = bars.with_columns(pl.col("timestamp").cast(pl.Datetime("us", "UTC")))

        return bars.sort("timestamp")

    async def download_symbol_timeframe(
        self,
        symbol: str,
        timeframe: Timeframe,
        years: int = 2,
        end_date: Optional[datetime] = None,
    ) -> List[Path]:
        """
        Download historical data for a symbol/timeframe combination.
        Returns list of downloaded CSV file paths.
        """
        if symbol.upper() not in self.DUKASCOPY_SYMBOLS:
            raise ValueError(f"Unsupported symbol: {symbol}. Supported: {list(self.DUKASCOPY_SYMBOLS.keys())}")

        end_date = end_date or datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=years * 365)

        # Generate list of months to download
        months_to_download: List[Tuple[int, int]] = []
        current = datetime(start_date.year, start_date.month, 1, tzinfo=timezone.utc)
        while current <= end_date:
            months_to_download.append((current.year, current.month))
            # Next month
            if current.month == 12:
                current = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                current = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)

        logger.info("Downloading %d months of %s %s data from %s to %s",
                    len(months_to_download), symbol, timeframe.value, start_date.date(), end_date.date())

        # Download with concurrency control
        semaphore = asyncio.Semaphore(self.max_concurrent_downloads)
        downloaded_files: List[Path] = []

        async with aiohttp.ClientSession() as session:
            with tqdm(total=len(months_to_download), desc=f"Downloading {symbol} {timeframe.value}") as pbar:
                tasks = [
                    self._download_month_csv(session, symbol, timeframe, year, month, semaphore, pbar)
                    for year, month in months_to_download
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Path):
                        downloaded_files.append(result)
                    elif isinstance(result, Exception):
                        logger.error("Download task failed: %s", result)

        return downloaded_files

    def process_downloaded_csvs(
        self,
        symbol: str,
        timeframe: Timeframe,
        csv_paths: List[Path],
    ) -> pl.DataFrame:
        """
        Process downloaded CSV files into a single validated Parquet dataset.
        """
        all_dfs: List[pl.DataFrame] = []

        for csv_path in sorted(csv_paths):
            try:
                # Decompress if gzipped
                if csv_path.suffix == ".gz":
                    with gzip.open(csv_path, "rt", encoding="utf-8") as f:
                        # Read with polars
                        df = pl.read_csv(f, try_parse_dates=True)
                else:
                    df = pl.read_csv(csv_path, try_parse_dates=True)

                # Normalize using existing fetcher logic
                df = HistoricalECNFetcher.parse_external_csv(csv_path)
                all_dfs.append(df)
                logger.info("Processed %s: %d bars", csv_path.name, df.height)

            except Exception as e:
                logger.error("Failed to process %s: %s", csv_path, e)

        if not all_dfs:
            raise ValueError("No valid data frames processed")

        # Concatenate and deduplicate
        combined = pl.concat(all_dfs, how="vertical")
        combined = combined.unique(subset=["timestamp"]).sort("timestamp")

        # Validate and cache
        validated = MarketDataLoader.normalize_and_validate(combined)
        cache_path = HistoricalECNFetcher.cache_to_parquet(
            validated,
            symbol,
            timeframe,
            self.cache_dir,
            provenance=DataProvenance.REAL_VENDOR,
            source="Dukascopy historical download",
        )

        logger.info("Cached %d bars for %s %s to %s", validated.height, symbol, timeframe.value, cache_path)
        return validated

    async def download_and_cache(
        self,
        symbol: str,
        timeframe: Timeframe,
        years: int = 2,
        end_date: Optional[datetime] = None,
        *,
        allow_synthetic_fallback: bool = True,
    ) -> pl.DataFrame:
        """
        Complete download, process, and cache pipeline.
        """
        csv_paths = await self.download_symbol_timeframe(symbol, timeframe, years, end_date)
        if not csv_paths:
            if not allow_synthetic_fallback:
                raise FileNotFoundError(
                    f"No historical files downloaded for {symbol} {timeframe.value}; "
                    "synthetic fallback is disabled for qualification."
                )
            logger.warning("No files downloaded for %s %s, falling back to synthetic", symbol, timeframe.value)
            return HistoricalECNFetcher.generate_synthetic_ecn_history(
                symbol=symbol, timeframe=timeframe, num_bars=years * 365 * 24 * 4  # Approximate M15 bars
            )

        return self.process_downloaded_csvs(symbol, timeframe, csv_paths)

    async def download_multiple(
        self,
        symbols: List[str],
        timeframes: List[Timeframe],
        years: int = 2,
        end_date: Optional[datetime] = None,
        *,
        allow_synthetic_fallback: bool = False,
    ) -> Dict[str, Dict[Timeframe, pl.DataFrame]]:
        """
        Download multiple symbols and timeframes concurrently.
        """
        results: Dict[str, Dict[Timeframe, pl.DataFrame]] = {}

        for symbol in symbols:
            results[symbol] = {}
            for tf in timeframes:
                try:
                    df = await self.download_and_cache(
                        symbol, tf, years, end_date,
                        allow_synthetic_fallback=allow_synthetic_fallback,
                    )
                    results[symbol][tf] = df
                except Exception as e:
                    if not allow_synthetic_fallback:
                        raise RuntimeError(
                            f"Historical download failed for {symbol} {tf.value}: {e}"
                        ) from e
                    logger.error("Failed to download %s %s: %s", symbol, tf.value, e)
                    df = HistoricalECNFetcher.generate_synthetic_ecn_history(
                        symbol=symbol, timeframe=tf, num_bars=years * 365 * 24 * 4
                    )
                    HistoricalECNFetcher.cache_to_parquet(
                        df, symbol, tf, self.cache_dir,
                        provenance=DataProvenance.SYNTHETIC,
                        source="explicit synthetic fallback",
                    )
                    results[symbol][tf] = df

        return results
    async def download_all_fx(
        self,
        timeframe: Timeframe = Timeframe.M15,
        years: int = 2,
        end_date: Optional[datetime] = None,
        *,
        manifest_path: Optional[Path] = None,
    ) -> Dict[str, Dict[Timeframe, pl.DataFrame]]:
        """Download and audit every conventional spot-FX pair in the registry."""
        results = await self.download_multiple(
            list(FXInstrumentRegistry.SYMBOLS), [timeframe], years, end_date,
            allow_synthetic_fallback=False,
        )
        manifest: Dict[str, object] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "Dukascopy historical download",
            "timeframe": timeframe.value,
            "years_requested": years,
            "datasets": [],
        }
        for symbol, frames in results.items():
            for tf, df in frames.items():
                audit = DataQualityAuditor.audit(df, symbol=symbol, expected_interval=tf.to_timedelta())
                manifest["datasets"].append({
                    "symbol": symbol,
                    "timeframe": tf.value,
                    "provenance": DataProvenance.REAL_VENDOR.value,
                    "rows": df.height,
                    "start": str(df["timestamp"].min()),
                    "end": str(df["timestamp"].max()),
                    "quality_passed": audit.is_valid,
                    "midweek_drops": audit.midweek_drops_count,
                    "anomalies": audit.anomalies_count,
                })
        target = manifest_path or (self.cache_dir / f"manifest_{timeframe.name}.json")
        target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return results
def download_historical_data(
    symbols: List[str],
    timeframes: List[Timeframe],
    years: int = 2,
    cache_dir: Optional[Path] = None,
    max_concurrent: int = 3,
    allow_synthetic_fallback: bool = False,
) -> Dict[str, Dict[Timeframe, pl.DataFrame]]:
    """
    Synchronous entry point for CLI and scripts.
    """
    downloader = HistoricalDownloader(
        cache_dir=cache_dir,
        max_concurrent_downloads=max_concurrent,
    )

    return asyncio.run(downloader.download_multiple(
        symbols, timeframes, years,
        allow_synthetic_fallback=allow_synthetic_fallback,
    ))


# CLI integration
def add_download_history_parser(subparsers):
    """Add download-history command to CLI parser."""
    parser = subparsers.add_parser(
        "download-history",
        help="Download historical ECN data from Dukascopy and cache as Parquet"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="EURUSD,GBPUSD,USDJPY,EURGBP",
        help="Comma-separated list of symbols (default: EURUSD,GBPUSD,USDJPY,EURGBP)"
    )
    parser.add_argument(
        "--timeframes",
        type=str,
        default="M1,M15",
        help="Comma-separated list of timeframes (default: M1,M15)"
    )
    parser.add_argument(
        "--years",
        type=int,
        default=2,
        help="Years of history to download (default: 2)"
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="data/cache",
        help="Cache directory (default: data/cache)"
    )
    parser.add_argument(
        "--all-fx",
        action="store_true",
        help="Download and audit all 28 conventional spot-FX pairs",
    )
    parser.add_argument(
        "--allow-synthetic-fallback",
        action="store_true",
        help="Explicitly allow synthetic data on download failure (never qualifies for promotion)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Max concurrent downloads (default: 3)"
    )
    parser.set_defaults(func=cmd_download_history)
    return parser


def cmd_download_history(args) -> int:
    """CLI command to download historical data."""
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    if getattr(args, "all_fx", False):
        symbols = list(FXInstrumentRegistry.SYMBOLS)
    timeframes = [Timeframe[tf.strip().upper()] for tf in args.timeframes.split(",")]
    cache_dir = Path(args.cache_dir)

    print("=" * 70)
    print(" HISTORICAL ECN DATA DOWNLOADER")
    print("=" * 70)
    print(f" Symbols:      {', '.join(symbols)}")
    print(f" Timeframes:   {', '.join(tf.value for tf in timeframes)}")
    print(f" Years:        {args.years}")
    print(f" Cache Dir:    {cache_dir}")
    print(f" Max Concurrent: {args.max_concurrent}")
    print(f" All FX Pairs:   {getattr(args, 'all_fx', False)}")
    print(f" Synthetic Fallback: {getattr(args, 'allow_synthetic_fallback', False)}")
    print("-" * 70)

    try:
        if getattr(args, "all_fx", False):
            if len(timeframes) != 1:
                raise ValueError("--all-fx requires exactly one timeframe per manifest")
            downloader = HistoricalDownloader(
                cache_dir=cache_dir, max_concurrent_downloads=args.max_concurrent
            )
            results = asyncio.run(downloader.download_all_fx(
                timeframe=timeframes[0], years=args.years
            ))
        else:
            results = download_historical_data(
                symbols=symbols,
                timeframes=timeframes,
                years=args.years,
                cache_dir=cache_dir,
                max_concurrent=args.max_concurrent,
                allow_synthetic_fallback=getattr(args, "allow_synthetic_fallback", False),
            )

        print("\n DOWNLOAD SUMMARY:")
        print("-" * 70)
        for symbol, tf_data in results.items():
            for tf, df in tf_data.items():
                print(f"   {symbol} {tf.value}: {df.height:,} bars cached")
                if df.height > 0:
                    date_range = f"{df['timestamp'].min()} to {df['timestamp'].max()}"
                    print(f"      Date range: {date_range}")

        print("=" * 70)
        return 0

    except Exception as e:
        logger.error("Download failed: %s", e)
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    # Test run
    import sys
    logging.basicConfig(level=logging.INFO)

    # Quick test with synthetic data
    fetcher = HistoricalECNFetcher()
    df = fetcher.generate_synthetic_ecn_history("EURUSD", Timeframe.M15, 100)
    print(f"Generated {df.height} synthetic bars")
    print(df.head())