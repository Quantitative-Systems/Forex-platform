"""Download and aggregate HistData bid/ask tick archives for offline research.

HistData publishes its archive timestamps in fixed EST (UTC-5, without DST).
This importer preserves that convention when converting timestamps to UTC and
keeps observed bid and ask OHLC separate for quote-sided simulation.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
import logging
from pathlib import Path
import threading
import time
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.cookiejar import CookieJar
import zipfile

import polars as pl

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_data.provenance import DataProvenance

LOG = logging.getLogger(__name__)
_FORM_ROOT = "https://www.histdata.com/download-free-forex-historical-data/"
_POST_URL = "https://www.histdata.com/get.php"
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


class _TokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "input" and fields.get("id") == "tk":
            value = fields.get("value")
            self.token = value if value else None


def _reserve_request_slot(minimum_interval: float) -> None:
    global _NEXT_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _NEXT_REQUEST_AT - now)
        _NEXT_REQUEST_AT = max(now, _NEXT_REQUEST_AT) + minimum_interval
    if wait:
        time.sleep(wait)


def _month_range(start_month: str, end_month: str) -> list[tuple[int, int]]:
    def parse(value: str) -> tuple[int, int]:
        try:
            year_text, month_text = value.split("-", 1)
            year, month = int(year_text), int(month_text)
        except (ValueError, AttributeError) as exc:
            raise ValueError("months must use YYYY-MM format") from exc
        if year < 2000 or not 1 <= month <= 12:
            raise ValueError(f"invalid year-month: {value}")
        return year, month

    start_year, start_num = parse(start_month)
    end_year, end_num = parse(end_month)
    if (start_year, start_num) > (end_year, end_num):
        raise ValueError("start_month must not be after end_month")
    result = []
    year, month = start_year, start_num
    while (year, month) <= (end_year, end_num):
        result.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


class HistDataTickDownloader:
    """Fetch monthly ASCII bid/ask ticks and cache derived M1 OHLC bars."""

    def __init__(
        self,
        *,
        raw_dir: Path,
        cache_dir: Path,
        timeout_seconds: int = 90,
        minimum_request_interval: float = 0.75,
        max_workers: int = 2,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.cache_dir = Path(cache_dir)
        self.timeout_seconds = timeout_seconds
        self.minimum_request_interval = minimum_request_interval
        self.max_workers = max(1, min(int(max_workers), 2))

    @staticmethod
    def archive_path(raw_dir: Path, symbol: str, year: int, month: int) -> Path:
        return Path(raw_dir) / f"HISTDATA_COM_ASCII_{symbol.upper()}_T_{year}{month:02d}.zip"

    def download_month(self, symbol: str, year: int, month: int) -> Path:
        pair = CurrencyPair.from_symbol(symbol)
        route = f"?/ascii/tick-data-quotes/{pair.symbol.lower()}/{year}/{month}"
        referer = _FORM_ROOT + route
        destination = self.archive_path(self.raw_dir, pair.symbol, year, month)
        if destination.exists() and zipfile.is_zipfile(destination):
            return destination

        _reserve_request_slot(self.minimum_request_interval)
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        page_request = Request(
            referer,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
        )
        with opener.open(page_request, timeout=self.timeout_seconds) as response:
            page = response.read().decode("utf-8", "replace")
        parser = _TokenParser()
        parser.feed(page)
        if not parser.token:
            raise RuntimeError(f"HistData page supplied no download token for {pair.symbol} {year}-{month:02d}")

        body = urlencode({
            "tk": parser.token,
            "date": str(year),
            "datemonth": f"{year}{month:02d}",
            "platform": "ASCII",
            "timeframe": "T",
            "fxpair": pair.symbol,
        }).encode()
        _reserve_request_slot(self.minimum_request_interval)
        post_request = Request(
            _POST_URL,
            data=body,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
                "Origin": "https://www.histdata.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with opener.open(post_request, timeout=self.timeout_seconds) as response:
            payload = response.read()
        if not zipfile.is_zipfile(io.BytesIO(payload)):
            raise RuntimeError(
                f"HistData returned a non-ZIP response for {pair.symbol} {year}-{month:02d} "
                f"({len(payload)} bytes)"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(payload)
        temporary.replace(destination)
        return destination

    @staticmethod
    def aggregate_archive(path: Path, symbol: str) -> pl.DataFrame:
        """Build UTC M1 bid/ask OHLC and measured mean spread from raw ticks."""
        pair = CurrencyPair.from_symbol(symbol)
        with zipfile.ZipFile(path) as archive:
            csv_members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(csv_members) != 1:
                raise ValueError(f"Expected one CSV member in {path.name}, found {len(csv_members)}")
            with archive.open(csv_members[0]) as source:
                ticks = pl.read_csv(
                    source,
                    has_header=False,
                    new_columns=["raw_timestamp", "bid", "ask", "volume"],
                    schema_overrides={
                        "raw_timestamp": pl.String,
                        "bid": pl.Float64,
                        "ask": pl.Float64,
                        "volume": pl.Float64,
                    },
                    ignore_errors=False,
                )
        if ticks.is_empty():
            return pl.DataFrame()
        if ticks.filter(
            (pl.col("raw_timestamp").str.len_chars() != 18)
            | pl.col("bid").is_null()
            | pl.col("ask").is_null()
            | (pl.col("bid") <= 0)
            | (pl.col("ask") < pl.col("bid"))
        ).height:
            raise ValueError(f"Invalid or crossed bid/ask ticks found in {path.name}")

        ticks = ticks.with_row_index("_source_order").with_columns(
            (
                pl.col("raw_timestamp").str.slice(0, 15).str.to_datetime(
                    format="%Y%m%d %H%M%S", time_zone="UTC"
                )
                + pl.duration(
                    hours=5,
                    milliseconds=pl.col("raw_timestamp").str.slice(15, 3).cast(pl.Int64),
                )
            ).alias("tick_timestamp")
        ).sort(["tick_timestamp", "_source_order"])

        bars = (
            ticks.with_columns(pl.col("tick_timestamp").dt.truncate("1m").alias("timestamp"))
            .group_by("timestamp", maintain_order=True)
            .agg(
                pl.col("bid").first().alias("open"),
                pl.col("bid").max().alias("high"),
                pl.col("bid").min().alias("low"),
                pl.col("bid").last().alias("close"),
                pl.col("ask").first().alias("ask_open"),
                pl.col("ask").max().alias("ask_high"),
                pl.col("ask").min().alias("ask_low"),
                pl.col("ask").last().alias("ask_close"),
                pl.col("volume").sum().alias("volume"),
                ((pl.col("ask") - pl.col("bid")) / float(pair.pip_size)).mean().alias("spread"),
            )
            .sort("timestamp")
        )
        return MarketDataLoader.normalize_and_validate(bars)

    @staticmethod
    def fill_short_no_quote_gaps(bars: pl.DataFrame, *, maximum_missing_minutes: int = 5) -> tuple[pl.DataFrame, int]:
        """Causally carry the last quote over short no-tick minutes only.

        At the Sunday weekly reopen, permit up to 15 missing minutes after a
        quote resumes; all other gaps longer than ``maximum_missing_minutes``
        remain visible to the strict feed-quality audit.
        """
        if bars.height < 2:
            return bars, 0
        previous_columns = ["timestamp", "close", "ask_close", "spread"]
        gaps = (
            bars.with_columns(
                pl.col("timestamp").diff().alias("_gap"),
                pl.col("timestamp").shift(1).alias("_previous_timestamp"),
                pl.col("close").shift(1).alias("_previous_close"),
                pl.col("ask_close").shift(1).alias("_previous_ask_close"),
                pl.col("spread").shift(1).alias("_previous_spread"),
            )
            .filter(
                (pl.col("_gap") > timedelta(minutes=1))
                & (pl.col("_gap") <= timedelta(minutes=16))
            )
            .select(
                "timestamp", "_gap", "_previous_timestamp", "_previous_close",
                "_previous_ask_close", "_previous_spread",
            )
        )
        fills = []
        for timestamp, gap, previous_timestamp, previous_close, previous_ask_close, previous_spread in gaps.iter_rows():
            minutes = int(gap.total_seconds() // 60) - 1
            sunday_reopen = (
                previous_timestamp.weekday() == 6
                and timestamp.weekday() == 6
                and previous_timestamp.hour in (21, 22, 23)
                and timestamp.hour in (21, 22, 23)
                and timestamp.minute <= 30
            )
            if minutes > maximum_missing_minutes and not sunday_reopen:
                continue
            for offset in range(1, minutes + 1):
                bid = float(previous_close)
                ask = float(previous_ask_close)
                fills.append({
                    "timestamp": timestamp - timedelta(minutes=minutes - offset + 1),
                    "open": bid, "high": bid, "low": bid, "close": bid,
                    "ask_open": ask, "ask_high": ask, "ask_low": ask, "ask_close": ask,
                    "volume": 0.0, "spread": float(previous_spread),
                })
        if not fills:
            return bars, 0
        fill_frame = pl.DataFrame(fills).select(bars.columns)
        completed = pl.concat([bars, fill_frame], how="vertical_relaxed").sort("timestamp")
        return completed, len(fills)

    def download_and_cache(
        self,
        symbols: Iterable[str],
        start_month: str,
        end_month: str,
    ) -> dict[str, dict[str, object]]:
        months = _month_range(start_month, end_month)
        clean_symbols = [CurrencyPair.from_symbol(symbol).symbol for symbol in symbols]
        jobs = [(symbol, year, month) for symbol in clean_symbols for year, month in months]
        archives: dict[str, list[Path]] = {symbol: [] for symbol in clean_symbols}
        failures: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_jobs = {
                executor.submit(self.download_month, symbol, year, month): (symbol, year, month)
                for symbol, year, month in jobs
            }
            completed = 0
            for future in as_completed(future_jobs):
                symbol, year, month = future_jobs[future]
                completed += 1
                try:
                    archive_path = future.result()
                    archives[symbol].append(archive_path)
                    LOG.info("Downloaded %s %04d-%02d", symbol, year, month)
                except Exception as exc:
                    failures.append({"symbol": symbol, "month": f"{year:04d}-{month:02d}", "error": str(exc)})
                    LOG.warning("Failed HistData archive %s %04d-%02d: %s", symbol, year, month, exc)
                if completed % 10 == 0 or completed == len(future_jobs):
                    print(f"HistData archive progress: {completed}/{len(future_jobs)}", flush=True)

        reports: dict[str, dict[str, object]] = {}
        for symbol in clean_symbols:
            month_frames = []
            for archive_path in sorted(archives[symbol]):
                try:
                    month_frames.append(self.aggregate_archive(archive_path, symbol))
                except Exception as exc:
                    failures.append({"symbol": symbol, "month": archive_path.stem[-6:], "error": str(exc)})
            usable = [frame for frame in month_frames if not frame.is_empty()]
            if not usable:
                reports[symbol] = {"status": "NO_TICK_BARS", "archives": len(archives[symbol])}
                continue
            bars = pl.concat(usable, how="vertical_relaxed").sort("timestamp").unique(
                subset=["timestamp"], keep="last", maintain_order=True
            )
            bars, carried_minutes = self.fill_short_no_quote_gaps(bars)
            cache_path = HistoricalECNFetcher.cache_to_parquet(
                bars,
                symbol,
                Timeframe.M1,
                self.cache_dir,
                provenance=DataProvenance.REAL_VENDOR,
                source=(
                    "HistData.com ASCII bid/ask ticks, fixed EST-no-DST converted to UTC; "
                    f"{carried_minutes} short no-quote M1 bars carried forward causally"
                ),
            )
            reports[symbol] = {
                "status": "CACHED",
                "archives": len(archives[symbol]),
                "rows": bars.height,
                "start_utc": str(bars["timestamp"][0]),
                "end_utc": str(bars["timestamp"][-1]),
                "carried_forward_minutes": carried_minutes,
                "path": str(cache_path),
            }

        raw_manifest = {
            "source": "HistData.com ASCII tick quotes",
            "timestamp_convention": "fixed EST (UTC-5), no daylight-saving adjustment; converted by adding five hours",
            "requested_months": [start_month, end_month],
            "symbols": clean_symbols,
            "successful_archives": sum(len(value) for value in archives.values()),
            "failures": failures,
            "datasets": reports,
        }
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / "histdata_download_manifest.json").write_text(
            json.dumps(raw_manifest, indent=2), encoding="utf-8"
        )
        return raw_manifest


__all__ = ["HistDataTickDownloader"]
