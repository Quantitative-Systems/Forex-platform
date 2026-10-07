"""Market-structure primitives."""

from forex_platform.market_model.structure.breaks import detect_structure_breaks
from forex_platform.market_model.structure.hierarchy import (
    detect_internal_swings,
    split_external_internal,
)
from forex_platform.market_model.structure.strength import mark_protected_weak_swings
from forex_platform.market_model.structure.swings import detect_swings
from forex_platform.market_model.structure.trend import (
    get_trend_direction,
    is_trend_invalidated,
)

__all__ = [
    "detect_structure_breaks",
    "detect_internal_swings",
    "detect_swings",
    "get_trend_direction",
    "is_trend_invalidated",
    "mark_protected_weak_swings",
    "split_external_internal",
]