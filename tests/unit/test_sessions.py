"""
Unit tests for ForexSessionEngine: session schedules, overlaps, weekend closures,
rollovers, and Wednesday triple-swap settlement.
"""

from datetime import datetime, timezone
import pytest

from forex_platform.core.domain import SessionName
from forex_platform.core.sessions import ForexSessionEngine


class TestForexSessions:
    """Test individual session open/close boundaries and multi-session overlaps."""

    def test_sydney_tokyo_overlap(self):
        # Tuesday 02:00 UTC: Sydney (21:00-06:00) and Tokyo (00:00-09:00) are both open
        dt = datetime(2026, 1, 6, 2, 0, 0, tzinfo=timezone.utc)
        active = ForexSessionEngine.get_active_sessions(dt)
        assert SessionName.SYDNEY in active
        assert SessionName.TOKYO in active
        assert SessionName.LONDON not in active
        assert SessionName.NEW_YORK not in active

        state = ForexSessionEngine.get_session_state(dt)
        assert state.has_overlap
        assert state.overlap_type == "SYDNEY_TOKYO"

    def test_tokyo_london_overlap(self):
        # Tuesday 08:00 UTC: Tokyo (00:00-09:00) and London (07:00-16:00) are both open
        dt = datetime(2026, 1, 6, 8, 0, 0, tzinfo=timezone.utc)
        active = ForexSessionEngine.get_active_sessions(dt)
        assert SessionName.TOKYO in active
        assert SessionName.LONDON in active
        assert SessionName.NEW_YORK not in active

        state = ForexSessionEngine.get_session_state(dt)
        assert state.has_overlap
        assert state.overlap_type == "TOKYO_LONDON"

    def test_london_solo_session(self):
        # Tuesday 10:00 UTC: Only London is open
        dt = datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
        active = ForexSessionEngine.get_active_sessions(dt)
        assert active == [SessionName.LONDON]

        state = ForexSessionEngine.get_session_state(dt)
        assert not state.has_overlap
        assert state.regime_tag == "LONDON_SOLO"

    def test_london_new_york_peak_overlap(self):
        # Tuesday 14:00 UTC: London (07:00-16:00) and New York (12:00-21:00) overlap
        dt = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        active = ForexSessionEngine.get_active_sessions(dt)
        assert SessionName.LONDON in active
        assert SessionName.NEW_YORK in active

        state = ForexSessionEngine.get_session_state(dt)
        assert state.has_overlap
        assert state.overlap_type == "LONDON_NEW_YORK"
        assert state.regime_tag == "OVERLAP_LONDON_NY"

    def test_new_york_solo_session(self):
        # Tuesday 18:00 UTC: Only New York is open
        dt = datetime(2026, 1, 6, 18, 0, 0, tzinfo=timezone.utc)
        active = ForexSessionEngine.get_active_sessions(dt)
        assert active == [SessionName.NEW_YORK]

        state = ForexSessionEngine.get_session_state(dt)
        assert not state.has_overlap
        assert state.regime_tag == "NEW_YORK_SOLO"


class TestWeekendClosure:
    """Test Friday 21:00 UTC close through Sunday 21:00 UTC open."""

    def test_friday_before_close_is_open(self):
        # Friday 2026-01-09 20:59:00 UTC -> market is open
        dt = datetime(2026, 1, 9, 20, 59, 0, tzinfo=timezone.utc)
        assert not ForexSessionEngine.is_weekend(dt)
        assert ForexSessionEngine.is_market_open(dt)

    def test_friday_at_close_is_weekend(self):
        # Friday 2026-01-09 21:00:00 UTC -> market closes
        dt = datetime(2026, 1, 9, 21, 0, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_weekend(dt)
        assert not ForexSessionEngine.is_market_open(dt)
        assert ForexSessionEngine.get_active_sessions(dt) == []
        assert ForexSessionEngine.get_session_state(dt).regime_tag == "WEEKEND_CLOSED"

    def test_saturday_all_day_is_weekend(self):
        # Saturday 2026-01-10 12:00:00 UTC
        dt = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_weekend(dt)
        assert not ForexSessionEngine.is_market_open(dt)

    def test_sunday_before_reopen_is_weekend(self):
        # Sunday 2026-01-11 20:59:00 UTC -> still closed
        dt = datetime(2026, 1, 11, 20, 59, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_weekend(dt)
        assert not ForexSessionEngine.is_market_open(dt)

    def test_sunday_at_reopen_is_open(self):
        # Sunday 2026-01-11 21:00:00 UTC -> Sydney opens the trading week!
        dt = datetime(2026, 1, 11, 21, 0, 0, tzinfo=timezone.utc)
        assert not ForexSessionEngine.is_weekend(dt)
        assert ForexSessionEngine.is_market_open(dt)
        active = ForexSessionEngine.get_active_sessions(dt)
        assert SessionName.SYDNEY in active


class TestRolloverAndTripleSwap:
    """Test 20:55 to 21:15 UTC rollover and Wednesday triple swap trigger."""

    def test_rollover_window_boundaries(self):
        # Tuesday 2026-01-06 20:54:59 -> Not rollover
        dt_pre = datetime(2026, 1, 6, 20, 54, 59, tzinfo=timezone.utc)
        assert not ForexSessionEngine.is_rollover(dt_pre)

        # Tuesday 20:55:00 -> Rollover window begins
        dt_start = datetime(2026, 1, 6, 20, 55, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_rollover(dt_start)

        # Tuesday 21:00:00 -> Middle of rollover
        dt_mid = datetime(2026, 1, 6, 21, 0, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_rollover(dt_mid)

        # Tuesday 21:15:00 -> End of rollover
        dt_end = datetime(2026, 1, 6, 21, 15, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_rollover(dt_end)

        # Tuesday 21:15:01 -> Post-rollover normal trading
        dt_post = datetime(2026, 1, 6, 21, 15, 1, tzinfo=timezone.utc)
        assert not ForexSessionEngine.is_rollover(dt_post)

    def test_weekend_has_no_rollover(self):
        # Saturday 2026-01-10 21:00:00 -> Weekend, rollover is suppressed
        dt_sat = datetime(2026, 1, 10, 21, 0, 0, tzinfo=timezone.utc)
        assert not ForexSessionEngine.is_rollover(dt_sat)

    def test_wednesday_triple_swap_detector(self):
        # Wednesday 2026-01-07 21:00:00 UTC (weekday == 2)
        dt_wed_rollover = datetime(2026, 1, 7, 21, 0, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_triple_swap(dt_wed_rollover)

        # Window mode: Wednesday 20:55 UTC
        dt_wed_win = datetime(2026, 1, 7, 20, 55, 0, tzinfo=timezone.utc)
        assert ForexSessionEngine.is_triple_swap(dt_wed_win, window_mode=True)
        assert not ForexSessionEngine.is_triple_swap(dt_wed_win, window_mode=False)

        # Tuesday 2026-01-06 21:00:00 UTC -> regular rollover, not triple swap
        dt_tue = datetime(2026, 1, 6, 21, 0, 0, tzinfo=timezone.utc)
        assert not ForexSessionEngine.is_triple_swap(dt_tue)
        assert not ForexSessionEngine.is_triple_swap(dt_tue, window_mode=True)

        # Thursday 2026-01-08 21:00:00 UTC -> regular rollover, not triple swap
        dt_thu = datetime(2026, 1, 8, 21, 0, 0, tzinfo=timezone.utc)
        assert not ForexSessionEngine.is_triple_swap(dt_thu)
