"""Institutional campaign runner with walk-forward and enhanced strategy."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_data.instruments import ALL_SYMBOLS, MAJOR_SYMBOLS, MINOR_SYMBOLS
from forex_platform.research_engine.backtester import (
    EventDrivenBacktester,
    DynamicSpreadModel,
    CommissionModel,
    SwapEngine,
)
from forex_platform.research_engine.walkforward import WalkForwardAnalyzer
from forex_platform.strategy_engine.supertrend_stochastic_institutional import (
    SupertrendStochasticInstitutional,
)


def load_pairs(symbols_arg, mode):
    if symbols_arg:
        return symbols_arg
    if mode == "all":
        return ALL_SYMBOLS
    if mode == "majors":
        return MAJOR_SYMBOLS
    return MINOR_SYMBOLS


def build_strategy(symbols):
    return SupertrendStochasticInstitutional(
        symbols=symbols,
        account_equity=100000.0,
        risk_fraction=0.01,
        min_rr_tp3=6.0,
        enable_news_filter=True,
        enable_zone_filter=True,
        enable_ob_stop=True,
        enable_fvg_target=True,
        max_concurrent_trades_per_symbol=4,
    )


def run_commands(args):
    symbols = load_pairs(args.symbols, args.mode)
    print(f"Running campaign: {args.mode} currencies = {symbols}")
    results = {}
    for symbol in symbols:
        import polars as pl
        from forex_platform.core.domain import CurrencyPair

        target = Path(f"data/cache/dukascopy/{symbol}_M1.parquet")
        if not target.exists():
            print(f"SKIP {symbol}: no data")
            continue
        bars = pl.read_parquet(target)
        bars = MarketDataLoader.normalize_and_validate(bars, expected_interval=None, enforce_monotonicity=True)
        years = (bars["timestamp"].max() - bars["timestamp"].min()).days / 365.25
        if years < args.minimum_years:
            print(f"SKIP {symbol}: only {years:.2f}y")
            continue
        strategy = build_strategy([symbol])
        pair = CurrencyPair.from_symbol(symbol)
        backtester = EventDrivenBacktester(
            strategy=strategy,
            currency_pair=pair,
            spread_model=DynamicSpreadModel(),
            commission_model=CommissionModel(),
            swap_engine=SwapEngine(),
        )
        result = backtester.run(bars)
        results[symbol] = {
            "symbol": symbol,
            "strategy": "SupertrendStochasticInstitutional",
            "total_trades": result.total_trades,
            "winning": result.winning_trades,
            "losing": result.losing_trades,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "expectancy": float(result.expectancy),
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": result.sharpe_ratio,
            "total_net_pnl": float(result.total_net_pnl),
            "final_balance": float(result.final_balance),
            "data_years": years,
        }
        print(f"{symbol}: trades={result.total_trades} PF={result.profit_factor:.2f} expR={result.expectancy:.2f}R DD={result.max_drawdown_pct:.2%}")
    return results


def analyze_results(results, output_file):
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"Results written to {output_file}")
    total_trades = sum(r.get("total_trades", 0) for r in results.values())
    best_pf = max((r.get("profit_factor", 0) for r in results.values()), default=0)
    print(f"Total trades: {total_trades}")
    print(f"Best profit factor: {best_pf:.2f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Institutional SuperTrend-Stochastic Campaign")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols (e.g. EURUSD GBPUSD USDJPY AUDUSD)")
    parser.add_argument("--mode", choices=["all", "majors", "minors"], default="all")
    parser.add_argument("--results", type=Path, default=Path("research/results/institutional_campaign.json"))
    parser.add_argument("--minimum-years", type=float, default=2.0)
    args = parser.parse_args()

    results = run_commands(args)
    analyze_results(results, args.results)
