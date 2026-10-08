"""Parent-to-child requirement propagation across the canonical ladder.

A parent timeframe state defines what the next finer timeframe must do next:
while the parent trades in premium the child is required to shift against the
parent trend (pullback); in discount, with the parent trend (continuation).
A requirement resolves only against child states published at or after its
creation timestamp, so prefix-built and full-built lifecycles are identical.
Observation only: nothing here emits execution signals.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from forex_platform.fractal_engine.state_engine import (
    CanonicalTimeframeState,
    LocationState,
)
from forex_platform.market_model.contracts import MarketPhase

STATUS_PENDING = "PENDING"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_INVALIDATED = "INVALIDATED"
STATUS_SUPERSEDED = "SUPERSEDED"

EVENT_CREATED = "CREATED"
EVENT_CARRIED = "CARRIED"
EVENT_CONFIRMED = "CONFIRMED"
EVENT_INVALIDATED = "INVALIDATED"
EVENT_SUPERSEDED = "SUPERSEDED"


def expected_child_shift(parent: CanonicalTimeframeState) -> Optional[int]:
    """Structural shift the child timeframe is required to make next.

    Premium requires a counter-trend child shift (pullback), discount a
    with-trend child shift (continuation). Neutral trends and non-committal
    locations impose no requirement.
    """
    trend = parent.structural_trend
    if trend not in (-1, 1):
        return None
    if parent.location == LocationState.PREMIUM:
        return -trend
    if parent.location == LocationState.DISCOUNT:
        return trend
    return None


def expected_phase(parent: CanonicalTimeframeState) -> Optional[str]:
    """Market phase the required child shift represents, in upper-case."""
    shift = expected_child_shift(parent)
    trend = parent.structural_trend
    if shift is None or trend not in (-1, 1):
        return None
    phase = MarketPhase.PULLBACK if shift == -trend else MarketPhase.CONTINUATION
    return phase.value.upper()


def parent_invalidation_price(parent: CanonicalTimeframeState) -> Optional[Decimal]:
    """Close beyond this level voids the parent's own structural thesis."""
    trend = parent.structural_trend
    if trend not in (-1, 1):
        return None
    wanted = "low" if trend > 0 else "high"
    protected = [
        level for level in parent.key_levels
        if level.swing_type == wanted and level.protected
    ]
    if protected:
        return protected[-1].price
    if parent.structural_range is not None:
        return parent.structural_range.low if trend > 0 else parent.structural_range.high
    return None


def _confirmation_price(
    child: Optional[CanonicalTimeframeState], shift: int
) -> Optional[Decimal]:
    """Frozen child level whose close-through confirms the required shift.

    For a required bullish shift this is the nearest confirmed child swing
    high above the child close (its break is the bullish structure break);
    for a bearish shift, the nearest confirmed child swing low below it.
    """
    if child is None:
        return None
    wanted = "high" if shift > 0 else "low"
    levels = [level.price for level in child.key_levels if level.swing_type == wanted]
    price = child.current_price
    if shift > 0:
        candidates = [level for level in levels if level > price]
        if candidates:
            return min(candidates)
        if child.structural_range is not None and child.structural_range.high > price:
            return child.structural_range.high
    else:
        candidates = [level for level in levels if level < price]
        if candidates:
            return max(candidates)
        if child.structural_range is not None and child.structural_range.low < price:
            return child.structural_range.low
    return None


@dataclass(frozen=True)
class ChildRequirement:
    requirement_id: str
    symbol: str
    parent_timeframe: str
    child_timeframe: str
    parent_state_id: str
    parent_trend: int
    parent_location: str
    expected_child_shift: int
    expected_phase: str
    confirmation_price: Optional[Decimal]
    invalidation_price: Optional[Decimal]
    created_at: datetime
    expectation_signature: str


@dataclass(frozen=True)
class RequirementEvent:
    timestamp: datetime
    requirement_id: str
    event: str
    parent_state_id: str
    child_state_id: Optional[str]
    detail: str


@dataclass(frozen=True)
class ResolvedRequirement:
    requirement: ChildRequirement
    status: str
    resolved_at: Optional[datetime]
    resolved_by_state_id: Optional[str]
    resolution_detail: str
    child_states_observed: int


