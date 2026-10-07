"""Deterministic market-phase classification.

- CONTINUATION: close reached/exceeded latest swing in trend direction.
- PULLBACK: retraced while structural trend intact.

The trend is derived solely from the most recent confirmed structure break
(causal by construction: breaks are pre-filtered to ``index <= classified
index``). With no breaks the market is neutral: ``trend is None`` and the
conservative default phase is PULLBACK.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

import polars as pl
from pydantic import BaseModel, ConfigDict

from forex_platform.market_model.contracts import (
    MarketPhase,
    StructureBreak,
    SwingPoint,
    SwingType,
)


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class PhaseAssessment(BaseModel):
    """Phase verdict for one bar, with its reference swing and distance."""

    model_config = ConfigDict(frozen=True)

    index: int
    timestamp: datetime
    close_price: Decimal
    phase: MarketPhase
    trend: Optional[int] = None
    reference_swing_index: Optional[int] = None
    reference_level: Optional[Decimal] = None
    distance_to_reference: Optional[Decimal] = None


class MarketPhaseClassifier:
    """Stateless-trend phase classifier over captured swings and breaks."""

    def __init__(
        self,
        swings: Sequence[SwingPoint],
        breaks: Sequence[StructureBreak],
    ) -> None:
        self._swings = sorted(swings, key=lambda s: s.index)
        self._breaks = sorted(breaks, key=lambda b: b.index)

    def classify(
        self,
        bars: pl.DataFrame,
        index: int = -1,
        *,
        close_col: str = "close",
        timestamp_col: str = "timestamp",
    ) -> PhaseAssessment:
        """Classify the phase at ``index`` (default: final bar)."""
        if close_col not in bars.columns or timestamp_col not in bars.columns:
            raise ValueError("bars must contain close and timestamp columns")
        if bars.is_empty():
            raise ValueError("cannot classify an empty bar series")
        n = len(bars)
        idx = n - 1 if index == -1 else index
        if not (0 <= idx < n):
            raise IndexError(f"index {idx} outside bar series [0, {n})")

        close = _dec(bars[close_col][idx])
        timestamp = bars[timestamp_col][idx]

        causal_breaks = [b for b in self._breaks if b.index <= idx]
        if not causal_breaks:
            # Neutral: no confirmed structure -> no trend thesis yet.
            return PhaseAssessment(
                index=idx,
                timestamp=timestamp,
                close_price=close,
                phase=MarketPhase.PULLBACK,
            )

        trend = causal_breaks[-1].direction
        target_type = SwingType.HIGH if trend >= 0 else SwingType.LOW
        ref_candidates = [
            s
            for s in self._swings
            if s.index <= idx and s.swing_type == target_type
        ]
        if not ref_candidates:
            return PhaseAssessment(
                index=idx,
                timestamp=timestamp,
                close_price=close,
                phase=MarketPhase.PULLBACK,
                trend=trend,
            )

        ref = ref_candidates[-1]
        distance = close - ref.price
        if trend >= 0:
            phase = (
                MarketPhase.CONTINUATION
                if close >= ref.price
                else MarketPhase.PULLBACK
            )
        else:
            phase = (
                MarketPhase.CONTINUATION
                if close <= ref.price
                else MarketPhase.PULLBACK
            )

        return PhaseAssessment(
            index=idx,
            timestamp=timestamp,
            close_price=close,
            phase=phase,
            trend=trend,
            reference_swing_index=ref.index,
            reference_level=ref.price,
            distance_to_reference=distance,
        )


def classify_phase(
    bars: pl.DataFrame,
    swings: Sequence[SwingPoint],
    breaks: Sequence[StructureBreak],
    index: int = -1,
    *,
    close_col: str = "close",
    timestamp_col: str = "timestamp",
) -> PhaseAssessment:
    """Convenience wrapper: build a classifier and classify one bar."""
    return MarketPhaseClassifier(swings, breaks).classify(
        bars, index, close_col=close_col, timestamp_col=timestamp_col
    )
