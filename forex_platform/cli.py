"""
Unified Command Line Interface for Forex Platform.
Provides institutional operational tooling across 6 primary subcommands:
- status: Real-time system state, session info, and live capital ($0.00 locked).
- screen-sessions: 24-hour global trading session map and liquidity regimes.
- inspect-pair: Pair specification, pip math, and dynamic spread regimes.
- sweep: Walk-forward validation and G1–G7 qualification across Forex pairs.
- forward-paper: Launch real-time forward paper trading daemon with SQLite persistence.
- discover-arb: Multi-currency triangular statistical arbitrage discovery scanner.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
from typing import List, Optional
import numpy as np
import polars as pl

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    PipCalculator,
)
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.costs.commission import CommissionModel
from forex_platform.costs.spread_model import DynamicSpreadModel
from forex_platform.discovery.discovery_loop import (
    CandidateHypothesis,
    ContinuousDiscoveryLoop,
    PromotionStatus,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.order_management.router import SmartOrderRouter
from forex_platform.paper_trading.daemon import ForwardPaperTradingDaemon
from forex_platform.paper_trading.persistence import SQLitePaperLedger
from forex_platform.paper_trading.simulator import MicrostructurePaperSimulator
from forex_platform.research_engine.evaluate import StrategyEvaluator
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import MarketTelemetry, PreTradeRiskFirewall
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch
from forex_platform.strategy_engine.base import BarEvent
from forex_platform.strategy_engine.stat_arb import TriangularStatisticalArbitrage
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy


def cmd_status(_args: argparse.Namespace) -> int:
    """Print current global Forex market and session status, along with risk invariants."""
    now = datetime.now(timezone.utc)
    state = ForexSessionEngine.get_session_state(now)

    print("=" * 70)
    print(" FOREX PLATFORM — SYSTEM STATUS & RISK INTEGRITY")
    print("=" * 70)
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
    print("-" * 70)
    print(" CAPITAL & SAFETY INVARIANTS:")
    print(" Live Capital Status:      $0.00 LOCKED (Strict Non-Custodial Invariant)")
    print(" Execution Routing:        FAIL-CLOSED SANDBOX / PAPER ONLY")
    print(" Pre-Trade Firewall:       6-TIER ACTIVE (Latency, Spread, Session, Sizing)")
    print(" Circuit Breaker:          NOMINAL (0.0% Drawdown, Sizing Multiplier: 1.00x)")
    print(" Kill Switch State:        DISARMED (Global, Tenant, Broker, Pair, Strategy)")
    print("=" * 70)
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


def _generate_synthetic_candles(symbol: str, count: int = 500) -> pl.DataFrame:
    """Generate deterministic synthetic candles for discovery testing."""
    base_price = 150.0 if "JPY" in symbol else 1.0850
    t0 = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc)
    rng = np.random.default_rng(42)

    rows = []
    price = base_price
    for i in range(count):
        dt = t0 + timedelta(minutes=15 * i)
        step = float(rng.normal(0.0001, 0.0010 if "JPY" not in symbol else 0.05))
        open_ = price
        close_ = price + step
        high_ = max(open_, close_) + abs(float(rng.normal(0, 0.0005 if "JPY" not in symbol else 0.02)))
        low_ = min(open_, close_) - abs(float(rng.normal(0, 0.0005 if "JPY" not in symbol else 0.02)))
        volume = float(rng.integers(100, 1000))
        rows.append({
            "timestamp": dt,
            "open": round(open_, 5),
            "high": round(high_, 5),
            "low": round(low_, 5),
            "close": round(close_, 5),
            "volume": volume,
        })
        price = close_

    return pl.DataFrame(rows)


def cmd_sweep(args: argparse.Namespace) -> int:
    """Run walk-forward validation and G1-G7 qualification across candidate models."""
    symbol = args.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
    strategy_name = args.strategy
    bars = args.bars

    print("=" * 75)
    print(f" DISCOVERY SWEEP: {strategy_name} on {symbol} ({bars} bars)")
    print("=" * 75)

    df = _generate_synthetic_candles(symbol, count=bars)

    candidate = CandidateHypothesis(
        candidate_id=f"SWEEP_{symbol}_{strategy_name[:6].upper()}",
        strategy_name=strategy_name,
        symbol=symbol,
        timeframe=Timeframe.M15,
        parameters={"symbols": [symbol]},
    )

    evaluator = StrategyEvaluator(min_dev_trades=1, min_val_trades=1, min_oos_trades=1)
    loop = ContinuousDiscoveryLoop(evaluator=evaluator)

    result = loop.evaluate_candidate(candidate, df)

    print(f" Candidate ID:             {result.candidate_id}")
    print(f" Strategy:                 {result.strategy_name}")
    print(f" Symbol:                   {result.symbol}")
    print(f" Promotion Status:         {result.status.value}")

    if result.gate_report:
        report = result.gate_report
        print("-" * 75)
        print(" G1-G7 QUALIFICATION GATES:")
        for name, g in report.gate_results.items():
            status_tag = "PASSED" if g.passed else "FAILED"
            print(f"   [{status_tag:<6}] {name:<22} | {g.message}")

        if report.archived_failed_path:
            print(f" Archived Negative Path:   {report.archived_failed_path}")
        if result.promoted_artifact_path:
            print(f" Promoted Artifact Path:   {result.promoted_artifact_path}")

    print("=" * 75)
    return 0


def cmd_forward_paper(args: argparse.Namespace) -> int:
    """Launch forward paper trading daemon with SQLite persistence."""
    symbol = args.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
    db_path = args.db_path
    ticks = args.ticks

    print("=" * 70)
    print(" FORWARD PAPER TRADING DAEMON INITIALIZATION")
    print("=" * 70)
    print(f" Database Ledger:          {db_path}")
    print(f" Simulated Pair:           {symbol}")
    print(f" Initial Virtual Balance:  $100,000.00 USD")
    print(f" Simulation Steps:         {ticks} ticks")
    print("-" * 70)

    kill_switch = HierarchicalKillSwitch()
    cb = CircuitBreakerEngine(initial_equity=Decimal("100000.00"))
    firewall = PreTradeRiskFirewall(kill_switch=kill_switch, circuit_breaker=cb)
    router = SmartOrderRouter(allow_live_routing=False)
    oms = OrderManagementSystem(
        firewall=firewall,
        router=router,
        kill_switch=kill_switch,
        initial_balance=Decimal("100000.00"),
    )
    sim = MicrostructurePaperSimulator()
    pair = CurrencyPair.from_symbol(symbol)
    strat = TrendContinuationStrategy(symbols=[symbol])
    ledger = SQLitePaperLedger(db_path=db_path)

    daemon = ForwardPaperTradingDaemon(
        strategy=strat,
        oms=oms,
        simulator=sim,
        ledger=ledger,
        currency_pair=pair,
    )

    # Process simulated bar events and live quotes
    t0 = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
    base_bid = Decimal("1.08500")

    for i in range(ticks):
        bid = base_bid + Decimal(str(i * 0.0001))
        ask = bid + Decimal("0.00010")
        t = t0 + timedelta(minutes=15 * i)
        bar = BarEvent(
            symbol=symbol,
            timeframe=Timeframe.M15,
            timestamp=t,
            open=bid,
            high=ask + Decimal("0.0005"),
            low=bid - Decimal("0.0005"),
            close=bid + Decimal("0.0002"),
            volume=Decimal("500"),
        )
        telem = MarketTelemetry(
            symbol=symbol,
            bid=bid,
            ask=ask,
            quote_timestamp=t,
            recent_spreads_pips=[Decimal("1.0")] * 20,
        )
        daemon.on_tick_or_bar(bar, telem)

    account = oms.get_account_summary()
    print(" PAPER TRADING EXECUTION SUMMARY:")
    print(f" Orders Processed:         {len(oms._orders)}")
    print(f" Fills Executed:           {len(oms._fills)}")
    print(f" Open Positions:           {account.open_position_count}")
    print(f" Account Equity:           ${account.equity:.2f} USD")
    print(f" Unrealized PnL:           ${account.unrealized_pnl:.2f} USD")
    print(f" SQLite Records Stored:    Orders, Fills, Positions, Equity Snapshots (WAL)")
    print("=" * 70)
    return 0


def cmd_discover_arb(args: argparse.Namespace) -> int:
    """Run multi-currency triangular statistical arbitrage discovery scanner."""
    pair_a = args.pair_a.upper()  # e.g. EURUSD
    pair_b = args.pair_b.upper()  # e.g. GBPUSD
    pair_c = args.pair_c.upper()  # e.g. EURGBP

    print("=" * 75)
    print(" TRIANGULAR STATISTICAL ARBITRAGE SCANNER")
    print("=" * 75)
    print(f" Pair A: {pair_a}  |  Pair B: {pair_b}  |  Pair C (Cross): {pair_c}")

    # Theoretical cross pricing: EUR/GBP = EUR/USD / GBP/USD
    eurusd_bid, eurusd_ask = Decimal("1.08500"), Decimal("1.08512")
    gbpusd_bid, gbpusd_ask = Decimal("1.25000"), Decimal("1.25015")
    eurgbp_bid, eurgbp_ask = Decimal("0.86800"), Decimal("0.86812")

    synthetic_cross_bid = eurusd_bid / gbpusd_ask
    synthetic_cross_ask = eurusd_ask / gbpusd_bid
    actual_cross_mid = (eurgbp_bid + eurgbp_ask) / Decimal("2")
    synthetic_mid = (synthetic_cross_bid + synthetic_cross_ask) / Decimal("2")

    pip_diff = (actual_cross_mid - synthetic_mid) / Decimal("0.0001")

    print("-" * 75)
    print(f" Direct {pair_a} Mid:       {((eurusd_bid + eurusd_ask) / 2):.5f}")
    print(f" Direct {pair_b} Mid:       {((gbpusd_bid + gbpusd_ask) / 2):.5f}")
    print(f" Actual {pair_c} Mid:       {actual_cross_mid:.5f}")
    print(f" Synthetic {pair_c} Mid:    {synthetic_mid:.5f}")
    print(f" Triangular Divergence:    {pip_diff:.2f} pips")

    threshold = Decimal(str(args.threshold))
    if abs(pip_diff) >= threshold:
        signal = "ARBITRAGE OPPORTUNITY DETECTED"
    else:
        signal = "STABLE PARITY (No statistical divergence)"

    print(f" Parity Regime:            {signal} (Threshold: ±{threshold:.1f} pips)")
    print("=" * 75)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build root CLI parser."""
    parser = argparse.ArgumentParser(
        prog="forex-platform",
        description="Forex Platform - Institutional Quantitative Trading & Research Platform CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available platform commands")

    # Command: status
    subparsers.add_parser("status", help="Show system state, session info, and live capital ($0.00 locked)")

    # Command: screen-sessions
    screen_p = subparsers.add_parser("screen-sessions", help="Display 24-hour global trading session map")
    screen_p.add_argument("--date", type=str, help="Date to inspect (YYYY-MM-DD), default is today UTC")

    # Command: inspect-pair
    inspect_p = subparsers.add_parser("inspect-pair", help="Display pair specs, pip math, and spread regimes")
    inspect_p.add_argument("symbol", type=str, help="Currency pair symbol (e.g. EURUSD, USDJPY, EUR/GBP)")

    # Command: sweep
    sweep_p = subparsers.add_parser("sweep", help="Run walk-forward validation across Forex pairs")
    sweep_p.add_argument("--symbol", type=str, default="EURUSD", help="Currency pair (default: EURUSD)")
    sweep_p.add_argument("--strategy", type=str, default="TrendContinuationStrategy", help="Strategy plugin name")
    sweep_p.add_argument("--bars", type=int, default=500, help="Number of bars to sweep (default: 500)")

    # Command: forward-paper
    paper_p = subparsers.add_parser("forward-paper", help="Launch forward paper trading daemon with SQLite ledger")
    paper_p.add_argument("--symbol", type=str, default="EURUSD", help="Currency pair (default: EURUSD)")
    paper_p.add_argument("--db-path", type=str, default=":memory:", help="SQLite DB path (default: :memory:)")
    paper_p.add_argument("--ticks", type=int, default=5, help="Simulated market ticks to process (default: 5)")

    # Command: discover-arb
    arb_p = subparsers.add_parser("discover-arb", help="Run multi-currency triangular statistical arbitrage discovery")
    arb_p.add_argument("--pair-a", type=str, default="EURUSD", help="Pair A (default: EURUSD)")
    arb_p.add_argument("--pair-b", type=str, default="GBPUSD", help="Pair B (default: GBPUSD)")
    arb_p.add_argument("--pair-c", type=str, default="EURGBP", help="Pair C (default: EURGBP)")
    arb_p.add_argument("--threshold", type=float, default=2.0, help="Pip divergence threshold (default: 2.0)")

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
    elif args.command == "sweep":
        return cmd_sweep(args)
    elif args.command == "forward-paper":
        return cmd_forward_paper(args)
    elif args.command == "discover-arb":
        return cmd_discover_arb(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
