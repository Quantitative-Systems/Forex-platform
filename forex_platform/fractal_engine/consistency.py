"""Cross-set consistency lifecycle for adjacent canonical timeframe sets.

Adjacent sets share one bridge timeframe (SET_n MTF == SET_{n+1} HTF). Each
set read derives what its own MTF must do next from its HTF trend and
location; pair observations compare the parent set's required bridge shift
with the child set's derived next-layer shift. Verdicts: ALIGNED when both
layers require the same directional shift (or the bridge shift confirmed and
handed off to the child set), CONFLICTED when the child set requires the
opposite shift at its layer, and WAITING when either side lacks a derivable
requirement. Observation only: nothing here emits execution signals.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from forex_platform.fractal_engine.requirements import (
    expected_child_shift,
    expected_phase,
)
from forex_platform.fractal_engine.state_graph import CausalMovementLedger, SetStateView
from forex_platform.fractal_engine.timeframes import TIMEFRAME_SETS, normalized_set_key

STATUS_INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
STATUS_HTF_UNRESOLVED = "HTF_UNRESOLVED"
STATUS_PENDING_MTF_SETUP = "PENDING_MTF_SETUP"
STATUS_MTF_SETUP_FORMED = "MTF_SETUP_FORMED"

VERDICT_ALIGNED = "ALIGNED"
VERDICT_CONFLICTED = "CONFLICTED"
VERDICT_WAITING = "WAITING"

ADJACENT_SET_PAIRS: tuple[tuple[str, str], ...] = (
    ("SET_1", "SET_2"),
    ("SET_2", "SET_3"),
    ("SET_3", "SET_4"),
    ("SET_4", "SET_5"),
)


@dataclass(frozen=True)
class SetRead:
    """One set's top-down read: what its MTF must do next, and whether it did."""

    set_key: str
    timestamp: datetime
    htf_state_id: Optional[str]
    mtf_state_id: Optional[str]
    htf_trend: Optional[int]
    htf_location: Optional[str]
    required_mtf_shift: Optional[int]
    expected_phase: Optional[str]
    mtf_trend: Optional[int]
    mtf_formed: Optional[bool]
    status: str


def evaluate_set_read(view: SetStateView) -> SetRead:
    key = normalized_set_key(view.set_key)
    htf, mtf, _ltf = view.states
    if htf is None or mtf is None:
        return SetRead(
            set_key=key,
            timestamp=view.timestamp,
            htf_state_id=htf.state_id if htf is not None else None,
            mtf_state_id=mtf.state_id if mtf is not None else None,
            htf_trend=htf.structural_trend if htf is not None else None,
            htf_location=htf.location if htf is not None else None,
            required_mtf_shift=None,
            expected_phase=None,
            mtf_trend=mtf.structural_trend if mtf is not None else None,
            mtf_formed=None,
            status=STATUS_INSUFFICIENT_COVERAGE,
        )
    shift = expected_child_shift(htf)
    if shift is None:
        return SetRead(
            set_key=key,
            timestamp=view.timestamp,
            htf_state_id=htf.state_id,
            mtf_state_id=mtf.state_id,
            htf_trend=htf.structural_trend,
            htf_location=htf.location,
            required_mtf_shift=None,
            expected_phase=None,
            mtf_trend=mtf.structural_trend,
            mtf_formed=None,
            status=STATUS_HTF_UNRESOLVED,
        )
    formed = mtf.structural_trend == shift
    return SetRead(
        set_key=key,
        timestamp=view.timestamp,
        htf_state_id=htf.state_id,
        mtf_state_id=mtf.state_id,
        htf_trend=htf.structural_trend,
        htf_location=htf.location,
        required_mtf_shift=shift,
        expected_phase=expected_phase(htf),
        mtf_trend=mtf.structural_trend,
        mtf_formed=formed,
        status=STATUS_MTF_SETUP_FORMED if formed else STATUS_PENDING_MTF_SETUP,
    )


@dataclass(frozen=True)
class ConsistencyObservation:
    set_pair: tuple[str, str]
    bridge_timeframe: str
    timestamp: datetime
    parent_set_status: str
    child_set_status: str
    required_bridge_shift: Optional[int]
    child_required_shift: Optional[int]
    bridge_trend: Optional[int]
    verdict: str
    reason: str
    movement_id: Optional[str]
    parent_mtf_state_id: Optional[str]
    child_htf_state_id: Optional[str]


@dataclass(frozen=True)
class ConsistencyEpisode:
    set_pair: tuple[str, str]
    bridge_timeframe: str
    verdict: str
    started_at: datetime
    ended_at: datetime
    observation_count: int


