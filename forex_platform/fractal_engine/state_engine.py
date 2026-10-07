"""Causal, shared state snapshots for the canonical seven-timeframe ladder.

Input bars are stamped with their opening time. A target candle is published
only after its exclusive close boundary. A pivot is not visible until all of
its right-side confirmation bars have closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import heapq
from typing import Any, Optional

import polars as pl

from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_model.contracts import (
    BreakType,
    MarketPhase,
    MarketZone,
    StructureBreak,
    SwingPoint,
    SwingScope,
    SwingType,
    ZoneScope,
    ZoneType,
)
from forex_platform.market_model.structure.swings import detect_swings
from forex_platform.market_model.zones.imbalances import (
    FVGDirection,
    detect_fair_value_gaps,
    fvgs_to_zones,
)
from forex_platform.market_model.zones.blocks import detect_order_blocks, order_blocks_to_zones
from forex_platform.market_model.zones.liquidity import (
    LiquidityKind,
    detect_liquidity_pools,
    detect_liquidity_sweeps,
    liquidity_pools_to_zones,
)
from forex_platform.fractal_engine.timeframes import (
    CANONICAL_LADDER,
    CanonicalTimeframe,
    parse_source_interval,
    target_is_available,
    timeframe_bucket_end,
    timeframe_bucket_start,
)


class LocationState:
    PREMIUM = "PREMIUM"
    EQUILIBRIUM = "EQUILIBRIUM"
    DISCOUNT = "DISCOUNT"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class StructuralRange:
    low: Decimal
    high: Decimal
    low_swing_index: int
    high_swing_index: int
    low_timestamp: datetime
    high_timestamp: datetime
    selection_rule: str = "most_recent_confirmed_opposing_external_swing_pair"

    @property
    def equilibrium(self) -> Decimal:
        return (self.high + self.low) / Decimal("2")


@dataclass(frozen=True)
class KeyLevel:
    level_id: str
    swing_type: str
    price: Decimal
    pivot_timestamp: datetime
    available_at: datetime
    weak: bool = False
    protected: bool = False


@dataclass(frozen=True)
class CanonicalTimeframeState:
    """Immutable observation for one symbol, timeframe, and close timestamp."""

    state_id: str
    symbol: str
    timeframe: str
    timestamp: datetime
    bar_index: int
    current_price: Decimal
    structural_trend: Optional[int]
    swing_state: str
    phase: MarketPhase
    location: str
    range_location: Optional[Decimal]
    structural_range: Optional[StructuralRange]
    range_ambiguous: bool
    range_candidate_count: int
    swings: tuple[SwingPoint, ...]
    breaks: tuple[StructureBreak, ...]
    key_levels: tuple[KeyLevel, ...]
    zones: tuple[MarketZone, ...]
    state_valid: bool
    validity_reason: str
    invalidation_condition: Optional[str]
    state_signature: str
    zone_ids: tuple[str, ...]
    transition: tuple[str, ...]
    duration_bars: int
    session_regime: str
    active_sessions: tuple[str, ...]

    @property
    def trend_label(self) -> str:
        if self.structural_trend in (None, 0):
            return "NEUTRAL"
        return "BULLISH" if self.structural_trend > 0 else "BEARISH"

    @property
    def latest_break_type(self) -> Optional[str]:
        return self.breaks[-1].break_type.value if self.breaks else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _empty_ohlcv_schema(*, include_count: bool = False) -> dict[str, pl.DataType]:
    schema: dict[str, pl.DataType] = {
        "timestamp": pl.Datetime("us", "UTC"),
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
        "close": pl.Float64, "volume": pl.Float64, "spread": pl.Float64,
    }
    if include_count:
        schema["source_bar_count"] = pl.Int64
    return schema


def aggregate_bars(
    bars: pl.DataFrame,
    target: CanonicalTimeframe | str,
    source_interval: Timeframe | timedelta | str,
) -> pl.DataFrame:
    """Aggregate open-stamped OHLCV bars into fully closed target candles."""
    target = CanonicalTimeframe(target)
    interval = parse_source_interval(source_interval)
    if bars.is_empty() or not target_is_available(target, interval):
        return pl.DataFrame(schema=_empty_ohlcv_schema(include_count=True))
    normalized = MarketDataLoader.normalize_and_validate(
        bars, expected_interval=interval, enforce_monotonicity=True
    ).sort("timestamp")
    groups: list[tuple[datetime, list[dict[str, Any]]]] = []
    active_start: Optional[datetime] = None
    active_rows: list[dict[str, Any]] = []
    for row in normalized.iter_rows(named=True):
        start = timeframe_bucket_start(_as_utc(row["timestamp"]), target)
        if active_start is not None and start != active_start:
            groups.append((active_start, active_rows))
            active_rows = []
        active_start = start
        active_rows.append(row)
    if active_start is not None:
        groups.append((active_start, active_rows))

    last_row = normalized.row(-1, named=True)
    known_through = _as_utc(last_row["timestamp"]) + interval
    output: list[dict[str, Any]] = []
    for start, values in groups:
        close_time = timeframe_bucket_end(start, target)
        if close_time > known_through:
            continue
        output.append({
            "timestamp": close_time,
            "open": values[0]["open"],
            "high": max(v["high"] for v in values),
            "low": min(v["low"] for v in values),
            "close": values[-1]["close"],
            "volume": sum(float(v["volume"] or 0.0) for v in values),
            "spread": sum(float(v["spread"] or 0.0) for v in values) / len(values),
            "source_bar_count": len(values),
        })
    if not output:
        return pl.DataFrame(schema=_empty_ohlcv_schema(include_count=True))
    return pl.DataFrame(output).with_columns(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC"))
    ).sort("timestamp")


class _OnlineTimeframeProcessor:
    """Streaming implementation of canonical confirmed-swing and break rules."""

    def __init__(self, symbol: str, timeframe: CanonicalTimeframe, lookback: int):
        if lookback < 1:
            raise ValueError("lookback must be at least one bar")
        self.symbol = symbol
        self.timeframe = timeframe
        self.lookback = lookback
        self._pip_size = CurrencyPair.from_symbol(symbol).pip_size
        self.bars: list[dict[str, Any]] = []
        self.swings: list[SwingPoint] = []
        self._highs: list[SwingPoint] = []
        self._lows: list[SwingPoint] = []
        self._pending_highs: list[tuple[Decimal, int, SwingPoint]] = []
        self._pending_lows: list[tuple[Decimal, int, SwingPoint]] = []
        self.breaks: list[StructureBreak] = []
        self._broken_swing_indices: set[int] = set()
        self._weak_indices: set[int] = set()
        self._protected_indices: set[int] = set()
        self._ranges: list[StructuralRange] = []
        self._active_zones: dict[str, tuple[MarketZone, int, str]] = {}
        self._liquidity_pool_zones: dict[str, tuple[MarketZone, int, str]] = {}
        self._seen_pool_ids: set[str] = set()
        self._last_zone_ids: tuple[str, ...] = ()
        self.states: list[CanonicalTimeframeState] = []
        self._previous_signature: Optional[str] = None
        self._duration = 0

    def push(self, bar: dict[str, Any]) -> CanonicalTimeframeState:
        close_time = _as_utc(bar["timestamp"])
        if self.bars and close_time <= self.bars[-1]["timestamp"]:
            raise ValueError(f"non-increasing {self.timeframe.value} close timestamps")
        row = dict(bar)
        row["timestamp"] = close_time
        self.bars.append(row)
        index = len(self.bars) - 1
        newly_confirmed_swing: Optional[SwingPoint] = None

        # The existing strict pivot detector runs on a rolling 2L+1 window.
        # The middle candle becomes observable on this rightmost close only.
        width = 2 * self.lookback + 1
        if len(self.bars) >= width:
            window = pl.DataFrame(self.bars[-width:]).with_columns(
                pl.col("timestamp").cast(pl.Datetime("us", "UTC"))
            )
            detected = detect_swings(window, lookback=self.lookback, scope=SwingScope.EXTERNAL)
            if detected:
                offset = index - width + 1
                swing = detected[-1].model_copy(update={"index": offset + detected[-1].index})
                if not self.swings or swing.index > self.swings[-1].index:
                    self.swings.append(swing)
                    newly_confirmed_swing = swing
                    if swing.swing_type == SwingType.HIGH:
                        self._highs.append(swing)
                        heapq.heappush(self._pending_highs, (swing.price, swing.index, swing))
                    else:
                        self._lows.append(swing)
                        heapq.heappush(self._pending_lows, (-swing.price, swing.index, swing))
                    self._record_range(swing)

        close = Decimal(str(row["close"]))
        crossed: list[tuple[SwingPoint, int]] = []
        while self._pending_highs and self._pending_highs[0][0] < close:
            _price, swing_index, swing = heapq.heappop(self._pending_highs)
            if swing.index < index and swing.index not in self._broken_swing_indices:
                crossed.append((swing, 1))
        while self._pending_lows and -self._pending_lows[0][0] > close:
            _neg_price, swing_index, swing = heapq.heappop(self._pending_lows)
            if swing.index < index and swing.index not in self._broken_swing_indices:
                crossed.append((swing, -1))
        crossed.sort(key=lambda item: item[0].index)
        new_breaks: list[StructureBreak] = []
        for swing, direction in crossed:
            trend_before = self._trend()
            kind = BreakType.BOS if trend_before in (None, 0, direction) else BreakType.CHOCH
            structure_break = StructureBreak(
                index=index, timestamp=close_time, break_type=kind,
                swing_index=swing.index, swing_price=swing.price,
                direction=direction, body_close=close,
            )
            self.breaks.append(structure_break)
            new_breaks.append(structure_break)
            self._broken_swing_indices.add(swing.index)
            if kind == BreakType.BOS:
                self._weak_indices.add(swing.index)
                origin_type = SwingType.LOW if direction > 0 else SwingType.HIGH
                origin = next((item for item in reversed(self.swings)
                               if item.index < index and item.swing_type == origin_type), None)
                if origin is not None:
                    self._protected_indices.add(origin.index)

        self._update_zones(index, close_time, newly_confirmed_swing, new_breaks)
        trend = self._trend()
        phase = self._phase(trend, close)
        swing_state = self._swing_state()
        ranges = list(reversed(self._ranges[-3:]))
        containing = [item for item in ranges if item.low <= close <= item.high]
        ambiguous = len({(item.low, item.high) for item in containing}) > 1
        selected_range = containing[0] if len(containing) == 1 else (ranges[0] if ranges else None)
        range_location: Optional[Decimal] = None
        location = LocationState.UNKNOWN
        if selected_range is not None and selected_range.high > selected_range.low:
            ratio = (close - selected_range.low) / (selected_range.high - selected_range.low)
            if ambiguous:
                location = LocationState.AMBIGUOUS
            else:
                range_location = ratio
                location = (LocationState.PREMIUM if ratio > Decimal("0.5")
                            else LocationState.DISCOUNT if ratio < Decimal("0.5")
                            else LocationState.EQUILIBRIUM)

        key_levels = self._key_levels(close_time)
        zones = tuple(item[0] for _, item in sorted(self._active_zones.items()))
        zone_ids = tuple(sorted(self._active_zones))
        signature = "|".join((
            str(trend if trend is not None else 0), phase.value, location,
            f"{selected_range.low}:{selected_range.high}" if selected_range else "NO_RANGE",
            ",".join(zone_ids),
        ))
        transition = self._transition(signature, trend, phase, location, zone_ids)
        session = ForexSessionEngine.get_session_state(close_time)
        state = CanonicalTimeframeState(
            state_id=f"{self.symbol}|{self.timeframe.value}|{close_time.isoformat()}",
            symbol=self.symbol,
            timeframe=self.timeframe.value,
            timestamp=close_time,
            bar_index=index,
            current_price=close,
            structural_trend=trend,
            swing_state=swing_state,
            phase=phase,
            location=location,
            range_location=range_location,
            structural_range=selected_range,
            range_ambiguous=ambiguous,
            range_candidate_count=len(ranges),
            swings=tuple(self.swings[-24:]),
            breaks=tuple(self.breaks[-24:]),
            key_levels=key_levels,
            zones=zones,
            state_valid=True,
            validity_reason="closed candle; confirmed swings only",
            invalidation_condition=self._invalidation(trend),
            state_signature=signature,
            zone_ids=zone_ids,
            transition=transition,
            duration_bars=self._duration,
            session_regime=session.regime_tag,
            active_sessions=tuple(item.value for item in session.active_sessions),
        )
        self.states.append(state)
        return state

    def _update_zones(
        self,
        index: int,
        available_at: datetime,
        new_swing: Optional[SwingPoint],
        new_breaks: list[StructureBreak],
    ) -> None:
        if len(self.bars) >= 3:
            recent = pl.DataFrame(self.bars[-3:]).with_columns(
                pl.col("timestamp").cast(pl.Datetime("us", "UTC"))
            )
            gaps = detect_fair_value_gaps(recent)
            if gaps:
                gap = gaps[-1]
                offset = index - 2
                local = fvgs_to_zones((gap,))[0]
                zone = local.model_copy(update={
                    "index_start": offset + local.index_start,
                    "index_end": offset + local.index_end,
                    "timestamp_start": available_at,
                    "timestamp_end": available_at,
                })
                direction = 1 if gap.direction == FVGDirection.BULLISH else -1
                zone_id = f"FVG:{index}:{gap.direction.value}"
                self._active_zones[zone_id] = (zone, direction, "FVG")

        if new_breaks:
            history = pl.DataFrame(self.bars).with_columns(
                pl.col("timestamp").cast(pl.Datetime("us", "UTC"))
            )
            for structure_break in new_breaks:
                blocks = detect_order_blocks(history, (structure_break,), self.swings)
                for block in blocks:
                    zone = order_blocks_to_zones((block,))[0].model_copy(update={
                        "timestamp_start": available_at,
                        "timestamp_end": available_at,
                    })
                    zone_id = f"OB:{structure_break.index}:{block.index}:{structure_break.direction}"
                    self._active_zones[zone_id] = (zone, structure_break.direction, "OB")

        if new_swing is not None:
            pools = detect_liquidity_pools(self.swings, tolerance=self._pip_size)
            for pool in pools:
                pool_id = f"{pool.kind.value}:{','.join(map(str, pool.swing_indexes))}"
                if pool_id in self._seen_pool_ids:
                    continue
                self._seen_pool_ids.add(pool_id)
                zone = liquidity_pools_to_zones((pool,))[0].model_copy(update={
                    "timestamp_start": available_at,
                    "timestamp_end": available_at,
                })
                direction = 0
                self._liquidity_pool_zones[pool_id] = (zone, direction, pool.kind.value)

        # A close through the far boundary invalidates an FVG/OB. Liquidity
        # pools are retired after a wick sweep or a close through the level.
        close = Decimal(str(self.bars[-1]["close"]))
        high = Decimal(str(self.bars[-1]["high"]))
        low = Decimal(str(self.bars[-1]["low"]))
        for zone_id, (zone, direction, _kind) in list(self._active_zones.items()):
            invalid = (direction > 0 and close < zone.price_low) or (
                direction < 0 and close > zone.price_high
            )
            if invalid:
                del self._active_zones[zone_id]

        if self._liquidity_pool_zones:
            pools_for_sweeps = []
            pool_id_by_signature: dict[tuple[str, Decimal], str] = {}
            for zone_id, (zone, _direction, kind) in self._liquidity_pool_zones.items():
                if zone.timestamp_end >= available_at:
                    continue
                liquidity_kind = LiquidityKind.EQH if kind == LiquidityKind.EQH.value else LiquidityKind.EQL
                from forex_platform.market_model.zones.liquidity import LiquidityPool
                pool = LiquidityPool(
                    kind=liquidity_kind,
                    price=zone.price_high,
                    tolerance=self._pip_size,
                    swing_indexes=[zone.index_start, zone.index_end],
                    timestamp=zone.timestamp_end,
                    scope=ZoneScope.EXTERNAL,
                )
                pools_for_sweeps.append(pool)
                pool_id_by_signature[(kind, zone.price_high)] = zone_id
            current_bar = pl.DataFrame([self.bars[-1]]).with_columns(
                pl.col("timestamp").cast(pl.Datetime("us", "UTC"))
            )
            swept = detect_liquidity_sweeps(current_bar, pools_for_sweeps)
            for event in swept:
                kind = LiquidityKind.EQH.value if event.direction < 0 else LiquidityKind.EQL.value
                self._liquidity_pool_zones.pop(pool_id_by_signature.get((kind, event.swept_price), ""), None)
            for zone_id, (zone, direction, kind) in list(self._liquidity_pool_zones.items()):
                if (kind == LiquidityKind.EQH.value and close > zone.price_high) or (
                    kind == LiquidityKind.EQL.value and close < zone.price_low
                ):
                    del self._liquidity_pool_zones[zone_id]

        # Include recognized, still-live liquidity zones with FVG/OB zones.
        self._active_zones.update(self._liquidity_pool_zones)
        for zone_id in tuple(self._active_zones):
            if zone_id.startswith(("EQH:", "EQL:")) and zone_id not in self._liquidity_pool_zones:
                self._active_zones.pop(zone_id, None)

    def _record_range(self, new_swing: SwingPoint) -> None:
        if len(self.swings) < 2:
            return
        previous = self.swings[-2]
        if previous.swing_type == new_swing.swing_type:
            return
        high = previous if previous.swing_type == SwingType.HIGH else new_swing
        low = previous if previous.swing_type == SwingType.LOW else new_swing
        if high.price > low.price:
            self._ranges.append(StructuralRange(
                low=low.price, high=high.price,
                low_swing_index=low.index, high_swing_index=high.index,
                low_timestamp=low.timestamp, high_timestamp=high.timestamp,
            ))

    def _trend(self) -> Optional[int]:
        if self.breaks:
            return self.breaks[-1].direction
        if len(self._highs) >= 2 and len(self._lows) >= 2:
            if self._highs[-1].price > self._highs[-2].price and self._lows[-1].price > self._lows[-2].price:
                return 1
            if self._highs[-1].price < self._highs[-2].price and self._lows[-1].price < self._lows[-2].price:
                return -1
        return 0

    def _phase(self, trend: Optional[int], close: Decimal) -> MarketPhase:
        # Same rule as MarketPhaseClassifier, evaluated only with confirmed data.
        if trend in (None, 0):
            return MarketPhase.PULLBACK
        references = self._highs if trend > 0 else self._lows
        if not references:
            return MarketPhase.PULLBACK
        reference = references[-1]
        if trend > 0:
            return MarketPhase.CONTINUATION if close >= reference.price else MarketPhase.PULLBACK
        return MarketPhase.CONTINUATION if close <= reference.price else MarketPhase.PULLBACK

    def _swing_state(self) -> str:
        if len(self._highs) < 2 or len(self._lows) < 2:
            return "INSUFFICIENT_SWINGS"
        hh = self._highs[-1].price > self._highs[-2].price
        hl = self._lows[-1].price > self._lows[-2].price
        lh = self._highs[-1].price < self._highs[-2].price
        ll = self._lows[-1].price < self._lows[-2].price
        if hh and hl:
            return "HH_HL"
        if lh and ll:
            return "LH_LL"
        return "MIXED"

    def _key_levels(self, available_at: datetime) -> tuple[KeyLevel, ...]:
        return tuple(KeyLevel(
            level_id=f"{self.timeframe.value}:{swing.index}:{swing.swing_type.value}",
            swing_type=swing.swing_type.value,
            price=swing.price,
            pivot_timestamp=swing.timestamp,
            available_at=available_at,
            weak=swing.index in self._weak_indices,
            protected=swing.index in self._protected_indices,
        ) for swing in self.swings[-8:])

    def _invalidation(self, trend: Optional[int]) -> Optional[str]:
        if trend in (None, 0):
            return None
        swing_type = SwingType.LOW if trend > 0 else SwingType.HIGH
        anchors = [s for s in self.swings
                   if s.index in self._protected_indices and s.swing_type == swing_type]
        if not anchors:
            anchors = self._lows if trend > 0 else self._highs
        if not anchors:
            return None
        anchor = anchors[-1]
        relation = "below" if trend > 0 else "above"
        return f"closed price {relation} {anchor.price} ({anchor.swing_type.value} at {anchor.timestamp.isoformat()})"

    def _transition(self, signature: str, trend: Optional[int],
                    phase: MarketPhase, location: str,
                    zone_ids: tuple[str, ...]) -> tuple[str, ...]:
        if self._previous_signature is None:
            self._duration = 1
            labels = ("STATE_START",)
        elif self._previous_signature == signature:
            self._duration += 1
            labels = ("PERSISTENCE",)
        else:
            self._duration = 1
            old = self._previous_signature.split("|")
            labels_list: list[str] = []
            if old[0] != str(trend if trend is not None else 0):
                labels_list.append("STRUCTURAL_TRANSITION")
            if old[1] != phase.value:
                labels_list.append("PHASE_TRANSITION")
            if old[2] != location:
                labels_list.append("LOCATION_MIGRATION")
            if old[4] != ",".join(zone_ids):
                labels_list.append("ZONE_MIGRATION")
            if not labels_list:
                labels_list.append("RANGE_TRANSITION")
            labels = tuple(labels_list)
        self._previous_signature = signature
        return labels


class UniversalTimeframeStateEngine:
    """Maintain one canonical state history per timeframe, shared by all sets."""

    def __init__(
        self,
        symbol: str,
        base_timeframe: Timeframe | timedelta | str = Timeframe.M1,
        max_history_bars: int = 100_000,
        swing_lookback: int = 5,
    ) -> None:
        self.symbol = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        self.base_timeframe = base_timeframe
        self.source_interval = parse_source_interval(base_timeframe)
        self.max_history_bars = max_history_bars
        self.swing_lookback = swing_lookback
        self._processors: dict[CanonicalTimeframe, _OnlineTimeframeProcessor] = {}
        self._groups: dict[CanonicalTimeframe, tuple[datetime, list[dict[str, Any]]]] = {}
        self._states: dict[str, CanonicalTimeframeState] = {}
        self._history: dict[str, list[CanonicalTimeframeState]] = {}
        self._last_source_timestamp: Optional[datetime] = None
        self._reset_processors()

    def _reset_processors(self) -> None:
        self._processors = {
            tf: _OnlineTimeframeProcessor(self.symbol, tf, self.swing_lookback)
            for tf in CANONICAL_LADDER if target_is_available(tf, self.source_interval)
        }

    @property
    def available_timeframes(self) -> tuple[str, ...]:
        return tuple(tf.value for tf in self._processors)

    def update(self, bar: pl.DataFrame | dict[str, Any]) -> dict[str, CanonicalTimeframeState]:
        """Consume one completed source bar; partial higher bars are not exposed."""
        if isinstance(bar, pl.DataFrame):
            if bar.is_empty() or bar.height != 1:
                raise ValueError("bar must contain exactly one row")
            row = bar.row(0, named=True)
        else:
            row = dict(bar)
        required = {"timestamp", "open", "high", "low", "close"}
        missing = required - row.keys()
        if missing:
            raise ValueError(f"bar missing required fields: {sorted(missing)}")
        row.setdefault("volume", 0.0)
        row.setdefault("spread", 1.0)
        row["timestamp"] = _as_utc(row["timestamp"])
        if self._last_source_timestamp is not None and row["timestamp"] <= self._last_source_timestamp:
            raise ValueError("source bars must arrive in strictly increasing timestamp order")
        self._last_source_timestamp = row["timestamp"]
        newly_closed: dict[str, CanonicalTimeframeState] = {}
        for timeframe, processor in self._processors.items():
            start = timeframe_bucket_start(row["timestamp"], timeframe)
            current = self._groups.get(timeframe)
            if current is not None and current[0] != start:
                old_start, old_rows = current
                close_time = timeframe_bucket_end(old_start, timeframe)
                state = processor.push(self._aggregate_group(old_rows, close_time))
                self._publish(timeframe.value, state, newly_closed)
                current = None
            if current is None:
                current = (start, [])
            current[1].append(row)
            self._groups[timeframe] = current
            close_time = timeframe_bucket_end(start, timeframe)
            if row["timestamp"] + self.source_interval == close_time:
                state = processor.push(self._aggregate_group(current[1], close_time))
                self._publish(timeframe.value, state, newly_closed)
                del self._groups[timeframe]
        return newly_closed

    def _publish(self, name: str, state: CanonicalTimeframeState,
                 newly_closed: dict[str, CanonicalTimeframeState]) -> None:
        self._states[name] = state
        history = self._history.setdefault(name, [])
        history.append(state)
        if len(history) > self.max_history_bars:
            del history[:-self.max_history_bars]
        newly_closed[name] = state

    @staticmethod
    def _aggregate_group(rows: list[dict[str, Any]], close_time: datetime) -> dict[str, Any]:
        return {
            "timestamp": close_time,
            "open": rows[0]["open"],
            "high": max(item["high"] for item in rows),
            "low": min(item["low"] for item in rows),
            "close": rows[-1]["close"],
            "volume": sum(float(item.get("volume", 0.0) or 0.0) for item in rows),
            "spread": sum(float(item.get("spread", 1.0) or 0.0) for item in rows) / len(rows),
            "source_bar_count": len(rows),
        }

    def build(self, bars: pl.DataFrame) -> dict[str, list[CanonicalTimeframeState]]:
        """Build closed-candle histories in one causal pass for each scale."""
        if bars.is_empty():
            self._states.clear()
            self._history.clear()
            return {}
        self._reset_processors()
        self._states = {}
        self._history = {}
        source = MarketDataLoader.normalize_and_validate(
            bars, expected_interval=self.source_interval, enforce_monotonicity=True
        ).sort("timestamp")
        histories: dict[str, list[CanonicalTimeframeState]] = {}
        for timeframe, processor in self._processors.items():
            aggregated = aggregate_bars(source, timeframe, self.source_interval)
            snapshots = [processor.push(item) for item in aggregated.iter_rows(named=True)]
            histories[timeframe.value] = snapshots
        self._history = histories
        self._states = {name: values[-1] for name, values in histories.items() if values}
        return {name: list(values) for name, values in histories.items()}

    def get_state(self, timeframe: CanonicalTimeframe | str) -> Optional[CanonicalTimeframeState]:
        return self._states.get(CanonicalTimeframe(timeframe).value)

    def get_history(self, timeframe: CanonicalTimeframe | str) -> tuple[CanonicalTimeframeState, ...]:
        return tuple(self._history.get(CanonicalTimeframe(timeframe).value, ()))
