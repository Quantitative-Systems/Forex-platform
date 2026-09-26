"""
Causal Event-Driven Backtester with Adverse-First Stop Resolution.
Fills orders strictly at next-bar-open, enforces session spreads, ECN commissions,
overnight swap financing, and prioritizes stop-losses on dual-touch candles.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import math
from typing import Dict, List, Optional, Tuple
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    PipCalculator,
    Position,
    to_decimal,
)
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.costs.commission import CommissionModel
from forex_platform.costs.spread_model import DynamicSpreadModel
from forex_platform.costs.swap_engine import SwapEngine
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class TradeRecord(BaseModel):
    """Immutable audit record of a completed backtest trade."""
    model_config = ConfigDict(frozen=True)

    trade_id: str
    symbol: str
    side: OrderSide
    units: int
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    gross_pnl: Decimal
    commission: Decimal
    swap: Decimal
    net_pnl: Decimal
    exit_reason: str


class BacktestResult(BaseModel):
    """Comprehensive backtest performance report."""
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    initial_balance: Decimal
    final_balance: Decimal
    total_net_pnl: Decimal
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    expectancy: Decimal
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: List[TradeRecord]
    equity_curve: List[Tuple[datetime, Decimal]]


class EventDrivenBacktester:
    """
    Institutional Causal Backtest Engine.
    Guarantees zero-lookahead bias:
    1. Order intents generated on bar[t] are filled at bar[t+1].open.
    2. Intra-bar Adverse-First: if both Stop Loss and Take Profit are breached, Stop Loss triggers first.
    3. Mandatory deduction of session dynamic spreads, ECN commission ($7/lot), and daily swaps.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        currency_pair: CurrencyPair,
        initial_balance: Decimal | float | str = Decimal("100000.00"),
        spread_model: Optional[DynamicSpreadModel] = None,
        commission_model: Optional[CommissionModel] = None,
        swap_engine: Optional[SwapEngine] = None,
        long_swap_pips: Decimal | float | str = Decimal("-0.6"),
        short_swap_pips: Decimal | float | str = Decimal("0.2"),
        cost_multiplier: float = 1.0,
    ):
        self.strategy = strategy
        self.pair = currency_pair
        self.initial_balance = to_decimal(initial_balance)
        self.spread_model = spread_model or DynamicSpreadModel()
        self.commission_model = commission_model or CommissionModel()
        self.swap_engine = swap_engine or SwapEngine()
        self.long_swap_pips = to_decimal(long_swap_pips)
        self.short_swap_pips = to_decimal(short_swap_pips)
        self.cost_multiplier = Decimal(str(cost_multiplier))

    @staticmethod
    def _time_based_sharpe(
        equity_curve: List[Tuple[datetime, Decimal]],
        timeframe: Timeframe,
    ) -> float:
        """Calculate annualized Sharpe from timestamped equity returns.

        This intentionally does not annualize per-trade PnL. The frequency of
        returns must match the tested bar frequency, otherwise intraday results
        are mathematically overstated.
        """
        if len(equity_curve) < 3:
            return 0.0
        values = [float(value) for _, value in equity_curve]
        returns = [
            (current / previous) - 1.0
            for previous, current in zip(values, values[1:])
            if previous > 0.0
        ]
        if len(returns) < 2:
            return 0.0
        mean_return = sum(returns) / len(returns)
        variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
        std_return = math.sqrt(variance)
        if std_return <= 0.0:
            return 0.0
        periods_per_year = {
            Timeframe.M1: 252.0 * 24.0 * 60.0,
            Timeframe.M5: 252.0 * 24.0 * 12.0,
            Timeframe.M15: 252.0 * 24.0 * 4.0,
            Timeframe.M30: 252.0 * 24.0 * 2.0,
            Timeframe.H1: 252.0 * 24.0,
            Timeframe.H4: 252.0 * 6.0,
            Timeframe.D1: 252.0,
        }[timeframe]
        return (mean_return / std_return) * math.sqrt(periods_per_year)

    def run(self, df: pl.DataFrame, timeframe: Timeframe = Timeframe.M15) -> BacktestResult:

        """
        Execute event-driven causal simulation across chronological bar data.
        """
        if df.is_empty():
            raise ValueError("Cannot run backtest on empty DataFrame.")

        balance = self.initial_balance
        peak_balance = balance
        max_drawdown = Decimal("0.0")

        open_position: Optional[Position] = None
        open_intent: Optional[OrderIntent] = None
        pending_intents: List[OrderIntent] = []
        trades: List[TradeRecord] = []
        equity_curve: List[Tuple[datetime, Decimal]] = []

        total_rows = df.height
        trade_counter = 0

        for i in range(total_rows):
            row = df.row(i, named=True)
            raw_ts = row["timestamp"]
            ts = raw_ts if isinstance(raw_ts, datetime) else datetime.fromisoformat(str(raw_ts))
            utc_ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

            bar_open = to_decimal(row["open"])
            bar_high = to_decimal(row["high"])
            bar_low = to_decimal(row["low"])
            bar_close = to_decimal(row["close"])
            bar_volume = to_decimal(row["volume"])
            bar_spread = to_decimal(row.get("spread", 1.0)) * self.cost_multiplier

            # -----------------------------------------------------------------
            # STEP 1: RESOLVE PENDING INTENTS FROM PREVIOUS BAR (NEXT-BAR-OPEN FILL)
            # -----------------------------------------------------------------
            if pending_intents and open_position is None:
                intent_to_fill = pending_intents.pop(0)
                pending_intents.clear()  # Drop any secondary signals

                # Compute execution price including half-spread
                spread_price = self.pair.to_price(bar_spread)
                half_spread = spread_price / Decimal("2")

                if intent_to_fill.side == OrderSide.BUY:
                    fill_price = bar_open + half_spread
                else:
                    fill_price = bar_open - half_spread

                comm = self.commission_model.calculate_commission(
                    intent_to_fill.lot_size,
                    is_roundturn=False,
                ) * self.cost_multiplier

                trade_counter += 1
                open_position = Position(
                    position_id=f"BT-POS-{trade_counter}",
                    symbol=self.pair.symbol,
                    side=intent_to_fill.side,
                    units=intent_to_fill.lot_size.units,
                    average_entry_price=fill_price,
                    current_price=fill_price,
                    realized_pnl=Decimal("0.0"),
                    unrealized_pnl=Decimal("0.0"),
                    total_commission=comm,
                    total_swap=Decimal("0.0"),
                    opened_at=utc_ts,
                    updated_at=utc_ts,
                    is_open=True,
                )
                open_intent = intent_to_fill

            # -----------------------------------------------------------------
            # STEP 2: CHECK OVERNIGHT SWAP (21:00 UTC ROLLOVER)
            # -----------------------------------------------------------------
            if open_position is not None and i > 0:
                prev_row = df.row(i - 1, named=True)
                prev_ts = prev_row["timestamp"]
                prev_utc = prev_ts if isinstance(prev_ts, datetime) else datetime.fromisoformat(str(prev_ts))
                if prev_utc.tzinfo is None:
                    prev_utc = prev_utc.replace(tzinfo=timezone.utc)

                # Check if 21:00 UTC boundary was crossed
                if prev_utc.hour < 21 <= utc_ts.hour or (prev_utc.date() < utc_ts.date() and utc_ts.hour >= 21):
                    self.swap_engine.apply_rollover_to_position(
                        position=open_position,
                        pair=self.pair,
                        rollover_dt=utc_ts,
                        long_swap_pips=self.long_swap_pips,
                        short_swap_pips=self.short_swap_pips,
                    )

            # -----------------------------------------------------------------
            # STEP 3: ADVERSE-FIRST INTRA-BAR STOP VS TARGET RESOLUTION
            # -----------------------------------------------------------------
            if open_position is not None and open_intent is not None:
                sl = open_intent.stop_loss
                tp = open_intent.take_profit
                pos_closed = False
                exit_price = bar_close
                exit_reason = ""

                if open_position.side == OrderSide.BUY:
                    sl_hit = sl is not None and bar_low <= sl
                    tp_hit = tp is not None and bar_high >= tp

                    if sl_hit and tp_hit:
                        # ADVERSE-FIRST: Stop loss takes priority!
                        pos_closed = True
                        exit_price = sl  # Stop loss price
                        exit_reason = "STOP_LOSS_ADVERSE_COLLISION"
                    elif sl_hit:
                        pos_closed = True
                        exit_price = sl
                        exit_reason = "STOP_LOSS"
                    elif tp_hit:
                        pos_closed = True
                        exit_price = tp
                        exit_reason = "TAKE_PROFIT"

                elif open_position.side == OrderSide.SELL:
                    sl_hit = sl is not None and bar_high >= sl
                    tp_hit = tp is not None and bar_low <= tp

                    if sl_hit and tp_hit:
                        # ADVERSE-FIRST: Stop loss takes priority!
                        pos_closed = True
                        exit_price = sl
                        exit_reason = "STOP_LOSS_ADVERSE_COLLISION"
                    elif sl_hit:
                        pos_closed = True
                        exit_price = sl
                        exit_reason = "STOP_LOSS"
                    elif tp_hit:
                        pos_closed = True
                        exit_price = tp
                        exit_reason = "TAKE_PROFIT"

                if pos_closed:
                    # Closing commission
                    closing_comm = self.commission_model.calculate_commission(
                        LotSize.from_units(open_position.units),
                        is_roundturn=False,
                    ) * self.cost_multiplier
                    open_position.total_commission += closing_comm

                    # Calculate gross PnL
                    gross_pnl = open_position.close(exit_price)
                    net_pnl = gross_pnl - open_position.total_commission + open_position.total_swap
                    balance += net_pnl

                    trades.append(
                        TradeRecord(
                            trade_id=f"TRD-{trade_counter:04d}",
                            symbol=self.pair.symbol,
                            side=open_position.side,
                            units=open_intent.lot_size.units,
                            entry_time=open_position.opened_at,
                            entry_price=open_position.average_entry_price,
                            exit_time=utc_ts,
                            exit_price=exit_price,
                            gross_pnl=gross_pnl,
                            commission=open_position.total_commission,
                            swap=open_position.total_swap,
                            net_pnl=net_pnl,
                            exit_reason=exit_reason,
                        )
                    )
                    open_position = None
                    open_intent = None

            # -----------------------------------------------------------------
            # STEP 4: DELIVER CLOSED BAR EVENT TO STRATEGY (CAUSAL HOOK)
            # -----------------------------------------------------------------
            event = BarEvent(
                symbol=self.pair.symbol,
                timeframe=timeframe,
                timestamp=utc_ts,
                open=bar_open,
                high=bar_high,
                low=bar_low,
                close=bar_close,
                volume=bar_volume,
                spread=bar_spread,
            )
            new_intents = self.strategy.on_bar(event)

            # Queue new intents for execution on NEXT bar open
            if new_intents:
                pending_intents.extend(new_intents)

            # Record equity curve
            unrealized = Decimal("0.0")
            if open_position is not None:
                open_position.update_market_price(bar_close)
                unrealized = open_position.unrealized_pnl

            curr_equity = balance + unrealized
            equity_curve.append((utc_ts, curr_equity))

            if curr_equity > peak_balance:
                peak_balance = curr_equity
            dd = (peak_balance - curr_equity) / peak_balance if peak_balance > Decimal("0") else Decimal("0")
            if dd > max_drawdown:
                max_drawdown = dd

        # Calculate summary statistics
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t.net_pnl > Decimal("0.0"))
        losing_trades = sum(1 for t in trades if t.net_pnl < Decimal("0.0"))
        win_rate = (winning_trades / total_trades) if total_trades > 0 else 0.0

        gross_wins = sum((t.net_pnl for t in trades if t.net_pnl > Decimal("0.0")), Decimal("0.0"))
        gross_losses = abs(sum((t.net_pnl for t in trades if t.net_pnl < Decimal("0.0")), Decimal("0.0")))

        if gross_losses > Decimal("0.0"):
            profit_factor = float(gross_wins / gross_losses)
        elif gross_wins > Decimal("0.0"):
            profit_factor = 999.0
        else:
            profit_factor = 0.0

        expectancy = (
            sum((t.net_pnl for t in trades), Decimal("0.0")) / Decimal(str(total_trades))
            if total_trades > 0
            else Decimal("0.0")
        )

        # Sharpe is based on the timestamped equity curve, not trade PnL.
        sharpe = self._time_based_sharpe(equity_curve, timeframe)

        return BacktestResult(
            strategy_id=self.strategy.strategy_id,
            initial_balance=self.initial_balance,
            final_balance=balance,
            total_net_pnl=balance - self.initial_balance,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            max_drawdown_pct=float(max_drawdown * Decimal("100")),
            sharpe_ratio=sharpe,
            trades=trades,
            equity_curve=equity_curve,
        )