def _verdict(parent_read: SetRead, child_read: SetRead) -> tuple[str, str]:
    if parent_read.status == STATUS_INSUFFICIENT_COVERAGE:
        return VERDICT_WAITING, "parent set lacks scale coverage"
    if parent_read.status == STATUS_HTF_UNRESOLVED:
        return VERDICT_WAITING, "parent HTF has no derivable bridge requirement"
    if child_read.status == STATUS_INSUFFICIENT_COVERAGE:
        return VERDICT_WAITING, "child set lacks scale coverage"
    if parent_read.status == STATUS_MTF_SETUP_FORMED:
        if child_read.required_mtf_shift is None:
            return VERDICT_WAITING, "bridge shift confirmed; child set awaiting next-layer requirement"
        return VERDICT_ALIGNED, "bridge shift confirmed; child set derived the next-layer requirement"
    required = parent_read.required_mtf_shift
    child_shift = child_read.required_mtf_shift
    if required is None or child_shift is None:
        return VERDICT_WAITING, "missing derived shift on one side"
    if required == child_shift:
        return VERDICT_ALIGNED, "adjacent sets require the same directional shift at successive layers"
    return VERDICT_CONFLICTED, "child set requires the opposite shift at its layer"


def _view_asof(
    views: Sequence[SetStateView], timestamp: datetime
) -> Optional[SetStateView]:
    times = [view.timestamp for view in views]
    index = bisect_right(times, timestamp) - 1
    return views[index] if index >= 0 else None


def _build_episodes(
    observations: Sequence[ConsistencyObservation],
) -> tuple[ConsistencyEpisode, ...]:
    episodes: list[ConsistencyEpisode] = []
    for observation in observations:
        if (
            episodes
            and episodes[-1].set_pair == observation.set_pair
            and episodes[-1].verdict == observation.verdict
        ):
            previous = episodes[-1]
            episodes[-1] = ConsistencyEpisode(
                set_pair=previous.set_pair,
                bridge_timeframe=previous.bridge_timeframe,
                verdict=previous.verdict,
                started_at=previous.started_at,
                ended_at=observation.timestamp,
                observation_count=previous.observation_count + 1,
            )
        else:
            episodes.append(ConsistencyEpisode(
                set_pair=observation.set_pair,
                bridge_timeframe=observation.bridge_timeframe,
                verdict=observation.verdict,
                started_at=observation.timestamp,
                ended_at=observation.timestamp,
                observation_count=1,
            ))
    return tuple(episodes)


@dataclass(frozen=True)
class CrossSetConsistencyReport:
    observations: tuple[ConsistencyObservation, ...]
    episodes: tuple[ConsistencyEpisode, ...]

    @property
    def pair_summaries(self) -> dict[str, dict[str, object]]:
        summaries: dict[str, dict[str, object]] = {}
        for observation in self.observations:
            key = f"{observation.set_pair[0]}->{observation.set_pair[1]}"
            bucket = summaries.setdefault(key, {
                "bridge_timeframe": observation.bridge_timeframe,
                "observations": 0,
                "by_verdict": {},
                "episodes": 0,
            })
            bucket["observations"] = int(bucket["observations"]) + 1
            by_verdict = bucket["by_verdict"]
            by_verdict[observation.verdict] = by_verdict.get(observation.verdict, 0) + 1
        for episode in self.episodes:
            key = f"{episode.set_pair[0]}->{episode.set_pair[1]}"
            bucket = summaries.setdefault(key, {
                "bridge_timeframe": episode.bridge_timeframe,
                "observations": 0,
                "by_verdict": {},
                "episodes": 0,
            })
            bucket["episodes"] = int(bucket["episodes"]) + 1
        return summaries


def evaluate_cross_set_consistency(
    views_by_set: dict[str, Sequence[SetStateView]],
    movement_ledger: Optional[CausalMovementLedger] = None,
) -> CrossSetConsistencyReport:
    """Compare adjacent set reads on their union timeline (as-of, causal)."""
    normalized = {
        normalized_set_key(key): tuple(sorted(views, key=lambda view: view.timestamp))
        for key, views in views_by_set.items()
    }
    observations: list[ConsistencyObservation] = []
    for parent_key, child_key in ADJACENT_SET_PAIRS:
        if parent_key not in normalized or child_key not in normalized:
            continue
        bridge = TIMEFRAME_SETS[parent_key][1]
        parent_views = normalized[parent_key]
        child_views = normalized[child_key]
        times = sorted(
            {view.timestamp for view in parent_views}
            | {view.timestamp for view in child_views}
        )
        for timestamp in times:
            parent_view = _view_asof(parent_views, timestamp)
            child_view = _view_asof(child_views, timestamp)
            if parent_view is None or child_view is None:
                continue
            parent_read = evaluate_set_read(parent_view)
            child_read = evaluate_set_read(child_view)
            verdict, reason = _verdict(parent_read, child_read)
            observations.append(ConsistencyObservation(
                set_pair=(parent_key, child_key),
                bridge_timeframe=bridge.value,
                timestamp=timestamp,
                parent_set_status=parent_read.status,
                child_set_status=child_read.status,
                required_bridge_shift=parent_read.required_mtf_shift,
                child_required_shift=child_read.required_mtf_shift,
                bridge_trend=child_read.htf_trend,
                verdict=verdict,
                reason=reason,
                movement_id=movement_ledger.at(timestamp) if movement_ledger is not None else None,
                parent_mtf_state_id=parent_read.mtf_state_id,
                child_htf_state_id=child_read.htf_state_id,
            ))
    return CrossSetConsistencyReport(
        observations=tuple(observations),
        episodes=_build_episodes(observations),
    )
