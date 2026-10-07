"""
Causal Event-Driven Backtester with Adverse-First Stop Resolution.
Fills orders strictly at next-bar-open, enforces session spreads, ECN commissions,
overnight swap financing, and prioritizes stop-losses on dual-touch candles.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import math
from typing import Any, Dict, List, Mapping, Optional, Tuple
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
    # Initial protective risk levels of the opening intent. Used to convert
    # realized PnL into R-multiples for robustness research gates.
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None


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
        account_currency: str = "USD",
        quote_to_account_rates: Optional[Mapping[str, Decimal | float | str]] = None,
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
        self.account_currency = account_currency.upper()
        self.quote_to_account_rates = {
            currency.upper(): to_decimal(rate)
            for currency, rate in (quote_to_account_rates or {}).items()
        }

    def quote_to_account_rate(self, row: Mapping[str, Any], price: Decimal) -> Decimal:
        """Resolve quote-currency conversion at this bar, failing closed if absent."""
        quote = self.pair.quote_currency
        if quote == self.account_currency:
            return Decimal("1")
        row_rate = row.get("quote_to_account_rate")
        if row_rate is not None:
            rate = to_decimal(row_rate)
            if rate > 0:
                return rate
        configured = self.quote_to_account_rates.get(quote)
        if configured is not None and configured > 0:
            return configured
        if self.pair.base_currency == self.account_currency and price > 0:
            return Decimal("1") / price
        raise ValueError(
            f"Missing {quote}-to-{self.account_currency} conversion for {self.pair.symbol}; "
            "provide quote_to_account_rates or a quote_to_account_rate column."
        )

    def _ask_price(
        self,
        row: Mapping[str, Any],
        bid: Decimal,
        field: str,
        modeled_spread_price: Decimal,
    ) -> Decimal:
        """Return a cost-stressed ask quote, using supplied ask OHLC when present.

        Canonical bars are bid-quoted. A dataset with no ask OHLC therefore gets
        a synthetic ask at bid + its spread column; the spread is never split
        around the bid. With real ask OHLC, preserve the observed quote and scale
        its bid/ask distance for cost stress runs.
        """
        raw_ask = row.get(f"ask_{field}")
        if raw_ask is None:
            return bid + modeled_spread_price
        ask = to_decimal(raw_ask)
        return bid + max(Decimal("0"), ask - bid) * self.cost_multiplier

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
        account_day = None
        day_start_balance = balance

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
            raw_spread = row.get("spread_pips")
            if raw_spread is None:
                raw_spread = row.get("spread", 1.0)
            bar_spread = to_decimal(raw_spread) * self.cost_multiplier
            spread_price = self.pair.to_price(bar_spread)
            ask_open = self._ask_price(row, bar_open, "open", spread_price)
            ask_high = self._ask_price(row, bar_high, "high", spread_price)
            ask_low = self._ask_price(row, bar_low, "low", spread_price)
            ask_close = self._ask_price(row, bar_close, "close", spread_price)

            # Optional causally-aligned multi-timeframe context (struct column).
            # Used by research sweeps to pass Market Model state (e.g. HTF/MTF
            # structural trend) into strategies without lookahead.
            raw_htf_data = row.get("htf_data")
            if isinstance(raw_htf_data, dict):
                htf_context: Dict[str, Any] = dict(raw_htf_data)
            elif raw_htf_data is not None:
                try:
                    htf_context = dict(raw_htf_data)
                except (TypeError, ValueError):
                    htf_context = {}
            else:
                htf_context = {}

            # -----------------------------------------------------------------
            # STEP 1: RESOLVE PENDING INTENTS FROM PREVIOUS BAR (NEXT-BAR-OPEN FILL)
            # -----------------------------------------------------------------
            if pending_intents and open_position is None:
                intent_to_fill = pending_intents.pop(0)
                pending_intents.clear()  # Drop any secondary signals

                # MARKET vs LIMIT: LIMIT orders fill only on a price TOUCH.
                # Bars are bid-quoted, so BUY limits are checked against the
                # ask (bid + spread); SELL limits are checked against the bid.
                fill_price: Optional[Decimal] = None

                if (
                    intent_to_fill.order_type == OrderType.LIMIT
                    and intent_to_fill.limit_price is not None
                ):
                    limit_price = intent_to_fill.limit_price
                    if intent_to_fill.side == OrderSide.BUY:
                        if ask_low <= limit_price:
                            # Gap-down through the limit fills at the
                            # favorable open; otherwise at the limit price.
                            fill_price = min(ask_open, limit_price)
                    else:
                        bid_open = bar_open
                        bid_high = bar_high
                        if bid_high >= limit_price:
                            fill_price = max(bid_open, limit_price)
                    if fill_price is None:
                        # Not touched this bar: the order rests in the queue.
                        pending_intents.insert(0, intent_to_fill)
                else:
                    if intent_to_fill.side == OrderSide.BUY:
                        fill_price = ask_open
                    else:
                        fill_price = bar_open

                if fill_price is not None:
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
                        exit_price = min(bar_open, sl)  # Stops slip adversely through gaps.
                        exit_reason = "STOP_LOSS_ADVERSE_COLLISION"
                    elif sl_hit:
                        pos_closed = True
                        exit_price = min(bar_open, sl)
                        exit_reason = "STOP_LOSS"
                    elif tp_hit:
                        pos_closed = True
                        exit_price = max(bar_open, tp)
                        exit_reason = "TAKE_PROFIT"

                elif open_position.side == OrderSide.SELL:
                    sl_hit = sl is not None and ask_high >= sl
                    tp_hit = tp is not None and ask_low <= tp

                    if sl_hit and tp_hit:
                        # ADVERSE-FIRST: Stop loss takes priority!
                        pos_closed = True
                        exit_price = max(ask_open, sl)
                        exit_reason = "STOP_LOSS_ADVERSE_COLLISION"
                    elif sl_hit:
                        pos_closed = True
                        exit_price = max(ask_open, sl)
                        exit_reason = "STOP_LOSS"
                    elif tp_hit:
                        pos_closed = True
                        exit_price = min(ask_open, tp)
                        exit_reason = "TAKE_PROFIT"

                if pos_closed:
                    # Closing commission
                    closing_comm = self.commission_model.calculate_commission(
                        LotSize.from_units(open_position.units),
                        is_roundturn=False,
                    ) * self.cost_multiplier
                    open_position.total_commission += closing_comm

                    # Calculate gross PnL
                    gross_pnl_quote = open_position.close(exit_price)
                    quote_rate = self.quote_to_account_rate(row, exit_price)
                    gross_pnl = gross_pnl_quote * quote_rate
                    swap_account = open_position.total_swap * quote_rate
                    net_pnl = gross_pnl - open_position.total_commission + swap_account
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
                            swap=swap_account,
                            net_pnl=net_pnl,
                            exit_reason=exit_reason,
                            stop_loss=open_intent.stop_loss,
                            take_profit=open_intent.take_profit,
                        )
                    )
                    open_position = None
                    open_intent = None

            # -----------------------------------------------------------------
            # STEP 4: DELIVER CLOSED BAR EVENT TO STRATEGY (CAUSAL HOOK)
            # -----------------------------------------------------------------
            if account_day != utc_ts.date():
                account_day = utc_ts.date()
                day_start_balance = balance
            unrealized_before_signal = Decimal("0.0")
            if open_position is not None:
                liquidation_price = bar_close if open_position.side == OrderSide.BUY else ask_close
                open_position.update_market_price(liquidation_price)
                unrealized_before_signal = open_position.unrealized_pnl * self.quote_to_account_rate(row, liquidation_price)
            update_account_state = getattr(self.strategy, "update_account_state", None)
            if callable(update_account_state):
                update_account_state(
                    equity=balance + unrealized_before_signal,
                    daily_realized_pnl=balance - day_start_balance,
                )
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
                htf_data=htf_context,
            )
            new_intents = self.strategy.on_bar(event)

            # Queue new intents for execution on NEXT bar open
            if new_intents:
                pending_intents.extend(new_intents)

            # Record equity curve
            unrealized = Decimal("0.0")
            if open_position is not None:
                liquidation_price = bar_close if open_position.side == OrderSide.BUY else ask_close
                open_position.update_market_price(liquidation_price)
                unrealized = open_position.unrealized_pnl * self.quote_to_account_rate(row, liquidation_price)

            curr_equity = balance + unrealized
            equity_curve.append((utc_ts, curr_equity))

            if curr_equity > peak_balance:
                peak_balance = curr_equity
            dd = (peak_balance - curr_equity) / peak_balance if peak_balance > Decimal("0") else Decimal("0")
            if dd > max_drawdown:
                max_drawdown = dd

        # A window ending with an open position must realize its liquidation
        # value at the final executable quote; otherwise OOS windows omit both
        # the trade and its costs from reported expectancy and final balance.
        if open_position is not None and open_intent is not None and total_rows:
            last_row = df.row(total_rows - 1, named=True)
            last_bid_close = to_decimal(last_row["close"])
            last_spread = last_row.get("spread_pips")
            if last_spread is None:
                last_spread = last_row.get("spread", 1.0)
            last_modeled_spread = to_decimal(last_spread) * self.cost_multiplier
            last_ask_close = self._ask_price(
                last_row,
                last_bid_close,
                "close",
                self.pair.to_price(last_modeled_spread),
            )
            liquidation_price = last_bid_close if open_position.side == OrderSide.BUY else last_ask_close
            last_ts = last_row["timestamp"]
            last_utc_ts = last_ts if isinstance(last_ts, datetime) else datetime.fromisoformat(str(last_ts))
            if last_utc_ts.tzinfo is None:
                last_utc_ts = last_utc_ts.replace(tzinfo=timezone.utc)
            closing_comm = self.commission_model.calculate_commission(
                LotSize.from_units(open_position.units), is_roundturn=False,
            ) * self.cost_multiplier
            open_position.total_commission += closing_comm
            gross_pnl_quote = open_position.close(liquidation_price)
            quote_rate = self.quote_to_account_rate(last_row, liquidation_price)
            gross_pnl = gross_pnl_quote * quote_rate
            swap_account = open_position.total_swap * quote_rate
            net_pnl = gross_pnl - open_position.total_commission + swap_account
            balance += net_pnl
            trades.append(
                TradeRecord(
                    trade_id=f"TRD-{trade_counter:04d}",
                    symbol=self.pair.symbol,
                    side=open_position.side,
                    units=open_intent.lot_size.units,
                    entry_time=open_position.opened_at,
                    entry_price=open_position.average_entry_price,
                    exit_time=last_utc_ts,
                    exit_price=liquidation_price,
                    gross_pnl=gross_pnl,
                    commission=open_position.total_commission,
                    swap=swap_account,
                    net_pnl=net_pnl,
                    exit_reason="END_OF_DATA_LIQUIDATION",
                    stop_loss=open_intent.stop_loss,
                    take_profit=open_intent.take_profit,
                )
            )
            if equity_curve:
                equity_curve[-1] = (last_utc_ts, balance)
            if balance > peak_balance:
                peak_balance = balance
            final_dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else Decimal("0")
            if final_dd > max_drawdown:
                max_drawdown = final_dd

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
