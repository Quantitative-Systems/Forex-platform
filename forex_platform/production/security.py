"""
Authentication, authorization, and live-trading capital gating.

Design rules:
- Secrets are only ever stored as scrypt/HMAC hashes.
- API keys are shown once at creation and never persisted in plaintext.
- Sessions are random bearer tokens stored hashed with an expiry.
- Live trading is denied unless every independent condition passes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict

from forex_platform.core.domain import to_decimal
from forex_platform.production.observability import get_logger
from forex_platform.production.settings import LIVE_ACK_PHRASE, Settings
from forex_platform.production.store import Database, StateStore, utc_now

logger = get_logger(__name__)

ROLE_LEVELS: Dict[str, int] = {"viewer": 1, "trader": 2, "operator": 2, "admin": 3}


class AuthError(RuntimeError):
    """Authentication failed."""


class AuthorizationError(RuntimeError):
    """Authenticated principal lacks the required role/permission."""


class LiveTradingDenied(RuntimeError):
    """Live trading authorization failed closed."""


class Principal(BaseModel):
    """Authenticated identity attached to every request."""

    model_config = ConfigDict(frozen=True)

    actor: str
    role: str
    method: str
    api_key_name: Optional[str] = None
    session_expires_at: Optional[datetime] = None

    @property
    def level(self) -> int:
        return ROLE_LEVELS.get(self.role, 0)

    def has_role(self, required: str) -> bool:
        return self.level >= ROLE_LEVELS.get(required, 99)


def _scrypt_hash(secret: str, salt: Optional[bytes] = None) -> str:
    salt_bytes = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        secret.encode("utf-8"), salt=salt_bytes, n=2**14, r=8, p=1, dklen=32
    )
    return f"scrypt$16384$8$1${salt_bytes.hex()}${digest.hex()}"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    return _scrypt_hash(password)


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def generate_api_key() -> str:
    return f"fp_{secrets.token_urlsafe(32)}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_or_create_session_secret(settings: Settings) -> str:
    if settings.session_secret:
        return settings.session_secret
    path = Path(settings.session_secret_file)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return secret


class AuthService:
    """API-key, session, and password authentication with RBAC."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.state = StateStore(db)
        pepper = settings.api_key_pepper or load_or_create_session_secret(settings)
        self._pepper = pepper.encode("utf-8")
        self._session_secret = load_or_create_session_secret(settings).encode("utf-8")
        self.bootstrap()

    def _hash_api_key(self, api_key: str) -> str:
        return hmac.new(self._pepper, api_key.encode("utf-8"), hashlib.sha256).hexdigest()

    def bootstrap(self) -> None:
        """Register the bootstrap admin key exactly once."""
        key = self.settings.bootstrap_admin_key
        if not key:
            return
        key_hash = self._hash_api_key(key)
        exists = self.db.query_one("SELECT id FROM api_keys WHERE key_hash = ?", (key_hash,))
        if not exists:
            self.db.execute(
                "INSERT INTO api_keys(name, key_hash, role, created_at) VALUES (?, ?, ?, ?)",
                ("bootstrap-admin", key_hash, "admin", utc_now()),
            )
            logger.info("Bootstrap admin API key registered")

    def register_api_key(self, name: str, role: str = "viewer") -> str:
        role = role.lower()
        if role not in ROLE_LEVELS:
            raise ValueError(f"Unknown role {role!r}. Valid roles: {sorted(ROLE_LEVELS)}")
        api_key = generate_api_key()
        self.db.execute(
            "INSERT INTO api_keys(name, key_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (name, self._hash_api_key(api_key), role, utc_now()),
        )
        return api_key

    def revoke_api_key(self, key_id: int) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (utc_now(), key_id),
            )
            return bool(cursor.rowcount)

    def list_api_keys(self) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, name, role, created_at, last_used_at, revoked_at FROM api_keys ORDER BY id"
        )
        for row in rows:
            row["revoked"] = bool(row["revoked_at"])
        return rows

    def _lookup_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        key_hash = self._hash_api_key(api_key)
        row = self.db.query_one("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
        if not row or row["revoked_at"]:
            return None
        return row

    def issue_session(self, api_key: str, ttl_seconds: Optional[int] = None) -> Tuple[str, datetime]:
        row = self._lookup_api_key(api_key)
        if not row:
            raise AuthError("Invalid or revoked API key.")
        ttl = int(ttl_seconds or self.settings.session_ttl_seconds)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        self.db.execute(
            "INSERT INTO sessions(token_hash, username, role, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (_token_hash(token), row["name"], row["role"], utc_now(), expires_at.isoformat()),
        )
        self.db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (utc_now(), row["id"]))
        return token, expires_at

    def revoke_session(self, token: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (utc_now(), _token_hash(token)),
            )
            return bool(cursor.rowcount)

    def cleanup_sessions(self) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE expires_at < ? OR revoked_at IS NOT NULL",
                (utc_now(),),
            )
            return int(cursor.rowcount or 0)

    def _lookup_session(self, token: str) -> Optional[Dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM sessions WHERE token_hash = ?", (_token_hash(token),)
        )
        if not row or row["revoked_at"]:
            return None
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return None
        return row

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        normalized = {str(k).lower(): v for k, v in headers.items()}
        api_key = normalized.get("x-api-key", "").strip()
        authorization = normalized.get("authorization", "").strip()
        bearer = ""
        if authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip()

        if api_key:
            row = self._lookup_api_key(api_key)
            if not row:
                raise AuthError("Invalid or revoked API key.")
            self.db.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (utc_now(), row["id"])
            )
            return Principal(
                actor=row["name"], role=row["role"], method="api_key", api_key_name=row["name"]
            )

        if bearer:
            row = self._lookup_session(bearer)
            if not row:
                raise AuthError("Invalid or expired session token.")
            return Principal(
                actor=row["username"],
                role=row["role"],
                method="session",
                session_expires_at=datetime.fromisoformat(row["expires_at"]),
            )

        if not self.settings.require_api_key:
            if self.settings.is_production:
                raise AuthError("Authentication disabled in a production environment.")
            return Principal(actor="local-dev", role="admin", method="disabled")

        raise AuthError("Missing credentials. Provide X-API-Key or Authorization: Bearer <token>.")

    @staticmethod
    def require(principal: Principal, role: str) -> None:
        if not principal.has_role(role):
            raise AuthorizationError(
                f"Role {principal.role!r} cannot perform an action requiring {role!r}."
            )

    def create_user(self, username: str, password: str, role: str = "viewer") -> None:
        role = role.lower()
        if role not in ROLE_LEVELS:
            raise ValueError(f"Unknown role {role!r}.")
        self.db.execute(
            """
            INSERT INTO users(username, password_hash, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (username, hash_password(password), role, utc_now(), utc_now()),
        )

    def authenticate_password(self, username: str, password: str) -> Principal:
        if not self.settings.allow_password_login:
            raise AuthError("Password login is disabled.")
        row = self.db.query_one("SELECT * FROM users WHERE username = ?", (username,))
        if not row or not row["is_active"] or not row["password_hash"]:
            raise AuthError("Invalid credentials.")
        if not verify_password(password, row["password_hash"]):
            raise AuthError("Invalid credentials.")
        return Principal(actor=username, role=row["role"], method="password")

class LiveAuthorization(BaseModel):
    """Short-lived authorization proof for a live broker route."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    broker: str
    max_capital: Decimal
    authorized_at: datetime
    conditions: Dict[str, bool]


class LiveTradingGate:
    """Fail-closed authorization for real-money routing.

    Every condition must be true. The gate never enables live trading by
    itself: it only authorizes an already-configured live account.
    """

    SOAK_KEY = "live_gate.demo_soak"

    def __init__(self, settings: Settings, state: StateStore) -> None:
        self.settings = settings
        self.state = state

    def record_demo_soak(self, account_id: str, hours: float, operator: str) -> None:
        soaks = self.state.get(self.SOAK_KEY, {}) or {}
        soaks[account_id] = {
            "hours": float(hours),
            "verified_by": operator,
            "verified_at": utc_now(),
        }
        self.state.set(self.SOAK_KEY, soaks)

    def demo_soak_hours(self, account_id: str) -> float:
        soaks = self.state.get(self.SOAK_KEY, {}) or {}
        record = soaks.get(account_id) or {}
        return float(record.get("hours", 0.0))

    def evaluate(
        self,
        account_id: str,
        broker: str,
        requested_capital: Decimal | float | str,
        *,
        broker_supports_live: bool,
        tls_active: bool,
    ) -> Dict[str, bool]:
        capital = to_decimal(requested_capital)
        return {
            "environment_is_live": self.settings.environment.value == "live",
            "live_trading_enabled": bool(self.settings.live_trading_enabled),
            "acknowledgement_recorded": self.settings.live_trading_ack == LIVE_ACK_PHRASE,
            "account_whitelisted": account_id in set(self.settings.live_account_whitelist),
            "capital_positive_within_limit": Decimal("0") < capital <= self.settings.live_max_capital,
            "tls_active": bool(tls_active) or not self.settings.live_require_tls,
            "demo_soak_verified": (
                self.settings.live_require_demo_soak_hours <= 0
                or self.demo_soak_hours(account_id) >= self.settings.live_require_demo_soak_hours
            ),
            "broker_supports_live": bool(broker_supports_live),
        }

    def authorize(
        self,
        account_id: str,
        broker: str,
        requested_capital: Decimal | float | str,
        *,
        broker_supports_live: bool,
        tls_active: bool,
    ) -> LiveAuthorization:
        conditions = self.evaluate(
            account_id,
            broker,
            requested_capital,
            broker_supports_live=broker_supports_live,
            tls_active=tls_active,
        )
        failed = [name for name, passed in conditions.items() if not passed]
        if failed:
            raise LiveTradingDenied(
                "LIVE TRADING DENIED. Failed conditions: " + ", ".join(failed)
            )
        return LiveAuthorization(
            account_id=account_id,
            broker=broker,
            max_capital=to_decimal(requested_capital),
            authorized_at=datetime.now(timezone.utc),
            conditions=conditions,
        )

    def status(self, account_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            "environment": self.settings.environment.value,
            "live_trading_enabled": self.settings.live_trading_enabled,
            "acknowledgement_recorded": self.settings.live_trading_ack == LIVE_ACK_PHRASE,
            "whitelisted_accounts": list(self.settings.live_account_whitelist),
            "max_capital": str(self.settings.live_max_capital),
            "require_tls": self.settings.live_require_tls,
            "required_demo_soak_hours": self.settings.live_require_demo_soak_hours,
            "demo_soak": {
                account_id: self.demo_soak_hours(account_id)
                for account_id in (account_ids or self.settings.live_account_whitelist)
            },
        }

