"""
Production broker connectivity.

Adapters:
- PAPER  : the in-process simulator (never touches a real account)
- MT5_REMOTE : signed HTTP to a Windows MT5 agent (`tools/mt5_agent.py`)
- FIX    : FIX 4.4 session adapter (see `fix_protocol.py`)

Live accounts are disabled by default and require an explicit
`LiveTradingGate.authorize()` before the adapter will accept orders.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forex_platform.broker_adapters.base import (
    BrokerAccountInfo,
    BrokerOrderAck,
    BrokerPosition,
    IBrokerAdapter,
    PermissionSecurityError,
    TokenBucketRateLimiter,
)
from forex_platform.broker_adapters.mt5_bridge import MT5BridgeAdapter
from forex_platform.core.domain import OrderIntent, OrderSide, OrderStatus, OrderType, to_decimal
from forex_platform.production.observability import MetricsRegistry, Timer, get_logger
from forex_platform.production.security import LiveTradingGate
from forex_platform.production.settings import Settings
from forex_platform.production.store import AuditLog, utc_now

logger = get_logger(__name__)


class BrokerError(RuntimeError):
    """Broker gateway returned an error or was unreachable."""


class NoRouteAvailable(BrokerError):
    """No enabled, connected broker account can accept the order."""


class ExecutionMode(str, Enum):
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"


class AccountSpec(BaseModel):
    """Declarative broker-account configuration."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    broker: str
    mode: ExecutionMode = ExecutionMode.PAPER
    environment: str = "paper"
    display_name: str = ""
    adapter: str = "paper"  # paper | mt5_remote | fix
    enabled: bool = False
    supports_live: bool = False
    priority: int = 100
    requested_capital: Decimal = Decimal("0.00")
    symbol_suffix: str = ""
    config: Dict[str, Any] = Field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.mode == ExecutionMode.LIVE

    @model_validator(mode="after")
    def validate_execution_boundary(self) -> "AccountSpec":
        if self.mode == ExecutionMode.PAPER and self.adapter != "paper":
            raise ValueError("PAPER accounts must use the in-process paper adapter")
        if self.mode in {ExecutionMode.DEMO, ExecutionMode.LIVE} and self.adapter != "mt5_remote":
            raise ValueError("DEMO/LIVE accounts require the authenticated mt5_remote adapter")
        if self.mode == ExecutionMode.LIVE:
            if not self.supports_live:
                raise ValueError("LIVE accounts require supports_live=true")
            if self.requested_capital <= Decimal("0"):
                raise ValueError("LIVE accounts require a positive requested capital")
        return self


def resolve_secret(config: Dict[str, Any], key: str) -> Optional[str]:
    """Resolve a secret from inline config, an env var, or a 0600 file."""
    if config.get(key):
        return str(config[key])
    env_name = config.get(f"{key}_env")
    if env_name and os.environ.get(str(env_name)):
        return os.environ[str(env_name)]
    file_name = config.get(f"{key}_file")
    if file_name:
        path = Path(str(file_name))
        if path.exists():
            if os.name == "posix":
                mode = path.stat().st_mode & 0o777
                if mode & 0o077:
                    raise BrokerError(
                        f"Secret file {path} must not be accessible by group or others (mode {mode:o})"
                    )
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                raise BrokerError(f"Secret file {path} is empty")
            return value
    return None


