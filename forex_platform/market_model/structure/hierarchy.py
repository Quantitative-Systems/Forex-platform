"""External/internal swing hierarchy.

External swings are the macro leg bounds (detected with the larger,
default ``lookback``). Internal swings are subordinate pivots detected
with a smaller confirmation window that fall strictly inside an
external leg -- i.e. between two consecutive opposite-type external
swings. Both layers use the same deterministic pivot rule; only the
confirmation window and scope tag differ.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import polars as pl

from forex_platform.market_model.contracts import SwingPoint, SwingScope
from forex_platform.market_model.structure.swings import detect_swings


def detect_internal_swings(
    bars: pl.DataFrame,
    external_swings: Sequence[SwingPoint] | None = None,
    *,
    external_lookback: int = 2,
    internal_lookback: int = 1,
    high_col: str = "high",
    low_col: str = "low",
    timestamp_col: str = "timestamp",
) -> List[SwingPoint]:
    """Detect internal (subordinate) swing pivots.

    Requires ``internal_lookback < external_lookback`` and at least two
    external swings to bound an external leg; otherwise ``ValueError``.
    Internal pivots strictly inside an external leg are kept (scope
    tagged ``INTERNAL``); everything else is discarded.
    """
    if external_lookback < 1:
        raise ValueError(f"external_lookback must be >= 1, got {external_lookback}")
    if internal_lookback < 1:
        raise ValueError(f"internal_lookback must be >= 1, got {internal_lookback}")
    if internal_lookback >= external_lookback:
        raise ValueError(
            "internal_lookback must be smaller than external_lookback: "
            f"internal={internal_lookback}, external={external_lookback}"
        )

    if external_swings is None:
        external_swings = detect_swings(
            bars,
            external_lookback,
            high_col=high_col,
            low_col=low_col,
            timestamp_col=timestamp_col,
            scope=SwingScope.EXTERNAL,
        )
    if len(external_swings) < 2:
        raise ValueError(
            "at least two external swings are required to define an external leg, "
            f"got {len(external_swings)}"
        )

    externals = sorted(external_swings, key=lambda s: s.index)
    legs: List[Tuple[int, int]] = []
    for leg_start, leg_end in zip(externals, externals[1:]):
        if leg_start.swing_type != leg_end.swing_type:
            legs.append((leg_start.index, leg_end.index))

    candidates = detect_swings(
        bars,
        internal_lookback,
        high_col=high_col,
        low_col=low_col,
        timestamp_col=timestamp_col,
        scope=SwingScope.INTERNAL,
    )
    return [
        swing
        for swing in candidates
        if any(lo < swing.index < hi for lo, hi in legs)
    ]


def split_external_internal(
    bars: pl.DataFrame,
    *,
    external_lookback: int = 2,
    internal_lookback: int = 1,
    high_col: str = "high",
    low_col: str = "low",
    timestamp_col: str = "timestamp",
) -> Tuple[List[SwingPoint], List[SwingPoint]]:
    """Detect both hierarchy layers in one call.

    Returns ``(external_swings, internal_swings)``. Raises ``ValueError``
    when the window configuration is invalid or the data yields fewer than
    two external swings.
    """
    external = detect_swings(
        bars,
        external_lookback,
        high_col=high_col,
        low_col=low_col,
        timestamp_col=timestamp_col,
        scope=SwingScope.EXTERNAL,
    )
    internal = detect_internal_swings(
        bars,
        external,
        external_lookback=external_lookback,
        internal_lookback=internal_lookback,
        high_col=high_col,
        low_col=low_col,
        timestamp_col=timestamp_col,
    )
    return external, internal