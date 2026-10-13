"""Cascading Fractal Coordinator — top-down hypothesis transmission.

This module implements the Master Blueprint's central idea:

    The five timeframe sets are not independent strategies. They are five
    correlated *views* of one continuous, causal market state. A structural
    event on a shared timeframe feeds every set that consumes it.

    SET_1: 1M -> 1W -> 1D     (Macro central-bank trend)
    SET_2: 1W -> 1D -> 4H     (Weekly swing)
    SET_3: 1D -> 4H -> 1H     (Core intraday swing)
    SET_4: 4H -> 1H -> 15M    (London/NY execution)
    SET_5: 1H -> 15M -> 3M/1M (Micro sniper trigger)

The coordinator computes ONE canonical state per timeframe (shared across
all sets), then determines, for each set, a ``MacroPhaseBaton``:

    - HTF trend + HTF location -> phase hypothesis (expansion vs pullback).
    - MTF structural behaviour -> confirmation / invalidation of hypothesis.
    - LTF trigger -> precise execution.

Nothing in this module is an order fact. It is a *hypothesis engine* whose
output is consumed by the strategy layer for entry decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

from forex_platform.fractal_engine.state_engine import CanonicalTimeframeState
from forex_platform.fractal_engine.timeframes import TIMEFRAME_SETS


# ---------------------------------------------------------------------------
# Phase Baton
# ---------------------------------------------------------------------------

class MacroPhaseBaton(str, Enum):
    """Dual-phase dealing-range baton per the Institutional Knowledge Core."""
    BULLISH_EXPANSION = "BULLISH_EXPANSION"   # ride continuation long
    BEARISH_EXPANSION = "BEARISH_EXPANSION"   # ride continuation short
    BULLISH_PULLBACK = "BULLISH_PULLBACK"     # bearish trend retrace to discount -> look long
    BEARISH_PULLBACK = "BEARISH_PULLBACK"     # bullish trend retrace to premium -> look short
    TRANSITION = "TRANSITION"                 # structure shifting; not yet confirmed
    UNKNOWN = "UNKNOWN"                       # insufficient evidence


# Normalized dealing-range boundaries (Premium > 0.65, Discount < 0.35 per blueprint)
PREMIUM_BOUNDARY = Decimal("0.65")
DISCOUNT_BOUNDARY = Decimal("0.35")


@dataclass(frozen=True)
class SetHypothesis:
    """The conditional expectation for one set at one causal moment."""
    set_key: str
    phase: MacroPhaseBaton
    direction: int                 # +1 long bias, -1 short bias, 0 none
    htf_location: Optional[Decimal]
    htf_trend: int
    valid: bool                    # hypothesis internally consistent
    invalidation: Optional[str] = None  # what kills this hypothesis


@dataclass
class CoordinatorSnapshot:
    """Per-symbol snapshot: one hypothesis per set + shared canonical states."""
    symbol: str
    hypotheses: dict[str, SetHypothesis] = field(default_factory=dict)
    htf_states: dict[str, CanonicalTimeframeState] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Coordinator (one canonical state per timeframe)
# ---------------------------------------------------------------------------

class CascadingFractalCoordinator:
    """Consume closed canonical timeframe states; emit set hypotheses.

    Key invariant: a timeframe's state is computed ONCE by the underlying
    ``UniversalTimeframeStateEngine`` and *shared* across every set that
    contains that timeframe. This class only *interprets* those states
    within each set's structural context — it never recomputes them.
    """

    def __init__(self) -> None:
        self.snapshots: dict[str, CoordinatorSnapshot] = {}

    # -- Phase determination from HTF (trend + premium/discount) ------------
    def _htf_phase(self, state: CanonicalTimeframeState) -> tuple[MacroPhaseBaton, int]:
        trend = state.structural_trend
        loc = state.range_location
        if trend not in (-1, 1) or loc is None:
            return MacroPhaseBaton.UNKNOWN, 0

        in_premium = loc >= PREMIUM_BOUNDARY
        in_discount = loc <= DISCOUNT_BOUNDARY

        if trend > 0:
            if in_discount:
                return MacroPhaseBaton.BULLISH_EXPANSION, 1    # ride continuation
            if in_premium:
                return MacroPhaseBaton.BEARISH_PULLBACK, -1    # expect pullback to premium
            return MacroPhaseBaton.TRANSITION, 0               # mid-range: wait
        else:
            if in_premium:
                return MacroPhaseBaton.BEARISH_EXPANSION, -1   # ride continuation
            if in_discount:
                return MacroPhaseBaton.BULLISH_PULLBACK, 1     # expect pullback to discount
            return MacroPhaseBaton.TRANSITION, 0

    # -- MTF confirmation ----------------------------------------------------
    def _mtf_confirms(
        self,
        mtf: Optional[CanonicalTimeframeState],
        phase: MacroPhaseBaton,
        direction: int,
    ) -> bool:
        """MTF must show a *structural shift* in the direction of the hypothesis.

        For an expansion: MTF trend agrees with direction.
        For a pullback:  MTF shows opposite trend (the pullback leg) AND a
        recent CHoCH/BOS against the HTF trend direction has a chance to
        terminate — i.e., we want the pullback to be *developing*, not yet
        exhausted. Confirmation = MTF structural trend == direction.
        """
        if mtf is None or mtf.structural_trend is None:
            return False
        return int(mtf.structural_trend) == direction

    # -- Public evaluation ----------------------------------------------------
    def evaluate(
        self,
        symbol: str,
        state_getter,  # callable: (CanonicalTimeframe) -> CanonicalTimeframeState | None
        selected_set: Optional[str] = None,
    ) -> CoordinatorSnapshot:
        snap = self.snapshots.setdefault(symbol, CoordinatorSnapshot(symbol=symbol))

        for set_key, ladder in TIMEFRAME_SETS.items():
            if selected_set is not None and set_key != selected_set:
                continue
            htf = state_getter(ladder[0])
            mtf = state_getter(ladder[1])
            if htf is None:
                snap.hypotheses[set_key] = SetHypothesis(
                    set_key=set_key, phase=MacroPhaseBaton.UNKNOWN, direction=0,
                    htf_location=None, htf_trend=0, valid=False,
                    invalidation="NO_HTF_STATE",
                )
                continue
            snap.htf_states[set_key] = htf
            phase, direction = self._htf_phase(htf)
            confirmed = self._mtf_confirms(mtf, phase, direction)
            snap.hypotheses[set_key] = SetHypothesis(
                set_key=set_key,
                phase=phase,
                direction=direction,
                htf_location=htf.range_location,
                htf_trend=int(htf.structural_trend or 0),
                valid=confirmed and direction != 0,
                invalidation=None if confirmed else "MTF_NOT_ALIGNED",
            )
        return snap

    @staticmethod
    def phase_for_alignment(phase: MacroPhaseBaton) -> str:
        """Map baton -> phase string used by strategy entry/target logic."""
        if phase in (MacroPhaseBaton.BULLISH_EXPANSION, MacroPhaseBaton.BEARISH_EXPANSION):
            return "CONTINUATION"
        if phase in (MacroPhaseBaton.BULLISH_PULLBACK, MacroPhaseBaton.BEARISH_PULLBACK):
            return "PULLBACK"
        return "TRANSITION"
