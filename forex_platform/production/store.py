"""
Durable persistence and audit trail for Forex Platform.

Uses SQLite in WAL mode because it is crash-safe, zero-dependency, and
sufficient for a single-node trading control plane. The schema is versioned;
`Database.migrate()` is idempotent and safe to run at every startup.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'viewer',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'viewer',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    account_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    display_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    account_id TEXT,
    broker TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    units INTEGER NOT NULL,
    limit_price TEXT,
    stop_loss TEXT,
    take_profit TEXT,
    status TEXT NOT NULL,
    filled_units INTEGER NOT NULL DEFAULT 0,
    average_price TEXT,
    broker_ticket TEXT,
    reason TEXT,
    strategy_id TEXT,
    tenant_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    broker_ticket TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    units INTEGER NOT NULL,
    price TEXT NOT NULL,
    commission TEXT NOT NULL DEFAULT '0',
    swap TEXT NOT NULL DEFAULT '0',
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    ip TEXT,
    request_id TEXT
);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_events (
    event_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    currency TEXT,
    impact TEXT,
    event_time TEXT NOT NULL,
    actual TEXT,
    forecast TEXT,
    previous TEXT,
    ingested_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS research_candidates (
    candidate_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    gates_json TEXT NOT NULL DEFAULT '{}',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    data_fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotions (
    promotion_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    from_stage TEXT NOT NULL,
    to_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_by TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(client_order_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_events(event_time);
CREATE INDEX IF NOT EXISTS idx_candidates_stage ON research_candidates(stage);
"""

def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")


def to_json(value: Any) -> str:
    return json.dumps(value, default=json_default, sort_keys=True)


