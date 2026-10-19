"""
Shadow (paper) trading runner for SuperTrend-Stochastic MTF strategy.

Runs the validated strategy in paper-trading mode using the existing
MicrostructurePaperSimulator + SQLitePaperLedger + AutoTradingController,
with the pre-trade risk firewall enforced (1% risk per trade, drawdown limits).

Usage:
    python research/shadow_trading_runner.py \
        --symbols EURUSD GBPUSD USDJPY AUDUSD \
        --days 60 \
        --equity 100000

This does NOT submit live orders. It runs in PAPER mode only.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forex_platform.core.domain import CurrencyPair, to_decimal
from forex_platform.auto_trading.controller import AutoTradingController, BrokerAccountConfig
from forex_platform.strategy_engine.supertrend_stochastic import SupertrendStochasticStrategy
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.adapters.fix_adapter import FIXAdapter


def build_controller(symbols: list[str], equity: Decimal) -> AutoTradingController:
    strategies = [
        SupertrendStochasticStrategy(
            symbols=symbols,
            account_equity=equity,
            risk_fraction=Decimal("0.01"),
            min_rr_tp3=Decimal("6.0"),
            max_concurrent_trades_per_symbol=6,
            enable_news_filter=True,
        )
    ]

    broker_configs = [
        BrokerAccountConfig(
            account_id="paper_forex_001",
            broker_name="PAPER_ECN",
            broker_type="PAPER",
            is_live=False,
        )
    ]

    market_data = FIXAdapter(
        symbols=symbols,
        timeframes=[Timeframe.M1, Timeframe.M5, Timeframe.H1, Timeframe.H4, Timeframe.D1],
    )

    controller = AutoTradingController(
        strategies=strategies,
        broker_configs=broker_configs,
        market_data_adapter=market_data,
        initial_balance=equity,
    )
    return controller


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shadow (paper) trading runner")
    parser.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])
    parser.add_argument("--days", type=int, default=60, help="Shadow validation days")
    parser.add_argument("--equity", type=str, default="100000", help="Starting account equity")
    parser.add_argument("--dry-run", action="store_true", help="Build controller only, do not start")
    args = parser.parse_args(argv)

    equity = to_decimal(args.equity)
    print(f"Shadow trading runner — PAPER MODE (no live orders)")
    print(f"Symbols: {args.symbols}")
    print(f"Equity: ${equity:,}")
    print(f"Shadow window: {args.days} days")
    print(f"Start: {datetime.now(timezone.utc).isoformat()}")
    print("Live capital: $0.00\n")

    controller = build_controller(args.symbols, equity)

    if args.dry_run:
        print("Dry run complete — controller built successfully. Not starting.")
        return 0

    if not controller.start():
        print(f"Failed to start shadow controller: {controller.last_error}")
        return 1

    print("Shadow controller started. Press Ctrl+C to stop.\n")
    try:
        end_time = datetime.now(timezone.utc) + timedelta(days=args.days)
        while datetime.now(timezone.utc) < end_time:
            status = controller.get_status()
            print(
                f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} | "
                f"Status: {status.get('controller_status', '?')} | "
                f"Orders: {status.get('stats', {}).get('orders_submitted', 0)} | "
                f"PnL: ${status.get('stats', {}).get('total_pnl', 0):.2f}",
                flush=True,
            )
            time.sleep(30.0)
    except KeyboardInterrupt:
        print("\nStopping shadow trading...")
    finally:
        controller.stop()

    print("Shadow validation complete.")
    print("Paper ledger: data/paper_shadow_ledger.sqlite")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
