"""
Validated production settings for Forex Platform.

Configuration is loaded from environment variables and an optional `.env`
file. Secrets are never written to logs or API responses. Validation is
fail-closed: an unsafe production configuration refuses to start.
"""

from __future__ import annotations

import os
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forex_platform.core.domain import to_decimal


class Environment(str, Enum):
    """Deployment environment. Capital permissions are derived from this."""

    DEVELOPMENT = "development"
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class SettingsError(RuntimeError):
    """Raised when configuration is missing or unsafe for the environment."""


LIVE_ACK_PHRASE = "I_ACCEPT_FULL_LOSS_RESPONSIBILITY"


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    if raw.startswith("["):
        import json

        parsed = json.loads(raw)
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_env_file(path: str | Path | None) -> Dict[str, str]:
    """Minimal `.env` reader. Real environment variables take precedence."""
    result: Dict[str, str] = {}
    if not path:
        return result
    env_path = Path(path)
    if not env_path.exists():
        return result
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


class Settings(BaseModel):
    """Runtime configuration for the production control plane."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    environment: Environment = Environment.PAPER
    service_name: str = "forex-platform"
    host: str = "127.0.0.1"
    port: int = 8787
    allow_public_bind: bool = False
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    cors_allow_origins: List[str] = Field(default_factory=list)
    trust_proxy_headers: bool = False

    data_dir: str = "var"
    db_path: str = "var/forex_platform.db"
    audit_retention_days: int = 730
    heartbeat_path: str = "var/forex_platform.heartbeat"

    require_api_key: bool = True
    bootstrap_admin_key: Optional[str] = None
    bootstrap_admin_key_file: Optional[str] = None
    api_key_pepper: Optional[str] = None
    session_ttl_seconds: int = 3600
    session_secret: Optional[str] = None
    session_secret_file: str = "var/session_secret"
    max_request_body_bytes: int = 1_048_576
    rate_limit_requests_per_minute: int = 240
    allow_password_login: bool = False

    live_trading_enabled: bool = False
    live_trading_ack: str = ""
    live_account_whitelist: List[str] = Field(default_factory=list)
    live_max_capital: Decimal = Decimal("0.00")
    live_require_tls: bool = True
    live_require_demo_soak_hours: int = 720
    live_demo_soak_verified_at: Optional[str] = None

    account_equity: Decimal = Decimal("100000.00")
    max_risk_per_trade_pct: Decimal = Decimal("0.50")
    max_daily_loss_pct: Decimal = Decimal("2.00")
    max_drawdown_pct: Decimal = Decimal("10.00")
    max_gross_exposure_pct: Decimal = Decimal("300.00")
    max_net_exposure_per_currency_pct: Decimal = Decimal("100.00")
    max_open_positions: int = 20
    max_order_units: int = 5_000_000
    stale_quote_ms: int = 1000
    max_clock_drift_ms: int = 200

    broker_config_file: str = "config/brokers.json"
    broker_connect_timeout_seconds: int = 15
    broker_request_timeout_seconds: int = 30
    broker_max_retries: int = 2
    default_execution_mode: str = "PAPER"

    news_enabled: bool = True
    news_provider_kind: str = "file"
    news_provider_url: Optional[str] = None
    news_provider_token: Optional[str] = None
    news_refresh_seconds: int = 900
    news_blackout_minutes_before: int = 15
    news_blackout_minutes_after: int = 15
    news_blackout_impacts: List[str] = Field(default_factory=lambda: ["HIGH"])
    fundamentals_enabled: bool = False

    learning_enabled: bool = True
    learning_auto_promote_to_paper: bool = True
    learning_require_human_for_live: bool = True
    learning_min_oos_trades: int = 30
    learning_min_oos_sharpe: Decimal = Decimal("0.50")
    learning_max_drawdown_pct: Decimal = Decimal("15.00")
    research_dir: str = "research"

    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True
    reconciliation_interval_seconds: int = 60
    risk_snapshot_interval_seconds: int = 15
    shutdown_grace_seconds: int = 20

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        external_bind = self.host not in {"127.0.0.1", "::1", "localhost", ""}
        if external_bind and not self.allow_public_bind:
            raise SettingsError(
                f"Refusing to bind public interface {self.host!r} without "
                f"FOREX_ALLOW_PUBLIC_BIND=true."
            )
        if external_bind and not self.require_api_key:
            raise SettingsError("Public bind requires FOREX_REQUIRE_API_KEY=true.")
        if external_bind and self.environment == Environment.LIVE:
            if not (self.tls_cert_file and self.tls_key_file):
                raise SettingsError("LIVE environment on a public interface requires TLS.")
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise SettingsError(
                "Both FOREX_TLS_CERT_FILE and FOREX_TLS_KEY_FILE are required for TLS."
            )
        if self.require_api_key and not (
            self.api_key_pepper or self.bootstrap_admin_key or self.bootstrap_admin_key_file
        ):
            raise SettingsError(
                "API-key authentication is enabled but no pepper/bootstrap admin key is configured."
            )
        if self.api_key_pepper is not None and len(self.api_key_pepper) < 32:
            raise SettingsError("FOREX_API_KEY_PEPPER must be at least 32 characters.")
        if self.environment == Environment.LIVE:
            if not self.live_trading_enabled:
                raise SettingsError("LIVE environment requires FOREX_LIVE_TRADING_ENABLED=true.")
            if self.live_trading_ack != LIVE_ACK_PHRASE:
                raise SettingsError(
                    f"LIVE environment requires FOREX_LIVE_TRADING_ACK={LIVE_ACK_PHRASE}."
                )
            if not self.live_account_whitelist:
                raise SettingsError("LIVE environment requires FOREX_LIVE_ACCOUNT_WHITELIST.")
            if self.live_max_capital <= Decimal("0.00"):
                raise SettingsError("LIVE environment requires FOREX_LIVE_MAX_CAPITAL > 0.")
            if self.live_require_tls and not (self.tls_cert_file and self.tls_key_file):
                raise SettingsError("LIVE environment requires TLS configuration.")
        if self.max_request_body_bytes < 1024:
            raise SettingsError("FOREX_MAX_REQUEST_BODY_BYTES must be >= 1024.")
        if self.session_ttl_seconds < 60:
            raise SettingsError("FOREX_SESSION_TTL_SECONDS must be >= 60.")
        return self

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)

    @property
    def data_dir_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def is_live(self) -> bool:
        return self.environment == Environment.LIVE

    @property
    def is_production(self) -> bool:
        return self.environment in {Environment.DEMO, Environment.LIVE}

    def ensure_directories(self) -> None:
        """Create runtime directories with restrictive permissions where possible."""
        directories = {
            self.data_dir_path,
            Path(self.db_path).parent,
            Path(self.heartbeat_path).parent,
        }
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    def public_summary(self) -> Dict[str, Any]:
        """Safe summary for logs/API. Never contains secrets."""
        return {
            "service_name": self.service_name,
            "environment": self.environment.value,
            "host": self.host,
            "port": self.port,
            "live_trading_enabled": self.live_trading_enabled,
            "live_account_whitelist": list(self.live_account_whitelist),
            "live_max_capital": str(self.live_max_capital),
            "news_enabled": self.news_enabled,
            "learning_enabled": self.learning_enabled,
            "db_path": self.db_path,
            "log_level": self.log_level,
        }

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = ".env",
        environ: Optional[Mapping[str, str]] = None,
    ) -> "Settings":
        file_values = load_env_file(env_file)
        source: Dict[str, str] = dict(file_values)
        source.update(dict(os.environ if environ is None else environ))

        def get(name: str, default: Any = None) -> Any:
            return source.get(name, default)

        bootstrap_key_file = get("FOREX_BOOTSTRAP_ADMIN_KEY_FILE")
        bootstrap_key = get("FOREX_BOOTSTRAP_ADMIN_KEY")
        if not bootstrap_key and bootstrap_key_file:
            key_path = Path(str(bootstrap_key_file))
            if key_path.exists():
                bootstrap_key = key_path.read_text(encoding="utf-8").strip()
            else:
                raise SettingsError(f"Bootstrap admin key file not found: {key_path}")

        payload = {
            "environment": get("FOREX_ENVIRONMENT", "paper"),
            "service_name": get("FOREX_SERVICE_NAME", "forex-platform"),
            "host": get("FOREX_HOST", "127.0.0.1"),
            "port": int(get("FOREX_PORT", 8787)),
            "allow_public_bind": _parse_bool(get("FOREX_ALLOW_PUBLIC_BIND"), False),
            "tls_cert_file": get("FOREX_TLS_CERT_FILE"),
            "tls_key_file": get("FOREX_TLS_KEY_FILE"),
            "cors_allow_origins": _parse_csv(get("FOREX_CORS_ALLOW_ORIGINS")),
            "trust_proxy_headers": _parse_bool(get("FOREX_TRUST_PROXY_HEADERS"), False),
            "data_dir": get("FOREX_DATA_DIR", "var"),
            "db_path": get("FOREX_DB_PATH", "var/forex_platform.db"),
            "audit_retention_days": int(get("FOREX_AUDIT_RETENTION_DAYS", 730)),
            "heartbeat_path": get("FOREX_HEARTBEAT_PATH", "var/forex_platform.heartbeat"),
            "require_api_key": _parse_bool(get("FOREX_REQUIRE_API_KEY"), True),
            "bootstrap_admin_key": bootstrap_key,
            "bootstrap_admin_key_file": bootstrap_key_file,
            "api_key_pepper": get("FOREX_API_KEY_PEPPER"),
            "session_ttl_seconds": int(get("FOREX_SESSION_TTL_SECONDS", 3600)),
            "session_secret": get("FOREX_SESSION_SECRET"),
            "session_secret_file": get("FOREX_SESSION_SECRET_FILE", "var/session_secret"),
            "max_request_body_bytes": int(get("FOREX_MAX_REQUEST_BODY_BYTES", 1_048_576)),
            "rate_limit_requests_per_minute": int(get("FOREX_RATE_LIMIT_PER_MINUTE", 240)),
            "allow_password_login": _parse_bool(get("FOREX_ALLOW_PASSWORD_LOGIN"), False),
            "live_trading_enabled": _parse_bool(get("FOREX_LIVE_TRADING_ENABLED"), False),
            "live_trading_ack": get("FOREX_LIVE_TRADING_ACK", ""),
            "live_account_whitelist": _parse_csv(get("FOREX_LIVE_ACCOUNT_WHITELIST")),
            "live_max_capital": to_decimal(get("FOREX_LIVE_MAX_CAPITAL", "0.00")),
            "live_require_tls": _parse_bool(get("FOREX_LIVE_REQUIRE_TLS"), True),
            "live_require_demo_soak_hours": int(get("FOREX_LIVE_REQUIRE_DEMO_SOAK_HOURS", 720)),
            "live_demo_soak_verified_at": get("FOREX_LIVE_DEMO_SOAK_VERIFIED_AT"),
            "account_equity": to_decimal(get("FOREX_ACCOUNT_EQUITY", "100000.00")),
            "max_risk_per_trade_pct": to_decimal(get("FOREX_MAX_RISK_PER_TRADE_PCT", "0.50")),
            "max_daily_loss_pct": to_decimal(get("FOREX_MAX_DAILY_LOSS_PCT", "2.00")),
            "max_drawdown_pct": to_decimal(get("FOREX_MAX_DRAWDOWN_PCT", "10.00")),
            "max_gross_exposure_pct": to_decimal(get("FOREX_MAX_GROSS_EXPOSURE_PCT", "300.00")),
            "max_net_exposure_per_currency_pct": to_decimal(
                get("FOREX_MAX_NET_EXPOSURE_PER_CURRENCY_PCT", "100.00")
            ),
            "max_open_positions": int(get("FOREX_MAX_OPEN_POSITIONS", 20)),
            "max_order_units": int(get("FOREX_MAX_ORDER_UNITS", 5_000_000)),
            "stale_quote_ms": int(get("FOREX_STALE_QUOTE_MS", 1000)),
            "max_clock_drift_ms": int(get("FOREX_MAX_CLOCK_DRIFT_MS", 200)),
            "broker_config_file": get("FOREX_BROKER_CONFIG_FILE", "config/brokers.json"),
            "broker_connect_timeout_seconds": int(get("FOREX_BROKER_CONNECT_TIMEOUT", 15)),
            "broker_request_timeout_seconds": int(get("FOREX_BROKER_REQUEST_TIMEOUT", 30)),
            "broker_max_retries": int(get("FOREX_BROKER_MAX_RETRIES", 2)),
            "default_execution_mode": get("FOREX_EXECUTION_MODE", "PAPER"),
            "news_enabled": _parse_bool(get("FOREX_NEWS_ENABLED"), True),
            "news_provider_kind": get("FOREX_NEWS_PROVIDER_KIND", "file"),
            "news_provider_url": get("FOREX_NEWS_PROVIDER_URL"),
            "news_provider_token": get("FOREX_NEWS_PROVIDER_TOKEN"),
            "news_refresh_seconds": int(get("FOREX_NEWS_REFRESH_SECONDS", 900)),
            "news_blackout_minutes_before": int(get("FOREX_NEWS_BLACKOUT_MINUTES_BEFORE", 15)),
            "news_blackout_minutes_after": int(get("FOREX_NEWS_BLACKOUT_MINUTES_AFTER", 15)),
            "news_blackout_impacts": _parse_csv(get("FOREX_NEWS_BLACKOUT_IMPACTS")) or ["HIGH"],
            "fundamentals_enabled": _parse_bool(get("FOREX_FUNDAMENTALS_ENABLED"), False),
            "learning_enabled": _parse_bool(get("FOREX_LEARNING_ENABLED"), True),
            "learning_auto_promote_to_paper": _parse_bool(
                get("FOREX_LEARNING_AUTO_PROMOTE_TO_PAPER"), True
            ),
            "learning_require_human_for_live": _parse_bool(
                get("FOREX_LEARNING_REQUIRE_HUMAN_FOR_LIVE"), True
            ),
            "learning_min_oos_trades": int(get("FOREX_LEARNING_MIN_OOS_TRADES", 30)),
            "learning_min_oos_sharpe": to_decimal(get("FOREX_LEARNING_MIN_OOS_SHARPE", "0.50")),
            "learning_max_drawdown_pct": to_decimal(get("FOREX_LEARNING_MAX_DRAWDOWN_PCT", "15.00")),
            "research_dir": get("FOREX_RESEARCH_DIR", "research"),
            "log_level": get("FOREX_LOG_LEVEL", "INFO").upper(),
            "log_json": _parse_bool(get("FOREX_LOG_JSON"), True),
            "metrics_enabled": _parse_bool(get("FOREX_METRICS_ENABLED"), True),
            "reconciliation_interval_seconds": int(get("FOREX_RECONCILIATION_INTERVAL", 60)),
            "risk_snapshot_interval_seconds": int(get("FOREX_RISK_SNAPSHOT_INTERVAL", 15)),
            "shutdown_grace_seconds": int(get("FOREX_SHUTDOWN_GRACE", 20)),
        }
        return cls(**payload)

