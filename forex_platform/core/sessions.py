"""
Forex 24/5 session state machine and temporal event detectors.
Tracks Sydney, Tokyo, London, and New York sessions, overlaps, weekend closures,
bank rollovers, and Wednesday triple-swap settlement.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import List
from pydantic import BaseModel, ConfigDict

from forex_platform.core.domain import SessionName


class ForexSessionState(BaseModel):
    """
    Snapshot of global Forex market state at a specific point in time.
    """
    model_config = ConfigDict(frozen=True)

    dt: datetime
    is_market_open: bool
    is_weekend: bool
    is_rollover: bool
    is_triple_swap: bool
    active_sessions: List[SessionName]
    has_overlap: bool
    overlap_type: str | None = None
    regime_tag: str


class ForexSessionEngine:
    """
    Engine responsible for detecting active forex trading sessions and regime boundaries.
    All times are evaluated in UTC.

    Sessions:
    - Sydney:   21:00 - 06:00 UTC
    - Tokyo:    00:00 - 09:00 UTC
    - London:   07:00 - 16:00 UTC
    - New York: 12:00 - 21:00 UTC

    Special Windows:
    - Weekend:      Friday 21:00 UTC through Sunday 21:00 UTC
    - Rollover:     20:55 to 21:15 UTC (illiquid clearing regime, high spreads)
    - Triple Swap:  Wednesday 21:00 UTC (T+2 weekend settlement)
    """

    SYDNEY_OPEN = time(21, 0, 0)
    SYDNEY_CLOSE = time(6, 0, 0)

    TOKYO_OPEN = time(0, 0, 0)
    TOKYO_CLOSE = time(9, 0, 0)

    LONDON_OPEN = time(7, 0, 0)
    LONDON_CLOSE = time(16, 0, 0)

    NEW_YORK_OPEN = time(12, 0, 0)
    NEW_YORK_CLOSE = time(21, 0, 0)

    ROLLOVER_START = time(20, 55, 0)
    ROLLOVER_END = time(21, 15, 0)

    @classmethod
    def _ensure_utc(cls, dt: datetime) -> datetime:
        """Ensure datetime is UTC."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @classmethod
    def is_weekend(cls, dt: datetime) -> bool:
        """
        Detect whether the forex market is closed for the weekend.
        Weekend starts Friday 21:00 UTC and ends Sunday 21:00 UTC.
        """
        utc_dt = cls._ensure_utc(dt)
        weekday = utc_dt.weekday()  # Monday is 0, Sunday is 6
        t = utc_dt.time()

        # Friday after 21:00 UTC
        if weekday == 4 and t >= time(21, 0, 0):
            return True
        # Saturday all day
        if weekday == 5:
            return True
        # Sunday before 21:00 UTC
        if weekday == 6 and t < time(21, 0, 0):
            return True

        return False

    @classmethod
    def is_market_open(cls, dt: datetime) -> bool:
        """Market is open outside the weekend boundary."""
        return not cls.is_weekend(dt)

    @classmethod
    def is_rollover(cls, dt: datetime) -> bool:
        """
        Detects if current time falls in the daily bank rollover window (20:55 - 21:15 UTC).
        Note: Rollover occurs on active market days (not during weekend closed market).
        """
        utc_dt = cls._ensure_utc(dt)
        if cls.is_weekend(utc_dt):
            return False

        t = utc_dt.time()
        weekday = utc_dt.weekday()

        # On Friday, market closes at 21:00 UTC, so Friday rollover window is 20:55 to 21:00 UTC
        if weekday == 4:
            return cls.ROLLOVER_START <= t < time(21, 0, 0)

        # Standard non-weekend days: 20:55 to 21:15 UTC
        return cls.ROLLOVER_START <= t <= cls.ROLLOVER_END

    @classmethod
    def is_triple_swap(cls, dt: datetime, window_mode: bool = False) -> bool:
        """
        Detects Wednesday triple-swap settlement.
        If window_mode is True, flags True during the entire Wednesday rollover window (20:55-21:15 UTC).
        If window_mode is False, flags True at or past 21:00 UTC on Wednesday up to 21:15 UTC.
        """
        utc_dt = cls._ensure_utc(dt)
        if utc_dt.weekday() != 2:  # Wednesday is 2
            return False

        t = utc_dt.time()
        if window_mode:
            return cls.ROLLOVER_START <= t <= cls.ROLLOVER_END
        return time(21, 0, 0) <= t <= cls.ROLLOVER_END

    @classmethod
    def get_active_sessions(cls, dt: datetime) -> list[SessionName]:
        """
        Returns list of active major sessions at given datetime.
        If weekend, returns an empty list.
        """
        utc_dt = cls._ensure_utc(dt)
        if cls.is_weekend(utc_dt):
            return []

        t = utc_dt.time()
        active: list[SessionName] = []

        # Sydney: 21:00 to 06:00 UTC
        if t >= cls.SYDNEY_OPEN or t < cls.SYDNEY_CLOSE:
            active.append(SessionName.SYDNEY)

        # Tokyo: 00:00 to 09:00 UTC
        if cls.TOKYO_OPEN <= t < cls.TOKYO_CLOSE:
            active.append(SessionName.TOKYO)

        # London: 07:00 to 16:00 UTC
        if cls.LONDON_OPEN <= t < cls.LONDON_CLOSE:
            active.append(SessionName.LONDON)

        # New York: 12:00 to 21:00 UTC
        if cls.NEW_YORK_OPEN <= t < cls.NEW_YORK_CLOSE:
            active.append(SessionName.NEW_YORK)

        return active

    @classmethod
    def get_session_state(cls, dt: datetime) -> ForexSessionState:
        """
        Computes composite session state and regime tag for quantitative risk and spread models.
        """
        utc_dt = cls._ensure_utc(dt)
        weekend = cls.is_weekend(utc_dt)
        open_market = not weekend
        rollover = cls.is_rollover(utc_dt)
        triple_swap = cls.is_triple_swap(utc_dt, window_mode=True)
        active = cls.get_active_sessions(utc_dt)

        has_overlap = len(active) > 1
        overlap_type: str | None = None

        if SessionName.LONDON in active and SessionName.NEW_YORK in active:
            overlap_type = "LONDON_NEW_YORK"
        elif SessionName.SYDNEY in active and SessionName.TOKYO in active:
            overlap_type = "SYDNEY_TOKYO"
        elif SessionName.TOKYO in active and SessionName.LONDON in active:
            overlap_type = "TOKYO_LONDON"
        elif has_overlap:
            overlap_type = "_".join(s.value for s in active)

        # Determine regime tag for cost and volatility engines
        if weekend:
            regime = "WEEKEND_CLOSED"
        elif rollover:
            regime = "ROLLOVER_TRIPLE_SWAP" if triple_swap else "ROLLOVER"
        elif overlap_type == "LONDON_NEW_YORK":
            regime = "OVERLAP_LONDON_NY"
        elif overlap_type == "TOKYO_LONDON":
            regime = "OVERLAP_TOKYO_LONDON"
        elif overlap_type == "SYDNEY_TOKYO":
            regime = "OVERLAP_SYDNEY_TOKYO"
        elif SessionName.LONDON in active:
            regime = "LONDON_SOLO"
        elif SessionName.NEW_YORK in active:
            regime = "NEW_YORK_SOLO"
        elif SessionName.TOKYO in active or SessionName.SYDNEY in active:
            regime = "ASIAN_SESSION"
        else:
            regime = "QUIET_HOURS"

        return ForexSessionState(
            dt=utc_dt,
            is_market_open=open_market,
            is_weekend=weekend,
            is_rollover=rollover,
            is_triple_swap=triple_swap,
            active_sessions=active,
            has_overlap=has_overlap,
            overlap_type=overlap_type,
            regime_tag=regime,
        )
