"""
Economic calendar, news blackout policy, and fundamentals context.

Providers are pluggable so a licensed feed can be added without touching the
risk path. The blackout policy is consumed by `production.execution.RiskKernel`
and fails closed for high-impact events on the pair's currencies.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import to_decimal
from forex_platform.production.observability import MetricsRegistry, get_logger
from forex_platform.production.settings import Settings
from forex_platform.production.store import NewsRepository, StateStore, utc_now

logger = get_logger(__name__)


class Impact(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class NewsEvent(BaseModel):
    """Normalized economic-calendar event."""

    model_config = ConfigDict(frozen=True)

    event_key: str
    title: str
    currency: Optional[str] = None
    impact: str = "LOW"
    event_time: datetime
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None
    source: str = "unknown"
    raw: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        title: str,
        currency: Optional[str],
        impact: str,
        event_time: datetime,
        *,
        source: str = "unknown",
        actual: Optional[str] = None,
        forecast: Optional[str] = None,
        previous: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> "NewsEvent":
        key_src = f"{source}|{currency or ''}|{title}|{event_time.astimezone(timezone.utc).isoformat()}"
        event_key = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:32]
        return cls(
            event_key=event_key,
            title=title,
            currency=(currency or "").upper() or None,
            impact=(impact or "LOW").upper(),
            event_time=event_time,
            actual=actual,
            forecast=forecast,
            previous=previous,
            source=source,
            raw=raw or {},
        )


class NewsBlackout(BaseModel):
    """Active blackout window with the events that triggered it."""

    model_config = ConfigDict(frozen=True)

    reason: str
    starts_at: datetime
    ends_at: datetime
    events: List[Dict[str, Any]] = Field(default_factory=list)


class NewsProvider(ABC):
    """Source of economic-calendar events."""

    @abstractmethod
    def fetch(self) -> List[NewsEvent]:
        raise NotImplementedError


class StaticNewsProvider(NewsProvider):
    def __init__(self, events: Iterable[NewsEvent]) -> None:
        self.events = list(events)

    def fetch(self) -> List[NewsEvent]:
        return list(self.events)


class JSONFileNewsProvider(NewsProvider):
    """Reads a JSON array or newline-delimited JSON objects from a file/dir."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _parse(self, payload: Any) -> List[NewsEvent]:
        entries = payload.get("events", payload) if isinstance(payload, dict) else payload
        events: List[NewsEvent] = []
        for entry in entries or []:
            event_time = entry.get("event_time") or entry.get("time") or entry.get("date")
            if not event_time:
                continue
            parsed_time = datetime.fromisoformat(str(event_time).replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                parsed_time = parsed_time.replace(tzinfo=timezone.utc)
            events.append(
                NewsEvent.create(
                    title=str(entry.get("title", entry.get("event", "unknown"))),
                    currency=entry.get("currency"),
                    impact=str(entry.get("impact", "LOW")),
                    event_time=parsed_time,
                    source=str(entry.get("source", self.path.stem)),
                    actual=entry.get("actual"),
                    forecast=entry.get("forecast"),
                    previous=entry.get("previous"),
                    raw=entry,
                )
            )
        return events

    def fetch(self) -> List[NewsEvent]:
        events: List[NewsEvent] = []
        targets: List[Path] = []
        if self.path.is_dir():
            targets = sorted(self.path.glob("*.json"))
        elif self.path.exists():
            targets = [self.path]
        for target in targets:
            try:
                for line in target.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    events.extend(self._parse(json.loads(line)))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Failed to read news provider %s: %s", target, exc)
        return events


class HTTPJSONNewsProvider(NewsProvider):
    """Fetches events from an authenticated JSON HTTP endpoint."""

    def __init__(self, url: str, token: Optional[str] = None, timeout: int = 20) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout

    def fetch(self) -> List[NewsEvent]:
        request = urllib.request.Request(self.url, method="GET")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "forex-platform/1.0")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - feed outage must not crash the daemon
            logger.error("News provider HTTP failure: %s", exc)
            return []
        entries = payload.get("events", payload) if isinstance(payload, dict) else payload
        events: List[NewsEvent] = []
        for entry in entries or []:
            event_time = entry.get("event_time") or entry.get("time") or entry.get("date")
            if not event_time:
                continue
            parsed_time = datetime.fromisoformat(str(event_time).replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                parsed_time = parsed_time.replace(tzinfo=timezone.utc)
            events.append(
                NewsEvent.create(
                    title=str(entry.get("title", entry.get("event", "unknown"))),
                    currency=entry.get("currency"),
                    impact=str(entry.get("impact", "LOW")),
                    event_time=parsed_time,
                    source=str(entry.get("source", urllib.parse.urlparse(self.url).netloc)),
                    actual=entry.get("actual"),
                    forecast=entry.get("forecast"),
                    previous=entry.get("previous"),
                    raw=entry,
                )
            )
        return events


class NewsCalendarService:
    """Caches calendar events and answers blackout queries."""

    def __init__(
        self,
        settings: Settings,
        repo: NewsRepository,
        provider: Optional[NewsProvider] = None,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.provider = provider
        self.metrics = metrics or MetricsRegistry()
        self.last_refresh: Optional[datetime] = None
        self.last_error: Optional[str] = None

    def build_provider(self) -> Optional[NewsProvider]:
        kind = (self.settings.news_provider_kind or "disabled").lower()
        if kind == "disabled":
            return None
        if kind == "http" and self.settings.news_provider_url:
            return HTTPJSONNewsProvider(
                self.settings.news_provider_url, self.settings.news_provider_token
            )
        if kind == "file":
            candidate = Path(self.settings.data_dir) / "news"
            if candidate.exists():
                return JSONFileNewsProvider(candidate)
            return JSONFileNewsProvider(Path(self.settings.data_dir) / "news.json")
        return None

    def refresh(self) -> int:
        provider = self.provider or self.build_provider()
        if provider is None:
            self.last_error = "No news provider configured."
            return 0
        events = provider.fetch()
        if not events:
            self.last_error = "Provider returned no events."
            return 0
        count = self.repo.upsert_many(
            [
                {
                    "event_key": event.event_key,
                    "source": event.source,
                    "title": event.title,
                    "currency": event.currency,
                    "impact": event.impact,
                    "event_time": event.event_time.astimezone(timezone.utc).isoformat(),
                    "actual": event.actual,
                    "forecast": event.forecast,
                    "previous": event.previous,
                    "raw": event.raw,
                }
                for event in events
            ]
        )
        self.last_refresh = datetime.now(timezone.utc)
        self.last_error = None
        self.metrics.set_gauge("forex_news_events_cached", self.repo.count())
        return count

    def upcoming(self, hours: int = 24) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        rows = self.repo.list_between(now, now + timedelta(hours=max(1, hours)))
        for row in rows:
            event_time = datetime.fromisoformat(row["event_time"])
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            row["minutes_until"] = round((event_time - now).total_seconds() / 60.0, 1)
        return rows

    def blackout_for(self, symbol: str, at: Optional[datetime] = None) -> Optional[NewsBlackout]:
        clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        if len(clean) < 6:
            return None
        currencies = {clean[:3], clean[3:6]}
        moment = at or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        window_start = moment - timedelta(minutes=self.settings.news_blackout_minutes_after)
        window_end = moment + timedelta(minutes=self.settings.news_blackout_minutes_before)
        rows = self.repo.list_between(window_start, window_end)
        impacts = {impact.upper() for impact in self.settings.news_blackout_impacts}
        triggering = [
            row
            for row in rows
            if (row.get("currency") or "").upper() in currencies
            and (row.get("impact") or "").upper() in impacts
        ]
        if not triggering:
            return None
        first = triggering[0]
        event_time = datetime.fromisoformat(first["event_time"])
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        return NewsBlackout(
            reason=(
                f"{first['impact']} impact event '{first['title']}' ({first['currency']}) "
                f"at {first['event_time']}"
            ),
            starts_at=event_time - timedelta(minutes=self.settings.news_blackout_minutes_before),
            ends_at=event_time + timedelta(minutes=self.settings.news_blackout_minutes_after),
            events=triggering,
        )

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.settings.news_enabled,
            "provider_kind": self.settings.news_provider_kind,
            "cached_events": self.repo.count(),
            "last_refresh": self.last_refresh.isoformat() if self.last_refresh else None,
            "last_error": self.last_error,
            "blackout_impacts": list(self.settings.news_blackout_impacts),
            "blackout_minutes_before": self.settings.news_blackout_minutes_before,
            "blackout_minutes_after": self.settings.news_blackout_minutes_after,
        }


class FundamentalRate(BaseModel):
    """Policy-rate context for a single currency."""

    model_config = ConfigDict(frozen=True)

    currency: str
    policy_rate: Decimal
    as_of: datetime
    source: str = "manual"

    @property
    def rate(self) -> Decimal:
        return self.policy_rate


class FundamentalsService:
    """Stores policy-rate context used by macro/carry strategies."""

    KEY = "fundamentals.rates"

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def update(self, rates: Iterable[FundamentalRate]) -> None:
        payload = {
            rate.currency.upper(): {
                "policy_rate": str(rate.policy_rate),
                "as_of": rate.as_of.astimezone(timezone.utc).isoformat(),
                "source": rate.source,
            }
            for rate in rates
        }
        self.state.set(self.KEY, payload)

    def get_rate(self, currency: str) -> Optional[Decimal]:
        payload = self.state.get(self.KEY, {}) or {}
        entry = payload.get(currency.upper())
        return to_decimal(entry["policy_rate"]) if entry else None

    def rate_differential(self, base_currency: str, quote_currency: str) -> Optional[Decimal]:
        base = self.get_rate(base_currency)
        quote = self.get_rate(quote_currency)
        if base is None or quote is None:
            return None
        return base - quote

    def snapshot(self) -> Dict[str, Any]:
        return self.state.get(self.KEY, {}) or {}
