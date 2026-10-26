from pathlib import Path

import pytest

from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.provenance import DataProvenance
from research.supertrend_stochastic_baseline import load_real_m5_history


def test_baseline_loader_rejects_missing_real_history(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="will not fetch data"):
        load_real_m5_history("GBPUSD", tmp_path, minimum_years=0)


def test_baseline_loader_rejects_synthetic_history_before_backtesting(tmp_path: Path):
    bars = HistoricalECNFetcher.generate_synthetic_ecn_history(
        symbol="GBPUSD", timeframe=Timeframe.M1, num_bars=20,
    )
    HistoricalECNFetcher.cache_to_parquet(
        bars, "GBPUSD", Timeframe.M1, cache_dir=tmp_path,
        provenance=DataProvenance.SYNTHETIC, source="synthetic unit test",
    )
    with pytest.raises(ValueError, match="requires real vendor or broker-exported data"):
        load_real_m5_history("GBPUSD", tmp_path, minimum_years=0)
