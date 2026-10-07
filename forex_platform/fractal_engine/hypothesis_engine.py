"""Empirical conditional path hypotheses; observed states stay immutable."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence

from forex_platform.fractal_engine.state_engine import CanonicalTimeframeState
from forex_platform.fractal_engine.state_graph import SetStateView
from forex_platform.fractal_engine.timeframes import normalized_set_key


@dataclass(frozen=True)
class TransitionObservation:
    timestamp: datetime
    set_key: str
    condition_key: tuple[str, ...]
    outcome: str


@dataclass(frozen=True)
class PathAlternative:
    outcome: str
    count: int
    probability: float


@dataclass(frozen=True)
class FractalHypothesis:
    """A conditional, evidence-backed path distribution for one current view."""

    set_key: str
    as_of: datetime
    observed_state_ids: tuple[str, ...]
    observed_state: tuple[tuple[str, str], ...]
    condition_key: tuple[str, ...]
    status: str
    expected_path: Optional[str]
    required_confirmation: tuple[str, ...]
    acceptable_alternatives: tuple[PathAlternative, ...]
    invalidation_condition: Optional[str]
    transition_confidence: Optional[float]
    historical_sample_count: int
    destination_condition: Optional[str]
    execution_eligibility: str


def _categorical_condition(
    htf: CanonicalTimeframeState,
    mtf: CanonicalTimeframeState,
) -> tuple[str, ...]:
    relation = (
        "ALIGNED" if htf.structural_trend and htf.structural_trend == mtf.structural_trend
        else "OPPOSED" if htf.structural_trend and mtf.structural_trend and htf.structural_trend == -mtf.structural_trend
        else "UNRESOLVED"
    )
    return (
        htf.trend_label,
        htf.phase.value.upper(),
        htf.location,
        mtf.trend_label,
        mtf.phase.value.upper(),
        mtf.location,
        relation,
    )


def _state_outcome(state: CanonicalTimeframeState) -> str:
    return "|".join((
        state.trend_label,
        state.phase.value.upper(),
        state.location,
        state.swing_state,
    ))


class FractalHypothesisEngine:
    """Estimate next-LTF-state outcomes using only observations before as_of."""

    def __init__(self, min_observations: int = 20):
        if min_observations < 1:
            raise ValueError("min_observations must be positive")
        self.min_observations = min_observations
        self.observations: list[TransitionObservation] = []

    def fit_observations(self, views: Sequence[SetStateView]) -> list[TransitionObservation]:
        """Extract one observation when a new LTF state becomes available."""
        ordered = sorted(views, key=lambda view: view.timestamp)
        observations: list[TransitionObservation] = []
        prior_ltf_id: Optional[str] = None
        for view in ordered:
            htf, mtf, ltf = view.states
            if htf is None or mtf is None or ltf is None:
                continue
            if ltf.state_id == prior_ltf_id:
                continue
            prior_ltf_id = ltf.state_id
            if len(ltf.transition) == 1 and ltf.transition[0] == "STATE_START":
                continue
            observations.append(TransitionObservation(
                timestamp=ltf.timestamp,
                set_key=normalized_set_key(view.set_key),
                condition_key=_categorical_condition(htf, mtf),
                outcome=",".join(ltf.transition) + ":" + _state_outcome(ltf),
            ))
        self.observations = observations
        return list(observations)

    @staticmethod
    def _destination(htf: CanonicalTimeframeState) -> Optional[str]:
        direction = htf.structural_trend
        if direction not in (-1, 1):
            return None
        wanted = "high" if direction > 0 else "low"
        levels = [
            level for level in htf.key_levels
            if level.swing_type == wanted and level.price != htf.current_price
        ]
        if not levels:
            return f"next confirmed {wanted} liquidity level on {htf.timeframe}"
        ordered = sorted(levels, key=lambda level: level.price)
        if direction > 0:
            candidates = [level for level in ordered if level.price > htf.current_price]
            level = min(candidates, key=lambda item: item.price) if candidates else None
        else:
            candidates = [level for level in ordered if level.price < htf.current_price]
            level = max(candidates, key=lambda item: item.price) if candidates else None
        if level is None:
            return f"next confirmed {wanted} liquidity level on {htf.timeframe}"
        return f"{wanted} key level at {level.price} (available by {level.available_at.isoformat()})"

    def infer(self, view: SetStateView) -> FractalHypothesis:
        key = normalized_set_key(view.set_key)
        htf, mtf, ltf = view.states
        states = tuple(state for state in (htf, mtf, ltf) if state is not None)
        state_labels = tuple((state.timeframe, state.state_signature) for state in states)
        ids = tuple(state.state_id for state in states)
        if htf is None or mtf is None or ltf is None:
            return FractalHypothesis(
                set_key=key,
                as_of=view.timestamp,
                observed_state_ids=ids,
                observed_state=state_labels,
                condition_key=(),
                status="INSUFFICIENT_SCALE_COVERAGE",
                expected_path=None,
                required_confirmation=(),
                acceptable_alternatives=(),
                invalidation_condition=None,
                transition_confidence=None,
                historical_sample_count=0,
                destination_condition=None,
                execution_eligibility="OBSERVATION_ONLY",
            )

        condition = _categorical_condition(htf, mtf)
        prior = [
            item for item in self.observations
            if item.set_key == key
            and item.condition_key == condition
            and item.timestamp < view.timestamp
        ]
        counts = Counter(item.outcome for item in prior)
        total = sum(counts.values())
        if total < self.min_observations:
            status = "INSUFFICIENT_HISTORICAL_EVIDENCE"
            expected = None
            alternatives: tuple[PathAlternative, ...] = ()
            confidence = None
            confirmation: tuple[str, ...] = ()
        else:
            alternatives = tuple(
                PathAlternative(outcome=outcome, count=count, probability=count / total)
                for outcome, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            )
            expected = alternatives[0].outcome
            confidence = alternatives[0].probability
            status = "CONDITIONAL_HYPOTHESIS"
            confirmation = (f"observe lower-state transition {expected}",)
        return FractalHypothesis(
            set_key=key,
            as_of=view.timestamp,
            observed_state_ids=ids,
            observed_state=state_labels,
            condition_key=condition,
            status=status,
            expected_path=expected,
            required_confirmation=confirmation,
            acceptable_alternatives=alternatives,
            invalidation_condition=htf.invalidation_condition,
            transition_confidence=confidence,
            historical_sample_count=total,
            destination_condition=self._destination(htf),
            execution_eligibility="OBSERVATION_ONLY",
        )
