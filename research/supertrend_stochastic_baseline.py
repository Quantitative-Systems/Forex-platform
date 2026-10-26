"""Run the frozen GBPUSD M5 SuperTrend/Stochastic structural candidate through research gates.

This command never downloads data, connects to a broker, or enables live trading.
It refuses synthetic/unknown data and never auto-promotes a candidate.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import polars as pl

from forex_platform.discovery.discovery_loop import CandidateHypothesis, ContinuousDiscoveryLoop
from forex_platform.market_data.causal_aligner import CausalAligner, Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_data.provenance import DataProvenance, require_real_data


def load_real_m5_history(
    symbol: str,
    cache_dir: Path,
    *,
    minimum_years: float = 2.0,
) -> tuple[pl.DataFrame, DataProvenance, str, float, int]:
    """Load cached M1 data, verify provenance, and downsample only completed M5 bars."""
    m1_path = HistoricalECNFetcher.get_cache_path(symbol, Timeframe.M1, cache_dir)
    if not m1_path.exists():
        raise FileNotFoundError(
            f"Required cached M1 history is missing: {m1_path}. "
            "Download and audit real vendor/broker data separately; this runner will not fetch data."
        )
    provenance, source = HistoricalECNFetcher.load_provenance(symbol, Timeframe.M1, cache_dir)
    require_real_data(provenance, context=f"{symbol} M1 baseline")
    m1 = MarketDataLoader.normalize_and_validate(
        pl.read_parquet(m1_path),
        expected_interval=Timeframe.M1.duration,
        enforce_monotonicity=True,
    ).sort("timestamp")
    if m1.height < 2:
        raise ValueError(f"{symbol} M1 history is too short")
    years = (m1["timestamp"][-1] - m1["timestamp"][0]).total_seconds() / (365.25 * 86400)
    if years < minimum_years:
        raise ValueError(f"{symbol} history spans {years:.2f} years; minimum required is {minimum_years:.2f}")

    m5 = CausalAligner.downsample(m1, Timeframe.M5)
    # The last M5 bucket may be partial when the source cache ends mid-bucket.
    known_through = m1["timestamp"][-1] + Timeframe.M1.duration
    if "close_time" in m5.columns:
        m5 = m5.filter(pl.col("close_time") <= known_through).drop("close_time")
    m5 = MarketDataLoader.normalize_and_validate(
        m5, expected_interval=Timeframe.M5.duration, enforce_monotonicity=True
    ).sort("timestamp")
    if m5.is_empty():
        raise ValueError(f"{symbol} produced no completed M5 bars")
    return m5, provenance, source, years, m1.height


def run_baseline(
    *,
    cache_dir: Path = Path("data/cache"),
    output_path: Path = Path("research/results/supertrend_stochastic_gbpusd_m5_baseline.json"),
    minimum_years: float = 2.0,
) -> dict[str, Any]:
    symbol = "GBPUSD"
    bars, provenance, source, years, source_bars = load_real_m5_history(
        symbol, cache_dir, minimum_years=minimum_years
    )
    candidate = CandidateHypothesis(
        candidate_id="supertrend-stochastic-gbpusd-m5-v1",
        strategy_name="SuperTrendStochasticStructuralStrategy",
        symbol=symbol,
        timeframe=Timeframe.M5,
        data_provenance=provenance,
    )
    loop = ContinuousDiscoveryLoop(require_real_data=True)
    # Promotion is explicitly disabled: this run can reject or report pending,
    # but can never promote the strategy to paper trading automatically.
    result = loop.evaluate_candidate(candidate, bars, allow_promotion=False)
    payload: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "strategy": candidate.strategy_name,
        "symbol": symbol,
        "entry_timeframe": Timeframe.M5.value,
        "structural_context": {"htf": "1h", "mtf": "15m", "ltf": "5m"},
        "data": {
            "provenance": provenance.value,
            "source": source,
            "source_timeframe": Timeframe.M1.value,
            "source_bars": source_bars,
            "m5_bars": bars.height,
            "years": round(years, 4),
            "start": str(bars["timestamp"][0]),
            "end": str(bars["timestamp"][-1]),
        },
        "result": result.model_dump(mode="json"),
        "safety": {
            "live_trading": False,
            "broker_connection": False,
            "automatic_promotion": False,
            "synthetic_data_allowed": False,
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument(
        "--output", type=Path,
        default=Path("research/results/supertrend_stochastic_gbpusd_m5_baseline.json"),
    )
    parser.add_argument("--minimum-years", type=float, default=2.0)
    args = parser.parse_args()
    if args.minimum_years <= 0:
        parser.error("--minimum-years must be positive")
    payload = run_baseline(
        cache_dir=args.cache_dir,
        output_path=args.output,
        minimum_years=args.minimum_years,
    )
    print(json.dumps({
        "candidate_id": payload["candidate_id"],
        "status": payload["result"]["status"],
        "trade_counts": payload["result"].get("trade_counts", {}),
        "metrics": payload["result"].get("performance_metrics", {}),
        "report": str(args.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