def from_json(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thread-safe SQLite database with WAL, migrations, and transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path,
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def migrate(self) -> None:
        with self._write_lock:
            conn = self.connection()
            conn.executescript(SCHEMA_SQL)
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = int(row["v"] or 0) if row else 0
            if current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, utc_now()),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction. Rolls back on any exception."""
        with self._write_lock:
            conn = self.connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.lastrowid or cursor.rowcount or 0)

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        cursor = self.connection().execute(sql, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        cursor = self.connection().execute(sql, tuple(params))
        row = cursor.fetchone()
        return dict(row) if row else None

    def healthcheck(self) -> bool:
        try:
            self.connection().execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


class AuditLog:
    """Append-only audit trail for every privileged action."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        actor: str,
        role: str,
        action: str,
        target: str = "",
        details: Optional[Dict[str, Any]] = None,
        ip: str = "",
        request_id: str = "",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO audit_log(timestamp, actor, role, action, target, details_json, ip, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                actor or "anonymous",
                role or "none",
                action,
                target,
                to_json(details or {}),
                ip,
                request_id,
            ),
        )

    def list(
        self,
        limit: int = 100,
        actor: Optional[str] = None,
        action: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if action:
            clauses.append("action = ?")
            params.append(action)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
            params + [max(1, min(limit, 1000))],
        )
        for row in rows:
            row["details"] = from_json(row.pop("details_json"), {})
        return rows

    def purge_older_than(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
            return int(cursor.rowcount or 0)


class StateStore:
    """JSON key/value state used for risk snapshots and runtime markers."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            """
            INSERT INTO system_state(key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (key, to_json(value), utc_now()),
        )

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value_json FROM system_state WHERE key = ?", (key,))
        if not row:
            return default
        return from_json(row["value_json"], default)

    def all(self) -> Dict[str, Any]:
        return {
            row["key"]: from_json(row["value_json"])
            for row in self.db.query("SELECT key, value_json FROM system_state ORDER BY key")
        }


class AccountRepository:
    """Durable broker account registry.

    Disabled by default: an operator must explicitly enable an account before
    the execution service is allowed to consider it.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, record: Dict[str, Any]) -> None:
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO accounts(id, broker, account_id, environment, display_name, mode,
                                 is_enabled, config_json, created_at, updated_at)
            VALUES (:id, :broker, :account_id, :environment, :display_name, :mode,
                    :is_enabled, :config_json, :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
                broker=excluded.broker, account_id=excluded.account_id,
                environment=excluded.environment, display_name=excluded.display_name,
                mode=excluded.mode, is_enabled=excluded.is_enabled,
                config_json=excluded.config_json, updated_at=excluded.updated_at
            """,
            {
                "id": record["id"],
                "broker": record.get("broker", ""),
                "account_id": record.get("account_id", ""),
                "environment": record.get("environment", "paper"),
                "display_name": record.get("display_name", record["id"]),
                "mode": record.get("mode", "PAPER"),
                "is_enabled": 1 if record.get("is_enabled") else 0,
                "config_json": to_json(record.get("config", {})),
                "created_at": record.get("created_at", now),
                "updated_at": now,
            },
        )

    def set_enabled(self, account_id: str, enabled: bool) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE accounts SET is_enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, utc_now(), account_id),
            )
            return bool(cursor.rowcount)

    def list(self, include_disabled: bool = True) -> List[Dict[str, Any]]:
        clause = "" if include_disabled else "WHERE is_enabled = 1"
        rows = self.db.query(f"SELECT * FROM accounts {clause} ORDER BY id")
        for row in rows:
            row["config"] = from_json(row.pop("config_json"), {})
            row["is_enabled"] = bool(row["is_enabled"])
        return rows

    def get(self, account_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if not row:
            return None
        row["config"] = from_json(row.pop("config_json"), {})
        row["is_enabled"] = bool(row["is_enabled"])
        return row


class OrderRepository:
    """Durable order/fill ledger with idempotent upserts."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_order(self, order: Dict[str, Any]) -> None:
        now = utc_now()
        payload = {
            "client_order_id": order["client_order_id"],
            "intent_id": order.get("intent_id", ""),
            "account_id": order.get("account_id"),
            "broker": order.get("broker"),
            "symbol": order["symbol"],
            "side": order["side"],
            "order_type": order.get("order_type", "MARKET"),
            "units": int(order.get("units", 0)),
            "limit_price": str(order["limit_price"]) if order.get("limit_price") is not None else None,
            "stop_loss": str(order["stop_loss"]) if order.get("stop_loss") is not None else None,
            "take_profit": str(order["take_profit"]) if order.get("take_profit") is not None else None,
            "status": order.get("status", "PENDING"),
            "filled_units": int(order.get("filled_units", 0)),
            "average_price": str(order["average_price"]) if order.get("average_price") is not None else None,
            "broker_ticket": order.get("broker_ticket"),
            "reason": order.get("reason"),
            "strategy_id": order.get("strategy_id"),
            "tenant_id": order.get("tenant_id"),
            "created_at": order.get("created_at", now),
            "updated_at": now,
        }
        self.db.execute(
            """
            INSERT INTO orders(client_order_id, intent_id, account_id, broker, symbol, side,
                               order_type, units, limit_price, stop_loss, take_profit, status,
                               filled_units, average_price, broker_ticket, reason, strategy_id,
                               tenant_id, created_at, updated_at)
            VALUES (:client_order_id, :intent_id, :account_id, :broker, :symbol, :side,
                    :order_type, :units, :limit_price, :stop_loss, :take_profit, :status,
                    :filled_units, :average_price, :broker_ticket, :reason, :strategy_id,
                    :tenant_id, :created_at, :updated_at)
            ON CONFLICT(client_order_id) DO UPDATE SET
                status=excluded.status, filled_units=excluded.filled_units,
                average_price=excluded.average_price, broker_ticket=excluded.broker_ticket,
                reason=excluded.reason, updated_at=excluded.updated_at
            """,
            payload,
        )

    def get(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        return self.db.query_one(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        )

    def list(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            return self.db.query(
                "SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, max(1, min(limit, 1000))),
            )
        return self.db.query(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        )

    def add_fill(self, fill: Dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO fills(fill_id, client_order_id, broker_ticket, symbol, side, units,
                              price, commission, swap, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fill_id) DO NOTHING
            """,
            (
                fill["fill_id"],
                fill["client_order_id"],
                fill.get("broker_ticket"),
                fill["symbol"],
                fill["side"],
                int(fill["units"]),
                str(fill["price"]),
                str(fill.get("commission", "0")),
                str(fill.get("swap", "0")),
                fill.get("timestamp", utc_now()),
            ),
        )

    def list_fills(
        self, client_order_id: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        if client_order_id:
            return self.db.query(
                "SELECT * FROM fills WHERE client_order_id = ? ORDER BY timestamp DESC LIMIT ?",
                (client_order_id, max(1, min(limit, 1000))),
            )
        return self.db.query(
            "SELECT * FROM fills ORDER BY timestamp DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        )


class NewsRepository:
    """Cache of economic-calendar events used by the blackout policy."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_many(self, events: Iterable[Dict[str, Any]]) -> int:
        count = 0
        with self.db.transaction() as conn:
            for event in events:
                conn.execute(
                    """
                    INSERT INTO news_events(event_key, source, title, currency, impact, event_time,
                                            actual, forecast, previous, ingested_at, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO UPDATE SET
                        title=excluded.title, impact=excluded.impact, event_time=excluded.event_time,
                        actual=excluded.actual, forecast=excluded.forecast,
                        previous=excluded.previous, ingested_at=excluded.ingested_at,
                        raw_json=excluded.raw_json
                    """,
                    (
                        event["event_key"],
                        event.get("source", "unknown"),
                        event.get("title", ""),
                        event.get("currency"),
                        event.get("impact"),
                        event["event_time"],
                        event.get("actual"),
                        event.get("forecast"),
                        event.get("previous"),
                        utc_now(),
                        to_json(event.get("raw", {})),
                    ),
                )
                count += 1
        return count

    def list_between(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT * FROM news_events WHERE event_time >= ? AND event_time <= ?
            ORDER BY event_time ASC
            """,
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        )
        for row in rows:
            row["raw"] = from_json(row.pop("raw_json"), {})
        return rows

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM news_events")
        return int(row["n"]) if row else 0


class ResearchRepository:
    """Persistent registry for candidates, promotions, and champions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert_candidate(self, candidate: Dict[str, Any]) -> None:
        now = utc_now()
        self.db.execute(
            """
            INSERT INTO research_candidates(candidate_id, strategy_id, stage, metrics_json,
                                            gates_json, parameters_json, data_fingerprint,
                                            created_at, updated_at)
            VALUES (:candidate_id, :strategy_id, :stage, :metrics_json, :gates_json,
                    :parameters_json, :data_fingerprint, :created_at, :updated_at)
            ON CONFLICT(candidate_id) DO UPDATE SET
                stage=excluded.stage, metrics_json=excluded.metrics_json,
                gates_json=excluded.gates_json, parameters_json=excluded.parameters_json,
                data_fingerprint=excluded.data_fingerprint, updated_at=excluded.updated_at
            """,
            {
                "candidate_id": candidate["candidate_id"],
                "strategy_id": candidate["strategy_id"],
                "stage": candidate.get("stage", "CANDIDATE"),
                "metrics_json": to_json(candidate.get("metrics", {})),
                "gates_json": to_json(candidate.get("gates", {})),
                "parameters_json": to_json(candidate.get("parameters", {})),
                "data_fingerprint": candidate.get("data_fingerprint"),
                "created_at": candidate.get("created_at", now),
                "updated_at": now,
            },
        )

    @staticmethod
    def _decode_candidate(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        row["metrics"] = from_json(row.pop("metrics_json"), {})
        row["gates"] = from_json(row.pop("gates_json"), {})
        row["parameters"] = from_json(row.pop("parameters_json"), {})
        return row

    def get_candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        return self._decode_candidate(
            self.db.query_one(
                "SELECT * FROM research_candidates WHERE candidate_id = ?", (candidate_id,)
            )
        )

    def list_candidates(self, limit: int = 100, stage: Optional[str] = None) -> List[Dict[str, Any]]:
        if stage:
            rows = self.db.query(
                "SELECT * FROM research_candidates WHERE stage = ? ORDER BY updated_at DESC LIMIT ?",
                (stage, max(1, min(limit, 1000))),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM research_candidates ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            )
        return [self._decode_candidate(row) for row in rows if row]

    def record_promotion(self, promotion: Dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO promotions(promotion_id, candidate_id, strategy_id, from_stage,
                                   to_stage, status, approved_by, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                promotion["promotion_id"],
                promotion["candidate_id"],
                promotion["strategy_id"],
                promotion["from_stage"],
                promotion["to_stage"],
                promotion.get("status", "APPLIED"),
                promotion.get("approved_by"),
                promotion.get("notes"),
                promotion.get("created_at", utc_now()),
            ),
        )

    def list_promotions(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM promotions ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        )






