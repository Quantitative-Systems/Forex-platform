"""
Historical ECN Market Data Fetcher, Parser, and Parquet Cache Manager.
Supports institutional formats (Dukascopy, HistData, MetaTrader ECN) and
converts historical feeds to high-performance Parquet datasets in data/cache/.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union
import numpy as np
import polars as pl

from forex_platform.core.domain import CurrencyPair
from forex_platform.core.instruments import FXInstrumentRegistry
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.loader import MarketDataError, MarketDataLoader
from forex_platform.market_data.quality import DataQualityAuditor, QualityAuditReport
from forex_platform.market_data.provenance import DataProvenance

logger = logging.getLogger(__name__)


class HistoricalECNFetcher:
    """
    Ingestion engine and format normalizer for institutional OTC Forex datasets.
    Stores and reads binary Parquet caches inside data/cache/.
    """

    CACHE_DIR = Path("data/cache")
    SUPPORTED_PAIRS = list(FXInstrumentRegistry.SYMBOLS)

    @classmethod
    def get_cache_path(
        cls,
        symbol: str,
        timeframe: Timeframe = Timeframe.M15,
        cache_dir: Optional[Path] = None,
    ) -> Path:
        target_dir = cache_dir or cls.CACHE_DIR
        clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        return target_dir / f"{clean}_{timeframe.name}.parquet"

    @classmethod
    def parse_external_csv(cls, file_path: Union[str, Path]) -> pl.DataFrame:
        """
        Auto-detects and parses external CSV formats:
        - Dukascopy: 'Gmt time,Open,High,Low,Close,Volume'
        - HistData: 'YYYYMMDD HHMMSS;open;high;low;close;volume'
        - MetaTrader MT4/MT5: '<DATE> <TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<TICKVOL>,<VOL>,<SPREAD>'
        - Standard canonical: 'timestamp,open,high,low,close,volume,spread'
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Source file not found at {path}")

        # Read first few lines to detect delimiter and header
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline().strip()
            second_line = f.readline().strip()

        if "\t" in first_line:
            delimiter = "\t"
        elif ";" in first_line:
            delimiter = ";"
        else:
            delimiter = ","

        # 1. Check if Dukascopy format
        if "gmt time" in first_line.lower():
            return cls._parse_dukascopy_csv(path, delimiter)

        # 2. Check if MetaTrader header
        if "<date>" in first_line.lower() or "<time>" in first_line.lower():
            return cls._parse_metatrader_csv(path, delimiter)

        # 3. Check if headerless HistData (e.g. '20260105 000000;1.0850;1.0855;...')
        if not any(c.isalpha() for c in first_line.replace("GMT", "").replace("UTC", "")):
            return cls._parse_histdata_csv(path, delimiter)

        # 4. Standard delimited CSV with column names
        df = pl.read_csv(path, separator=delimiter, try_parse_dates=True)
        return MarketDataLoader.normalize_and_validate(df)

    @classmethod
    def _parse_dukascopy_csv(cls, path: Path, delimiter: str) -> pl.DataFrame:
        """
        Parses Dukascopy historical bar export.
        Format: 'Gmt time,Open,High,Low,Close,Volume'
        Timestamp formats: 'DD.MM.YYYY HH:MM:SS.mmm' or 'YYYY-MM-DD HH:MM:SS'
        """
        df = pl.read_csv(path, separator=delimiter)
        time_col = [c for c in df.columns if "time" in c.lower() or "gmt" in c.lower()][0]

        # Convert timestamp to UTC Datetime
        # Try multiple formats
        sample_val = str(df[time_col][0]).strip()
        if "." in sample_val and len(sample_val.split(".")[0]) == 2:
            # DD.MM.YYYY HH:MM:SS or DD.MM.YYYY HH:MM:SS.mmm
            fmt = "%d.%m.%Y %H:%M:%S.%3f" if len(sample_val) > 19 else "%d.%m.%Y %H:%M:%S"
            df = df.with_columns(
                pl.col(time_col).str.to_datetime(format=fmt, time_zone="UTC").alias("timestamp")
            )
        else:
            df = df.with_columns(
                pl.col(time_col).str.to_datetime(time_zone="UTC").alias("timestamp")
            )

        rename_dict = {
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
        for k, v in rename_dict.items():
            if k in df.columns:
                df = df.rename({k: v})

        if "spread" not in df.columns:
            df = df.with_columns(pl.lit(1.0, dtype=pl.Float64).alias("spread"))

        return MarketDataLoader.normalize_and_validate(df)

    @classmethod
    def _parse_histdata_csv(cls, path: Path, delimiter: str) -> pl.DataFrame:
        """
        Parses HistData headerless export.
        Format: 'YYYYMMDD HHMMSS;open;high;low;close;volume'
        """
        raw_df = pl.read_csv(path, separator=delimiter, has_header=False)
        ts_series = raw_df.select(
            pl.col("column_1").str.to_datetime(format="%Y%m%d %H%M%S", time_zone="UTC")
        )["column_1"]

        n = raw_df.height
        vol_series = (
            raw_df["column_6"].cast(pl.Float64)
            if raw_df.width > 5
            else pl.Series("volume", [100.0] * n, dtype=pl.Float64)
        )
        spread_series = pl.Series("spread", [1.0] * n, dtype=pl.Float64)

        df = pl.DataFrame({
            "timestamp": ts_series,
            "open": raw_df["column_2"].cast(pl.Float64),
            "high": raw_df["column_3"].cast(pl.Float64),
            "low": raw_df["column_4"].cast(pl.Float64),
            "close": raw_df["column_5"].cast(pl.Float64),
            "volume": vol_series,
            "spread": spread_series,
        })
        return MarketDataLoader.normalize_and_validate(df)

    @classmethod
    def _parse_metatrader_csv(cls, path: Path, delimiter: str) -> pl.DataFrame:
        """
        Parses MetaTrader MT4/MT5 CSV export.
        Format: '<DATE>	<TIME>	<OPEN>	<HIGH>	<LOW>	<CLOSE>	<TICKVOL>	<VOL>	<SPREAD>'
        """
        raw_df = pl.read_csv(path, separator=delimiter)
        col_map = {c: c.strip().lower().replace("<", "").replace(">", "") for c in raw_df.columns}
        renamed = raw_df.rename(col_map)

        if "date" in renamed.columns and "time" in renamed.columns:
            # Combine Date and Time
            combined_ts = renamed.select(
                (pl.col("date").str.strip_chars() + " " + pl.col("time").str.strip_chars()).alias("raw_ts")
            )["raw_ts"]

            sample = str(combined_ts[0])
            sep = "." if "." in sample.split()[0] else "-"
            fmt = f"%Y{sep}%m{sep}%d %H:%M:%S"
            dt_series = combined_ts.str.to_datetime(format=fmt, time_zone="UTC")
        else:
            dt_series = renamed["timestamp"].str.to_datetime(time_zone="UTC")

        volume_col = "tickvol" if "tickvol" in renamed.columns else ("vol" if "vol" in renamed.columns else "volume")
        spread_col = "spread" if "spread" in renamed.columns else None

        n = renamed.height
        vol_series = (
            renamed[volume_col].cast(pl.Float64)
            if volume_col in renamed.columns
            else pl.Series("volume", [100.0] * n, dtype=pl.Float64)
        )
        spread_series = (
            renamed[spread_col].cast(pl.Float64)
            if spread_col in renamed.columns
            else pl.Series("spread", [1.0] * n, dtype=pl.Float64)
        )

        df = pl.DataFrame({
            "timestamp": dt_series,
            "open": renamed["open"].cast(pl.Float64),
            "high": renamed["high"].cast(pl.Float64),
            "low": renamed["low"].cast(pl.Float64),
            "close": renamed["close"].cast(pl.Float64),
            "volume": vol_series,
            "spread": spread_series,
        })
        return MarketDataLoader.normalize_and_validate(df)

    @classmethod
    def cache_to_parquet(
        cls,
        df: pl.DataFrame,
        symbol: str,
        timeframe: Timeframe = Timeframe.M15,
        cache_dir: Optional[Path] = None,
        *,
        provenance: DataProvenance = DataProvenance.UNKNOWN,
        source: str = "cache",
    ) -> Path:
        """
        Save canonical validated DataFrame to high-performance Parquet format.
        """
        target_path = cls.get_cache_path(symbol, timeframe, cache_dir)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(target_path, compression="zstd")
        metadata_path = target_path.with_suffix(target_path.suffix + ".meta.json")
        metadata_path.write_text(json.dumps({
            "classification": provenance.value,
            "source": source,
            "rows": df.height,
        }, indent=2), encoding="utf-8")
        logger.info("Saved %d bars for %s (%s) to %s", df.height, symbol, timeframe.value, target_path)
        return target_path

    @classmethod
    def load_provenance(
        cls,
        symbol: str,
        timeframe: Timeframe = Timeframe.M15,
        cache_dir: Optional[Path] = None,
    ) -> tuple[DataProvenance, str]:
        """Read cache provenance; missing or malformed metadata is UNKNOWN."""
        path = cls.get_cache_path(symbol, timeframe, cache_dir).with_suffix(
            cls.get_cache_path(symbol, timeframe, cache_dir).suffix + ".meta.json"
        )
        if not path.exists():
            return DataProvenance.UNKNOWN, "missing cache metadata"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return DataProvenance(payload["classification"]), str(payload.get("source", "cache"))
        except (OSError, ValueError, KeyError, TypeError):
            return DataProvenance.UNKNOWN, "invalid cache metadata"

    @classmethod
    def load_from_parquet(
        cls,
        symbol: str,
        timeframe: Timeframe = Timeframe.M15,
        cache_dir: Optional[Path] = None,
    ) -> Optional[pl.DataFrame]:
        """
        Read dataset from Parquet cache if available.
        """
        target_path = cls.get_cache_path(symbol, timeframe, cache_dir)
        if target_path.exists():
            return pl.read_parquet(target_path)
        target_dir = cache_dir or cls.CACHE_DIR
        clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        alt_path = target_dir / f"{clean}_{timeframe.value}.parquet"
        if alt_path.exists():
            return pl.read_parquet(alt_path)
        return None

    @classmethod
    def generate_synthetic_ecn_history(
        cls,
        symbol: str = "EURUSD",
        timeframe: Timeframe = Timeframe.M15,
        num_bars: int = 1000,
        start_dt: Optional[datetime] = None,
    ) -> pl.DataFrame:
        """
        Generates deterministic, high-fidelity synthetic ECN market history.
        Includes session-varying spreads, volatility cycles, and structural weekend gaps.
        """
        pair = CurrencyPair.from_symbol(symbol)
        is_jpy = pair.is_jpy_cross
        base_price = 150.00 if is_jpy else (1.2500 if "GBP" in symbol else 1.0850)
        t0 = start_dt or datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
        rng = np.random.default_rng(12345)

        interval = timeframe.to_timedelta()
        rows = []
        curr_time = t0
        curr_price = base_price

        bars_generated = 0
        while bars_generated < num_bars:
            # Check weekend closure: Friday 21:00 UTC to Sunday 21:00 UTC
            if ForexSessionEngine.is_weekend(curr_time):
                # Jump past weekend to Sunday 21:00 UTC
                days_ahead = (6 - curr_time.weekday()) % 7
                if days_ahead == 0 and curr_time.hour < 21:
                    reopen = curr_time.replace(hour=21, minute=0, second=0, microsecond=0)
                else:
                    reopen = (curr_time + timedelta(days=(days_ahead or 7))).replace(
                        hour=21, minute=0, second=0, microsecond=0
                    )
                curr_time = reopen
                # Small weekend gap
                curr_price += float(rng.normal(0, 0.0008 if not is_jpy else 0.08))

            state = ForexSessionEngine.get_session_state(curr_time)

            # Empirical spread modeling
            if state.is_rollover:
                spread = 12.0
            elif state.has_overlap and "NEW_YORK" in [s.value for s in state.active_sessions]:
                spread = 0.8
            elif "LONDON" in [s.value for s in state.active_sessions]:
                spread = 1.0
            elif "TOKYO" in [s.value for s in state.active_sessions]:
                spread = 2.2
            else:
                spread = 3.0

            vol_step = 0.0003 if not is_jpy else 0.03
            if state.has_overlap:
                vol_step *= 1.8

            step = float(rng.normal(0.00001, vol_step))
            open_ = curr_price
            close_ = curr_price + step
            high_ = max(open_, close_) + abs(float(rng.normal(0, vol_step * 0.5)))
            low_ = min(open_, close_) - abs(float(rng.normal(0, vol_step * 0.5)))
            volume = float(rng.integers(100, 1500))

            rows.append({
                "timestamp": curr_time,
                "open": round(open_, pair.price_precision),
                "high": round(high_, pair.price_precision),
                "low": round(low_, pair.price_precision),
                "close": round(close_, pair.price_precision),
                "volume": volume,
                "spread": spread,
            })

            curr_price = close_
            curr_time += interval
            bars_generated += 1

        df = pl.DataFrame(rows)
        return MarketDataLoader.normalize_and_validate(df, expected_interval=interval)

    @classmethod
    def fetch_or_load(
        cls,
        symbol: str,
        timeframe: Timeframe = Timeframe.M15,
        source_file: Optional[Union[str, Path]] = None,
        cache_dir: Optional[Path] = None,
        auto_generate_synthetic_if_missing: bool = True,
        synthetic_bars: int = 1000,
    ) -> pl.DataFrame:
        """
        Unified loading method:
        1. Checks Parquet cache.
        2. If absent and source_file provided, parses, validates, and caches to Parquet.
        3. If absent and auto_generate_synthetic_if_missing, generates synthetic history and caches.
        """
        # 1. Cache hit
        cached_df = cls.load_from_parquet(symbol, timeframe, cache_dir)
        if cached_df is not None:
            logger.info("Loaded %s (%s) from Parquet cache.", symbol, timeframe.value)
            return cached_df

        # 2. Source file provided
        if source_file is not None and Path(source_file).exists():
            df = cls.parse_external_csv(source_file)
            cls.cache_to_parquet(
                df, symbol, timeframe, cache_dir,
                provenance=DataProvenance.REAL_VENDOR,
                source=str(source_file),
            )
            return df

        # 3. Fallback to synthetic ECN history
        if auto_generate_synthetic_if_missing:
            logger.info("Generating synthetic ECN dataset for %s (%s)...", symbol, timeframe.value)
            df = cls.generate_synthetic_ecn_history(symbol=symbol, timeframe=timeframe, num_bars=synthetic_bars)
            cls.cache_to_parquet(
                df, symbol, timeframe, cache_dir,
                provenance=DataProvenance.SYNTHETIC,
                source="synthetic generator",
            )
            return df

        raise FileNotFoundError(
            f"No cached Parquet or source file available for {symbol} ({timeframe.value})."
        )
