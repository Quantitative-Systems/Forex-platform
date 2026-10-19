"""Economic News Calendar with 30-Minute Pre/Post Blackout."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass(frozen=True)
class NewsEvent:
    timestamp: datetime
    currency: str
    impact: str
    name: str


class NewsCalendar:
    def __init__(self, blackout_minutes: int = 30):
        self.blackout_minutes = blackout_minutes
        self._events: list[NewsEvent] = self._seed_events()

    def is_blackout(
        self,
        timestamp: datetime,
        currencies: Optional[list[str]] = None,
        min_impact: str = "HIGH",
    ) -> bool:
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        impact_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        min_rank = impact_order.get(min_impact, 2)
        for event in self._events:
            if impact_order.get(event.impact, 0) < min_rank:
                continue
            if currencies and event.currency not in currencies:
                continue
            delta = abs((ts - event.timestamp).total_seconds()) / 60.0
            if delta <= self.blackout_minutes:
                return True
        return False

    def next_event(
        self,
        timestamp: datetime,
        currencies: Optional[list[str]] = None,
    ) -> Optional[NewsEvent]:
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        future = [
            e for e in self._events
            if e.timestamp > ts
            and (not currencies or e.currency in currencies)
        ]
        return min(future, key=lambda e: e.timestamp) if future else None

    def _seed_events(self) -> list[NewsEvent]:
        events: list[NewsEvent] = []

        def add(year: int, month: int, day: int, hour: int, curr: str, name: str) -> None:
            events.append(
                NewsEvent(
                    timestamp=datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc),
                    currency=curr,
                    impact="HIGH",
                    name=name,
                )
            )

        # Known 2026 FOMC dates (19:00 UTC)
        add(2026, 1, 28, 19, "USD", "FOMC Rate Decision")
        add(2026, 3, 18, 19, "USD", "FOMC Rate Decision")
        add(2026, 5, 6, 19, "USD", "FOMC Rate Decision")
        add(2026, 6, 10, 19, "USD", "FOMC Rate Decision")
        add(2026, 7, 29, 19, "USD", "FOMC Rate Decision")
        add(2026, 9, 16, 19, "USD", "FOMC Rate Decision")
        add(2026, 10, 28, 19, "USD", "FOMC Rate Decision")
        add(2026, 12, 9, 19, "USD", "FOMC Rate Decision")

        # NFP — First Friday of each month, 13:30 UTC
        for yr in (2025, 2026):
            for m in range(1, 13):
                first_day = datetime(yr, m, 1, tzinfo=timezone.utc)
                days_to_friday = (4 - first_day.weekday()) % 7
                nfp_day = first_day.day + days_to_friday
                events.append(
                    NewsEvent(
                        timestamp=datetime(yr, m, nfp_day, 13, 30, 0, tzinfo=timezone.utc),
                        currency="USD",
                        impact="HIGH",
                        name="Non-Farm Payrolls",
                    )
                )

        # CPI — approx mid-month, 13:30 UTC
        for yr in (2025, 2026):
            for m in range(1, 13):
                add(yr, m, 15, 13, "USD", "CPI Release")

        # ECB rate decisions
        add(2026, 1, 23, 12, "EUR", "ECB Rate Decision")
        add(2026, 3, 6, 12, "EUR", "ECB Rate Decision")
        add(2026, 5, 8, 12, "EUR", "ECB Rate Decision")
        add(2026, 7, 17, 12, "EUR", "ECB Rate Decision")
        add(2026, 9, 10, 12, "EUR", "ECB Rate Decision")
        add(2026, 12, 17, 12, "EUR", "ECB Rate Decision")

        # BOE rate decisions
        add(2026, 2, 6, 12, "GBP", "BOE Rate Decision")
        add(2026, 3, 20, 12, "GBP", "BOE Rate Decision")
        add(2026, 5, 8, 12, "GBP", "BOE Rate Decision")
        add(2026, 6, 19, 12, "GBP", "BOE Rate Decision")
        add(2026, 8, 7, 12, "GBP", "BOE Rate Decision")
        add(2026, 9, 18, 12, "GBP", "BOE Rate Decision")
        add(2026, 11, 6, 12, "GBP", "BOE Rate Decision")
        add(2026, 12, 17, 12, "GBP", "BOE Rate Decision")

        # BOJ rate decisions
        add(2026, 1, 23, 3, "JPY", "BOJ Rate Decision")
        add(2026, 3, 7, 3, "JPY", "BOJ Rate Decision")
        add(2026, 5, 9, 3, "JPY", "BOJ Rate Decision")
        add(2026, 7, 11, 3, "JPY", "BOJ Rate Decision")
        add(2026, 9, 19, 3, "JPY", "BOJ Rate Decision")
        add(2026, 11, 21, 3, "JPY", "BOJ Rate Decision")

        return events
