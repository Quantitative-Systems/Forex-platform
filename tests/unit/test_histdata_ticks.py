from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

import polars as pl
import pytest

from forex_platform.market_data.histdata_ticks import HistDataTickDownloader
from forex_platform.market_data.loader import MarketDataLoader, SchemaError
from forex_platform.market_data.quality import DataQualityAuditor


def _archive(path: Path, rows: list[str]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("DAT_ASCII_EURUSD_T_202501.csv", "\n".join(rows) + "\n")
    return path


def test_aggregate_archive_keeps_observed_bid_ask_ohlc_and_converts_fixed_est():
    archive = _archive(
        Path("tick_sample.zip"),
        [
            "20250101 000001000,1.10000,1.10010,0",
            "20250101 000020000,1.10020,1.10035,0",
            "20250101 000050000,1.10005,1.10015,0",
            "20250101 000101000,1.10010,1.10025,0",
        ],
    )
    try:
        bars = HistDataTickDownloader.aggregate_archive(archive, "EURUSD")
    finally:
        archive.unlink()

    first = bars.row(0, named=True)
    assert first["timestamp"] == datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc)
    assert first["open"] == pytest.approx(1.1)
    assert first["high"] == pytest.approx(1.1002)
    assert first["low"] == pytest.approx(1.1000)
    assert first["close"] == pytest.approx(1.10005)
    assert first["ask_open"] == pytest.approx(1.1001)
    assert first["ask_high"] == pytest.approx(1.10035)
    assert first["ask_low"] == pytest.approx(1.1001)
    assert first["ask_close"] == pytest.approx(1.10015)
    assert first["spread"] == pytest.approx(1.1666666667)


def test_loader_preserves_complete_ask_ohlc_and_spread_pips_alias():
    timestamp = [datetime(2025, 1, 1, tzinfo=timezone.utc)]
    source = pl.DataFrame({
        "timestamp": timestamp,
        "open": [1.1], "high": [1.101], "low": [1.099], "close": [1.1],
        "volume": [0.0], "spread_pips": [0.5],
        "ask_open": [1.10005], "ask_high": [1.10105],
        "ask_low": [1.09905], "ask_close": [1.10005],
    })

    normalized = MarketDataLoader.normalize_and_validate(source)

    assert "ask_open" in normalized.columns
    assert normalized["spread"][0] == pytest.approx(0.5)
    assert DataQualityAuditor.audit(normalized, expected_interval=timedelta(minutes=1)).is_valid


def test_loader_rejects_partial_ask_quote_schema():
    source = pl.DataFrame({
        "timestamp": [datetime(2025, 1, 1, tzinfo=timezone.utc)],
        "open": [1.1], "high": [1.101], "low": [1.099], "close": [1.1],
        "volume": [0.0], "ask_open": [1.1001],
    })

    with pytest.raises(SchemaError, match="Ask-side OHLC"):
        MarketDataLoader.normalize_and_validate(source)


def test_short_no_quote_gap_fill_is_causal_and_bounded():
    start = datetime(2025, 1, 1, 22, 0, tzinfo=timezone.utc)
    bars = pl.DataFrame({
        "timestamp": [start, start + timedelta(minutes=3)],
        "open": [1.1, 1.1003], "high": [1.1001, 1.1004],
        "low": [1.0999, 1.1002], "close": [1.1, 1.1003],
        "ask_open": [1.1001, 1.1004], "ask_high": [1.1002, 1.1005],
        "ask_low": [1.1000, 1.1003], "ask_close": [1.1001, 1.1004],
        "volume": [0.0, 0.0], "spread": [1.0, 1.0],
    })

    completed, carried = HistDataTickDownloader.fill_short_no_quote_gaps(bars)

    assert carried == 2
    assert completed.height == 4
    middle = completed.row(1, named=True)
    assert middle["timestamp"] == start + timedelta(minutes=1)
    assert middle["open"] == middle["high"] == middle["low"] == middle["close"] == 1.1
    assert middle["ask_open"] == middle["ask_close"] == 1.1001


def test_sunday_reopen_gap_fill_is_bounded_to_the_opening_window():
    start = datetime(2025, 1, 5, 22, 6, tzinfo=timezone.utc)
    end = start + timedelta(minutes=9)
    bars = pl.DataFrame({
        "timestamp": [start, end],
        "open": [1.1, 1.1002], "high": [1.1001, 1.1003],
        "low": [1.0999, 1.1001], "close": [1.1, 1.1002],
        "ask_open": [1.1001, 1.1003], "ask_high": [1.1002, 1.1004],
        "ask_low": [1.1, 1.1002], "ask_close": [1.1001, 1.1003],
        "volume": [0.0, 0.0], "spread": [1.0, 1.0],
    })

    completed, carried = HistDataTickDownloader.fill_short_no_quote_gaps(bars)

    assert carried == 8
    assert completed.height == 10

    weekday_start = datetime(2025, 1, 6, 10, 0, tzinfo=timezone.utc)
    weekday_bars = bars.with_columns(
        pl.Series("timestamp", [weekday_start, weekday_start + timedelta(minutes=9)])
    )
    weekday_completed, weekday_carried = HistDataTickDownloader.fill_short_no_quote_gaps(weekday_bars)
    assert weekday_carried == 0
    assert weekday_completed.height == 2
