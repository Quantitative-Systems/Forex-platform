"""
Runtime facade and supervisor.

`TradingPlatform` is the composition root: it constructs every production
service exactly once and exposes a stable API for the HTTP layer. The
supervisor runs the recurring maintenance loops (reconciliation, risk
snapshots, news refresh, session cleanup, heartbeat) with graceful shutdown.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from forex_platform.core.domain import LotSize, OrderIntent, OrderSide, OrderType, to_decimal
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.order_management.router import SmartOrderRouter
from forex_platform.portfolio_engine.allocator import CurrencyExposureGovernor
from forex_platform.production.brokers import AccountSpec, BrokerRegistry
from forex_platform.production.execution import (
    ExecutionResult,
    ProductionExecutionService,
    RiskKernel,
)
from forex_platform.production.learning import LearningPipeline
from forex_platform.production.markets import MarketDataService
from forex_platform.production.news import FundamentalsService, NewsCalendarService
from forex_platform.production.observability import (
    HealthRegistry,
    MetricsRegistry,
    RateLimiter,
    configure_logging,
    get_logger,
)
from forex_platform.production.security import AuthService, LiveTradingGate
from forex_platform.production.settings import Settings
from forex_platform.production.store import (
    AccountRepository,
    AuditLog,
    Database,
    NewsRepository,
    OrderRepository,
    ResearchRepository,
    StateStore,
)
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import PreTradeRiskFirewall
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch

logger = get_logger(__name__)


class TradingPlatform:
    """Composition root for the production trading control plane."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_directories()
        configure_logging(settings.log_level, settings.log_json)

        self.metrics = MetricsRegistry()
        self.health = HealthRegistry()
        self.rate_limiter = RateLimiter(settings.rate_limit_requests_per_minute)

        self.db = Database(settings.db_path)
        self.db.migrate()
        self.audit = AuditLog(self.db)
        self.state = StateStore(self.db)
        self.accounts_repo = AccountRepository(self.db)
        self.orders_repo = OrderRepository(self.db)
        self.news_repo = NewsRepository(self.db)
        self.research_repo = ResearchRepository(self.db)

        self.auth = AuthService(self.db, settings)
        self.live_gate = LiveTradingGate(settings, self.state)

        self.brokers = BrokerRegistry(
            settings, self.live_gate, metrics=self.metrics, audit=self.audit
        )
        self.brokers.register_from_file()
        self._sync_accounts_to_db()

        self.kill_switch = HierarchicalKillSwitch()
        self.circuit_breaker = CircuitBreakerEngine(initial_equity=settings.account_equity)
        self.exposure_governor = CurrencyExposureGovernor()
        self.firewall = PreTradeRiskFirewall(
            kill_switch=self.kill_switch,
            circuit_breaker=self.circuit_breaker,
            allow_live_capital=settings.is_live,
            exposure_governor=self.exposure_governor,
            account_equity=settings.account_equity,
        )
        self.router = SmartOrderRouter(allow_live_routing=settings.is_live)
        self.oms = OrderManagementSystem(
            firewall=self.firewall,
            router=self.router,
            kill_switch=self.kill_switch,
            initial_balance=settings.account_equity,
        )

        self.news = NewsCalendarService(settings, self.news_repo, metrics=self.metrics)
        self.fundamentals = FundamentalsService(self.state)
        self.market_data = MarketDataService(settings, self.brokers)
        self.risk = RiskKernel(
            settings=settings,
            firewall=self.firewall,
            circuit_breaker=self.circuit_breaker,
            kill_switch=self.kill_switch,
            state=self.state,
            metrics=self.metrics,
            news=self.news,
        )
        self.execution = ProductionExecutionService(
            settings=settings,
            brokers=self.brokers,
            oms=self.oms,
            risk=self.risk,
            orders_repo=self.orders_repo,
            audit=self.audit,
            metrics=self.metrics,
            health=self.health,
            market_data=self.market_data,
        )
        self.learning = LearningPipeline(
            settings=settings,
            repo=self.research_repo,
            state=self.state,
            audit=self.audit,
            metrics=self.metrics,
        )

        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._started_at: Optional[datetime] = None
        self._started = False

        self.health.set("database", self.db.healthcheck(), f"sqlite={settings.db_path}")
        self.health.set("live_gate", True, f"environment={settings.environment.value}")
        self.health.set("brokers", self.brokers.health()["all_enabled_connected"], "not started")
        self.health.set("execution", False, "not started")

    # ------------------------------------------------------------------
    # Account wiring
    # ------------------------------------------------------------------
    def _sync_accounts_to_db(self) -> None:
        for spec in self.brokers.list_specs():
            existing = self.accounts_repo.get(spec.account_id)
            self.accounts_repo.upsert(
                {
                    "id": spec.account_id,
                    "broker": spec.broker,
                    "account_id": spec.config.get("login", spec.account_id),
                    "environment": spec.environment,
                    "display_name": spec.display_name or spec.account_id,
                    "mode": spec.mode.value,
                    "is_enabled": spec.enabled,
                    "config": {
                        "adapter": spec.adapter,
                        "supports_live": spec.supports_live,
                        "priority": spec.priority,
                    },
                    "created_at": (existing or {}).get("created_at"),
                }
            )

    def add_account(self, spec: AccountSpec) -> None:
        self.brokers.register(spec)
        self.accounts_repo.upsert(
            {
                "id": spec.account_id,
                "broker": spec.broker,
                "account_id": spec.config.get("login", spec.account_id),
                "environment": spec.environment,
                "display_name": spec.display_name or spec.account_id,
                "mode": spec.mode.value,
                "is_enabled": spec.enabled,
                "config": {"adapter": spec.adapter, "supports_live": spec.supports_live},
            }
        )

    def enable_account(self, account_id: str, enabled: bool, actor: str = "operator") -> bool:
        spec = next(
            (item for item in self.brokers.list_specs() if item.account_id == account_id), None
        )
        if spec is None:
            raise KeyError(f"Unknown account {account_id!r}")
        self.brokers.register(spec.model_copy(update={"enabled": enabled}))
        self.accounts_repo.set_enabled(account_id, enabled)
        self.audit.record(
            actor=actor,
            role="operator",
            action="account.enabled" if enabled else "account.disabled",
            target=account_id,
        )
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, connect_brokers: bool = True) -> Dict[str, Any]:
        if self._started:
            return {"started": True, "already_running": True}
        self._started_at = datetime.now(timezone.utc)
        self._stop.clear()
        tls_active = bool(self.settings.tls_cert_file and self.settings.tls_key_file)
        broker_results: Dict[str, Any] = {}
        if connect_brokers:
            broker_results = self.brokers.connect_enabled(tls_active=tls_active)
        if self.settings.news_enabled:
            try:
                self.news.refresh()
            except Exception as exc:  # noqa: BLE001 - feed outage must not block startup
                logger.error("Initial news refresh failed: %s", exc)
        self._update_equity_from_brokers()
        self._write_heartbeat()

        loops = [
            ("heartbeat", 5, self._write_heartbeat),
            ("reconcile", self.settings.reconciliation_interval_seconds, self._reconcile_loop),
            ("risk", self.settings.risk_snapshot_interval_seconds, self._risk_loop),
            ("news", self.settings.news_refresh_seconds, self._news_loop),
            ("sessions", 300, self._session_cleanup_loop),
        ]
        for name, interval, function in loops:
            thread = threading.Thread(
                target=self._loop, args=(name, interval, function), name=f"fp-{name}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

        self._started = True
        broker_health = self.brokers.health()
        self.health.set("brokers", broker_health["all_enabled_connected"], f"{broker_health['connected_accounts']}/{broker_health['enabled_accounts']} enabled accounts connected")
        self.health.set("execution", True, "running")
        self.audit.record(
            actor="system",
            role="system",
            action="platform.started",
            details={"brokers": broker_results, "environment": self.settings.environment.value},
        )
        logger.info("TradingPlatform started environment=%s", self.settings.environment.value)
        return {"started": True, "brokers": broker_results}

    def stop(self) -> Dict[str, Any]:
        if not self._started:
            return {"stopped": True, "already_stopped": True}
        self._stop.set()
        deadline = time.time() + max(1, self.settings.shutdown_grace_seconds)
        for thread in self._threads:
            remaining = max(0.1, deadline - time.time())
            thread.join(timeout=remaining)
        self._threads.clear()
        for spec in self.brokers.list_specs():
            if spec.enabled:
                try:
                    self.brokers.disconnect(spec.account_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Disconnect failed for %s: %s", spec.account_id, exc)
        self._started = False
        self.health.set("execution", False, "stopped")
        self.audit.record(actor="system", role="system", action="platform.stopped")
        logger.info("TradingPlatform stopped")
        return {"stopped": True}

    def _loop(self, name: str, interval: int, function: Any) -> None:
        while not self._stop.wait(max(1, int(interval))):
            try:
                function()
                self.health.set(f"loop.{name}", True, "ok")
            except Exception as exc:  # noqa: BLE001 - loops must never die silently
                self.health.set(f"loop.{name}", False, str(exc))
                logger.error("Supervisor loop %s failed: %s", name, exc)

    def _write_heartbeat(self) -> None:
        path = Path(self.settings.heartbeat_path)
        payload = {
            "pid": __import__("os").getpid(),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": self.settings.environment.value,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _reconcile_loop(self) -> None:
        result = self.execution.reconcile()
        broker_health = self.brokers.health()
        self.health.set("brokers", broker_health["all_enabled_connected"], f"{broker_health['connected_accounts']}/{broker_health['enabled_accounts']} enabled accounts connected")
        self.health.set("reconciliation", not result["alerts"], f"{len(result['alerts'])} alert(s)")

    def _risk_loop(self) -> None:
        self._update_equity_from_brokers()
        self.metrics.set_gauge("forex_risk_daily_pnl", float(self.risk.daily_pnl()))
        self.health.set("risk", True, "risk snapshot updated")

    def _news_loop(self) -> None:
        if not self.settings.news_enabled:
            return
        count = self.news.refresh()
        self.health.set("news", True, f"{count} event(s) refreshed")

    def _session_cleanup_loop(self) -> None:
        removed = self.auth.cleanup_sessions()
        purged = self.audit.purge_older_than(self.settings.audit_retention_days)
        self.metrics.increment("forex_sessions_cleaned_total", value=removed)
        self.metrics.increment("forex_audit_purged_total", value=purged)

    def _update_equity_from_brokers(self) -> None:
        equity = self.settings.account_equity
        for spec in self.brokers.list_specs():
            adapter = self.brokers.get_adapter(spec.account_id)
            if not adapter or not adapter.is_connected:
                continue
            try:
                info = adapter.get_account_info()
                equity = info.equity
                self.metrics.set_gauge(
                    "forex_account_equity", float(info.equity), {"account": spec.account_id}
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("Account telemetry failed for %s: %s", spec.account_id, exc)
        self.firewall.account_equity = equity
        self.risk.update_equity(equity)

    # ------------------------------------------------------------------
    # Trading operations
    # ------------------------------------------------------------------
    def submit_order(self, payload: Dict[str, Any]) -> ExecutionResult:
        """Build an OrderIntent from an API payload and execute it."""
        symbol = str(payload.get("symbol", "")).upper().replace("/", "").replace("_", "")
        if len(symbol) != 6:
            raise ValueError("symbol must be a 6-character pair, e.g. EURUSD")
        side = OrderSide(str(payload.get("side", "")).upper())
        order_type = OrderType(str(payload.get("order_type", "MARKET")).upper())
        if payload.get("units") is not None:
            lot_size = LotSize.from_units(int(payload["units"]))
        elif payload.get("lots") is not None:
            lot_size = LotSize.from_lots(payload["lots"])
        else:
            raise ValueError("Either 'units' or 'lots' is required.")
        intent = OrderIntent(
            intent_id=str(payload.get("intent_id") or f"API-{uuid.uuid4().hex[:16]}"),
            symbol=symbol,
            side=side,
            order_type=order_type,
            lot_size=lot_size,
            limit_price=payload.get("limit_price"),
            stop_loss=payload.get("stop_loss"),
            take_profit=payload.get("take_profit"),
            timestamp=datetime.now(timezone.utc),
            client_tag=payload.get("client_tag"),
        )
        return self.execution.submit_intent(
            intent,
            tenant_id=payload.get("tenant_id"),
            strategy_id=payload.get("strategy_id"),
            mode=payload.get("mode"),
            account_id=payload.get("account_id"),
            is_carry_order=bool(payload.get("is_carry_order", False)),
        )

    def flatten_all(self, reason: str = "operator flatten", mode: Optional[str] = None) -> Dict[str, Any]:
        return self.execution.flatten_all(reason=reason, mode=mode)

    def arm_kill_switch(self, reason: str, scope: str = "global", target: str = "") -> None:
        if scope == "global":
            self.kill_switch.arm_global(reason)
        elif scope == "pair":
            self.kill_switch.arm_pair(target, reason)
        elif scope == "strategy":
            self.kill_switch.arm_strategy(target, reason)
        elif scope == "broker":
            self.kill_switch.arm_broker(target, reason)
        else:
            raise ValueError(f"Unknown kill-switch scope {scope!r}")
        self.audit.record(
            actor="operator",
            role="operator",
            action="risk.kill_switch_armed",
            target=target or scope,
            details={"reason": reason, "scope": scope},
        )

    def disarm_kill_switch(self, scope: str = "global", target: str = "") -> None:
        if scope == "global":
            self.kill_switch.disarm_global()
        elif scope == "pair":
            self.kill_switch.disarm_pair(target)
        elif scope == "strategy":
            self.kill_switch.disarm_strategy(target)
        elif scope == "broker":
            self.kill_switch.disarm_broker(target)
        else:
            raise ValueError(f"Unknown kill-switch scope {scope!r}")
        self.audit.record(
            actor="operator",
            role="operator",
            action="risk.kill_switch_disarmed",
            target=target or scope,
            details={"scope": scope},
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {
            "platform": {
                "started": self._started,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "environment": self.settings.environment.value,
                "settings": self.settings.public_summary(),
            },
            "brokers": self.brokers.status(),
            "risk": self.risk.snapshot(),
            "news": self.news.status(),
            "learning": self.learning.status(),
            "orders": {
                "total": len(self.orders_repo.list(limit=1000)),
                "recent": self.orders_repo.list(limit=20),
            },
            "positions": self.brokers.positions(),
            "health": self.health.snapshot(),
            "metrics": self.metrics.snapshot(),
        }

    def positions(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "broker": self.brokers.positions(account_id=account_id),
            "oms": [
                {
                    "position_id": position.position_id,
                    "symbol": position.symbol,
                    "side": position.side.value,
                    "units": position.units,
                    "average_entry_price": str(position.average_entry_price),
                    "unrealized_pnl": str(position.unrealized_pnl),
                }
                for position in self.oms.get_open_positions()
            ],
        }

    def readiness(self) -> Dict[str, Any]:
        snapshot = self.health.snapshot()
        broker_health = self.brokers.health()
        snapshot["broker_connected"] = broker_health["all_enabled_connected"]
        snapshot["broker_health"] = broker_health
        snapshot["ready"] = bool(snapshot.get("ready")) and broker_health["all_enabled_connected"]
        return snapshot
