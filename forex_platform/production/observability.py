"""
Structured logging, metrics, and health registry for Forex Platform.

No third-party observability dependency is required: logs are JSON, metrics
render in Prometheus text format, and health is exposed as component state.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "pepper",
    "client_secret",
    "session",
}


def _redact(value: Any, key: str = "") -> Any:
    if key and any(s in key.lower() for s in _SENSITIVE_KEYS):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON with redaction."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "actor", "account_id", "strategy_id", "order_id", "component"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload["fields"] = _redact(extra)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure the root logger once, without duplicating handlers."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log with structured fields, redacting sensitive keys."""
    logger.log(level, message, extra={"extra_fields": _redact(fields)})


class _Histogram:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum: Optional[float] = None
        self.maximum: Optional[float] = None
        self.buckets: Dict[float, int] = defaultdict(int)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        for bound in (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0):
            if value <= bound:
                self.buckets[bound] += 1


class MetricsRegistry:
    """Thread-safe counters, gauges, and histograms with Prometheus export."""

    def __init__(self) -> None:
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, _Histogram] = defaultdict(_Histogram)
        self._lock = threading.Lock()
        self._started = time.time()

    @staticmethod
    def _key(name: str, labels: Optional[Dict[str, str]]) -> str:
        if not labels:
            return name
        label_text = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_text}}}"

    def increment(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = float(value)

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self._histograms[self._key(name, labels)].observe(float(value))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": time.time() - self._started,
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    key: {
                        "count": hist.count,
                        "total": hist.total,
                        "min": hist.minimum,
                        "max": hist.maximum,
                    }
                    for key, hist in self._histograms.items()
                },
            }

    def render_prometheus(self) -> str:
        lines: List[str] = []
        with self._lock:
            for name, value in sorted(self._counters.items()):
                lines.append(f"# TYPE {name.split('{')[0]} counter")
                lines.append(f"{name} {value}")
            for name, value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name.split('{')[0]} gauge")
                lines.append(f"{name} {value}")
            for name, hist in sorted(self._histograms.items()):
                base = name.split("{")[0]
                lines.append(f"# TYPE {base} histogram")
                cumulative = 0
                for bound, count in sorted(hist.buckets.items()):
                    cumulative += count
                    if "{" in name:
                        label_part = name[name.index("{") + 1 : -1]
                        lines.append(f'{base}_bucket{{{label_part},le="{bound}"}} {cumulative}')
                    else:
                        lines.append(f'{base}_bucket{{le="{bound}"}} {cumulative}')
                lines.append(f"{base}_count {hist.count}")
                lines.append(f"{base}_sum {hist.total}")
        lines.append(f"forex_platform_uptime_seconds {time.time() - self._started}")
        return "\n".join(lines) + "\n"


class HealthRegistry:
    """Tracks readiness/liveness of named components."""

    def __init__(self) -> None:
        self._components: Dict[str, Tuple[bool, str, float]] = {}
        self._lock = threading.Lock()

    def set(self, name: str, healthy: bool, detail: str = "") -> None:
        with self._lock:
            self._components[name] = (healthy, detail, time.time())

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            components = {
                name: {
                    "healthy": healthy,
                    "detail": detail,
                    "updated_at": updated_at,
                }
                for name, (healthy, detail, updated_at) in self._components.items()
            }
        return {
            "live": True,
            "ready": all(item["healthy"] for item in components.values()) if components else False,
            "components": components,
        }


class Timer:
    """Context manager that observes duration in seconds."""

    def __init__(self, metrics: MetricsRegistry, name: str, labels: Optional[Dict[str, str]] = None):
        self.metrics = metrics
        self.name = name
        self.labels = labels
        self.start = 0.0

    def __enter__(self) -> "Timer":
        self.start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.metrics.observe(self.name, time.monotonic() - self.start, self.labels)


class RateLimiter:
    """Simple thread-safe per-identity fixed-window rate limiter."""

    def __init__(self, limit_per_minute: int) -> None:
        self.limit = max(1, limit_per_minute)
        self._windows: Dict[str, Tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, identity: str) -> bool:
        now = time.time()
        with self._lock:
            window_start, count = self._windows.get(identity, (now, 0))
            if now - window_start >= 60.0:
                window_start, count = now, 0
            count += 1
            self._windows[identity] = (window_start, count)
            return count <= self.limit

