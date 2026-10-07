"""Swing strength: protected vs weak swings.

An external swing that generated a valid BOS is *protected*: it is the
invalidation anchor for the leg. The opposing swing that the break crossed
is *weak*: it is the liquidity target.
"""

from __future__ import annotations

from typing import List, Sequence

from forex_platform.market_model.contracts import (
    BreakType,
    StructureBreak,
    SwingPoint,
    SwingType,
)


def mark_protected_weak_swings(
    swings: Sequence[SwingPoint],
    breaks: Sequence[StructureBreak],
) -> List[SwingPoint]:
    """Classify swings as protected (invalidation anchors) and weak (targets).

    For every Break of Structure:

    * the **origin** swing the broken leg launched from (the most recent
      opposing-type pivot before the break bar) becomes *protected*;
    * the **broken** swing becomes *weak* (the opposing liquidity target).

    Flags are cumulative: a swing previously marked keeps its flag.
    Returns a new list; input models are frozen and never mutated.
    """
    ordered = sorted(swings, key=lambda s: s.index)
    position_by_index = {s.index: pos for pos, s in enumerate(ordered)}
    protected: set[int] = set()
    weak: set[int] = set()

    for structure_break in breaks:
        if structure_break.break_type != BreakType.BOS:
            continue
        broken_pos = position_by_index.get(structure_break.swing_index)
        if broken_pos is None:
            continue
        broken = ordered[broken_pos]
        # Origin: most recent opposing-type pivot strictly before the break.
        origin_type = (
            SwingType.LOW if structure_break.direction == 1 else SwingType.HIGH
        )
        origin: SwingPoint | None = None
        for swing in ordered:
            if swing.index >= structure_break.index:
                break
            if swing.swing_type == origin_type:
                origin = swing
        if origin is not None:
            protected.add(origin.index)
        weak.add(broken.index)

    marked: List[SwingPoint] = []
    for swing in swings:
        is_protected = swing.is_protected or swing.index in protected
        is_weak = swing.is_weak or swing.index in weak
        if is_protected != swing.is_protected or is_weak != swing.is_weak:
            swing = swing.model_copy(
                update={"is_protected": is_protected, "is_weak": is_weak}
            )
        marked.append(swing)
    return marked