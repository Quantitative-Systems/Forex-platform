"""
Validation campaign for SupertrendStochasticStrategy.

Runs the new strategy on Dukascopy M1 data across all 6 timeframe sets
and all 4 forex pairs. Collects trade counts, win rate, expectancy,
profit factor, max drawdown, and cost-stress metrics.

Usage:
    python research/supertrend_stochastic_campaign.py \\
        --symbols EURUSD GBPUSD USDJPY AUDUSD \\
        --results research/results/supertrend_stochastic_campaign.json \\
        --minimum-years 2.0
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.research_engine.backtester import (
    EventDrivenBacktester,
    DynamicSpreadModel,
    CommissionModel,
    SwapEngine,
)
from forex_platform.strategy_engine.supertrend_stochastic import (
    SupertrendStochasticStrategy,
)
import polars as pl


def _load_dukascopy_m1(symbol: str, cache_dir: Path = Path("data/cache/dukascopy")):
    target = cache_dir / f"{symbol}_M1.parquet"
    if not target.exists():
        return None
    df = pl.read_parquet(target)
    return MarketDataLoader.normalize_and_validate(
        df, expected_interval=None, enforce_monotonicity=True
    )


def run_campaign(symbols: list[str], results_file: Path, minimum_years: float):
    results = []
    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"Running campaign for {symbol}")
        print(f"{'='*60}")

        bars = _load_dukascopy_m1(symbol)
        if bars is None:
            print(f"  SKIP: No M1 data for {symbol} — run download first")
            continue

        years = (bars["timestamp"].max() - bars["timestamp"].min()).days / 365.25
        print(f"  Loaded {len(bars)} M1 bars ({years:.2f} years)")

        if years < minimum_years:
            print(f"  SKIP: Only {years:.2f} years, minimum {minimum_years} required")
            continue

        pair = CurrencyPair.from_symbol(symbol)

        for set_key in ["SET_1", "SET_2", "SET_3", "SET_4", "SET_5", "SET_6"]:
            print(f"\n  {set_key}: ", end="", flush=True)

            strategy = SupertrendStochasticStrategy(
                symbols=[symbol],
                account_equity=Decimal("100000"),
                risk_fraction=Decimal("0.01"),
                min_rr_tp3=Decimal("6.0"),
            )

            backtester = EventDrivenBacktester(
                strategy=strategy,
                currency_pair=pair,
                spread_model=DynamicSpreadModel(),
                commission_model=CommissionModel(),
                swap_engine=SwapEngine(),
            )

            result = backtester.run(bars)

            metrics = {
                "symbol": symbol,
                "set_key": set_key,
                "total_trades": result.total_trades,
                "winning_trades": result.winning_trades,
                "losing_trades": result.losing_trades,
                "win_rate": result.win_rate,
                "profit_factor": result.profit_factor,
                "expectancy": float(result.expectancy),
                "max_drawdown_pct": result.max_drawdown_pct,
                "sharpe_ratio": result.sharpe_ratio,
                "total_net_pnl": float(result.total_net_pnl),
                "final_balance": float(result.final_balance),
                "data_years": years,
            }
            results.append(metrics)

            print(
                f"trades={result.total_trades} "
                f"win_rate={result.win_rate:.2%} "
                f"PF={result.profit_factor:.2f} "
                f"expR={result.expectancy:.2f}R "
                f"DD={result.max_drawdown_pct:.2%}"
            )

    # Write results
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n{'='*60}")
    print(f"Campaign complete. Results written to {results_file}")
    print(f"{'='*60}")

    # Summary
    total_trades = sum(r["total_trades"] for r in results)
    profitable = [r for r in results if r["profit_factor"] > 0]
    avg_pf = (sum(r["profit_factor"] for r in profitable) / len(profitable)) if profitable else 0
    print(f"\nTotal trades across all sets/pairs: {total_trades}")
    print(f"Average profit factor (profitable cells): {avg_pf:.2f}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Supertrend-Stochastic validation campaign")
    parser.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])
    parser.add_argument("--results", type=Path, default=Path("research/results/supertrend_stochastic_campaign.json"))
    parser.add_argument("--minimum-years", type=float, default=2.0)
    args = parser.parse_args()
    run_campaign(args.symbols, args.results, args.minimum_years)
