"""Download paired Dukascopy bid/ask M1 history for the fractal research ladder.

This stores the two observed offer sides and derives the bar spread from the
observed open and close quote differences. No fixed or synthetic spread is
inserted. Output is isolated under ``data/cache/dukascopy`` so it does not
replace existing vendor caches.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

import dukascopy_python as dp
import polars as pl

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_data.provenance import DataProvenance

INSTRUMENTS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
}


def _side_frame(symbol: str, start: datetime, end: datetime, side: str) -> pl.DataFrame:
    source = dp.fetch(
        INSTRUMENTS[symbol], dp.INTERVAL_MIN_1, side, start, end,
        max_retries=5, limit=30_000,
    )
    if source.empty:
        return pl.DataFrame(schema={"timestamp": pl.Datetime("us", "UTC")})
    frame = pl.from_pandas(source.reset_index())
    timestamp = "timestamp" if "timestamp" in frame.columns else frame.columns[0]
    return frame.rename({timestamp: "timestamp"}).with_columns(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC"))
    ).filter(
        (pl.col("timestamp") >= start) & (pl.col("timestamp") < end)
    ).sort("timestamp")


def download_pair(symbol: str, start: datetime, end: datetime, cache_dir: Path) -> Path:
    symbol = symbol.upper()
    if symbol not in INSTRUMENTS:
        raise ValueError(f"Unsupported symbol: {symbol}")
    pair = CurrencyPair.from_symbol(symbol)
    print(f"{symbol}: fetching Dukascopy BID M1 from {start.isoformat()} to {end.isoformat()}", flush=True)
    bid = _side_frame(symbol, start, end, dp.OFFER_SIDE_BID).rename({
        name: f"bid_{name}" for name in ("open", "high", "low", "close", "volume")
    })
    print(f"{symbol}: BID rows={bid.height}; fetching ASK", flush=True)
    ask = _side_frame(symbol, start, end, dp.OFFER_SIDE_ASK).rename({
        name: f"ask_{name}" for name in ("open", "high", "low", "close", "volume")
    })
    if bid.is_empty() or ask.is_empty():
        raise RuntimeError(f"Dukascopy returned an empty BID or ASK history for {symbol}")

    bars = bid.join(ask, on="timestamp", how="inner", validate="1:1").sort("timestamp")
    if bars.is_empty():
        raise RuntimeError(f"No aligned BID/ASK timestamps for {symbol}")
    bars = bars.with_columns(
        pl.col("bid_open").alias("open"),
        pl.col("bid_high").alias("high"),
        pl.col("bid_low").alias("low"),
        pl.col("bid_close").alias("close"),
        pl.col("bid_volume").alias("volume"),
        (
            ((pl.col("ask_open") - pl.col("bid_open"))
             + (pl.col("ask_close") - pl.col("bid_close")))
            / (2.0 * float(pair.pip_size))
        ).alias("spread"),
    ).select(
        "timestamp", "open", "high", "low", "close", "volume", "spread",
        "ask_open", "ask_high", "ask_low", "ask_close",
    )
    bars = MarketDataLoader.normalize_and_validate(
        bars, expected_interval=Timeframe.M1.duration, enforce_monotonicity=True
    )
    invalid_quotes = bars.filter(
        (pl.col("ask_open") < pl.col("open"))
        | (pl.col("ask_high") < pl.col("high"))
        | (pl.col("ask_low") < pl.col("low"))
        | (pl.col("ask_close") < pl.col("close"))
        | (pl.col("spread") < 0)
    )
    if invalid_quotes.height:
        raise RuntimeError(f"Dukascopy returned {invalid_quotes.height} crossed/invalid quote bars for {symbol}")

    target = HistoricalECNFetcher.get_cache_path(symbol, Timeframe.M1, cache_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    bars.write_parquet(target, compression="zstd")
    metadata_path = target.with_suffix(target.suffix + ".meta.json")
    metadata_path.write_text(json.dumps({
        "classification": DataProvenance.REAL_VENDOR.value,
        "source": "Dukascopy historical BID and ASK M1 candlesticks; spread is mean of observed open and close quote differences in pips",
        "rows": bars.height,
        "requested_start_utc": start.isoformat(),
        "requested_end_utc_exclusive": end.isoformat(),
        "first_timestamp_utc": bars["timestamp"][0].isoformat(),
        "last_timestamp_utc": bars["timestamp"][-1].isoformat(),
    }, indent=2), encoding="utf-8")
    print(f"{symbol}: wrote {bars.height:,} aligned BID/ASK M1 bars to {target}", flush=True)
    return target


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", choices=sorted(INSTRUMENTS), default=list(INSTRUMENTS))
    parser.add_argument("--start", default="2024-01-01T00:00:00+00:00")
    parser.add_argument("--end", default=None, help="Exclusive UTC end; defaults to the current minute")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/dukascopy"))
    args = parser.parse_args(list(argv) if argv is not None else None)

    start = datetime.fromisoformat(args.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    end = end.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if start >= end:
        parser.error("--start must be earlier than --end")

    failures: list[str] = []
    for symbol in args.symbols:
        try:
            download_pair(symbol, start, end, args.cache_dir)
        except Exception as exc:
            failures.append(f"{symbol}: {type(exc).__name__}: {exc}")
            print(f"{failures[-1]}", flush=True)
    print(f"Dukascopy M1 acquisition complete: {len(args.symbols) - len(failures)}/{len(args.symbols)} pairs succeeded.", flush=True)
    for failure in failures:
        print(f"FAILED {failure}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