class RequirementLifecycle:
    """CREATED -> CONFIRMED / INVALIDATED / SUPERSEDED state machine.

    At most one requirement per parent->child pair is pending. A parent state
    whose expectation signature matches the pending one only carries it (the
    setup is still forming; a changed signature supersedes it).
    """

    def __init__(self, parent_timeframe: str, child_timeframe: str) -> None:
        self.parent_timeframe = parent_timeframe
        self.child_timeframe = child_timeframe
        self._pending: Optional[ChildRequirement] = None
        self._pending_children = 0
        self._sequence = 0
        self.events: list[RequirementEvent] = []
        self.resolved: list[ResolvedRequirement] = []

    @property
    def pending(self) -> Optional[ChildRequirement]:
        return self._pending

    def push_parent(
        self,
        parent: CanonicalTimeframeState,
        child_asof: Optional[CanonicalTimeframeState] = None,
    ) -> list[RequirementEvent]:
        shift = expected_child_shift(parent)
        signature = f"{parent.structural_trend or 0}|{shift if shift is not None else 'NONE'}"
        emitted: list[RequirementEvent] = []
        if self._pending is not None and self._pending.expectation_signature != signature:
            emitted.append(self._resolve(
                self._pending, EVENT_SUPERSEDED, parent.timestamp, None,
                f"parent expectation changed to {signature}",
            ))
        if shift is None:
            return emitted
        if self._pending is not None:
            refreshed = self._pending
            invalidation = parent_invalidation_price(parent)
            if invalidation is not None and invalidation != refreshed.invalidation_price:
                refreshed = replace(refreshed, invalidation_price=invalidation)
                self._pending = refreshed
            emitted.append(RequirementEvent(
                timestamp=parent.timestamp,
                requirement_id=refreshed.requirement_id,
                event=EVENT_CARRIED,
                parent_state_id=parent.state_id,
                child_state_id=None,
                detail=f"parent expectation persists as {signature}",
            ))
            return emitted
        self._sequence += 1
        requirement = ChildRequirement(
            requirement_id=(
                f"{parent.symbol}|{self.parent_timeframe}->{self.child_timeframe}"
                f"|REQ-{self._sequence:06d}"
            ),
            symbol=parent.symbol,
            parent_timeframe=self.parent_timeframe,
            child_timeframe=self.child_timeframe,
            parent_state_id=parent.state_id,
            parent_trend=parent.structural_trend,
            parent_location=parent.location,
            expected_child_shift=shift,
            expected_phase=expected_phase(parent),
            confirmation_price=_confirmation_price(child_asof, shift),
            invalidation_price=parent_invalidation_price(parent),
            created_at=parent.timestamp,
            expectation_signature=signature,
        )
        self._pending = requirement
        self._pending_children = 0
        emitted.append(RequirementEvent(
            timestamp=parent.timestamp,
            requirement_id=requirement.requirement_id,
            event=EVENT_CREATED,
            parent_state_id=parent.state_id,
            child_state_id=child_asof.state_id if child_asof is not None else None,
            detail=(
                f"child {self.child_timeframe} required to shift "
                f"{'bullish' if shift > 0 else 'bearish'} ({requirement.expected_phase})"
            ),
        ))
        return emitted

    def push_child(self, child: CanonicalTimeframeState) -> list[RequirementEvent]:
        requirement = self._pending
        if requirement is None or child.timestamp < requirement.created_at:
            return []
        self._pending_children += 1
        invalidation = requirement.invalidation_price
        if invalidation is not None:
            breached = (
                requirement.parent_trend > 0 and child.current_price < invalidation
            ) or (
                requirement.parent_trend < 0 and child.current_price > invalidation
            )
            if breached:
                return [self._resolve(
                    requirement, EVENT_INVALIDATED, child.timestamp, child.state_id,
                    f"child close {child.current_price} beyond parent invalidation {invalidation}",
                )]
        if child.structural_trend == requirement.expected_child_shift:
            return [self._resolve(
                requirement, EVENT_CONFIRMED, child.timestamp, child.state_id,
                f"child structural trend shifted to {child.structural_trend}",
            )]
        confirmation = requirement.confirmation_price
        if confirmation is not None:
            crossed = (
                requirement.expected_child_shift > 0 and child.current_price >= confirmation
            ) or (
                requirement.expected_child_shift < 0 and child.current_price <= confirmation
            )
            if crossed:
                return [self._resolve(
                    requirement, EVENT_CONFIRMED, child.timestamp, child.state_id,
                    f"child close {child.current_price} crossed confirmation level {confirmation}",
                )]
        return []

    def _resolve(
        self,
        requirement: ChildRequirement,
        event: str,
        timestamp: datetime,
        child_state_id: Optional[str],
        detail: str,
    ) -> RequirementEvent:
        self.resolved.append(ResolvedRequirement(
            requirement=requirement,
            status=event,
            resolved_at=timestamp,
            resolved_by_state_id=child_state_id,
            resolution_detail=detail,
            child_states_observed=self._pending_children,
        ))
        if self._pending is requirement:
            self._pending = None
            self._pending_children = 0
        return RequirementEvent(
            timestamp=timestamp,
            requirement_id=requirement.requirement_id,
            event=event,
            parent_state_id=requirement.parent_state_id,
            child_state_id=child_state_id,
            detail=detail,
        )

    def summary(self) -> dict[str, object]:
        by_status: dict[str, int] = {}
        by_phase: dict[str, int] = {}
        for item in self.resolved:
            by_status[item.status] = by_status.get(item.status, 0) + 1
            by_phase[item.requirement.expected_phase] = by_phase.get(item.requirement.expected_phase, 0) + 1
        return {
            "parent_timeframe": self.parent_timeframe,
            "child_timeframe": self.child_timeframe,
            "requirements_created": self._sequence,
            "resolved_by_status": by_status,
            "resolved_by_expected_phase": by_phase,
            "pending": self._pending.requirement_id if self._pending else None,
            "events": len(self.events),
        }


def build_requirement_lifecycle(
    parent_states: Sequence[CanonicalTimeframeState],
    child_states: Sequence[CanonicalTimeframeState],
) -> RequirementLifecycle:
    """Merge parent and child histories chronologically into one lifecycle.

    At equal timestamps the parent state is processed first, so a requirement
    created at time T may be resolved by a child state closing at T. Only
    immutable state objects are read; nothing is recomputed.
    """
    parents = sorted(parent_states, key=lambda state: state.timestamp)
    children = sorted(child_states, key=lambda state: state.timestamp)
    if parents and children and parents[0].timeframe == children[0].timeframe:
        raise ValueError("parent and child histories must come from different timeframes")
    lifecycle = RequirementLifecycle(
        parents[0].timeframe if parents else "",
        children[0].timeframe if children else "",
    )
    merged = [("P", state) for state in parents] + [("C", state) for state in children]
    merged.sort(key=lambda item: (item[1].timestamp, 0 if item[0] == "P" else 1))
    last_child: Optional[CanonicalTimeframeState] = None
    for kind, state in merged:
        if kind == "P":
            lifecycle.events.extend(lifecycle.push_parent(state, last_child))
        else:
            lifecycle.events.extend(lifecycle.push_child(state))
            last_child = state
    return lifecycle
