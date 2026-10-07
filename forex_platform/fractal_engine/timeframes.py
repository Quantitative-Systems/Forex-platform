"""Canonical timeframe ladder and its five overlapping set views.

The ladder label 1M means one calendar month. Input intervals use the
platform's lower-case duration labels (for example 1m means one minute).
Keeping those namespaces separate avoids the common 1M/month vs M1/minute
ambiguity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable

from forex_platform.market_data.causal_aligner import Timeframe


class CanonicalTimeframe(str, Enum):
    MONTH = "1M"
    WEEK = "1W"
    DAY = "1D"
    H4 = "4H"
    H1 = "1H"
    M15 = "15M"
    M3 = "3M"


CANONICAL_LADDER: tuple[CanonicalTimeframe, ...] = tuple(CanonicalTimeframe)

TIMEFRAME_SETS: dict[str, tuple[CanonicalTimeframe, CanonicalTimeframe, CanonicalTimeframe]] = {
    "SET_1": (CanonicalTimeframe.MONTH, CanonicalTimeframe.WEEK, CanonicalTimeframe.DAY),
    "SET_2": (CanonicalTimeframe.WEEK, CanonicalTimeframe.DAY, CanonicalTimeframe.H4),
    "SET_3": (CanonicalTimeframe.DAY, CanonicalTimeframe.H4, CanonicalTimeframe.H1),
    "SET_4": (CanonicalTimeframe.H4, CanonicalTimeframe.H1, CanonicalTimeframe.M15),
    "SET_5": (CanonicalTimeframe.H1, CanonicalTimeframe.M15, CanonicalTimeframe.M3),
}

ADJACENT_PAIRS: tuple[tuple[CanonicalTimeframe, CanonicalTimeframe], ...] = tuple(
    zip(CANONICAL_LADDER, CANONICAL_LADDER[1:])
)

NON_ADJACENT_PAIRS: tuple[tuple[CanonicalTimeframe, CanonicalTimeframe], ...] = tuple(
    (CANONICAL_LADDER[i], CANONICAL_LADDER[j])
    for i in range(len(CANONICAL_LADDER))
    for j in range(i + 2, len(CANONICAL_LADDER))
)


def parse_source_interval(value: Timeframe | timedelta | str) -> timedelta:
    """Parse a source bar interval, keeping month labels out of this namespace."""
    if isinstance(value, timedelta):
        if value <= timedelta(0):
            raise ValueError("source interval must be positive")
        return value
    if isinstance(value, Timeframe):
        return value.duration
    aliases = {
        "1m": timedelta(minutes=1), "m1": timedelta(minutes=1),
        "3m": timedelta(minutes=3), "m3": timedelta(minutes=3),
        "5m": timedelta(minutes=5), "m5": timedelta(minutes=5),
        "15m": timedelta(minutes=15), "m15": timedelta(minutes=15),
        "30m": timedelta(minutes=30), "m30": timedelta(minutes=30),
        "1h": timedelta(hours=1), "h1": timedelta(hours=1),
        "4h": timedelta(hours=4), "h4": timedelta(hours=4),
        "1d": timedelta(days=1), "d1": timedelta(days=1),
    }
    key = value.strip().lower()
    if key not in aliases:
        raise ValueError(f"unsupported source bar interval: {value!r}")
    return aliases[key]


def target_is_available(
    target: CanonicalTimeframe | str,
    source_interval: Timeframe | timedelta | str,
) -> bool:
    """Whether the source bars can construct the target without upsampling."""
    target = CanonicalTimeframe(target)
    source = parse_source_interval(source_interval)
    fixed_minutes = {
        CanonicalTimeframe.M3: 3,
        CanonicalTimeframe.M15: 15,
        CanonicalTimeframe.H1: 60,
        CanonicalTimeframe.H4: 240,
        CanonicalTimeframe.DAY: 1440,
    }
    if target in (CanonicalTimeframe.MONTH, CanonicalTimeframe.WEEK):
        return True
    target_duration = timedelta(minutes=fixed_minutes[target])
    return target_duration >= source and target_duration % source == timedelta(0)


def timeframe_bucket_start(
    timestamp: datetime, target: CanonicalTimeframe | str
) -> datetime:
    """Return the UTC opening boundary of the target candle containing timestamp."""
    target = CanonicalTimeframe(target)
    ts = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
    if target == CanonicalTimeframe.MONTH:
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if target == CanonicalTimeframe.WEEK:
        midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=ts.weekday())
    if target == CanonicalTimeframe.DAY:
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = {
        CanonicalTimeframe.H4: 240,
        CanonicalTimeframe.H1: 60,
        CanonicalTimeframe.M15: 15,
        CanonicalTimeframe.M3: 3,
    }[target]
    minute_of_day = ts.hour * 60 + ts.minute
    floored = minute_of_day - (minute_of_day % minutes)
    return ts.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def timeframe_bucket_end(
    bucket_start: datetime, target: CanonicalTimeframe | str
) -> datetime:
    """Return the exclusive UTC close boundary for a target candle."""
    target = CanonicalTimeframe(target)
    if target == CanonicalTimeframe.MONTH:
        if bucket_start.month == 12:
            return bucket_start.replace(year=bucket_start.year + 1, month=1, day=1)
        return bucket_start.replace(month=bucket_start.month + 1, day=1)
    if target == CanonicalTimeframe.WEEK:
        return bucket_start + timedelta(days=7)
    fixed_minutes = {
        CanonicalTimeframe.DAY: 1440,
        CanonicalTimeframe.H4: 240,
        CanonicalTimeframe.H1: 60,
        CanonicalTimeframe.M15: 15,
        CanonicalTimeframe.M3: 3,
    }
    return bucket_start + timedelta(minutes=fixed_minutes[target])


def role_map() -> dict[str, dict[str, str]]:
    """Return timeframe-to-set roles, useful in docs and diagnostics."""
    result: dict[str, dict[str, str]] = {}
    for set_key, ladder in TIMEFRAME_SETS.items():
        for role, timeframe in zip(("HTF", "MTF", "LTF"), ladder):
            result.setdefault(timeframe.value, {})[set_key] = role
    return result


def normalized_set_key(value: str) -> str:
    """Accept SET1 and SET_1 spellings while keeping canonical keys stable."""
    key = value.strip().upper().replace(" ", "").replace("-", "_")
    if key.startswith("SET") and not key.startswith("SET_"):
        key = "SET_" + key[3:]
    if key not in TIMEFRAME_SETS:
        raise ValueError(f"unknown timeframe set {value!r}; expected one of {tuple(TIMEFRAME_SETS)}")
    return key


def iter_available_timeframes(
    source_interval: Timeframe | timedelta | str,
) -> Iterable[CanonicalTimeframe]:
    """Yield the frozen ladder entries representable from this input interval."""
    return (tf for tf in CANONICAL_LADDER if target_is_available(tf, source_interval))
