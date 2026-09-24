"""
Command Line Interface for Forex Platform.
Provides operational utilities:
- status: Real-time session state, regime tag, rollover and market status.
- screen-sessions: 24-hour session schedule and overlap matrix.
- inspect-pair: Pair specification, pip math, and spread regimes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import sys

from forex_platform.core.domain import CurrencyPair, LotSize, PipCalculator
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.costs.commission import CommissionModel
from forex_platform.costs.spread_model import DynamicSpreadModel


def cmd_status(_args: argparse.Namespace) -> int:
    """Print current global Forex market and session status."""
    now = datetime.now(timezone.utc)
    state = ForexSessionEngine.get_session_state(now)

    print("=" * 65)
    print(" FOREX PLATFORM — SYSTEM STATUS")
    print("=" * 65)
    print(f" Current Time (UTC):       {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f" Day of Week:              {now.strftime('%A')}")
    print(f" Market Status:            {'OPEN' if state.is_market_open else 'CLOSED (WEEKEND)'}")
    print(f" Regime Tag:               {state.regime_tag}")

    if state.active_sessions:
        sessions_str = ", ".join(s.value for s in state.active_sessions)
        print(f" Active Sessions:          {sessions_str}")
    else:
        print(" Active Sessions:          None (Market Inactive)")

    if state.has_overlap:
        print(f" Overlap Type:             {state.overlap_type}")
    print(f" Rollover Window:          {'ACTIVE (Illiquid spread regime)' if state.is_rollover else 'Inactive'}")
    print(f" Triple Swap Trigger:      {'ACTIVE (Wednesday rollover)' if state.is_triple_swap else 'Inactive'}")
    print("=" * 65)
    return 0


def cmd_screen_sessions(args: argparse.Namespace) -> int:
    """Display 24-hour global trading sessions schedule."""
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Error: Invalid date format '{args.date}'. Expected YYYY-MM-DD.", file=sys.stderr)
            return 1
    else:
        target_date = datetime.now(timezone.utc).date()

    print("=" * 75)
    print(f" FOREX 24-HOUR SESSION SCHEDULE ({target_date.strftime('%Y-%m-%d')})")
    print("=" * 75)
    print(" UTC Time Window      Active Sessions             Liquidity Regime")
    print("-" * 75)

    windows = [
        ("00:00 - 06:00 UTC", "Sydney + Tokyo", "Asian Peak Overlap (moderate)"),
        ("06:00 - 07:00 UTC", "Tokyo Solo", "Asian Afternoon (moderate)"),
        ("07:00 - 09:00 UTC", "Tokyo + London", "Asia / Europe Overlap (high)"),
        ("09:00 - 12:00 UTC", "London Solo", "European Morning (high)"),
        ("12:00 - 16:00 UTC", "London + New York", "GLOBAL PEAK OVERLAP (highest liquidity)"),
        ("16:00 - 20:55 UTC", "New York Solo", "US Afternoon (moderate/high)"),
        ("20:55 - 21:15 UTC", "Bank Clearing Window", "DAILY ROLLOVER (10x-20x spreads)"),
        ("21:15 - 24:00 UTC", "Sydney Solo", "Pacific Evening (thin)"),
    ]

    for win, sess, regime in windows:
        print(f" {win:<20} {sess:<27} {regime}")

    print("-" * 75)
    print(" Weekend Closure:   Friday 21:00 UTC through Sunday 21:00 UTC")
    print(" Triple Swap Day:   Wednesday 21:00 UTC (T+2 weekend settlement)")
    print("=" * 75)
    return 0


def cmd_inspect_pair(args: argparse.Namespace) -> int:
    """Display pair specifications, precision pip math, and spread regimes."""
    symbol = args.symbol
    try:
        pair = CurrencyPair.from_symbol(symbol)
    except Exception as e:
        print(f"Error parsing symbol '{symbol}': {e}", file=sys.stderr)
        return 1

    lot = LotSize.from_lots(1.0)
    pip_val_quote = PipCalculator.pip_value_in_quote(pair, lot)

    spread_model = DynamicSpreadModel()
    comm_model = CommissionModel()
    base_spread = spread_model.get_base_spread(pair)

    print("=" * 65)
    print(f" CURRENCY PAIR SPECIFICATION: {pair.symbol}")
    print("=" * 65)
    print(f" Base Currency:            {pair.base_currency}")
    print(f" Quote Currency:           {pair.quote_currency}")
    print(f" Price Precision:          {pair.price_precision} decimals")
    print(f" Pip Size:                 {pair.pip_size}")
    print(f" Pipette Size:             {pair.pipette_size}")
    print(f" Standard Lot Units:       {pair.standard_lot_units:,} units")
    print(f" JPY Cross:                {pair.is_jpy_cross}")
    print("-" * 65)
    print(" PIP VALUE & COMMISSIONS (1.0 Standard Lot = 100,000 units):")
    print(f" 1 Pip Value (in Quote):   {pip_val_quote} {pair.quote_currency}")
    if pair.quote_currency == "USD":
        print(f" 1 Pip Value (in USD):     ${pip_val_quote:.2f} USD")
    elif pair.base_currency == "USD":
        sample_rate = Decimal("150.00") if pair.is_jpy_cross else Decimal("1.3000")
        sample_pip_usd = PipCalculator.pip_value(pair, lot, Decimal("1.0") / sample_rate)
        print(f" 1 Pip Value (at {sample_rate}): ~${sample_pip_usd:.2f} USD")

    single_side_comm = comm_model.calculate_commission(lot, is_roundturn=False)
    roundturn_comm = comm_model.calculate_commission(lot, is_roundturn=True)
    print(f" Raw ECN Commission:       ${single_side_comm:.2f} / side (${roundturn_comm:.2f} roundturn)")
    print("-" * 65)
    print(" DYNAMIC SPREAD REGIMES:")
    overlap_spread = base_spread * DynamicSpreadModel.MULTIPLIER_LONDON_NY_OVERLAP
    london_spread = base_spread * DynamicSpreadModel.MULTIPLIER_WESTERN_SOLO
    asian_spread = base_spread * DynamicSpreadModel.MULTIPLIER_ASIAN_SESSION
    rollover_spread = base_spread * DynamicSpreadModel.MULTIPLIER_ROLLOVER

    print(f" London / NY Overlap:      {overlap_spread:.2f} pips (Baseline 1.0x)")
    print(f" London / NY Solo:         {london_spread:.2f} pips (1.3x)")
    print(f" Asian Session:            {asian_spread:.2f} pips (3.5x)")
    print(f" Rollover Window:          {rollover_spread:.2f} pips (15.0x)")
    print("=" * 65)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build root CLI parser."""
    parser = argparse.ArgumentParser(
        prog="forex-platform",
        description="Forex Platform - Quantitative Trading and Backtesting Core Engine CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: status
    subparsers.add_parser("status", help="Show current Forex market and session status")

    # Command: screen-sessions
    screen_p = subparsers.add_parser("screen-sessions", help="Display 24-hour session schedule")
    screen_p.add_argument("--date", type=str, help="Date to inspect (YYYY-MM-DD), default is today UTC")

    # Command: inspect-pair
    inspect_p = subparsers.add_parser("inspect-pair", help="Inspect currency pair specs, pips, and spreads")
    inspect_p.add_argument("symbol", type=str, help="Currency pair symbol (e.g. EURUSD, USDJPY, EUR/GBP)")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI Entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        return cmd_status(args)
    elif args.command == "screen-sessions":
        return cmd_screen_sessions(args)
    elif args.command == "inspect-pair":
        return cmd_inspect_pair(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
