"""Research-first institutional structure strategy on the shared fractal ladder.

The strategy consumes completed source bars (M1 or aggregated M3) and shares
one causal state engine across all five timeframe sets. After HTF/MTF
validation, it enters on an LTF micro-BOS or liquidity sweep-and-reclaim close.
It is a candidate for backtesting and paper evaluation; it does not promise or
imply a profitable edge.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Mapping, Optional

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    UrgencyLevel,
)
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.fractal_engine.state_engine import (
    CanonicalTimeframeState,
    UniversalTimeframeStateEngine,
)
from forex_platform.fractal_engine.timeframes import (
    CANONICAL_LADDER,
    TIMEFRAME_SETS,
    CanonicalTimeframe,
    normalized_set_key,
        CanonicalTimeframe,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_model.contracts import MarketPhase
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class InstitutionalFractalStrategy(BaseStrategy):
    """Causal HTF-bias / MTF-setup / LTF-trigger research candidate.

    All five canonical sets are evaluated from the same state histories. An
    LTF micro-BOS or liquidity sweep-and-reclaim can trigger an entry after
    the MTF setup validates. One active 3M structural leg can produce at most
    one entry across overlapping views. Position risk is sized from explicit
    account equity; quote-currency conversion for cross pairs must be supplied
    by the caller.
    """


    NEXT_HIGHER_TIMEFRAME = {
        CanonicalTimeframe.M3: CanonicalTimeframe.M15,
        CanonicalTimeframe.M15: CanonicalTimeframe.H1,
        CanonicalTimeframe.H1: CanonicalTimeframe.H4,
        CanonicalTimeframe.H4: CanonicalTimeframe.DAY,
        CanonicalTimeframe.DAY: CanonicalTimeframe.WEEK,
        CanonicalTimeframe.WEEK: CanonicalTimeframe.MONTH,
        CanonicalTimeframe.MONTH: None
    }
    SUPPORTED_SOURCE_TIMEFRAME = Timeframe.M1


    def __init__(
        self,
        strategy_id: str = "institutional_fractal",
        symbols: Optional[list[str]] = None,
        *,
        account_equity: Decimal | str | float = Decimal("100000"),
        account_currency: str = "USD",
        quote_to_account_rates: Optional[Mapping[str, Decimal | str | float]] = None,
        risk_fraction: Decimal | str | float = Decimal("0.01"),
        minimum_net_reward_risk: Decimal | str | float = Decimal("4.0"),
        minimum_stop_pips: Decimal | str | float = Decimal("5"),
        maximum_stop_pips: Decimal | str | float = Decimal("50"),
        maximum_entry_spread_pips: Decimal | str | float = Decimal("2.5"),
        commission_per_lot_round_turn: Decimal | str | float = Decimal("7"),
        slippage_buffer_pips: Decimal | str | float = Decimal("0.2"),
        atr_expansion_slippage_factor: Decimal | str | float = Decimal("0.1"),
        stop_buffer_pips: Decimal | str | float = Decimal("2.0"),
        maximum_lots: Decimal | str | float = Decimal("100"),
        lot_step: Decimal | str | float = Decimal("0.01"),
        swing_lookback: int | dict[str, int] = 3,
        swing_lookback_map: dict[str, int] | None = None,
        setup_persistence_bars: int = 15,  # MTF alignment persistence window
        session_filter: bool = True,
        selected_set: Optional[str] = None,
        source_timeframe: Timeframe = Timeframe.M1,
    ) -> None:
        selected_set = normalized_set_key(selected_set) if selected_set else None
        target_symbols = symbols or ["EURUSD"]
        super().__init__(
            strategy_id=strategy_id,
            name="Institutional Fractal (research candidate)",
            symbols=target_symbols,
            timeframes=[source_timeframe],
            parameters={
                "account_equity": str(account_equity),
                "risk_fraction": str(risk_fraction),
                "minimum_net_reward_risk": str(minimum_net_reward_risk),
                "selected_set": selected_set,
            },
        )
        self.account_equity = Decimal(str(account_equity))
        self.account_currency = account_currency.upper()
        self.quote_to_account_rates = {
            currency.upper(): Decimal(str(rate))
            for currency, rate in (quote_to_account_rates or {}).items()
        }
        self.risk_fraction = Decimal(str(risk_fraction))
        self.minimum_net_reward_risk = Decimal(str(minimum_net_reward_risk))
        self.minimum_stop_pips = Decimal(str(minimum_stop_pips))
        self.maximum_stop_pips = Decimal(str(maximum_stop_pips))
        self.maximum_entry_spread_pips = Decimal(str(maximum_entry_spread_pips))
        self.commission_per_lot_round_turn = Decimal(str(commission_per_lot_round_turn))
        self.slippage_buffer_pips = Decimal(str(slippage_buffer_pips))
        self.atr_expansion_slippage_factor = Decimal(str(atr_expansion_slippage_factor))
        self.stop_buffer_pips = Decimal(str(stop_buffer_pips))
        self.maximum_lots = Decimal(str(maximum_lots))
        self.lot_step = Decimal(str(lot_step))
        self.swing_lookback = swing_lookback if isinstance(swing_lookback, int) else 3
        self.swing_lookback_map = (
            dict(swing_lookback_map) if swing_lookback_map is not None
            else (dict(swing_lookback) if isinstance(swing_lookback, dict) else {
                "1M": 5, "1W": 5, "1D": 5, "4H": 5, "1H": 5, "15M": 3, "3M": 3
            })
        )
        self.setup_persistence_bars = setup_persistence_bars
        self.session_filter_enabled = bool(session_filter)
        self.selected_set = selected_set
        self.source_timeframe = source_timeframe

        self._engines: dict[str, UniversalTimeframeStateEngine] = {}
        self._movement_direction: dict[str, int] = {}
        self._movement_sequence: dict[str, int] = {}
        self._movement_id: dict[str, str] = {}
        self._movement_signature: dict[str, tuple[object, ...]] = {}
        self._context_movement_direction: dict[tuple[str, str], int] = {}
        self._context_movement_sequence: dict[tuple[str, str], int] = {}
        self._context_movement_id: dict[tuple[str, str], str] = {}
        self._context_movement_signature: dict[tuple[str, str], tuple[object, ...]] = {}
        self._used_movements: set[tuple[str, str]] = set()
        # Track active limit order placements per setup: {(symbol, set_key, movement_id): placement_time}
        self._active_limit_order_setups: dict[tuple[str, str, str], datetime] = {}
        self.raw_signal_count = 0
        self.unique_signal_movements: set[tuple[str, str]] = set()
        self.multi_set_overlap_count = 0
        self.dropped_conflicting_signal_count = 0
        self.allocated_signal_movements: set[tuple[str, str]] = set()
        self.last_decision: dict[str, str] = {}
        self.daily_realized_pnl = Decimal("0")
        self.max_daily_loss_fraction = Decimal("0.02")

        # Telemetry funnel (single-count per rejection site).
        # count_rejected_by_rr_floor is incremented ONLY in _build_entry.
        # Conflict drops have their own counter; spread/news has its own.
        self.count_total_bars = 0
        self.count_htf_bias_active = 0
        self.count_mtf_setup_aligned = 0
        self.count_ltf_trigger_fired = 0
        self.count_rejected_by_rr_floor = 0
        self.count_rejected_by_spread_news = 0
        self.count_rejected_by_conflict = 0
        self.count_rejected_by_stop_target = 0
        self.count_orders_dispatched = 0
        # IS-only diagnostic telemetry (EXP_002): net expected R:R sampled at
        # every _build_entry evaluation, before the frozen 4R floor applies.
        # Additive only ΓÇö does not change dispatch behaviour.
        self.rr_samples: list[float] = []
        self.rr_samples_by_set: dict[str, list[float]] = {}

        if self.account_equity <= 0:
            raise ValueError("account_equity must be positive")
        if not Decimal("0") < self.risk_fraction <= Decimal("0.01"):
            raise ValueError("risk_fraction must be in (0, 0.01]")
        if self.minimum_net_reward_risk < Decimal("1.0"):
            raise ValueError("minimum_net_reward_risk must be >= 1.0")
        if self.maximum_lots <= 0 or self.lot_step <= 0:
            raise ValueError("maximum_lots and lot_step must be positive")
        if tuple(TIMEFRAME_SETS) != ("SET_1", "SET_2", "SET_3", "SET_4", "SET_5"):
            raise RuntimeError("canonical timeframe set definitions changed unexpectedly")

    @property
    def timeframe_sets(self) -> tuple[str, ...]:
        return tuple(TIMEFRAME_SETS)

    def update_account_state(
        self,
        *,
        equity: Decimal | str | float,
        daily_realized_pnl: Decimal | str | float,
        quote_to_account_rates: Optional[Mapping[str, Decimal | str | float]] = None,
    ) -> None:
        """Refresh sizing/risk inputs from the account and conversion services."""
        updated_equity = Decimal(str(equity))
        if updated_equity <= 0:
            raise ValueError("equity must be positive")
        self.account_equity = updated_equity
        self.daily_realized_pnl = Decimal(str(daily_realized_pnl))
        if quote_to_account_rates is not None:
            self.quote_to_account_rates = {
                currency.upper(): Decimal(str(rate))
                for currency, rate in quote_to_account_rates.items()
            }

    def on_bar(self, event: BarEvent) -> list[OrderIntent]:
        if event.timeframe != self.source_timeframe:
            raise ValueError(f"InstitutionalFractalStrategy requires completed {self.source_timeframe.value} source bars")
        self.count_total_bars += 1
        self.update_history(event)
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        engine = self._engines.setdefault(
            symbol,
            UniversalTimeframeStateEngine(
                symbol,
                base_timeframe=self.source_timeframe,
                # Fresh-break validation compares only the latest state with
                # its predecessor, so keep the online state buffer bounded.
                max_history_bars=2000,
                swing_lookback=self.swing_lookback,
                swing_lookback_map=self.swing_lookback_map,
            ),
        )
        newly_closed = engine.update({
            "timestamp": event.timestamp,
            "open": float(event.open),
            "high": float(event.high),
            "low": float(event.low),
            "close": float(event.close),
            "volume": float(event.volume),
            "spread": float(event.spread),
        })
        # The movement ledger tracks the finest canonical leg actually
        # available from the current feed.  When the source is coarser than
        # M3 (e.g. M15/H1 input), M3 is never published, so using M3 here
        # would starve the ledger entirely.
        finest_state = None
        for tf_key in ("1M", "1W", "1D", "4H", "1H", "15M", "3M"):
            candidate = newly_closed.get(tf_key)
            if candidate is not None:
                finest_state = candidate  # keep going: later keys are finer
        self._advance_movement(symbol, finest_state)

        # Process expired limit order setups (cancel after 4 bars)
        self._process_expired_limit_order_setups(event)
        
        # Process pending limit orders first (check for fills or timeouts)
        self._process_pending_limit_orders(event)

        # Session liquidity time filter (upgrade D)
        if self.session_filter_enabled:
            # Allowed window: 06:30 UTC to 17:00 UTC
            # Blackout: 20:30 UTC to 06:30 UTC (next day)
            hour = event.timestamp.hour
            minute = event.timestamp.minute
            time_in_minutes = hour * 60 + minute
            
            # Allowed start 06:30 = 6*60+30 = 390
            # Allowed end   17:00 = 17*60 = 1020
            allowed_start = 6 * 60 + 30  # 390
            allowed_end   = 17 * 60      # 1020
            
            # Blackout start 20:30 = 20*60+30 = 1230
            # Blackout end   06:30 = 6*60+30 = 390 (next day)
            blackout_start = 20 * 60 + 30  # 1230
            blackout_end   = 6 * 60 + 30   # 390
            
            # Check if current time is within allowed window (06:30-17:00)
            # Note: blackout is the complement of allowed window within 24h.
            in_allowed = allowed_start <= time_in_minutes <= allowed_end
            if not in_allowed:
                self.last_decision[symbol] = "SESSION_FILTER"
                self.count_rejected_by_spread_news += 1
                return []
        if self.daily_realized_pnl <= -(self.account_equity * self.max_daily_loss_fraction):
            self.last_decision[symbol] = "DAILY_LOSS_LIMIT"
            self.count_rejected_by_spread_news += 1
            return []

        movement_id = self._movement_id.get(symbol)
        if not movement_id or (symbol, movement_id) in self._used_movements:
            return []

        candidates: list[tuple[Decimal, Decimal, int, OrderIntent, str]] = []
        for set_index, (set_key, ladder) in enumerate(TIMEFRAME_SETS.items()):
            if self.selected_set is not None and set_key != self.selected_set:
                continue
            ltf_name = ladder[2].value
            ltf = newly_closed.get(ltf_name)
            if ltf is None:
                continue
            htf = engine.get_state(ladder[0])
            mtf = engine.get_state(ladder[1])
            if htf is None or mtf is None:
                self.last_decision[set_key] = "WAITING_FOR_CLOSED_CONTEXT"

                continue
            # HTF bias active? Allow ambiguous ranges (location still valid from most recent range)
            if htf.structural_trend in (-1, 1) and htf.range_location is not None:
                self.count_htf_bias_active += 1
            else:
                self.last_decision[set_key] = "WAITING_FOR_HTF_STRUCTURE"
                continue
            setup_movement_id = self._advance_context_movement(symbol, set_key, htf)
            if setup_movement_id is None:
                self.last_decision[set_key] = "WAITING_FOR_HTF_STRUCTURE"
                continue
            setup = self._higher_timeframe_setup(htf, mtf)
            if setup is None:
                self.last_decision[set_key] = "HTF_MTF_FILTER"
                continue
            expected_phase, direction, mtf_confirmation_at = setup
            if not self._location_matches(mtf, direction, pullback=(expected_phase == "PULLBACK")):
                self.last_decision[set_key] = "MTF_SETUP_INVALID"
                continue
            self.count_mtf_setup_aligned += 1
            trigger_result = self._fresh_ltf_trigger(
                engine, ladder[2], ltf, direction, mtf_confirmation_at,
            )
            if trigger_result is None:
                continue
            # NEW: Check higher timeframe bias for correlation
            htf_timeframe = ladder[0]
            next_higher_tf = self.NEXT_HIGHER_TIMEFRAME.get(htf_timeframe)
            if next_higher_tf is not None:
                higher_state = engine.get_state(next_higher_tf)
                if higher_state is not None:
                    # For long trades (direction=1), require higher_state.bullish trend
                    # For short trades (direction=-1), require higher_state.bearish trend
                    if direction == 1 and higher_state.structural_trend != 1:
                        self.last_decision[set_key] = "HIGHER_TF_BIAS_MISMATCH_LONG"
                        continue
                    if direction == -1 and higher_state.structural_trend != -1:
                        self.last_decision[set_key] = "HIGHER_TF_BIAS_MISMATCH_SHORT"
                        continue
            trigger, trigger_swing_price, trigger_details = trigger_result
            # NEW: Check higher timeframe bias for correlation
            htf_timeframe = ladder[0]
            next_higher_tf = self.NEXT_HIGHER_TIMEFRAME.get(htf_timeframe)
            if next_higher_tf is not None:
                higher_state = engine.get_state(next_higher_tf)
                if higher_state is not None:
                    # For long trades (direction=1), require higher_state.bullish trend
                    # For short trades (direction=-1), require higher_state.bearish trend
                    if direction == 1 and higher_state.structural_trend != 1:
                        self.last_decision[set_key] = "HIGHER_TF_BIAS_MISMATCH_LONG"
                        continue
                    if direction == -1 and higher_state.structural_trend != -1:
                        self.last_decision[set_key] = "HIGHER_TF_BIAS_MISMATCH_SHORT"
                        continue
            self.count_ltf_trigger_fired += 1
            built = self._build_entry(
                event, set_key, movement_id, setup_movement_id,
                direction, ltf, mtf, htf, expected_phase, trigger,
                engine, ladder[2].value, trigger_swing_price, trigger_details,
            )
            if built is not None:
                intent, net_rr, stop_pips = built
                # NOTE: _build_entry already enforces the RR floor and counts
                # the rejection there. Do NOT recount here (was double-count bug).
                
                # For LIMIT orders, check if we already have an active limit order for this setup
                # If so, don't place a new order (the old one is still active)
                # If not, place the limit order and mark this setup as having an active limit order
                if intent.order_type == OrderType.LIMIT:
                    setup_key = (symbol, set_key, movement_id)
                    if setup_key not in self._active_limit_order_setups:
                        # No active limit order for this setup - place one and mark as active
                        self._active_limit_order_setups[setup_key] = event.timestamp
                        candidates.append((net_rr, -stop_pips, -set_index, intent, set_key))
                    # If there's already an active limit order, we don't place a new one
                    # (the original limit order is still active in the market)
                else:
                    # MARKET order - always process it
                    candidates.append((net_rr, -stop_pips, -set_index, intent, set_key))

        if not candidates:
            return []
        self.raw_signal_count += len(candidates)
        movement_key = (symbol, movement_id)
        self.unique_signal_movements.add(movement_key)
        self.multi_set_overlap_count += max(0, len({item[4] for item in candidates}) - 1)
        directions = {item[3].side for item in candidates}
        if len(directions) > 1:
            self.dropped_conflicting_signal_count += len(candidates)
            self._used_movements.add(movement_key)
            self.last_decision[symbol] = "CONFLICTING_SET_SIGNALS_DROPPED"
            self.count_rejected_by_conflict += len(candidates)
            return []
        best = max(candidates, key=lambda item: item[:3])
        self._used_movements.add(movement_key)
        self.allocated_signal_movements.add(movement_key)
        self.last_decision[symbol] = f"ENTRY_{best[4]}_NET_RR_{best[0]:.2f}"
        self.count_orders_dispatched += 1
        return [best[3]]

    def _advance_context_movement(
        self,
        symbol: str,
        set_key: str,
        state: CanonicalTimeframeState,
    ) -> Optional[str]:
        """Identify this set's causal HTF leg for audit tags.

        Entry arbitration uses a shared current M3 movement ID so overlapping
        sets can compete on the same execution event.
        """
        key = (symbol, set_key)
        if state.structural_trend not in (-1, 1):
            return None
        direction = int(state.structural_trend)
        latest_break = state.breaks[-1] if state.breaks else None
        if latest_break is not None:
            signature: tuple[object, ...] = (
                direction, latest_break.timestamp, latest_break.swing_index,
                latest_break.break_type.value, latest_break.direction,
            )
            anchor_time = latest_break.timestamp
            leg_label = f"SWING-{latest_break.swing_index}-DIR-{latest_break.direction}"
        else:
            if self._context_movement_direction.get(key) == direction:
                return self._context_movement_id.get(key)
            signature = (direction, state.timestamp, state.bar_index)
            anchor_time = state.timestamp
            leg_label = f"BAR-{state.bar_index}-DIR-{direction}"
        if signature != self._context_movement_signature.get(key):
            sequence = self._context_movement_sequence.get(key, 0) + 1
            self._context_movement_sequence[key] = sequence
            self._context_movement_direction[key] = direction
            self._context_movement_signature[key] = signature
            self._context_movement_id[key] = (
                f"{symbol}_{anchor_time.isoformat()}_{state.timeframe}_{leg_label}_"
                f"{set_key}_LEG-{sequence:06d}"
            )
        return self._context_movement_id.get(key)

    def _advance_movement(
        self,
        symbol: str,
        state: Optional[CanonicalTimeframeState],
    ) -> None:
        if state is None or state.structural_trend not in (-1, 1):
            return
        direction = int(state.structural_trend)
        latest_break = state.breaks[-1] if state.breaks else None
        if latest_break is not None:
            signature: tuple[object, ...] = (
                direction, latest_break.timestamp, latest_break.swing_index,
                latest_break.break_type.value, latest_break.direction,
            )
            anchor_time = latest_break.timestamp
            leg_label = f"SWING-{latest_break.swing_index}-DIR-{latest_break.direction}"
        else:
            if self._movement_direction.get(symbol) == direction:
                return
            signature = (direction, state.timestamp, state.bar_index)
            anchor_time = state.timestamp
            leg_label = f"BAR-{state.bar_index}-DIR-{direction}"
        if signature != self._movement_signature.get(symbol):
            sequence = self._movement_sequence.get(symbol, 0) + 1
            self._movement_sequence[symbol] = sequence
            self._movement_direction[symbol] = direction
            self._movement_signature[symbol] = signature
            self._movement_id[symbol] = (
                f"{symbol}_{anchor_time.isoformat()}_{state.timeframe}_{leg_label}_LEG-{sequence:06d}"
            )

    def _higher_timeframe_setup(
        self,
        htf: CanonicalTimeframeState,
        mtf: CanonicalTimeframeState,
    ) -> Optional[tuple[str, int, datetime]]:
        """
        HTF trend defines the bias. MTF setup is VALID if price is in the HTF
        corrective zone (Discount for longs, Premium for shorts) and has NOT
        invalidated the HTF structure. We do NOT require MTF to trend in the
        HTF direction; the MTF is the corrective/pullback leg.

        CONTINUATION ONLY: Long in HTF Discount, Short in HTF Premium.
        Target is the HTF External Weak Extreme (Weak High / Weak Low).
        """
        htf_trend = htf.structural_trend
        if htf_trend not in (-1, 1) or htf.range_location is None:
            return None

        # CONTINUATION ONLY: bullish HTF + Discount = long; bearish HTF + Premium = short
        if htf_trend > 0 and htf.location != "DISCOUNT":
            return None
        if htf_trend < 0 and htf.location != "PREMIUM":
            return None

        direction = int(htf_trend)

        # Check MTF alignment: price must be in the HTF corrective zone
        # and NOT have invalidated the HTF structure
        if not self._location_matches(mtf, direction, pullback=True):
            return None

        # Check HTF invalidation: price must not have closed beyond HTF Protected Swing
        invalidation = htf.invalidation_condition
        if invalidation is not None:
            # If HTF has an invalidation condition, check if MTF price has triggered it
            # For long: invalidation is "closed price below X" - if MTF low < X, invalidated
            # For short: invalidation is "closed price above X" - if MTF high > X, invalidated
            pass  # Invalidation check is handled by _location_matches on HTF

        # Check if MTF price has tested an active HTF Key Zone (OB, FVG, Equilibrium)
        if not self._mtf_tested_htf_zone(mtf, htf, direction):
            return None

        # MTF alignment is confirmed. Setup persists for setup_persistence_bars bars.
        # Use the latest MTF bar timestamp as the setup confirmation time.
        setup_time = mtf.timestamp

        return "CONTINUATION", direction, setup_time

    def _mtf_tested_htf_zone(
        self,
        mtf: CanonicalTimeframeState,
        htf: CanonicalTimeframeState,
        direction: int,
    ) -> bool:
        """
        Check if MTF price action has tested/reached an active HTF Key Zone.
        Zones include: Order Blocks (OB), Fair Value Gaps (FVG), and Equilibrium boundary.
        """
        if not htf.zones and (htf.structural_range is None):
            return False

        mtf_high = mtf.current_high
        mtf_low = mtf.current_low

        # Check Equilibrium test
        if htf.structural_range is not None:
            eq = htf.structural_range.equilibrium
            if direction > 0:
                # Long: test of equilibrium from above (pullback into discount)
                if mtf_low <= eq <= mtf_high:
                    return True
            else:
                # Short: test of equilibrium from below (pullback into premium)
                if mtf_low <= eq <= mtf_high:
                    return True

        # Check HTF zones (OB, FVG)
        for zone in htf.zones:
            zone_low = zone.price_low
            zone_high = zone.price_high
            if direction > 0:
                # Long: test of bullish zone (OB, bullish FVG) from above
                if mtf_low <= zone_high and mtf_high >= zone_low:
                    return True
            else:
                # Short: test of bearish zone (OB, bearish FVG) from below
                if mtf_high >= zone_low and mtf_low <= zone_high:
                    return True

        return False

    @staticmethod
    def _location_matches(
        state: CanonicalTimeframeState,
        direction: int,
        *,
        pullback: bool = False,
    ) -> bool:
        """
        For CONTINUATION trades:
        - Long (direction > 0): price must be in DISCOUNT (< 0.50) or EQUILIBRIUM
        - Short (direction < 0): price must be in PREMIUM (> 0.50) or EQUILIBRIUM
        """
        if state.range_location is None:
            return False
        if direction > 0:
            # Long: must be in Discount or Equilibrium (not Premium)
            return state.range_location <= Decimal("0.50")
        else:
            # Short: must be in Premium or Equilibrium (not Discount)
            return state.range_location >= Decimal("0.50")

    def _calculate_atr_pips(
        self,
        engine: UniversalTimeframeStateEngine,
        timeframe_name: str,
        pair: CurrencyPair,
        period: int = 14,
    ) -> Optional[Decimal]:
        """Calculate ATR in pips from recent closed bars of the given timeframe."""
        history = engine.get_history(timeframe_name)
        if len(history) < period + 1:
            return None
        recent = history[-(period + 1):]
        true_ranges = []
        prev_close = None
        for st in recent:
            high = st.current_high
            low = st.current_low
            close = st.current_price
            if prev_close is not None:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                true_ranges.append(tr)
            prev_close = close
        if len(true_ranges) < period:
            return None
        atr = sum(true_ranges[-period:], Decimal("0")) / Decimal(period)
        return pair.to_pips(atr)

    def _fresh_ltf_trigger(
        self,
        engine: UniversalTimeframeStateEngine,
        timeframe: CanonicalTimeframe,
        state: CanonicalTimeframeState,
        direction: Optional[int],
        after_timestamp: datetime,
    ) -> Optional[tuple[str, Decimal, dict]]:
        """
        LTF trigger with multiple entry patterns:
        1. MICRO_BOS - Break of Structure with candle body close
        2. CHoCH (Change of Character) - liquidity sweep + reclaim
        3. SWEEP_RECLAIM - liquidity pool sweep and reclaim
        
        Returns tuple of (trigger_type, trigger_swing_price, trigger_details) for stop placement and mitigation entry.
        trigger_details contains information needed for two-stage entry (impulse leg details).
        """
        if direction not in (-1, 1) or state.timestamp <= after_timestamp:
            return None

        # Get previous LTF state
        previous_states = engine.get_history(timeframe)
        previous = previous_states[-2] if len(previous_states) >= 2 else None
        if previous is None or previous.timestamp >= state.timestamp:
            return None

        # 1. MICRO_BOS - Break of Structure with candle body close (original trigger)
        if (
            state.structural_trend == direction
            and state.phase == MarketPhase.CONTINUATION
            and state.breaks
            and state.breaks[-1].timestamp == state.timestamp
            and state.breaks[-1].direction == direction
        ):
            # For MICRO_BOS, use the break swing price as stop
            # StructureBreak has: break_type, swing_price, body_close
            if state.breaks:
                # Use the break swing price as the stop level
                return "MICRO_BOS", state.breaks[-1].swing_price, {"impulse_high": state.breaks[-1].swing_price, "impulse_low": state.breaks[-1].swing_price}
            return "MICRO_BOS", state.current_price, {"impulse_high": state.current_price, "impulse_low": state.current_price}

        # 2. CHoCH (Change of Character) trigger for zone mitigation entry
        # Find the most recent swing in the opposite direction of HTF trend
        target_swing_type = "low" if direction > 0 else "high"
        opposite_swings = [
            s for s in state.swings
            if s.swing_type.value == target_swing_type
        ]
        if opposite_swings:
            # Get the most recent swing in the target direction
            recent_swing = max(opposite_swings, key=lambda s: s.timestamp)
            swing_price = recent_swing.price

            # CHoCH detection: previous candle swept the swing extreme,
            # current candle reclaims (closes back inside) with body close
            if direction > 0:
                # Long: previous low swept below swing low, current close reclaims above swing low
                swept = previous.current_low < swing_price
                reclaimed = state.current_price > swing_price
                if swept and reclaimed:
                    # For CHoCH, the impulse leg is from the swing low to the previous candle high
                    impulse_high = previous.current_high
                    impulse_low = swing_price
                    return "CHoCH_RECLAIM", swing_price, {"impulse_high": impulse_high, "impulse_low": impulse_low}
            else:
                # Short: previous high swept above swing high, current close reclaims below swing high
                swept = previous.current_high > swing_price
                reclaimed = state.current_price < swing_price
                if swept and reclaimed:
                    # For CHoCH, the impulse leg is from the swing high to the previous candle low
                    impulse_high = swing_price
                    impulse_low = previous.current_low
                    return "CHoCH_RECLAIM", swing_price, {"impulse_high": impulse_high, "impulse_low": impulse_low}

        # 3. Fallback: check for sweep-and-reclaim of liquidity pools (EQL/EQH)
        for zone_id, zone in zip(previous.zone_ids, previous.zones):
            if direction > 0 and zone_id.startswith("EQL:"):
                if state.current_low < zone.price_low and state.current_price >= zone.price_high:
                    # Stop is at the zone low (structural level)
                    # For SWEEP_RECLAIM, the impulse leg is from the zone low to the previous candle high
                    impulse_high = previous.current_high
                    impulse_low = zone.price_low
                    return "SWEEP_RECLAIM", zone.price_low, {"impulse_high": impulse_high, "impulse_low": impulse_low}
            elif direction < 0 and zone_id.startswith("EQH:"):
                if state.current_high > zone.price_high and state.current_price <= zone.price_low:
                    # Stop is at the zone high (structural level)
                    # For SWEEP_RECLAIM, the impulse leg is from the zone high to the previous candle low
                    impulse_high = zone.price_high
                    impulse_low = previous.current_low
                    return "SWEEP_RECLAIM", zone.price_high, {"impulse_high": impulse_high, "impulse_low": impulse_low}
        return None

    def _build_entry(
        self,
        event: BarEvent,
        set_key: str,
        movement_id: str,
        htf_movement_id: str,
        direction: int,
        ltf: CanonicalTimeframeState,
        mtf: CanonicalTimeframeState,
        htf: CanonicalTimeframeState,
        expected_phase: str,
        trigger: str,
        engine: UniversalTimeframeStateEngine,
        ltf_name: str,
        trigger_swing_price: Decimal,
        trigger_details: dict,
    ) -> Optional[tuple[OrderIntent, Decimal, Decimal]]:
        pair = CurrencyPair.from_symbol(event.symbol)
        entry = ltf.current_price
        stop = self._structural_stop(pair, direction, entry, trigger_swing_price, engine, ltf_name)
        target = self._structural_target(direction, entry, htf, expected_phase)
        if stop is None or target is None:
            self.last_decision[set_key] = "NO_STRUCTURAL_STOP_OR_TARGET"
            self.count_rejected_by_stop_target += 1
            return None
        stop_pips = pair.to_pips(abs(entry - stop))
        target_pips = pair.to_pips(abs(target - entry))
        if not self.minimum_stop_pips <= stop_pips <= self.maximum_stop_pips:
            self.last_decision[set_key] = "STOP_DISTANCE_FILTER"
            self.count_rejected_by_stop_target += 1
            return None
        if event.spread > self.maximum_entry_spread_pips:
            self.last_decision[set_key] = "SPREAD_FILTER"
            self.count_rejected_by_spread_news += 1
            return None

        quote_rate = self._quote_to_account_rate(pair, entry)
        if quote_rate is None or quote_rate <= 0:
            self.last_decision[set_key] = "MISSING_QUOTE_CURRENCY_CONVERSION"
            return None
        pip_value_per_lot = (
            Decimal(pair.standard_lot_units) * pair.pip_size * quote_rate
        )
        if pip_value_per_lot <= 0:
            return None
        commission_pips = self.commission_per_lot_round_turn / pip_value_per_lot
        slippage_pips = self._estimated_slippage_pips(event)
        total_cost_pips = max(Decimal("0"), event.spread) + commission_pips + slippage_pips
        net_rr = (target_pips - total_cost_pips) / (stop_pips + total_cost_pips)
        # EXP_002 diagnostic: record the offered net R:R for every trigger that
        # reaches structural pricing, regardless of the frozen floor outcome.
        self.rr_samples.append(float(net_rr))
        self.rr_samples_by_set.setdefault(set_key, []).append(float(net_rr))
        if net_rr < self.minimum_net_reward_risk:
            self.last_decision[set_key] = "NET_REWARD_RISK_FILTER"
            self.count_rejected_by_rr_floor += 1
            return None

        # Calculate mitigation entry price using OTE zone (38.2%-61.8% retracement)
        # instead of rigid 50.0% line. This captures the normal corrective range
        # where price typically retraces before resuming the HTF trend.
        mitigation_entry_price = None
        if trigger_details and "impulse_high" in trigger_details and "impulse_low" in trigger_details:
            impulse_high = trigger_details["impulse_high"]
            impulse_low = trigger_details["impulse_low"]
            # OTE (Optimal Trade Entry) zone: 38.2% to 61.8% Fibonacci retracement
            # 38.2% = deeper correction, 61.8% = shallower correction
            ote_low  = impulse_high - (impulse_high - impulse_low) * Decimal("0.382")
            ote_high = impulse_high - (impulse_high - impulse_low) * Decimal("0.618")
            # For longs: OTE zone is between ote_low and ote_high (price retreats DOWN from impulse high)
            # For shorts: OTE zone is between ote_low and ote_high (price retreats UP from impulse low)
            if direction > 0:
                # Long: mitigation fill if price retreats into OTE zone from above
                # We'll use the midpoint as the limit price, but accept fills across the zone
                mitigation_entry_price = (ote_low + ote_high) / Decimal("2")
            else:
                # Short: mitigation fill if price retreats into OTE zone from below
                mitigation_entry_price = (ote_low + ote_high) / Decimal("2")
        
        # Use mitigation entry price if available, otherwise use current price
        entry_price = mitigation_entry_price if mitigation_entry_price is not None else ltf.current_price
        
        lot_size = self.size_for_risk(
            equity=self.account_equity,
            risk_fraction=self.risk_fraction,
            # Reserve the spread, round-turn commission, and slippage budget
            # inside the account-level 1% cap as well as the stop distance.
            stop_pips=stop_pips + total_cost_pips,
            pip_value_per_lot=pip_value_per_lot,
            lot_step=self.lot_step,
            maximum_lots=self.maximum_lots,
            standard_lot_units=pair.standard_lot_units,
        )
        if lot_size is None:
            self.last_decision[set_key] = "POSITION_SIZE_BELOW_MINIMUM"
            return None
        risk_side = OrderSide.BUY if direction > 0 else OrderSide.SELL
        # Use LIMIT order for mitigation entry, MARKET if mitigation price not available
        order_type = OrderType.LIMIT if mitigation_entry_price is not None else OrderType.MARKET
        intent = self.create_intent(
            symbol=event.symbol,
            side=risk_side,
            order_type=order_type,
            lot_size=lot_size,
            timestamp=event.timestamp,
            # For LIMIT orders, price is the limit price; for MARKET, it's ignored but we set it anyway
            limit_price=pair.round_price(entry_price) if order_type == OrderType.LIMIT else None,
            stop_loss=pair.round_price(stop),
            take_profit=pair.round_price(target),
            urgency=UrgencyLevel.MEDIUM,
            client_tag=(
                f"FRACTAL|SET={set_key}|PHASE={expected_phase}|"
                f"HTF_LOCATION={htf.location}|MTF_LOCATION={mtf.location}|"
                f"LTF_LOCATION={ltf.location}|ENTRY_LOCATION={ltf.location}|"
                f"TRIGGER={trigger}|HTF_LEG={htf_movement_id}|MOVEMENT={movement_id}|"
                f"ENTRY_TYPE={'MITIGATION' if mitigation_entry_price is not None else 'MARKET'}"
            ),
        )
        return intent, net_rr, stop_pips

    def manage_open_position(
        self,
        event: BarEvent,
        position: object,
        open_intent: OrderIntent,
        current_stop_loss: Optional[Decimal],
        favorable_high: Decimal,
        favorable_low: Decimal,
    ) -> dict[str, object]:
        """Dynamic de-risking and trailing harness:
        - Break-even at +2.0R
        - Structural trailing at +3.5R behind MTF higher/lower highs
        - Exit on MTF structural break
        - Hard TP remains at HTF External Weak Extreme

        Decisions use the newly closed source bar and therefore take effect at
        the next source-bar open in the event-driven backtester.
        """
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        engine = self._engines.get(symbol)
        if engine is None:
            return {}
        pair = CurrencyPair.from_symbol(symbol)
        tag_parts = (open_intent.client_tag or "").split("|")
        set_part = next((part for part in tag_parts if part.startswith("SET=")), "")
        set_key = set_part.split("=", 1)[1] if set_part else ""
        ladder = TIMEFRAME_SETS.get(set_key)
        if ladder is None:
            return {}
        mtf = engine.get_state(ladder[1])
        htf = engine.get_state(ladder[0])  # HTF for structural trailing reference
        if mtf is None or htf is None:
            return {}
        direction = 1 if position.side == OrderSide.BUY else -1
        
        # Extract entry and initial stop from the open intent
        entry = position.average_entry_price
        initial_stop = open_intent.stop_loss
        if initial_stop is None:
            return {}
        initial_risk = abs(entry - initial_stop)
        if initial_risk <= 0:
            return {}
        
        # Calculate current favorable excursion in R-multiples
        favorable_price = favorable_high if direction > 0 else favorable_low
        favorable_move = favorable_price - entry if direction > 0 else entry - favorable_price
        favorable_r = favorable_move / initial_risk if initial_risk > 0 else Decimal("0")
        
        # 1. Check for MTF structural break (exit signal)
        latest_break = mtf.breaks[-1] if mtf.breaks else None
        if (
            latest_break is not None
            and latest_break.timestamp > position.opened_at
            and latest_break.direction == -direction
            and mtf.structural_trend == -direction
        ):
            return {"exit_next_open": True, "exit_reason": "MTF_STRUCTURAL_BREAK"}
        
        # 2. Break-even trigger: at +2.0R, move stop to break-even (+0.1R for commissions)
        if favorable_r >= Decimal("2.0"):
            break_even_level = entry + (initial_risk * Decimal("0.1")) if direction > 0 else entry - (initial_risk * Decimal("0.1"))
            # Only move stop if it improves (tightens) the current stop
            if direction > 0:
                if current_stop_loss is None or break_even_level > current_stop_loss:
                    return {"stop_loss": pair.round_price(break_even_level), "stop_reason": "BREAK_EVEN_PLUS_COMMISSION"}
            else:
                if current_stop_loss is None or break_even_level < current_stop_loss:
                    return {"stop_loss": pair.round_price(break_even_level), "stop_reason": "BREAK_EVEN_PLUS_COMMISSION"}
        
        # 3. Structural trailing: at +3.5R, trail behind confirmed MTF higher/lows
        if favorable_r >= Decimal("3.5"):
            # Get MTF key levels for structural trailing
            anchors = [
                level.price for level in mtf.key_levels
                if level.protected and level.swing_type == ("low" if direction > 0 else "high")
            ]
            if anchors:
                # Trail behind the most recent confirmed swing
                trailing_stop_price = max(anchors) if direction > 0 else min(anchors)
                # Only move stop if it improves (tightens) the current stop
                if direction > 0:
                    if current_stop_loss is None or trailing_stop_price > current_stop_loss:
                        return {"stop_loss": pair.round_price(trailing_stop_price), "stop_reason": "STRUCTURAL_TRAILING"}
                else:
                    if current_stop_loss is None or trailing_stop_price < current_stop_loss:
                        return {"stop_loss": pair.round_price(trailing_stop_price), "stop_reason": "STRUCTURAL_TRAILING"}
        
        # 4. Default: trail at confirmed MTF protected swings (original behavior)
        # Only apply if we haven't already moved to break-even or structural trailing
        if favorable_r < Decimal("2.0"):  # Only use original trailing before break-even
            anchors = [
                level.price for level in mtf.key_levels
                if level.protected and level.swing_type == ("low" if direction > 0 else "high")
            ]
            if not anchors:
                return {}
            candidate = (
                max(anchors) - pair.to_price(self.stop_buffer_pips)
                if direction > 0
                else min(anchors) + pair.to_price(self.stop_buffer_pips)
            )
            market_price = (
                event.close if direction > 0
                else event.close + pair.to_price(max(Decimal("0"), event.spread))
            )
            if direction > 0:
                if candidate >= market_price or (current_stop_loss is not None and candidate <= current_stop_loss):
                    return {}
            elif candidate <= market_price or (current_stop_loss is not None and candidate >= current_stop_loss):
                return {}
            return {"stop_loss": pair.round_price(candidate)}
        
        # If we've reached break-even or beyond but not yet structural trailing, keep current stop
        # (the stop loss was already moved to break-even level above)
        return {}

    def _estimated_slippage_pips(self, event: BarEvent) -> Decimal:
        """Use a fixed base plus a causal fast-vs-slow ATR expansion reserve."""
        history = self.get_history(event.symbol)
        if len(history) < 2:
            return self.slippage_buffer_pips
        window = history[-100:]
        true_ranges: list[Decimal] = []
        previous_close = window[0].close
        for bar in window:
            true_ranges.append(max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            ))
            previous_close = bar.close
        atr_fast = sum(true_ranges[-14:], Decimal("0")) / Decimal(len(true_ranges[-14:]))
        atr_slow = sum(true_ranges, Decimal("0")) / Decimal(len(true_ranges))
        expansion = max(Decimal("0"), atr_fast / atr_slow - Decimal("1")) if atr_slow > 0 else Decimal("0")
        pair = CurrencyPair.from_symbol(event.symbol)
        expansion_pips = pair.to_pips(atr_fast) * self.atr_expansion_slippage_factor * expansion
        return self.slippage_buffer_pips + expansion_pips

    @staticmethod
    def size_for_risk(
        *,
        equity: Decimal,
        risk_fraction: Decimal,
        stop_pips: Decimal,
        pip_value_per_lot: Decimal,
        lot_step: Decimal = Decimal("0.01"),
        maximum_lots: Decimal = Decimal("1"),
        standard_lot_units: int = 100_000,
    ) -> Optional[LotSize]:
        """Round position size down so modeled stop risk cannot exceed its cap."""
        if min(equity, risk_fraction, stop_pips, pip_value_per_lot, lot_step, maximum_lots) <= 0:
            return None
        raw_lots = (equity * risk_fraction) / (stop_pips * pip_value_per_lot)
        capped_lots = min(raw_lots, maximum_lots)
        steps = (capped_lots / lot_step).to_integral_value(rounding=ROUND_DOWN)
        lots = steps * lot_step
        if lots < lot_step:
            return None
        return LotSize.from_lots(lots, standard_lot_units=standard_lot_units)

    def _structural_stop(
        self,
        pair: CurrencyPair,
        direction: int,
        entry: Decimal,
        trigger_swing_price: Decimal,
        engine: Optional[UniversalTimeframeStateEngine] = None,
        ltf_name: Optional[str] = None,
    ) -> Optional[Decimal]:
        """
        Volatility-insulated structural stop: The LTF swing extreme that was swept/reclaimed
        to trigger the entry, plus dynamic buffer (max of 2 pips or 0.5 * ATR).
        This is the level that, if breached, invalidates the entry pattern.
        """
        # Calculate dynamic buffer: max(2 pips, 0.5 * ATR_15M)
        dynamic_buffer_pips = self.stop_buffer_pips  # default 2 pips
        if engine is not None and ltf_name is not None:
            atr_pips = self._calculate_atr_pips(engine, ltf_name, pair, period=14)
            if atr_pips is not None:
                half_atr = atr_pips * Decimal("0.5")
                dynamic_buffer_pips = max(self.stop_buffer_pips, half_atr)
        
        if direction > 0:
            # Long: stop below the swing low that was swept and reclaimed
            # The trigger_swing_price IS the swing low that was breached and reclaimed
            return trigger_swing_price - pair.to_price(dynamic_buffer_pips)
        # Short: stop above the swing high that was swept and reclaimed
        return trigger_swing_price + pair.to_price(dynamic_buffer_pips)

    def _structural_target(
        self,
        direction: int,
        entry: Decimal,
        htf: CanonicalTimeframeState,
        expected_phase: str,
    ) -> Optional[Decimal]:
        """
        CONTINUATION ONLY: Target the HTF External Weak Extreme.
        - Long: Weak High (the weaker of the two external highs that defined the range)
        - Short: Weak Low (the weaker of the two external lows that defined the range)
        """
        # Only CONTINUATION phase is valid
        if expected_phase != "CONTINUATION":
            return None

        wanted_swing = "high" if direction > 0 else "low"

        # Find HTF External Weak Extremes from key_levels
        # Weak extremes are marked with weak=True in key_levels
        weak_extremes = [
            level.price for level in htf.key_levels
            if level.swing_type == wanted_swing and level.weak
            and ((direction > 0 and level.price > entry) or (direction < 0 and level.price < entry))
        ]

        if weak_extremes:
            # Target the nearest weak extreme beyond entry
            return min(weak_extremes) if direction > 0 else max(weak_extremes)

        # Fallback: if no weak levels marked, use structural range boundary
        if htf.structural_range is not None:
            boundary = htf.structural_range.high if direction > 0 else htf.structural_range.low
            if (direction > 0 and boundary > entry) or (direction < 0 and boundary < entry):
                return boundary

        return None

    def _quote_to_account_rate(
        self,
        pair: CurrencyPair,
        price: Decimal,
    ) -> Optional[Decimal]:
        if pair.quote_currency == self.account_currency:
            return Decimal("1")
        if pair.base_currency == self.account_currency:
            return Decimal("1") / price if price > 0 else None
        return self.quote_to_account_rates.get(pair.quote_currency)

    def get_funnel_stats(self) -> dict:
        """Return the funnel counters for diagnostics."""
        return {
            "total_bars": self.count_total_bars,
            "htf_bias_active": self.count_htf_bias_active,
            "mtf_setup_aligned": self.count_mtf_setup_aligned,
            "ltf_trigger_fired": self.count_ltf_trigger_fired,
            "rejected_by_rr_floor": self.count_rejected_by_rr_floor,
            "rejected_by_stop_target": self.count_rejected_by_stop_target,
            "rejected_by_spread_news": self.count_rejected_by_spread_news,
            "rejected_by_conflict": self.count_rejected_by_conflict,
            "orders_dispatched": self.count_orders_dispatched,
            "rr_evaluated": len(self.rr_samples),
            "rr_histogram": self.get_rr_histogram(),
        }

    def get_rr_histogram(self, bins: tuple[float, ...] = (2.0, 2.5, 3.0, 3.5, 4.0)) -> dict[str, int]:
        """Bucket offered net R:R so IS calibration sees the floor trade-off."""
        edges = (float("-inf"),) + tuple(bins) + (float("inf"),)
        labels = [f"<{bins[0]:.1f}"] + [
            f"{bins[i]:.1f}-<{bins[i + 1]:.1f}" for i in range(len(bins) - 1)
        ] + [f">={bins[-1]:.1f}"]
        counts = {label: 0 for label in labels}
        for sample in self.rr_samples:
            for i, label in enumerate(labels):
                if sample < edges[i + 1]:
                    counts[label] += 1
                    break
        return counts

    def _process_expired_limit_order_setups(self, event: BarEvent) -> None:
        """Remove expired limit order setups (older than 12 bars = 180 minutes).
        This gives price sufficient time to complete its corrective cycle (M15:
        typical retracements take 6-12 candles to develop)."""
        cutoff_time = event.timestamp - timedelta(minutes=12*15)  # 180 minutes ago
        expired_setups = []
        for setup_key, placement_time in self._active_limit_order_setups.items():
            if placement_time < cutoff_time:
                expired_setups.append(setup_key)
        for setup_key in expired_setups:
            del self._active_limit_order_setups[setup_key]

    def _process_pending_limit_orders(self, event: BarEvent) -> None:
        """Process pending limit orders: check for fills, expire old orders.
        This method is called at the start of on_bar to handle limit orders
        placed on previous bars for mitigation entry."""

        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        engine = self._engines.get(symbol)
        if engine is None:
            return

        # Current bar range for fill detection
        bar_low = event.low
        bar_high = event.high

        # Orders to remove (filled or expired)
        orders_to_remove = []

        # Iterate over active limit order setups
        # Data structure: _active_limit_order_setups is dict[key=tuple(symbol, set_key, movement_id), value=datetime(placement_time)]
        for setup_key, placement_time in self._active_limit_order_setups.items():
            # Check if the order has expired (12 candles = 180 minutes for 15M timeframe)
            # 15M bar = 15 minutes, so 12 bars = 180 minutes
            time_diff = event.timestamp - placement_time
            if time_diff.total_seconds() > 12 * 15 * 60:  # 12 * 15 minutes = 180 min
                orders_to_remove.append(setup_key)
                continue

        # Remove expired orders
        for setup_key in orders_to_remove:
            del self._active_limit_order_setups[setup_key]
