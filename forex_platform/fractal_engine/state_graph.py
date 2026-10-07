"""Shared set-role views, temporal transition graph, and movement identity."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

from forex_platform.fractal_engine.state_engine import CanonicalTimeframeState
from forex_platform.fractal_engine.timeframes import CANONICAL_LADDER, TIMEFRAME_SETS, CanonicalTimeframe, normalized_set_key


@dataclass(frozen=True)
class SetStateView:
    set_key: str
    timestamp: datetime
    htf: Optional[CanonicalTimeframeState]
    mtf: Optional[CanonicalTimeframeState]
    ltf: Optional[CanonicalTimeframeState]
    movement_id: Optional[str] = None

    @property
    def states(self) -> tuple[Optional[CanonicalTimeframeState], ...]:
        return (self.htf, self.mtf, self.ltf)


@dataclass(frozen=True)
class StateTransitionEdge:
    source_state_id: str
    target_state_id: str
    source_timeframe: str
    target_timeframe: str
    observed_at: datetime
    transition: tuple[str, ...]
    scale_distance: int
    edge_kind: str
    movement_id: Optional[str] = None


@dataclass(frozen=True)
class MovementEpisode:
    movement_id: str
    symbol: str
    identity_timeframe: str
    direction: int
    started_at: datetime
    ended_at: Optional[datetime]


class CausalMovementLedger:
    """Assign one identity to each confirmed structural leg on the finest feed.

    The identity is an event-deduplication key, not a claim that every bar in a
    trend regime is a separate tradable move. Direction changes are confirmed
    by the state engine's closed-bar structural trend.
    """

    def __init__(self, states: Sequence[CanonicalTimeframeState]):
        if not states:
            self.identity_timeframe = ""
            self.episodes: tuple[MovementEpisode, ...] = ()
            self._state_to_movement: dict[str, str] = {}
            self._times: list[datetime] = []
            self._movement_by_time: list[Optional[str]] = []
            return
        timeframe = states[0].timeframe
        if any(state.timeframe != timeframe for state in states):
            raise ValueError("movement ledger requires states from one timeframe")
        ordered = sorted(states, key=lambda state: state.timestamp)
        self.identity_timeframe = timeframe
        self._state_to_movement: dict[str, str] = {}
        self._times: list[datetime] = []
        self._movement_by_time: list[Optional[str]] = []
        episodes: list[MovementEpisode] = []
        current_direction = 0
        current_id: Optional[str] = None
        episode_start: Optional[datetime] = None
        sequence = 0
        symbol = ordered[0].symbol
        for state in ordered:
            direction = state.structural_trend or 0
            if direction != 0 and direction != current_direction:
                if current_id is not None and episode_start is not None:
                    episodes.append(MovementEpisode(
                        current_id, symbol, timeframe, current_direction,
                        episode_start, state.timestamp,
                    ))
                sequence += 1
                current_id = f"{symbol}|{timeframe}|LEG-{sequence:06d}"
                episode_start = state.timestamp
                current_direction = direction
            movement_id = current_id if direction != 0 else None
            if movement_id is not None:
                self._state_to_movement[state.state_id] = movement_id
            self._times.append(state.timestamp)
            self._movement_by_time.append(movement_id)
        if current_id is not None and episode_start is not None:
            episodes.append(MovementEpisode(
                current_id, symbol, timeframe, current_direction, episode_start, None
            ))
        self.episodes = tuple(episodes)

    def at(self, timestamp: datetime) -> Optional[str]:
        index = bisect_right(self._times, timestamp) - 1
        return self._movement_by_time[index] if index >= 0 else None

    def for_state(self, state: Optional[CanonicalTimeframeState]) -> Optional[str]:
        if state is None:
            return None
        return self._state_to_movement.get(state.state_id)


def _state_asof(
    histories: dict[str, Sequence[CanonicalTimeframeState]],
    timeframe: CanonicalTimeframe,
    timestamps: dict[str, list[datetime]],
    timestamp: datetime,
) -> Optional[CanonicalTimeframeState]:
    key = timeframe.value
    states = histories.get(key, ())
    times = timestamps.get(key, ())
    index = bisect_right(times, timestamp) - 1
    return states[index] if index >= 0 else None


def build_set_views(
    histories: dict[str, Sequence[CanonicalTimeframeState]],
    set_key: str,
    *,
    timestamps: Optional[Iterable[datetime]] = None,
    movement_ledger: Optional[CausalMovementLedger] = None,
) -> list[SetStateView]:
    """Create role views from the exact same immutable canonical state objects."""
    key = normalized_set_key(set_key)
    normalized_histories = {
        CanonicalTimeframe(name).value: tuple(sorted(states, key=lambda state: state.timestamp))
        for name, states in histories.items()
    }
    state_times = {
        name: [state.timestamp for state in states]
        for name, states in normalized_histories.items()
    }
    if timestamps is None:
        eval_times = sorted({state.timestamp for values in normalized_histories.values() for state in values})
    else:
        eval_times = sorted(set(timestamps))
    htf_tf, mtf_tf, ltf_tf = TIMEFRAME_SETS[key]
    views = []
    for timestamp in eval_times:
        htf = _state_asof(normalized_histories, htf_tf, state_times, timestamp)
        mtf = _state_asof(normalized_histories, mtf_tf, state_times, timestamp)
        ltf = _state_asof(normalized_histories, ltf_tf, state_times, timestamp)
        movement_id = movement_ledger.at(timestamp) if movement_ledger else None
        views.append(SetStateView(key, timestamp, htf, mtf, ltf, movement_id))
    return views


class FractalStateGraph:
    """Directed graph of temporal transitions and causal scale observations."""

    def __init__(
        self,
        histories: dict[str, Sequence[CanonicalTimeframeState]],
        movement_ledger: Optional[CausalMovementLedger] = None,
    ) -> None:
        self.histories = {
            CanonicalTimeframe(name).value: tuple(sorted(states, key=lambda state: state.timestamp))
            for name, states in histories.items()
        }
        self.movement_ledger = movement_ledger
        self.nodes: dict[str, CanonicalTimeframeState] = {
            state.state_id: state
            for values in self.histories.values()
            for state in values
        }
        self.edges: list[StateTransitionEdge] = []
        self._build_temporal_edges()
        self._build_scale_edges()

    def _build_temporal_edges(self) -> None:
        for timeframe, states in self.histories.items():
            for previous, current in zip(states, states[1:]):
                self.edges.append(StateTransitionEdge(
                    source_state_id=previous.state_id,
                    target_state_id=current.state_id,
                    source_timeframe=timeframe,
                    target_timeframe=timeframe,
                    observed_at=current.timestamp,
                    transition=current.transition,
                    scale_distance=0,
                    edge_kind="TEMPORAL_TRANSITION",
                    movement_id=self.movement_ledger.for_state(current) if self.movement_ledger else None,
                ))

    def _build_scale_edges(self) -> None:
        available = [tf for tf in CANONICAL_LADDER if tf.value in self.histories]
        position = {tf.value: i for i, tf in enumerate(CANONICAL_LADDER)}
        times = {key: [state.timestamp for state in values] for key, values in self.histories.items()}
        for parent_tf, child_tf in zip(available, available[1:]):
            parents = self.histories[parent_tf.value]
            parent_times = times[parent_tf.value]
            distance = position[child_tf.value] - position[parent_tf.value]
            for child in self.histories[child_tf.value]:
                parent_index_asof = bisect_right(parent_times, child.timestamp) - 1
                if parent_index_asof < 0:
                    continue
                parent = parents[parent_index_asof]
                self.edges.append(StateTransitionEdge(
                    source_state_id=parent.state_id,
                    target_state_id=child.state_id,
                    source_timeframe=parent_tf.value,
                    target_timeframe=child_tf.value,
                    observed_at=child.timestamp,
                    transition=child.transition,
                    scale_distance=distance,
                    edge_kind="SCALE_OBSERVATION",
                    movement_id=self.movement_ledger.at(child.timestamp) if self.movement_ledger else None,
                ))

    @property
    def temporal_edge_count(self) -> int:
        return sum(edge.edge_kind == "TEMPORAL_TRANSITION" for edge in self.edges)

    @property
    def scale_edge_count(self) -> int:
        return sum(edge.edge_kind == "SCALE_OBSERVATION" for edge in self.edges)

    def references_by_set(
        self,
        set_views: dict[str, Sequence[SetStateView]],
    ) -> dict[str, set[str]]:
        """Return each shared state node and the set views that referenced it."""
        result: dict[str, set[str]] = {}
        for set_key, views in set_views.items():
            key = normalized_set_key(set_key)
            for view in views:
                for state in view.states:
                    if state is not None:
                        result.setdefault(state.state_id, set()).add(key)
        return result

    def movement_representation_counts(
        self,
        set_views: dict[str, Sequence[SetStateView]],
    ) -> dict[str, int]:
        """Count distinct set representations of each same base-leg identity."""
        counts: dict[str, set[str]] = {}
        for set_key, views in set_views.items():
            for view in views:
                if view.movement_id is not None and all(state is not None for state in view.states):
                    counts.setdefault(view.movement_id, set()).add(normalized_set_key(set_key))
        return {movement: len(sets) for movement, sets in counts.items()}
