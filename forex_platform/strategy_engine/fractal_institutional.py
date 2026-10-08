"""Research-first institutional structure strategy on the shared fractal ladder.

The strategy consumes completed source bars (M1 or aggregated M3) and shares
one causal state engine across all five timeframe sets. After HTF/MTF
validation, it enters on an LTF micro-BOS or liquidity sweep-and-reclaim close.
It is a candidate for backtesting and paper evaluation; it does not promise or
imply a profitable edge.
"""

from __future__ import annotations

from datetime import datetime, timezone
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
        minimum_net_reward_risk: Decimal | str | float = Decimal("4"),
        minimum_stop_pips: Decimal | str | float = Decimal("3"),
        maximum_stop_pips: Decimal | str | float = Decimal("80"),
        maximum_entry_spread_pips: Decimal | str | float = Decimal("3"),
        commission_per_lot_round_turn: Decimal | str | float = Decimal("7"),
        slippage_buffer_pips: Decimal | str | float = Decimal("0.2"),
        atr_expansion_slippage_factor: Decimal | str | float = Decimal("0.1"),
        stop_buffer_pips: Decimal | str | float = Decimal("2.0"),
        maximum_lots: Decimal | str | float = Decimal("100"),
        lot_step: Decimal | str | float = Decimal("0.01"),
        swing_lookback: int = 5,
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
        self.swing_lookback = int(swing_lookback)
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
        self.raw_signal_count = 0
        self.unique_signal_movements: set[tuple[str, str]] = set()
        self.multi_set_overlap_count = 0
        self.dropped_conflicting_signal_count = 0
        self.allocated_signal_movements: set[tuple[str, str]] = set()
        self.last_decision: dict[str, str] = {}
        self.daily_realized_pnl = Decimal("0")
        self.max_daily_loss_fraction = Decimal("0.02")

        if self.account_equity <= 0:
            raise ValueError("account_equity must be positive")
        if not Decimal("0") < self.risk_fraction <= Decimal("0.01"):
            raise ValueError("risk_fraction must be in (0, 0.01]")
        if self.minimum_net_reward_risk < Decimal("4"):
            raise ValueError("minimum_net_reward_risk cannot be below the frozen 4R floor")
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
        self.update_history(event)
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        engine = self._engines.setdefault(
            symbol,
            UniversalTimeframeStateEngine(
                symbol,
                base_timeframe=self.source_timeframe,
                # Fresh-break validation compares only the latest state with
                # its predecessor, so keep the online state buffer bounded.
                max_history_bars=3,
                swing_lookback=self.swing_lookback,
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
        self._advance_movement(symbol, newly_closed.get(CanonicalTimeframe.M3.value))

        session = ForexSessionEngine.get_session_state(event.timestamp)
        if self.session_filter_enabled and (not session.is_market_open or session.is_rollover):
            self.last_decision[symbol] = "SESSION_FILTER"
            return []
        if self.daily_realized_pnl <= -(self.account_equity * self.max_daily_loss_fraction):
            self.last_decision[symbol] = "DAILY_LOSS_LIMIT"
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
            setup_movement_id = self._advance_context_movement(symbol, set_key, htf)
            if setup_movement_id is None:
                self.last_decision[set_key] = "WAITING_FOR_HTF_STRUCTURE"
                continue
            setup = self._higher_timeframe_setup(htf, mtf)
            if setup is None:
                self.last_decision[set_key] = "HTF_MTF_FILTER"
                continue
            expected_phase, direction, mtf_confirmation_at = setup
            trigger = self._fresh_ltf_trigger(
                engine, ladder[2], ltf, direction, mtf_confirmation_at,
            )
            if trigger is None:
                continue
            built = self._build_entry(
                event, set_key, movement_id, setup_movement_id,
                direction, ltf, mtf, htf, expected_phase, trigger,
            )
            if built is not None:
                intent, net_rr, stop_pips = built
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
            return []
        best = max(candidates, key=lambda item: item[:3])
        self._used_movements.add(movement_key)
        self.allocated_signal_movements.add(movement_key)
        self.last_decision[symbol] = f"ENTRY_{best[4]}_NET_RR_{best[0]:.2f}"
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
        """Map HTF location to its expected phase, then require an MTF shift.

        Premium in a bullish range and discount in a bearish range call for a
        countertrend pullback. Discount in a bullish range and premium in a
        bearish range call for trend continuation. The MTF shift must close
        after the HTF state was published, keeping the parent-child sequence
        causal.
        """
        htf_trend = htf.structural_trend
        if htf_trend not in (-1, 1) or htf.range_ambiguous or htf.range_location is None:
            return None
        if htf.location == "PREMIUM":
            expected_phase, direction = "PULLBACK", -int(htf_trend)
        elif htf.location == "DISCOUNT":
            expected_phase, direction = "CONTINUATION", int(htf_trend)
        else:
            return None
        if mtf.structural_trend != direction or mtf.range_ambiguous:
            return None
        if not self._location_matches(mtf, direction, pullback=True):
            return None
        confirmations = [
            item for item in mtf.breaks
            if item.direction == direction and item.timestamp > htf.timestamp
        ]
        if not confirmations:
            return None
        # The MTF structure shift validates the setup. A closed LTF trigger
        # below determines the next-open execution point.
        return expected_phase, direction, confirmations[-1].timestamp

    @staticmethod
    def _location_matches(
        state: CanonicalTimeframeState,
        direction: int,
        *,
        pullback: bool = False,
    ) -> bool:
        if state.range_ambiguous or state.range_location is None:
            return False
        if direction > 0:
            return state.location in ("DISCOUNT", "EQUILIBRIUM") if not pullback else state.location == "DISCOUNT"
        return state.location in ("PREMIUM", "EQUILIBRIUM") if not pullback else state.location == "PREMIUM"

    def _fresh_ltf_trigger(
        self,
        engine: UniversalTimeframeStateEngine,
        timeframe: CanonicalTimeframe,
        state: CanonicalTimeframeState,
        direction: Optional[int],
        after_timestamp: datetime,
    ) -> Optional[str]:
        if direction not in (-1, 1) or state.timestamp <= after_timestamp:
            return None
        previous_states = engine.get_history(timeframe)
        previous = previous_states[-2] if len(previous_states) >= 2 else None
        if previous is None or previous.timestamp >= state.timestamp:
            return None
        if (
            state.structural_trend == direction
            and state.phase == MarketPhase.CONTINUATION
            and state.breaks
            and state.breaks[-1].timestamp == state.timestamp
            and state.breaks[-1].direction == direction
        ):
            return "MICRO_BOS"
        # Pools must have existed before this completed LTF candle. The state
        # engine retires a pool when swept, so inspect the preceding state.
        for zone_id, zone in zip(previous.zone_ids, previous.zones):
            if direction > 0 and zone_id.startswith("EQL:"):
                if state.current_low < zone.price_low and state.current_price >= zone.price_high:
                    return "SWEEP_RECLAIM"
            elif direction < 0 and zone_id.startswith("EQH:"):
                if state.current_high > zone.price_high and state.current_price <= zone.price_low:
                    return "SWEEP_RECLAIM"
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
    ) -> Optional[tuple[OrderIntent, Decimal, Decimal]]:
        pair = CurrencyPair.from_symbol(event.symbol)
        entry = ltf.current_price
        stop = self._structural_stop(pair, direction, entry, ltf)
        target = self._structural_target(direction, entry, htf, expected_phase)
        if stop is None or target is None:
            self.last_decision[set_key] = "NO_STRUCTURAL_STOP_OR_TARGET"
            return None
        stop_pips = pair.to_pips(abs(entry - stop))
        target_pips = pair.to_pips(abs(target - entry))
        if not self.minimum_stop_pips <= stop_pips <= self.maximum_stop_pips:
            self.last_decision[set_key] = "STOP_DISTANCE_FILTER"
            return None
        if event.spread > self.maximum_entry_spread_pips:
            self.last_decision[set_key] = "SPREAD_FILTER"
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
        if net_rr < self.minimum_net_reward_risk:
            self.last_decision[set_key] = "NET_REWARD_RISK_FILTER"
            return None

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
        intent = self.create_intent(
            symbol=event.symbol,
            side=risk_side,
            order_type=OrderType.MARKET,
            lot_size=lot_size,
            timestamp=event.timestamp,
            stop_loss=pair.round_price(stop),
            take_profit=pair.round_price(target),
            urgency=UrgencyLevel.MEDIUM,
            client_tag=(
                f"FRACTAL|SET={set_key}|PHASE={expected_phase}|"
                f"HTF_LOCATION={htf.location}|MTF_LOCATION={mtf.location}|"
                f"LTF_LOCATION={ltf.location}|ENTRY_LOCATION={ltf.location}|"
                f"TRIGGER={trigger}|HTF_LEG={htf_movement_id}|MOVEMENT={movement_id}"
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
        """Trail at confirmed MTF protected swings after +2R; exit on MTF break.

        Decisions use the newly closed source bar and therefore take effect at
        the next source-bar open in the event-driven backtester.
        """
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        engine = self._engines.get(symbol)
        if engine is None:
            return {}
        tag_parts = (open_intent.client_tag or "").split("|")
        set_part = next((part for part in tag_parts if part.startswith("SET=")), "")
        set_key = set_part.split("=", 1)[1] if set_part else ""
        ladder = TIMEFRAME_SETS.get(set_key)
        if ladder is None:
            return {}
        mtf = engine.get_state(ladder[1])
        if mtf is None:
            return {}
        direction = 1 if position.side == OrderSide.BUY else -1
        latest_break = mtf.breaks[-1] if mtf.breaks else None
        if (
            latest_break is not None
            and latest_break.timestamp > position.opened_at
            and latest_break.direction == -direction
            and mtf.structural_trend == -direction
        ):
            return {"exit_next_open": True, "exit_reason": "MTF_STRUCTURAL_BREAK"}

        initial_stop = open_intent.stop_loss
        if initial_stop is None:
            return {}
        entry = position.average_entry_price
        initial_risk = abs(entry - initial_stop)
        if initial_risk <= 0:
            return {}
        favorable_price = favorable_high if direction > 0 else favorable_low
        favorable_move = favorable_price - entry if direction > 0 else entry - favorable_price
        if favorable_move < Decimal("2") * initial_risk:
            return {}

        pair = CurrencyPair.from_symbol(symbol)
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
        state: CanonicalTimeframeState,
    ) -> Optional[Decimal]:
        if direction > 0:
            levels = [item for item in state.key_levels if item.swing_type == "low" and item.price < entry]
            swing_fallback = [item for item in state.swings if item.swing_type.value == "low" and item.price < entry]
            anchor = max(levels, key=lambda item: item.pivot_timestamp).price if levels else (
                max(swing_fallback, key=lambda item: item.timestamp).price if swing_fallback else
                state.structural_range.low if state.structural_range and state.structural_range.low < entry else None
            )
            if anchor is None:
                return None
            return anchor - pair.to_price(self.stop_buffer_pips)
        levels = [item for item in state.key_levels if item.swing_type == "high" and item.price > entry]
        swing_fallback = [item for item in state.swings if item.swing_type.value == "high" and item.price > entry]
        anchor = min(levels, key=lambda item: item.pivot_timestamp).price if levels else (
            min(swing_fallback, key=lambda item: item.timestamp).price if swing_fallback else
            state.structural_range.high if state.structural_range and state.structural_range.high > entry else None
        )
        if anchor is None:
            return None
        return anchor + pair.to_price(self.stop_buffer_pips)

    @staticmethod
    def _structural_target(
        direction: int,
        entry: Decimal,
        htf: CanonicalTimeframeState,
        expected_phase: str,
    ) -> Optional[Decimal]:
        wanted_swing = "high" if direction > 0 else "low"
        if expected_phase == "PULLBACK":
            if htf.structural_range is None:
                return None
            equilibrium = htf.structural_range.equilibrium
            candidates = [equilibrium] if (direction > 0 and equilibrium > entry) or (direction < 0 and equilibrium < entry) else []
            for zone in htf.zones:
                boundary = zone.price_low if direction > 0 else zone.price_high
                if (direction > 0 and boundary > entry) or (direction < 0 and boundary < entry):
                    candidates.append(boundary)
        else:
            candidates = [
                level.price for level in htf.key_levels
                if level.swing_type == wanted_swing and level.weak
                and ((direction > 0 and level.price > entry) or (direction < 0 and level.price < entry))
            ]
            if not candidates and htf.structural_range is not None:
                boundary = htf.structural_range.high if direction > 0 else htf.structural_range.low
                if (direction > 0 and boundary > entry) or (direction < 0 and boundary < entry):
                    candidates.append(boundary)
        if not candidates:
            return None
        return min(candidates) if direction > 0 else max(candidates)

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


__all__ = ["InstitutionalFractalStrategy"]
