"""Research-first institutional structure strategy on the shared fractal ladder.

The strategy consumes completed M1 bars, shares one causal state engine across
all five timeframe sets, arms only on a confirmed LTF structural break, and
enters only on a later zone retest while HTF/MTF context remains valid. It is a
candidate for backtesting and paper evaluation; it does not promise or imply a
profitable edge.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
from forex_platform.market_model.contracts import MarketPhase, MarketZone, ZoneType
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


@dataclass(frozen=True)
class ArmedRetest:
    set_key: str
    movement_id: str
    direction: int
    zone_id: str
    zone: MarketZone
    armed_at: datetime
    ltf_bars_waited: int = 0


class InstitutionalFractalStrategy(BaseStrategy):
    """Causal HTF-bias / MTF-pullback / LTF-reversal-and-retest candidate.

    All five canonical sets are evaluated from the same state histories. One
    confirmed 3M structural leg can produce at most one entry across those
    overlapping views. Position risk is sized from explicit account equity;
    quote-currency conversion for cross pairs must be supplied by the caller.
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
        risk_fraction: Decimal | str | float = Decimal("0.0025"),
        minimum_net_reward_risk: Decimal | str | float = Decimal("4"),
        minimum_stop_pips: Decimal | str | float = Decimal("3"),
        maximum_stop_pips: Decimal | str | float = Decimal("80"),
        maximum_entry_spread_pips: Decimal | str | float = Decimal("3"),
        commission_per_lot_round_turn: Decimal | str | float = Decimal("7"),
        slippage_buffer_pips: Decimal | str | float = Decimal("0.5"),
        stop_buffer_pips: Decimal | str | float = Decimal("1.5"),
        maximum_retest_distance_pips: Decimal | str | float = Decimal("30"),
        maximum_retest_bars: int = 8,
        maximum_lots: Decimal | str | float = Decimal("1"),
        lot_step: Decimal | str | float = Decimal("0.01"),
        swing_lookback: int = 5,
        session_filter: bool = True,
        selected_set: Optional[str] = None,
    ) -> None:
        selected_set = normalized_set_key(selected_set) if selected_set else None
        target_symbols = symbols or ["EURUSD"]
        super().__init__(
            strategy_id=strategy_id,
            name="Institutional Fractal (research candidate)",
            symbols=target_symbols,
            timeframes=[self.SUPPORTED_SOURCE_TIMEFRAME],
            parameters={
                "account_equity": str(account_equity),
                "risk_fraction": str(risk_fraction),
                "minimum_net_reward_risk": str(minimum_net_reward_risk),
                "maximum_retest_bars": maximum_retest_bars,
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
        self.stop_buffer_pips = Decimal(str(stop_buffer_pips))
        self.maximum_retest_distance_pips = Decimal(str(maximum_retest_distance_pips))
        self.maximum_retest_bars = int(maximum_retest_bars)
        self.maximum_lots = Decimal(str(maximum_lots))
        self.lot_step = Decimal(str(lot_step))
        self.swing_lookback = int(swing_lookback)
        self.session_filter_enabled = bool(session_filter)
        self.selected_set = selected_set

        self._engines: dict[str, UniversalTimeframeStateEngine] = {}
        self._movement_direction: dict[str, int] = {}
        self._movement_sequence: dict[str, int] = {}
        self._movement_id: dict[str, str] = {}
        self._armed: dict[tuple[str, str], ArmedRetest] = {}
        self._used_movements: set[tuple[str, str]] = set()
        self.last_decision: dict[str, str] = {}
        self.daily_realized_pnl = Decimal("0")
        self.max_daily_loss_fraction = Decimal("0.02")

        if self.account_equity <= 0:
            raise ValueError("account_equity must be positive")
        if not Decimal("0") < self.risk_fraction <= Decimal("0.01"):
            raise ValueError("risk_fraction must be in (0, 0.01]")
        if self.minimum_net_reward_risk < Decimal("4"):
            raise ValueError("minimum_net_reward_risk cannot be below the frozen 4R floor")
        if self.maximum_retest_bars < 1:
            raise ValueError("maximum_retest_bars must be positive")
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
        if event.timeframe != self.SUPPORTED_SOURCE_TIMEFRAME:
            raise ValueError("InstitutionalFractalStrategy requires completed M1 source bars")
        self.update_history(event)
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        engine = self._engines.setdefault(
            symbol,
            UniversalTimeframeStateEngine(
                symbol,
                base_timeframe=self.SUPPORTED_SOURCE_TIMEFRAME,
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
            key = (symbol, set_key)
            htf = engine.get_state(ladder[0])
            mtf = engine.get_state(ladder[1])
            if htf is None or mtf is None:
                self._armed.pop(key, None)
                self.last_decision[set_key] = "WAITING_FOR_CLOSED_CONTEXT"
                continue
            if not self._higher_timeframe_setup(htf, mtf):
                self._armed.pop(key, None)
                self.last_decision[set_key] = "HTF_MTF_FILTER"
                continue

            armed = self._armed.get(key)
            if armed is not None:
                if armed.movement_id != movement_id:
                    self._armed.pop(key, None)
                    armed = None
                else:
                    armed = replace(armed, ltf_bars_waited=armed.ltf_bars_waited + 1)
                    self._armed[key] = armed
                    live_zone = self._find_zone(ltf, armed.zone_id)
                    if (
                        armed.ltf_bars_waited > self.maximum_retest_bars
                        or live_zone is None
                        or not self._location_matches(ltf, armed.direction)
                    ):
                        self._armed.pop(key, None)
                        armed = None
                    elif live_zone.price_low <= ltf.current_price <= live_zone.price_high:
                        built = self._build_entry(
                            event, set_key, movement_id, armed.direction,
                            live_zone, ltf, mtf, htf,
                        )
                        if built is not None:
                            intent, net_rr, stop_pips = built
                            candidates.append((net_rr, -stop_pips, -set_index, intent, set_key))
                            self._armed.pop(key, None)

            if key not in self._armed and self._fresh_ltf_break(engine, ladder[2], ltf, htf.structural_trend):
                direction = int(htf.structural_trend)
                zone = self._nearest_retest_zone(ltf, direction, event.close)
                if zone is not None:
                    self._armed[key] = ArmedRetest(
                        set_key=set_key,
                        movement_id=movement_id,
                        direction=direction,
                        zone_id=zone[0],
                        zone=zone[1],
                        armed_at=ltf.timestamp,
                    )

        if not candidates:
            return []
        best = max(candidates, key=lambda item: item[:3])
        self._used_movements.add((symbol, movement_id))
        self._armed = {
            key: setup for key, setup in self._armed.items()
            if key[0] != symbol or setup.movement_id != movement_id
        }
        self.last_decision[symbol] = f"ENTRY_{best[4]}_NET_RR_{best[0]:.2f}"
        return [best[3]]

    def _advance_movement(
        self,
        symbol: str,
        state: Optional[CanonicalTimeframeState],
    ) -> None:
        if state is None or state.structural_trend not in (-1, 1):
            return
        direction = int(state.structural_trend)
        previous = self._movement_direction.get(symbol, 0)
        if direction != previous:
            sequence = self._movement_sequence.get(symbol, 0) + 1
            self._movement_sequence[symbol] = sequence
            self._movement_direction[symbol] = direction
            self._movement_id[symbol] = f"{symbol}|3M|LEG-{sequence:06d}"

    def _higher_timeframe_setup(
        self,
        htf: CanonicalTimeframeState,
        mtf: CanonicalTimeframeState,
    ) -> bool:
        direction = htf.structural_trend
        if direction not in (-1, 1) or htf.phase != MarketPhase.CONTINUATION:
            return False
        if htf.range_ambiguous or not self._location_matches(htf, int(direction)):
            return False
        if mtf.structural_trend not in (0, direction) or mtf.phase != MarketPhase.PULLBACK:
            return False
        if mtf.range_ambiguous or not self._location_matches(mtf, int(direction), pullback=True):
            return False
        return self._has_directional_zone_at_price(mtf, int(direction))

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

    def _fresh_ltf_break(
        self,
        engine: UniversalTimeframeStateEngine,
        timeframe: CanonicalTimeframe,
        state: CanonicalTimeframeState,
        direction: Optional[int],
    ) -> bool:
        if direction not in (-1, 1) or state.structural_trend != direction:
            return False
        if state.phase != MarketPhase.CONTINUATION:
            return False
        if not state.breaks or state.breaks[-1].timestamp != state.timestamp:
            return False
        if state.breaks[-1].direction != direction:
            return False
        previous_states = engine.get_history(timeframe)
        previous = previous_states[-2] if len(previous_states) >= 2 else None
        return previous is not None and previous.timestamp < state.timestamp

    @staticmethod
    def _zone_direction(zone_id: str) -> int:
        parts = zone_id.split(":")
        if parts[0] == "FVG" and len(parts) >= 3:
            label = parts[-1].upper()
            return 1 if label == "BULLISH" else -1 if label == "BEARISH" else 0
        if parts[0] == "OB" and len(parts) >= 4:
            try:
                return int(parts[-1])
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _zone_rows(state: CanonicalTimeframeState) -> list[tuple[str, MarketZone, int]]:
        return [
            (zone_id, zone, InstitutionalFractalStrategy._zone_direction(zone_id))
            for zone_id, zone in zip(state.zone_ids, state.zones)
        ]

    def _has_directional_zone_at_price(
        self,
        state: CanonicalTimeframeState,
        direction: int,
    ) -> bool:
        return any(
            zone_direction == direction
            and zone.zone_type in (ZoneType.FVG, ZoneType.ORDER_BLOCK)
            and zone.price_low <= state.current_price <= zone.price_high
            for _zone_id, zone, zone_direction in self._zone_rows(state)
        )

    def _nearest_retest_zone(
        self,
        state: CanonicalTimeframeState,
        direction: int,
        current_price: Decimal,
    ) -> Optional[tuple[str, MarketZone]]:
        pair = CurrencyPair.from_symbol(state.symbol)
        candidates: list[tuple[Decimal, str, MarketZone]] = []
        for zone_id, zone, zone_direction in self._zone_rows(state):
            if zone_direction != direction or zone.zone_type not in (ZoneType.FVG, ZoneType.ORDER_BLOCK):
                continue
            if direction > 0 and zone.price_low > current_price:
                continue
            if direction < 0 and zone.price_high < current_price:
                continue
            distance = max(
                Decimal("0"), zone.price_low - current_price,
                current_price - zone.price_high,
            )
            if pair.to_pips(distance) > self.maximum_retest_distance_pips:
                continue
            candidates.append((distance, zone_id, zone))
        if not candidates:
            return None
        _distance, zone_id, zone = min(candidates, key=lambda item: item[0])
        return zone_id, zone

    @staticmethod
    def _find_zone(state: CanonicalTimeframeState, zone_id: str) -> Optional[MarketZone]:
        return next((zone for name, zone in zip(state.zone_ids, state.zones) if name == zone_id), None)

    def _build_entry(
        self,
        event: BarEvent,
        set_key: str,
        movement_id: str,
        direction: int,
        entry_zone: MarketZone,
        ltf: CanonicalTimeframeState,
        mtf: CanonicalTimeframeState,
        htf: CanonicalTimeframeState,
    ) -> Optional[tuple[OrderIntent, Decimal, Decimal]]:
        pair = CurrencyPair.from_symbol(event.symbol)
        entry = ltf.current_price
        stop = self._structural_stop(pair, direction, entry_zone, ltf)
        target = self._structural_target(direction, entry, mtf, htf)
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
        total_cost_pips = max(Decimal("0"), event.spread) + commission_pips + self.slippage_buffer_pips
        net_rr = (target_pips - total_cost_pips) / (stop_pips + total_cost_pips)
        if net_rr < self.minimum_net_reward_risk:
            self.last_decision[set_key] = "NET_REWARD_RISK_FILTER"
            return None

        lot_size = self.size_for_risk(
            equity=self.account_equity,
            risk_fraction=self.risk_fraction,
            stop_pips=stop_pips,
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
            client_tag=f"FRACTAL_{set_key}_{movement_id.rsplit('|', 1)[-1]}",
        )
        return intent, net_rr, stop_pips

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
        entry_zone: MarketZone,
        state: CanonicalTimeframeState,
    ) -> Optional[Decimal]:
        if direction > 0:
            levels = [
                item.price for item in state.key_levels
                if item.swing_type == "low" and item.price < entry_zone.price_low
            ]
            anchor = max(levels) if levels else (
                state.structural_range.low
                if state.structural_range and state.structural_range.low < entry_zone.price_low
                else entry_zone.price_low
            )
            return anchor - pair.to_price(self.stop_buffer_pips)
        levels = [
            item.price for item in state.key_levels
            if item.swing_type == "high" and item.price > entry_zone.price_high
        ]
        anchor = min(levels) if levels else (
            state.structural_range.high
            if state.structural_range and state.structural_range.high > entry_zone.price_high
            else entry_zone.price_high
        )
        return anchor + pair.to_price(self.stop_buffer_pips)

    @staticmethod
    def _structural_target(
        direction: int,
        entry: Decimal,
        mtf: CanonicalTimeframeState,
        htf: CanonicalTimeframeState,
    ) -> Optional[Decimal]:
        candidates: list[Decimal] = []
        wanted_swing = "high" if direction > 0 else "low"
        for state in (htf, mtf):
            for level in state.key_levels:
                if level.swing_type != wanted_swing:
                    continue
                if (direction > 0 and level.price > entry) or (direction < 0 and level.price < entry):
                    candidates.append(level.price)
            structural = state.structural_range
            if structural is not None:
                boundary = structural.high if direction > 0 else structural.low
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


__all__ = ["ArmedRetest", "InstitutionalFractalStrategy"]
