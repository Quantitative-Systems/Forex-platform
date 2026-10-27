#!/usr/bin/env python3
"""
Precomputed Per-Set Native Strategy for Paper Trading.
Uses precomputed aligned data and generates explicit exit intents for OMS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import polars as pl

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    UrgencyLevel,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.research_engine.backtester import EventDrivenBacktester
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


# Phase constants
class Phase:
    TB = 1; TRB = 2; PB = 3; PRB = 4; CB = 5; CRB = 6; CH = 7; TR = 8

PHASE = Phase()

SET_DEFINITIONS = {
    "SET_1": {"HTF": "1M", "MTF": "1W", "LTF": "1D"},
    "SET_2": {"HTF": "1W", "MTF": "1D", "LTF": "4H"},
    "SET_3": {"HTF": "1D", "MTF": "4H", "LTF": "1H"},
    "SET_4": {"HTF": "4H", "MTF": "1H", "LTF": "15M"},
    "SET_5": {"HTF": "1H", "MTF": "15M", "LTF": "5M"},
}

COSTS = {"EURUSD": Decimal("0.65"), "GBPUSD": Decimal("0.75"), 
         "USDJPY": Decimal("0.70"), "AUDUSD": Decimal("0.80"), "DEFAULT": Decimal("0.80")}
RR_FLOOR = Decimal("4.0")
MAX_BARS_IN_TRADE = 500


class PrecomputedPerSetStrategyPaper(BaseStrategy):
    """
    Strategy that uses precomputed aligned data (LTF with HTF/MTF context joined).
    Generates explicit exit intents for paper trading OMS.
    """

    def __init__(
        self,
        strategy_id: str = "precomputed_per_set_paper",
        name: str = "Precomputed Per-Set Native Paper",
        symbols: Optional[List[str]] = None,
        selected_set: str = "SET_5",
        account_equity: float = 100000.0,
        risk_fraction: float = 0.01,
        minimum_net_reward_risk: float = 4.0,
        session_filter: bool = True,
    ) -> None:
        set_key = selected_set
        spec = SET_DEFINITIONS.get(set_key, SET_DEFINITIONS["SET_5"])
        ltf_str = spec["LTF"]
        
        tf_map = {
            "1D": Timeframe.D1,
            "4H": Timeframe.H4,
            "1H": Timeframe.H1,
            "15M": Timeframe.M15,
            "5M": Timeframe.M5,
        }
        ltf = tf_map.get(ltf_str, Timeframe.M5)
        
        target_symbols = symbols or ["EURUSD"]
        
        super().__init__(
            strategy_id=strategy_id,
            name=name,
            symbols=target_symbols,
            timeframes=[ltf],
            parameters={
                "account_equity": str(account_equity),
                "risk_fraction": str(risk_fraction),
                "minimum_net_reward_risk": str(minimum_net_reward_risk),
                "selected_set": set_key,
                "session_filter": str(session_filter),
            },
        )
        
        self.selected_set = set_key
        self.account_equity = Decimal(str(account_equity))
        self.risk_fraction = Decimal(str(risk_fraction))
        self.minimum_net_reward_risk = Decimal(str(minimum_net_reward_risk))
        self.session_filter_enabled = session_filter
        self.source_timeframe = ltf
        
        # Precomputed data (set externally)
        self._aligned_df: Optional[pl.DataFrame] = None
        self._cost: Decimal = Decimal("0.80")
        self._row_idx = 0
        
        # Trade tracking
        self._active_trades: Dict[str, Dict] = {}
        
        # State variables (matching per_set_native.py)
        self._bias: Optional[str] = None
        self._setup: Optional[bool] = None
        self._active_trade: Optional[Dict] = None
        
        # Track previous HTF trend for detecting trend changes
        self._prev_htf_trend: Optional[int] = None
        
        # Telemetry
        self.count_total_bars = 0
        self.count_htf_bias_active = 0
        self.count_mtf_setup_aligned = 0
        self.count_ltf_trigger_fired = 0
        self.count_rejected_by_rr_floor = 0
        self.count_rejected_by_spread_news = 0
        self.count_rejected_by_conflict = 0
        self.count_rejected_by_stop_target = 0
        self.count_orders_dispatched = 0

    def set_precomputed_data(self, aligned_df: pl.DataFrame, pair: str):
        """Set the precomputed aligned data."""
        self._aligned_df = aligned_df
        self._cost = COSTS.get(pair, COSTS["DEFAULT"])
        self._row_idx = 0

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        """Process bar using precomputed aligned data."""
        if self._aligned_df is None or self._row_idx >= len(self._aligned_df):
            return []
        
        self.count_total_bars += 1
        
        # Get current row from precomputed data
        row = self._aligned_df.row(self._row_idx, named=True)
        self._row_idx += 1
        
        # Verify timestamp alignment
        row_ts = row["timestamp"]
        if isinstance(row_ts, str):
            row_ts = datetime.fromisoformat(row_ts.replace("Z", "+00:00"))
        if row_ts.tzinfo is None:
            row_ts = row_ts.replace(tzinfo=timezone.utc)
        
        # Process active trade exits first
        exit_intent = self._manage_active_trade(row, row_ts)
        if exit_intent:
            return [exit_intent]
        
        # Session filter
        if self.session_filter_enabled:
            hour = row_ts.hour
            minute = row_ts.minute
            time_in_minutes = hour * 60 + minute
            allowed_start = 6 * 60 + 30  # 06:30 UTC
            allowed_end = 17 * 60        # 17:00 UTC
            if not (allowed_start <= time_in_minutes <= allowed_end):
                return []
        
        # Extract values
        hph = row.get("htf_phase", Phase.CH)
        hpz = row.get("htf_pz", 0.0)
        mph = row.get("mtf_phase", Phase.CH)
        mpz = row.get("mtf_pz", 0.0)
        lph = row.get("ltf_phase", Phase.CH)
        lcl = row.get("close", 0.0)
        lat = row.get("ltf_atr", 0.0)
        lpz = row.get("ltf_pz", 0.0)
        
        # Detect HTF trend change
        htf_trend = 1 if hph in (Phase.TB, Phase.CB, Phase.PB) else (-1 if hph in (Phase.TRB, Phase.CRB, Phase.PRB) else 0)
        if self._prev_htf_trend is not None and htf_trend != self._prev_htf_trend and htf_trend != 0:
            # HTF trend changed - reset bias and setup
            self._bias = None
            self._setup = None
        self._prev_htf_trend = htf_trend
        
        if lat is None or lat <= 0:
            return []
        
        px = Decimal(str(lcl))
        atr = Decimal(str(lat))
        
        # Manage active trade
        if self._active_trade is not None:
            exit_intent = self._manage_active_trade(row, row_ts)
            if exit_intent:
                return [exit_intent]
            return []
        
        # Bias detection
        if self._bias is None:
            if hph in (Phase.TB, Phase.CB, Phase.PB):
                self._bias = "LONG"
                self.count_htf_bias_active += 1
            elif hph in (Phase.TRB, Phase.CRB, Phase.PRB):
                self._bias = "SHORT"
                self.count_htf_bias_active += 1
        
        if self._bias is None:
            return []
        
        # MTF Setup
        if self._setup is None:
            if self._bias == "LONG" and mph in (Phase.PB, Phase.TB, Phase.CB) and (mpz is None or mpz >= -0.7):
                self._setup = True
                self.count_mtf_setup_aligned += 1
            elif self._bias == "SHORT" and mph in (Phase.PRB, Phase.TRB, Phase.CRB) and (mpz is None or mpz <= 0.7):
                self._setup = True
                self.count_mtf_setup_aligned += 1
        
        if not self._setup:
            return []
        
        # LTF Trigger
        trigger = self._check_ltf_trigger(lph, lpz)
        if not trigger:
            if self.count_total_bars % 5000 == 0:
                print(f"  Bar {self.count_total_bars}: bias={self._bias}, setup={self._setup}, lph={lph}, lpz={lpz}")
            return []
        
        self.count_ltf_trigger_fired += 1
        print(f"  TRIGGER at bar {self.count_total_bars}: {trigger} bias={self._bias} lph={lph} atr={atr}")
        
        # Build entry
        intent = self._build_entry(event, px, atr, trigger)
        if intent:
            self.count_orders_dispatched += 1
            return [intent]
        else:
            # Entry rejected - but don't reset bias/setup
            return []

    def _check_ltf_trigger(self, lph: int, lpz: float) -> Optional[str]:
        """Check for LTF trigger matching per_set_native.py."""
        if self._bias == "LONG" and lph in (Phase.TB, Phase.CB, Phase.PB):
            return "LTF_TRIGGER_LONG"
        if self._bias == "SHORT" and lph in (Phase.TRB, Phase.CRB, Phase.PRB):
            return "LTF_TRIGGER_SHORT"
        return None

    def _build_entry(
        self,
        event: BarEvent,
        entry: Decimal,
        atr: Decimal,
        trigger: str,
    ) -> Optional[OrderIntent]:
        """Build entry with 1.5 ATR stop, 4R target."""
        pair = CurrencyPair.from_symbol(event.symbol)
        
        stop_distance = atr * Decimal("1.5")
        
        if self._bias == "LONG":
            stop = entry - stop_distance
            target = entry + stop_distance * Decimal("4.0")
            side = OrderSide.BUY
        else:
            stop = entry + stop_distance
            target = entry - stop_distance * Decimal("4.0")
            side = OrderSide.SELL
        
        # Validate stop distance
        stop_pips = pair.to_pips(abs(entry - stop))
        print(f"    Entry check: bias={self._bias} entry={entry:.5f} stop={stop:.5f} target={target:.5f} stop_pips={stop_pips:.2f}")
        if not (Decimal("5") <= stop_pips <= Decimal("50")):
            print(f"      REJECTED: stop_pips={stop_pips} out of range")
            self.count_rejected_by_stop_target += 1
            return None
        
        # Check spread - data spread is in pipettes (0.1 pip), convert to pips
        spread_pipettes = Decimal(str(event.spread))
        spread_pips = spread_pipettes / Decimal("10")
        print(f"      spread_pips={spread_pips:.2f}")
        if spread_pips > Decimal("3.0"):
            print(f"      REJECTED: spread too wide")
            self.count_rejected_by_spread_news += 1
            return None
        
        # Check gross RR
        target_pips = pair.to_pips(abs(target - entry))
        gross_rr = target_pips / stop_pips if stop_pips > 0 else Decimal("0")
        print(f"      gross_rr={gross_rr:.2f} target_pips={target_pips:.2f}")
        if gross_rr < self.minimum_net_reward_risk:
            print(f"      REJECTED: gross_rr={gross_rr:.2f} < {self.minimum_net_reward_risk}")
            self.count_rejected_by_rr_floor += 1
            return None
        
        # Position sizing
        pip_value = Decimal(pair.standard_lot_units) * pair.pip_size
        quote_rate = self._quote_to_account_rate(pair, entry)
        if quote_rate is None or quote_rate <= 0:
            quote_rate = Decimal("1")
        pip_value_per_lot = pip_value * quote_rate
        
        risk_amount = self.account_equity * self.risk_fraction
        lots = risk_amount / (stop_pips * pip_value_per_lot)
        lot_step = Decimal("0.01")
        lots = (lots / lot_step).quantize(Decimal("1"), rounding=ROUND_DOWN) * lot_step
        
        print(f"      lots={lots}")
        if lots < lot_step:
            print(f"      REJECTED: lots too small")
            return None
        
        # Check for conflicting active trade
        trade_key = (event.symbol, self.selected_set)
        if trade_key in self._active_trades:
            print(f"      REJECTED: conflicting trade")
            self.count_rejected_by_conflict += 1
            return None
        
        intent = self.create_intent(
            symbol=event.symbol,
            side=side,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_lots(float(lots)),
            timestamp=event.timestamp,
            stop_loss=pair.round_price(stop),
            take_profit=pair.round_price(target),
            urgency=UrgencyLevel.MEDIUM,
            client_tag=f"PER_SET_NATIVE|SET={self.selected_set}|BIAS={self._bias}|TRIGGER={trigger}",
        )
        
        # Track active trade
        self._active_trade = {
            "entry": entry,
            "stop": stop,
            "target": target,
            "side": side,
            "entry_time": event.timestamp,
            "bars_held": 0,
            "symbol": event.symbol,
            "units": int(lots * 100_000),  # Convert lots to units
        }
        self._active_trades[(event.symbol, self.selected_set)] = self._active_trade
        print(f"      ENTRY PLACED: {side} {lots} lots @ {entry:.5f} SL={stop:.5f} TP={target:.5f}")
        return intent

    def _manage_active_trade(self, row: Dict, current_time) -> Optional[OrderIntent]:
        """Manage active trade using precomputed row data. Returns exit intent if trade closed."""
        if self._active_trade is None:
            return None
        
        trade = self._active_trade
        trade["bars_held"] += 1
        
        px = Decimal(str(row.get("close", 0)))
        if px <= 0:
            return None
        
        # Check stop
        if trade["side"] == OrderSide.BUY:
            if px <= trade["stop"]:
                return self._close_trade(px, "STOP_LOSS", current_time)
        else:
            if px >= trade["stop"]:
                return self._close_trade(px, "STOP_LOSS", current_time)
        
        # Check target
        if trade["side"] == OrderSide.BUY:
            if px >= trade["target"]:
                return self._close_trade(px, "TARGET", current_time)
        else:
            if px <= trade["target"]:
                return self._close_trade(px, "TARGET", current_time)
        
        # MTF Trailing
        mph = row.get("mtf_phase", Phase.CH)
        if trade["side"] == OrderSide.BUY:
            mtf_a = mph in (Phase.TR, Phase.PRB)
        else:
            mtf_a = mph in (Phase.TR, Phase.PB)
        
        if mtf_a and trade["bars_held"] > 20:
            return self._close_trade(px, "MTF_TRAIL", current_time)
        
        # Time stop
        if trade["bars_held"] >= MAX_BARS_IN_TRADE:
            return self._close_trade(px, "TIME", current_time)
        
        return None

    def _close_trade(self, exit_price: Decimal, reason: str, current_time) -> Optional[OrderIntent]:
        """Close active trade and return exit intent."""
        if self._active_trade is None:
            return None
        
        trade = self._active_trade
        print(f"  TRADE CLOSED: {trade['side']} @ {exit_price:.5f} reason={reason} bars_held={trade['bars_held']}")
        
        # Generate exit intent (opposite side to close position)
        pair = CurrencyPair.from_symbol(trade["symbol"])
        exit_side = OrderSide.SELL if trade["side"] == OrderSide.BUY else OrderSide.BUY
        
        intent = self.create_intent(
            symbol=trade["symbol"],
            side=exit_side,
            order_type=OrderType.MARKET,
            lot_size=LotSize.from_units(trade.get("units", 0)),
            timestamp=current_time,
            urgency=UrgencyLevel.HIGH,
            client_tag=f"PER_SET_NATIVE_EXIT|SET={self.selected_set}|REASON={reason}",
        )
        
        # Clear state
        trade_key = (trade["symbol"], self.selected_set)
        self._active_trades.pop(trade_key, None)
        self._active_trade = None
        self._bias = None
        self._setup = None
        
        return intent

    def _quote_to_account_rate(self, pair: CurrencyPair, price: Decimal) -> Optional[Decimal]:
        if pair.quote_currency == "USD":
            return Decimal("1")
        if pair.base_currency == "USD":
            return Decimal("1") / price if price > 0 else None
        if pair.quote_currency == "JPY":
            # JPY quote currency: rate = 1 / (JPY per account currency)
            # For USD account with JPY pair (e.g., USDJPY), rate = 1 / price
            # For EURJPY in USD account, rate = EURUSD / price
            # We need the quote currency to USD rate
            if pair.base_currency == "USD":
                return Decimal("1") / price if price > 0 else None
            # For cross-JPY pairs, we'd need the base/USD rate
            # For now, approximate with 1/price for USDJPY
            return Decimal("1") / price if price > 0 else None
        return Decimal("1")


def run_backtest(pair: str, set_key: str, data_path: str = "data/cache/dukascopy", max_bars: int = 50000):
    """Run backtest using precomputed data."""
    
    PROJECT_ROOT = Path(__file__).parent.parent
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "research" / "correlated_engine"))
    from indicators import compute_all_indicators, detect_swings, detect_market_structure
    
    # Phase detection
    def detect_phase(structure, pz, prev):
        if pz is None or pz != pz:
            return Phase.CH
        trend = structure.get("trend", "MIXED")
        stype = structure.get("structure", "CHOP")
        if trend == "BULLISH" and stype == "TRENDING":
            return (Phase.PB if pz < -0.3 else Phase.TB if pz <= 0.3 else
                    Phase.CB if prev in (Phase.PB, Phase.TR) else Phase.TB)
        if trend == "BEARISH" and stype == "TRENDING":
            return (Phase.PRB if pz > 0.3 else Phase.TRB if pz >= -0.3 else
                    Phase.CRB if prev in (Phase.PRB, Phase.TR) else Phase.TRB)
        return Phase.CH
    
    mtf_path = Path(data_path) / "mtf"
    
    def load_tf(tf):
        fp = mtf_path / f"{pair}_{tf}.parquet"
        if not fp.exists():
            return pl.DataFrame()
        df = pl.read_parquet(fp)
        if "open_time" in df.columns:
            df = df.drop("timestamp").rename({"open_time": "timestamp"})
        elif df["timestamp"].dtype != pl.Datetime:
            df = df.with_columns(pl.col("timestamp").cast(pl.Datetime("ns", "UTC")))
        df = compute_all_indicators(df)
        
        # Precompute phase
        if df.is_empty() or len(df) < 50:
            return df.with_columns([
                pl.lit("MIXED").alias("trend"),
                pl.lit("CHOP").alias("struct_type"),
                pl.lit(Phase.CH, dtype=pl.Int32).alias("phase")
            ])
        
        n = len(df)
        arr_t = np.full(n, "MIXED", dtype=object)
        arr_s = np.full(n, "CHOP", dtype=object)
        for start in range(0, n, 60):
            end = min(start + 60 + 20, n)
            w = df.slice(max(0, start - 60), end - max(0, start - 60))
            sh, sl = detect_swings(w, lookback=3)
            s = detect_market_structure(sh, sl)
            arr_t[start:end] = s.get("trend", "MIXED")
            arr_s[start:end] = s.get("structure", "CHOP")
        
        df = df.with_columns([
            pl.Series("trend", arr_t),
            pl.Series("struct_type", arr_s),
        ])
        
        phases = np.empty(n, dtype=np.int32)
        prev = Phase.CH
        for i in range(n):
            prev = detect_phase({"trend": arr_t[i], "structure": arr_s[i]}, df["pz"][i], prev)
            phases[i] = prev
        
        return df.with_columns(pl.Series("phase", phases))
    
    tf_data = {}
    for tf in ["1M", "1W", "1D", "4H", "1H", "15M", "5M"]:
        tf_data[tf] = load_tf(tf)
    
    spec = SET_DEFINITIONS[set_key]
    htf, mtf, ltf = spec["HTF"], spec["MTF"], spec["LTF"]
    
    # Align - include OHLCV for backtester
    # Keep LTF columns as standard names for backtester, prefix HTF/MTF
    cols = ["timestamp", "open", "high", "low", "close", "volume", "avg_spread", "phase", "pz", "atr_14", "vwma_50"]
    ltf_df = tf_data[ltf].select(cols).rename(
        {"avg_spread": "spread", "phase": "ltf_phase", "pz": "ltf_pz", "atr_14": "ltf_atr",
         "vwma_50": "ltf_vwma"})
    base = ltf_df.clone()
    for label, tf in (("htf", htf), ("mtf", mtf)):
        if tf_data.get(tf) is None or tf_data[tf].is_empty():
            continue
        df = tf_data[tf].select(["timestamp", "phase", "pz", "atr_14", "close", "vwma_50"])
        df = df.rename({"phase": f"{label}_phase", "pz": f"{label}_pz",
                        "atr_14": f"{label}_atr", "close": f"{label}_close",
                        "vwma_50": f"{label}_vwma"})
        base = base.join_asof(df, on="timestamp", strategy="backward")
    
    aligned = base.head(max_bars) if max_bars else base
    
    # Create strategy and set precomputed data
    strat = PrecomputedPerSetStrategyPaper(
        symbols=[pair],
        selected_set=set_key,
        account_equity=100000,
        risk_fraction=0.01,
        minimum_net_reward_risk=4.0,
        session_filter=False,
    )
    strat.set_precomputed_data(aligned, pair)
    
    # Run backtest
    bt = EventDrivenBacktester(strat, CurrencyPair.from_symbol(pair), initial_balance=100000)
    res = bt.run(aligned, strat.source_timeframe)
    
    return res, strat


if __name__ == "__main__":
    # Test
    res, strat = run_backtest("USDJPY", "SET_5", max_bars=50000)
    print(f"Trades: {res.total_trades}")
    print(f"Net PnL: ${float(res.total_net_pnl):,.2f}")
    print(f"Win Rate: {res.win_rate:.1%}")
    print(f"Profit Factor: {res.profit_factor:.2f}")