class MT5RemoteAdapter(IBrokerAdapter):
    """Broker adapter that talks to a signed MT5 agent over HTTP(S).

    The agent runs on a Windows host with the MetaTrader 5 terminal and the
    `MetaTrader5` Python package installed. This keeps broker credentials on
    the agent and gives the Linux control plane a small, audited surface.
    """

    def __init__(
        self,
        account_id: str,
        broker_name: str,
        base_url: str,
        *,
        agent_token: Optional[str] = None,
        hmac_secret: Optional[str] = None,
        is_live: bool = False,
        live_gate: Optional[LiveTradingGate] = None,
        broker_supports_live: bool = True,
        timeout_seconds: int = 30,
        connect_timeout_seconds: int = 15,
        max_retries: int = 2,
        verify_tls: bool = True,
        ca_file: Optional[str] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        super().__init__(
            account_id=account_id,
            broker_name=broker_name,
            rate_limiter=rate_limiter or TokenBucketRateLimiter(capacity=100, refill_rate=100.0),
            is_live=is_live,
            live_gate=live_gate,
            broker_supports_live=broker_supports_live,
            adapter_kind="mt5_remote",
        )
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith("https://"):
            raise BrokerError("mt5_remote requires an HTTPS base_url")
        if not agent_token or not hmac_secret:
            raise BrokerError("mt5_remote requires both agent_token and hmac_secret")
        self.agent_token = agent_token
        self.hmac_secret = hmac_secret
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.max_retries = max_retries
        self.verify_tls = verify_tls
        self.ca_file = ca_file
        self._lock = threading.Lock()

    def _signed_headers(self, method: str, path: str, body: bytes) -> Dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "X-FP-Timestamp": timestamp,
            "X-FP-Nonce": nonce,
            "X-FP-Account": self.account_id,
            "User-Agent": "forex-platform/1.0",
        }
        if self.agent_token:
            headers["Authorization"] = f"Bearer {self.agent_token}"
        if self.hmac_secret:
            payload = f"{method.upper()}.{path}.{timestamp}.{nonce}.".encode("utf-8") + body
            headers["X-FP-Signature"] = hmac.new(
                self.hmac_secret.encode("utf-8"), payload, hashlib.sha256
            ).hexdigest()
        return headers

    def _request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, default=str).encode("utf-8") if payload is not None else b""
        last_error: Optional[Exception] = None
        timeout = self.connect_timeout_seconds if path == "/v1/connect" else self.timeout_seconds
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                url, data=body if method.upper() != "GET" else None, method=method.upper()
            )
            for key, value in self._signed_headers(method, path, body).items():
                request.add_header(key, value)
            try:
                context = ssl.create_default_context(cafile=self.ca_file) if self.ca_file else ssl.create_default_context()
                if not self.verify_tls:
                    raise BrokerError("verify_tls=false is not permitted for mt5_remote")
                with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < self.max_retries:
                    last_error = BrokerError(f"Agent HTTP {exc.code}: {detail}")
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise BrokerError(f"MT5 agent HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2**attempt))
                    continue
        raise BrokerError(f"MT5 agent unreachable at {self.base_url}: {last_error}")

    @staticmethod
    def _map_status(raw: str, filled_units: int, total_units: int) -> OrderStatus:
        status = (raw or "").upper()
        if status in {"FILLED", "DONE"}:
            return OrderStatus.FILLED
        if status in {"PARTIAL", "PARTIALLY_FILLED"}:
            return OrderStatus.PARTIALLY_FILLED
        if status in {"REJECTED", "ERROR"}:
            return OrderStatus.REJECTED
        if status in {"CANCELED", "CANCELLED"}:
            return OrderStatus.CANCELLED
        if status == "EXPIRED":
            return OrderStatus.EXPIRED
        if 0 < filled_units < total_units:
            return OrderStatus.PARTIALLY_FILLED
        return OrderStatus.SUBMITTED

    def connect(self) -> bool:
        with self._lock:
            self.rate_limiter.consume(1)
            if self.is_live:
                self.verify_live_capital_guard()
            response = self._request("POST", "/v1/connect", {"account_id": self.account_id})
            if not response.get("connected", False):
                raise BrokerError(f"MT5 agent refused connection: {response.get('error', response)}")
            self._is_connected = True
            logger.info(
                "MT5 remote adapter connected",
                extra={"extra_fields": {"account_id": self.account_id, "broker": self.broker_name}},
            )
            return True

    def disconnect(self) -> None:
        with self._lock:
            try:
                if self._is_connected:
                    self._request("POST", "/v1/disconnect", {"account_id": self.account_id})
            except BrokerError as exc:
                logger.warning("MT5 remote disconnect error: %s", exc)
            finally:
                self._is_connected = False

    def send_order(self, intent: OrderIntent) -> BrokerOrderAck:
        if not self._is_connected:
            raise ConnectionError(f"MT5 agent for {self.account_id} is not connected.")
        self.rate_limiter.consume(1)
        self.verify_live_capital_guard()
        payload = {
            "client_order_id": intent.client_tag or intent.intent_id,
            "intent_id": intent.intent_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "type": intent.order_type.value,
            "units": int(intent.lot_size.units),
            "price": str(intent.limit_price) if intent.limit_price is not None else None,
            "stop_loss": str(intent.stop_loss) if intent.stop_loss is not None else None,
            "take_profit": str(intent.take_profit) if intent.take_profit is not None else None,
        }
        response = self._request("POST", "/v1/orders", payload)
        status = self._map_status(
            str(response.get("status", "")),
            int(response.get("filled_units", 0) or 0),
            int(intent.lot_size.units),
        )
        ack = BrokerOrderAck(
            order_id=intent.intent_id,
            broker_ticket=str(response.get("ticket", "")),
            status=status,
            timestamp=datetime.now(timezone.utc),
            message=str(response.get("message", "MT5 agent acknowledged order")),
            filled_units=int(response.get("filled_units", 0) or 0),
            fill_price=(
                to_decimal(response["fill_price"]) if response.get("fill_price") is not None else None
            ),
        )
        if status == OrderStatus.REJECTED:
            raise BrokerError(f"MT5 agent rejected order: {ack.message}")
        return ack

    def cancel_order(self, order_id: str) -> bool:
        if not self._is_connected:
            raise ConnectionError(f"MT5 agent for {self.account_id} is not connected.")
        self.rate_limiter.consume(1)
        response = self._request(
            "POST", "/v1/orders/cancel", {"ticket": order_id, "order_id": order_id}
        )
        return bool(response.get("cancelled", response.get("success", False)))

    def get_positions(self) -> List[BrokerPosition]:
        if not self._is_connected:
            raise ConnectionError(f"MT5 agent for {self.account_id} is not connected.")
        self.rate_limiter.consume(1)
        response = self._request("GET", "/v1/positions")
        positions: List[BrokerPosition] = []
        for item in response.get("positions", []):
            timestamp = item.get("timestamp")
            positions.append(
                BrokerPosition(
                    ticket_id=str(item["ticket_id"]),
                    symbol=str(item["symbol"]),
                    side=OrderSide(str(item["side"]).upper()),
                    units=int(item["units"]),
                    open_price=to_decimal(item["open_price"]),
                    unrealized_pnl=to_decimal(item.get("unrealized_pnl", "0")),
                    timestamp=(
                        datetime.fromisoformat(timestamp) if timestamp else datetime.now(timezone.utc)
                    ),
                )
            )
        return positions

    def get_account_info(self) -> BrokerAccountInfo:
        if not self._is_connected:
            raise ConnectionError(f"MT5 agent for {self.account_id} is not connected.")
        self.rate_limiter.consume(1)
        response = self._request("GET", "/v1/account")
        return BrokerAccountInfo(
            account_id=self.account_id,
            broker_name=self.broker_name,
            currency=str(response.get("currency", "USD")),
            balance=to_decimal(response.get("balance", "0")),
            equity=to_decimal(response.get("equity", "0")),
            margin=to_decimal(response.get("margin", "0")),
            free_margin=to_decimal(response.get("free_margin", "0")),
            is_live=self.is_live,
            permissions=list(response.get("permissions", ["TRADE", "MARKET_DATA"])),
        )

    def get_candles(self, symbol: str, timeframe: str = "M1", count: int = 500) -> List[Dict[str, Any]]:
        """Fetch historical candles from the MT5 terminal via the agent."""
        if not self._is_connected:
            raise ConnectionError(f"MT5 agent for {self.account_id} is not connected.")
        self.rate_limiter.consume(1)
        response = self._request(
            "GET",
            f"/v1/candles?symbol={urllib.parse.quote(symbol)}&timeframe={timeframe}&count={int(count)}",
        )
        return list(response.get("candles", []))

    def get_tick(self, symbol: str) -> Dict[str, Any]:
        if not self._is_connected:
            raise ConnectionError(f"MT5 agent for {self.account_id} is not connected.")
        self.rate_limiter.consume(1)
        return self._request("GET", f"/v1/ticks?symbol={urllib.parse.quote(symbol)}&count=1")

    def healthcheck(self) -> Dict[str, Any]:
        try:
            return self._request("GET", "/v1/health")
        except BrokerError as exc:
            return {"healthy": False, "error": str(exc)}


class BrokerRegistry:
    """Registry of broker accounts with deterministic routing and failover."""

    def __init__(
        self,
        settings: Settings,
        live_gate: LiveTradingGate,
        metrics: Optional[MetricsRegistry] = None,
        audit: Optional[AuditLog] = None,
    ) -> None:
        self.settings = settings
        self.live_gate = live_gate
        self.metrics = metrics or MetricsRegistry()
        self.audit = audit
        self._specs: Dict[str, AccountSpec] = {}
        self._adapters: Dict[str, IBrokerAdapter] = {}
        self._errors: Dict[str, str] = {}
        self._lock = threading.RLock()

    def register(self, spec: AccountSpec, adapter: Optional[IBrokerAdapter] = None) -> None:
        with self._lock:
            self._specs[spec.account_id] = spec
            self._adapters[spec.account_id] = adapter or self._build_adapter(spec)

    def _build_adapter(self, spec: AccountSpec) -> IBrokerAdapter:
        config = dict(spec.config)
        if spec.adapter == "paper":
            return MT5BridgeAdapter(
                account_id=spec.account_id,
                broker_name=spec.broker,
                is_live=False,
                custom_suffix=spec.symbol_suffix or None,
            )
        if spec.adapter == "mt5_remote":
            base_url = str(config.get("base_url", "")).strip()
            if not base_url:
                raise BrokerError(
                    f"Account {spec.account_id}: mt5_remote requires config.base_url"
                )
            return MT5RemoteAdapter(
                account_id=spec.account_id,
                broker_name=spec.broker,
                base_url=base_url,
                agent_token=resolve_secret(config, "agent_token"),
                hmac_secret=resolve_secret(config, "hmac_secret"),
                is_live=spec.is_live,
                live_gate=self.live_gate,
                broker_supports_live=spec.supports_live,
                timeout_seconds=int(
                    config.get("timeout_seconds", self.settings.broker_request_timeout_seconds)
                ),
                connect_timeout_seconds=int(
                    config.get("connect_timeout_seconds", self.settings.broker_connect_timeout_seconds)
                ),
                max_retries=int(config.get("max_retries", self.settings.broker_max_retries)),
                verify_tls=bool(config.get("verify_tls", True)),
                ca_file=config.get("ca_file"),
            )
        raise BrokerError(
            f"Account {spec.account_id}: unsupported adapter {spec.adapter!r}. "
            "Supported: paper, mt5_remote."
        )

    def register_from_file(self, path: Optional[str] = None) -> int:
        """Load account specs from a JSON file. Returns the number registered."""
        config_path = Path(path or self.settings.broker_config_file)
        if not config_path.exists():
            return 0
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        entries = payload.get("accounts", payload) if isinstance(payload, dict) else payload
        count = 0
        for entry in entries:
            self.register(AccountSpec(**entry))
            count += 1
        logger.info("Loaded %d broker account spec(s) from %s", count, config_path)
        return count

    def connect(self, account_id: str, tls_active: bool = False) -> bool:
        spec = self._require_spec(account_id)
        adapter = self._adapters[account_id]
        if spec.is_live:
            adapter.authorize_live(spec.requested_capital, tls_active=tls_active)
        if not spec.enabled:
            raise BrokerError(f"Account {account_id} is disabled. Enable it before connecting.")
        with Timer(self.metrics, "forex_broker_connect_seconds", {"account": account_id}):
            connected = adapter.connect()
        self.metrics.increment(
            "forex_broker_connect_total",
            labels={"account": account_id, "result": "ok" if connected else "failed"},
        )
        self._errors.pop(account_id, None)
        if self.audit:
            self.audit.record(
                actor="system",
                role="system",
                action="broker.connect",
                target=account_id,
                details={"broker": spec.broker, "mode": spec.mode.value, "connected": connected},
            )
        return connected

    def disconnect(self, account_id: str) -> None:
        adapter = self._adapters.get(account_id)
        if adapter:
            adapter.disconnect()
        if self.audit:
            self.audit.record(
                actor="system", role="system", action="broker.disconnect", target=account_id
            )

    def connect_enabled(self, tls_active: bool = False) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        for spec in self._specs.values():
            if not spec.enabled:
                continue
            try:
                results[spec.account_id] = {"connected": self.connect(spec.account_id, tls_active)}
            except Exception as exc:  # noqa: BLE001 - reported per account
                self._errors[spec.account_id] = str(exc)
                results[spec.account_id] = {"connected": False, "error": str(exc)}
                logger.error("Broker connect failed for %s: %s", spec.account_id, exc)
        return results

    def _require_spec(self, account_id: str) -> AccountSpec:
        spec = self._specs.get(account_id)
        if not spec:
            raise BrokerError(f"Unknown broker account {account_id!r}")
        return spec

    def _candidates(self, mode: Optional[str], account_id: Optional[str]) -> List[AccountSpec]:
        if account_id:
            return [self._require_spec(account_id)]
        target_mode = (mode or self.settings.default_execution_mode).upper()
        candidates = [
            spec
            for spec in self._specs.values()
            if spec.mode.value == target_mode and spec.enabled
        ]
        return sorted(candidates, key=lambda spec: (spec.priority, spec.account_id))

    def send_order(
        self, intent: OrderIntent, mode: Optional[str] = None, account_id: Optional[str] = None
    ) -> Tuple[str, BrokerOrderAck]:
        """Send an order to the first healthy candidate. Fails over on error."""
        candidates = self._candidates(mode, account_id)
        if not candidates:
            raise NoRouteAvailable(
                f"No enabled broker account for mode={mode or self.settings.default_execution_mode}"
            )
        errors: List[str] = []
        for spec in candidates:
            adapter = self._adapters[spec.account_id]
            if not adapter.is_connected:
                errors.append(f"{spec.account_id}: not connected")
                continue
            try:
                with Timer(
                    self.metrics, "forex_broker_order_seconds", {"account": spec.account_id}
                ):
                    ack = adapter.send_order(intent)
                self.metrics.increment(
                    "forex_orders_sent_total",
                    labels={"account": spec.account_id, "status": ack.status.value},
                )
                return spec.account_id, ack
            except Exception as exc:  # noqa: BLE001 - failover path
                errors.append(f"{spec.account_id}: {exc}")
                self.metrics.increment(
                    "forex_broker_errors_total", labels={"account": spec.account_id}
                )
                logger.error("Order routing failed on %s: %s", spec.account_id, exc)
        raise NoRouteAvailable("All broker routes failed: " + " | ".join(errors))

    def cancel(self, order_id: str, account_id: str) -> bool:
        adapter = self._adapters.get(account_id)
        if not adapter or not adapter.is_connected:
            return False
        try:
            return adapter.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Cancel failed on %s: %s", account_id, exc)
            return False

    def positions(
        self, account_id: Optional[str] = None, mode: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        specs = [self._require_spec(account_id)] if account_id else list(self._specs.values())
        target_mode = (mode or "").upper()
        for spec in specs:
            if target_mode and spec.mode.value != target_mode:
                continue
            adapter = self._adapters[spec.account_id]
            if not adapter.is_connected:
                continue
            try:
                result[spec.account_id] = [p.model_dump(mode="json") for p in adapter.get_positions()]
            except Exception as exc:  # noqa: BLE001
                result[spec.account_id] = []
                self._errors[spec.account_id] = str(exc)
        return result

    def account_info(self, account_id: str) -> Dict[str, Any]:
        adapter = self._adapters.get(account_id)
        if not adapter or not adapter.is_connected:
            raise BrokerError(f"Account {account_id} is not connected.")
        return adapter.get_account_info().model_dump(mode="json")

    def status(self) -> Dict[str, Any]:
        return {
            "accounts": [
                {
                    **spec.model_dump(mode="json"),
                    "connected": self._adapters[spec.account_id].is_connected,
                    "live_authorized": self._adapters[spec.account_id].live_authorized,
                    "adapter_kind": self._adapters[spec.account_id].adapter_kind,
                    "last_error": self._errors.get(spec.account_id),
                }
                for spec in self._specs.values()
            ],
            "live_gate": self.live_gate.status(),
        }

    def health(self) -> Dict[str, Any]:
        """Return per-account health for readiness and operator dashboards."""
        accounts: List[Dict[str, Any]] = []
        for spec in sorted(self._specs.values(), key=lambda item: (item.priority, item.account_id)):
            adapter = self._adapters[spec.account_id]
            accounts.append({
                "account_id": spec.account_id,
                "broker": spec.broker,
                "mode": spec.mode.value,
                "enabled": spec.enabled,
                "connected": adapter.is_connected,
                "live_authorized": adapter.live_authorized,
                "error": self._errors.get(spec.account_id),
            })
        enabled = [item for item in accounts if item["enabled"]]
        return {
            "accounts": accounts,
            "enabled_accounts": len(enabled),
            "connected_accounts": sum(1 for item in enabled if item["connected"]),
            "all_enabled_connected": all(item["connected"] for item in enabled) if enabled else True,
        }

    def list_specs(self) -> List[AccountSpec]:
        return list(self._specs.values())

    def get_adapter(self, account_id: str) -> Optional[IBrokerAdapter]:
        return self._adapters.get(account_id)



