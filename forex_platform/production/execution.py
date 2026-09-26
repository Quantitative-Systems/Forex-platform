"""
Production execution and risk orchestration.

The execution service is the only component allowed to send orders to a
broker. It combines the pre-trade risk firewall with portfolio-wide checks
(daily loss, news blackout, circuit breaker, order-size cap), routes each
sliced child order through the broker registry, persists every order/fill,
and writes an audit record for each decision.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import (
    Fill,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    to_decimal,
)
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.portfolio_engine.regime import MarketRegime
from forex_platform.portfolio_engine.target_allocator import TargetPosition, TargetPositionAllocator
from forex_platform.production.brokers import BrokerRegistry, NoRouteAvailable
from forex_platform.production.observability import HealthRegistry, MetricsRegistry, get_logger
from forex_platform.production.settings import Settings
from forex_platform.production.store import AuditLog, OrderRepository, StateStore, utc_now
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.firewall import MarketTelemetry, PreTradeRiskFirewall, RiskDecision
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch

logger = get_logger(__name__)


class ExecutionResult(BaseModel):
    """Outcome of a submit-intent call."""

    model_config = ConfigDict(frozen=True)

    approved: bool
    reason: str
    intent_id: str
    child_orders: List[Dict[str, Any]] = Field(default_factory=list)
    routes: List[Dict[str, Any]] = Field(default_factory=list)
    risk: Optional[Dict[str, Any]] = None


class RiskKernel:
    """Portfolio-wide pre-trade checks layered on the 6-tier firewall."""

    DAILY_KEY = "risk.daily_pnl"

    def __init__(
        self,
        settings: Settings,
        firewall: PreTradeRiskFirewall,
        circuit_breaker: CircuitBreakerEngine,
        kill_switch: HierarchicalKillSwitch,
        state: StateStore,
        metrics: Optional[MetricsRegistry] = None,
        news: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.firewall = firewall
        self.circuit_breaker = circuit_breaker
        self.kill_switch = kill_switch
        self.state = state
        self.metrics = metrics or MetricsRegistry()
        self.news = news
        self._status: Optional[Any] = None
        self._lock = threading.Lock()

    def update_equity(
        self, equity: Decimal | float | str, at: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Feed equity into the circuit breaker and cache its status."""
        value = to_decimal(equity)
        timestamp = at or datetime.now(timezone.utc)
        with self._lock:
            status = self.circuit_breaker.update_equity(value, timestamp)
            self._status = status
        halted = bool(
            getattr(status, "is_safe_mode", False) or getattr(status, "is_daily_halted", False)
        )
        self.metrics.set_gauge("forex_risk_equity", float(value))
        self.metrics.set_gauge("forex_risk_circuit_halted", 1.0 if halted else 0.0)
        if hasattr(status, "sizing_multiplier"):
            self.metrics.set_gauge(
                "forex_risk_sizing_multiplier", float(status.sizing_multiplier)
            )
        if hasattr(status, "model_dump"):
            return status.model_dump(mode="json")
        return {"status": str(status)}

    def daily_pnl(self, at: Optional[datetime] = None) -> Decimal:
        day = (at or datetime.now(timezone.utc)).date().isoformat()
        payload = self.state.get(f"{self.DAILY_KEY}.{day}", {}) or {}
        return to_decimal(payload.get("pnl", "0"))

    def record_realized_pnl(
        self, pnl: Decimal | float | str, at: Optional[datetime] = None
    ) -> Decimal:
        day = (at or datetime.now(timezone.utc)).date().isoformat()
        key = f"{self.DAILY_KEY}.{day}"
        payload = self.state.get(key, {}) or {}
        updated = to_decimal(payload.get("pnl", "0")) + to_decimal(pnl)
        self.state.set(key, {"pnl": str(updated), "updated_at": utc_now()})
        self.metrics.set_gauge("forex_risk_daily_pnl", float(updated))
        return updated

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        telemetry: Optional[MarketTelemetry] = None,
        tenant_id: Optional[str] = None,
        broker_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        is_carry_order: bool = False,
        open_positions: Optional[List[Position]] = None,
        account_equity: Optional[Decimal] = None,
        now: Optional[datetime] = None,
    ) -> RiskDecision:
        current_time = now or datetime.now(timezone.utc)
        equity = to_decimal(account_equity or self.settings.account_equity)

        kill_reason = ""
        try:
            kill_result = self.kill_switch.is_killed(
                symbol=intent.symbol,
                tenant_id=tenant_id,
                broker_id=broker_id,
                strategy_id=strategy_id,
            )
        except TypeError:  # pragma: no cover - older signature compatibility
            kill_result = self.kill_switch.is_killed()
        if isinstance(kill_result, tuple):
            killed, kill_reason = kill_result[0], str(kill_result[1] or "")
        else:
            killed, kill_reason = bool(kill_result), ""
        if killed:
            return RiskDecision(
                approved=False,
                tier_failed=2,
                reason=(
                    "KILL SWITCH ACTIVE: routing blocked by a hierarchical kill switch. "
                    f"{kill_reason}".strip()
                ),
                sizing_multiplier=Decimal("0.0"),
            )

        if intent.lot_size.units > self.settings.max_order_units:
            return RiskDecision(
                approved=False,
                tier_failed=1,
                reason=(
                    f"ORDER SIZE CAP: {intent.lot_size.units} units exceeds "
                    f"max_order_units={self.settings.max_order_units}."
                ),
                sizing_multiplier=Decimal("0.0"),
            )

        loss_limit = equity * self.settings.max_daily_loss_pct / Decimal("100")
        daily = self.daily_pnl(current_time)
        if loss_limit > 0 and daily <= -loss_limit:
            return RiskDecision(
                approved=False,
                tier_failed=6,
                reason=(
                    f"DAILY LOSS LIMIT: realized daily PnL {daily} breached "
                    f"-{loss_limit} ({self.settings.max_daily_loss_pct}% of equity)."
                ),
                sizing_multiplier=Decimal("0.0"),
            )

        with self._lock:
            status = self._status
        circuit_halted = bool(
            status is not None
            and (
                getattr(status, "is_safe_mode", False)
                or getattr(status, "is_daily_halted", False)
            )
        )
        if circuit_halted:
            return RiskDecision(
                approved=False,
                tier_failed=6,
                reason="CIRCUIT BREAKER: trading halted by drawdown/daily-loss circuit breaker.",
                sizing_multiplier=Decimal("0.0"),
            )

        if self.news is not None and self.settings.news_enabled and not is_carry_order:
            blackout = self.news.blackout_for(intent.symbol, current_time)
            if blackout is not None:
                return RiskDecision(
                    approved=False,
                    tier_failed=5,
                    reason=f"NEWS BLACKOUT: {blackout.reason}",
                    sizing_multiplier=Decimal("0.0"),
                )

        return self.firewall.evaluate_order(
            intent=intent,
            current_time=current_time,
            telemetry=telemetry,
            tenant_id=tenant_id,
            broker_id=broker_id,
            strategy_id=strategy_id,
            is_carry_order=is_carry_order,
            open_positions=open_positions,
            account_equity=equity,
        )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            status = self._status
        return {
            "daily_pnl": str(self.daily_pnl()),
            "daily_loss_limit_pct": str(self.settings.max_daily_loss_pct),
            "max_drawdown_pct": str(self.settings.max_drawdown_pct),
            "circuit_breaker": (
                status.model_dump(mode="json") if status is not None and hasattr(status, "model_dump") else None
            ),
        }

class ProductionExecutionService:
    """Single write path from strategy intent to broker execution."""

    def __init__(
        self,
        settings: Settings,
        brokers: BrokerRegistry,
        oms: OrderManagementSystem,
        risk: RiskKernel,
        orders_repo: OrderRepository,
        audit: Optional[AuditLog] = None,
        metrics: Optional[MetricsRegistry] = None,
        health: Optional[HealthRegistry] = None,
        market_data: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self.brokers = brokers
        self.oms = oms
        self.risk = risk
        self.orders_repo = orders_repo
        self.audit = audit
        self.metrics = metrics or MetricsRegistry()
        self.health = health or HealthRegistry()
        self.market_data = market_data
        self._lock = threading.RLock()

    def _child_intent(self, order: Any, parent: OrderIntent, now: datetime) -> OrderIntent:
        return OrderIntent(
            intent_id=order.intent_id or order.order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            lot_size=order.lot_size,
            limit_price=order.limit_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            urgency=parent.urgency,
            timestamp=now,
            # Deterministic client tag: the broker gateway uses it for idempotency.
            client_tag=order.order_id,
        )

    def _persist_order(
        self,
        order: Any,
        account_id: Optional[str],
        broker: Optional[str],
        status: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.orders_repo.upsert_order(
            {
                "client_order_id": order.order_id,
                "intent_id": order.intent_id or "",
                "account_id": account_id,
                "broker": broker,
                "symbol": order.symbol,
                "side": order.side.value,
                "order_type": order.order_type.value,
                "units": order.lot_size.units,
                "limit_price": order.limit_price,
                "stop_loss": order.stop_loss,
                "take_profit": order.take_profit,
                "status": status or order.status.value,
                "filled_units": order.filled_units,
                "average_price": order.average_fill_price,
                "reason": reason,
                "created_at": order.created_at.isoformat(),
            }
        )

    def submit_intent(
        self,
        intent: OrderIntent,
        *,
        tenant_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        mode: Optional[str] = None,
        account_id: Optional[str] = None,
        telemetry: Optional[MarketTelemetry] = None,
        is_carry_order: bool = False,
        account_equity: Optional[Decimal] = None,
    ) -> ExecutionResult:
        now = datetime.now(timezone.utc)
        if telemetry is None and self.market_data is not None:
            telemetry = self.market_data.telemetry_for(
                intent.symbol, mode or self.settings.default_execution_mode
            )
        decision = self.risk.evaluate(
            intent,
            telemetry=telemetry,
            tenant_id=tenant_id,
            broker_id=account_id,
            strategy_id=strategy_id,
            is_carry_order=is_carry_order,
            open_positions=self.oms.get_open_positions(),
            account_equity=account_equity,
            now=now,
        )
        risk_payload = decision.model_dump(mode="json")
        if not decision.approved:
            self.metrics.increment(
                "forex_execution_rejected_total", labels={"tier": str(decision.tier_failed)}
            )
            if self.audit:
                self.audit.record(
                    actor="system",
                    role="system",
                    action="execution.rejected",
                    target=intent.intent_id,
                    details={"reason": decision.reason, "risk": risk_payload},
                )
            return ExecutionResult(
                approved=False,
                reason=decision.reason,
                intent_id=intent.intent_id,
                risk=risk_payload,
            )

        try:
            _, child_orders = self.oms.submit_intent(
                intent,
                now,
                telemetry=telemetry,
                tenant_id=tenant_id,
                broker_id=account_id,
                strategy_id=strategy_id,
                is_carry_order=is_carry_order,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a rejected result
            logger.error("OMS routing failed for intent %s: %s", intent.intent_id, exc)
            if self.audit:
                self.audit.record(
                    actor="system",
                    role="system",
                    action="execution.routing_error",
                    target=intent.intent_id,
                    details={"error": str(exc)},
                )
            return ExecutionResult(
                approved=False,
                reason=f"OMS routing failed: {exc}",
                intent_id=intent.intent_id,
                risk=risk_payload,
            )

        spec_by_account = {spec.account_id: spec for spec in self.brokers.list_specs()}
        routes: List[Dict[str, Any]] = []
        for order in child_orders:
            child = self._child_intent(order, intent, now)
            self._persist_order(order, account_id, None, status=OrderStatus.PENDING.value)
            try:
                used_account, ack = self.brokers.send_order(child, mode=mode, account_id=account_id)
            except Exception as exc:  # noqa: BLE001 - per-order failover/rejection
                order.status = OrderStatus.REJECTED
                self._persist_order(order, account_id, None, reason=str(exc))
                try:
                    self.oms.mark_order_unknown(order.order_id, now, f"Broker route failed: {exc}")
                except Exception:  # noqa: BLE001
                    pass
                routes.append({"order_id": order.order_id, "status": "REJECTED", "error": str(exc)})
                self.metrics.increment("forex_execution_failed_total")
                continue

            spec = spec_by_account.get(used_account)
            order.status = ack.status
            if ack.fill_price is not None:
                order.average_fill_price = ack.fill_price
            if ack.filled_units:
                order.filled_units = ack.filled_units
            try:
                self.oms.acknowledge_order(order.order_id, ack.timestamp)
            except Exception as exc:  # noqa: BLE001 - state machine may already be terminal
                logger.warning("OMS acknowledge failed for %s: %s", order.order_id, exc)

            fill_recorded = False
            if ack.status == OrderStatus.FILLED and ack.fill_price is not None:
                units = ack.filled_units or order.lot_size.units
                fill = Fill(
                    fill_id=f"FILL-{ack.broker_ticket or order.order_id}",
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    fill_price=ack.fill_price,
                    units=units,
                    timestamp=ack.timestamp,
                )
                try:
                    self.oms.process_fill(fill)
                    fill_recorded = True
                except Exception as exc:  # noqa: BLE001 - persistence still required
                    logger.error("OMS fill attribution failed for %s: %s", order.order_id, exc)
                self.orders_repo.add_fill(
                    {
                        "fill_id": fill.fill_id,
                        "client_order_id": order.order_id,
                        "broker_ticket": ack.broker_ticket,
                        "symbol": fill.symbol,
                        "side": fill.side.value,
                        "units": fill.units,
                        "price": fill.fill_price,
                        "timestamp": fill.timestamp.isoformat(),
                    }
                )

            self._persist_order(
                order, used_account, spec.broker if spec else None, status=ack.status.value
            )
            routes.append(
                {
                    "order_id": order.order_id,
                    "account_id": used_account,
                    "broker": spec.broker if spec else None,
                    "ticket": ack.broker_ticket,
                    "status": ack.status.value,
                    "fill_price": str(ack.fill_price) if ack.fill_price is not None else None,
                    "filled_units": ack.filled_units,
                    "fill_recorded": fill_recorded,
                    "message": ack.message,
                }
            )
            self.metrics.increment("forex_execution_submitted_total")

        if self.audit:
            self.audit.record(
                actor="system",
                role="system",
                action="execution.submitted",
                target=intent.intent_id,
                details={
                    "strategy_id": strategy_id,
                    "tenant_id": tenant_id,
                    "mode": mode or self.settings.default_execution_mode,
                    "routes": routes,
                },
            )
        return ExecutionResult(
            approved=True,
            reason=decision.reason,
            intent_id=intent.intent_id,
            child_orders=[order.model_dump(mode="json") for order in child_orders],
            routes=routes,
            risk=risk_payload,
        )

    def submit_targets(
        self,
        targets: List[TargetPosition],
        *,
        regime: MarketRegime = MarketRegime.UNKNOWN,
        mode: Optional[str] = None,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        account_equity: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """Allocate target positions and submit every delta through the normal path.

        This method never sends a target directly to a broker. It first computes
        bounded deltas, then reuses ``submit_intent`` for risk, OMS, broker,
        persistence, and audit handling.
        """
        current_units: Dict[str, int] = {}
        for position in self.oms.get_open_positions():
            signed = int(position.units)
            if position.side == OrderSide.SELL:
                signed = -signed
            current_units[position.symbol] = current_units.get(position.symbol, 0) + signed
        allocator = TargetPositionAllocator()
        allocation = allocator.allocate(
            targets,
            current_units=current_units,
            regime=regime,
            timestamp=datetime.now(timezone.utc),
        )
        results: List[Dict[str, Any]] = []
        for intent in allocation.intents:
            result = self.submit_intent(
                intent,
                strategy_id=strategy_id or intent.client_tag,
                mode=mode,
                account_id=account_id,
                account_equity=account_equity,
            )
            results.append(result.model_dump(mode="json"))
        if self.audit:
            self.audit.record(
                actor="system",
                role="system",
                action="execution.targets_allocated",
                target=strategy_id or "portfolio",
                details={
                    "regime": regime.value,
                    "rejected": allocation.rejected,
                    "gross_units": allocation.gross_units,
                    "results": results,
                },
            )
        return {
            "approved": allocation.approved and all(item.get("approved") for item in results),
            "regime": regime.value,
            "allocation": {
                "rejected": allocation.rejected,
                "gross_units": allocation.gross_units,
                "intent_count": len(allocation.intents),
            },
            "results": results,
        }

    def reconcile(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """Compare broker positions with the local OMS ledger."""
        remote = self.brokers.positions(mode=mode)
        local_positions = self.oms.get_open_positions()
        remote_by_symbol: Dict[str, Dict[str, Any]] = {}
        for account_id, positions in remote.items():
            for position in positions:
                entry = remote_by_symbol.setdefault(
                    position["symbol"], {"units": 0, "accounts": []}
                )
                signed_units = int(position["units"])
                if str(position["side"]).upper() == "SELL":
                    signed_units = -signed_units
                entry["units"] += signed_units
                entry["accounts"].append(account_id)

        local_by_symbol: Dict[str, int] = {}
        for position in local_positions:
            signed = position.units if position.side == OrderSide.BUY else -position.units
            local_by_symbol[position.symbol] = local_by_symbol.get(position.symbol, 0) + signed

        alerts: List[Dict[str, Any]] = []
        for symbol in sorted(set(remote_by_symbol) | set(local_by_symbol)):
            remote_units = remote_by_symbol.get(symbol, {}).get("units", 0)
            local_units = local_by_symbol.get(symbol, 0)
            if remote_units != local_units:
                alerts.append(
                    {
                        "symbol": symbol,
                        "remote_units": remote_units,
                        "local_units": local_units,
                        "difference": remote_units - local_units,
                    }
                )
        if alerts:
            self.metrics.increment("forex_reconciliation_alerts_total", value=len(alerts))
            for alert in alerts:
                logger.error("Reconciliation mismatch: %s", alert)
                # Fail closed: lock the affected pair until an operator reconciles.
                try:
                    self.oms.kill_switch.arm_pair(
                        alert["symbol"], "Reconciliation mismatch between broker and OMS ledger"
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            self.metrics.set_gauge("forex_reconciliation_alerts", 0)
        if self.audit:
            self.audit.record(
                actor="system",
                role="system",
                action="reconciliation.run",
                target=mode or self.settings.default_execution_mode,
                details={"alerts": alerts, "remote_accounts": list(remote.keys())},
            )
        return {"alerts": alerts, "remote": remote, "local_positions": len(local_positions)}

    def flatten_all(
        self,
        *,
        reason: str = "operator flatten",
        mode: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close every open broker position with opposing market orders."""
        now = datetime.now(timezone.utc)
        remote = self.brokers.positions(account_id=account_id, mode=mode)
        actions: List[Dict[str, Any]] = []
        for account, positions in remote.items():
            for position in positions:
                units = int(position["units"])
                if units <= 0:
                    continue
                side = OrderSide.SELL if str(position["side"]).upper() == "BUY" else OrderSide.BUY
                closing = OrderIntent(
                    intent_id=f"FLAT-{uuid.uuid4().hex[:12]}",
                    symbol=position["symbol"],
                    side=side,
                    order_type=OrderType.MARKET,
                    lot_size=LotSize.from_units(units),
                    timestamp=now,
                    client_tag=f"FLAT-{position.get('ticket_id', uuid.uuid4().hex[:8])}",
                )
                try:
                    used_account, ack = self.brokers.send_order(closing, account_id=account)
                    actions.append(
                        {
                            "account_id": used_account,
                            "ticket": position.get("ticket_id"),
                            "symbol": position["symbol"],
                            "units": units,
                            "status": ack.status.value,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    actions.append(
                        {
                            "account_id": account,
                            "ticket": position.get("ticket_id"),
                            "symbol": position["symbol"],
                            "status": "FAILED",
                            "error": str(exc),
                        }
                    )
        if self.audit:
            self.audit.record(
                actor="system",
                role="system",
                action="risk.flatten_all",
                target=account_id or mode or self.settings.default_execution_mode,
                details={"reason": reason, "actions": actions},
            )
        self.metrics.increment("forex_flatten_all_total")
        return {"reason": reason, "actions": actions}

    def cancel(self, order_id: str, account_id: str) -> bool:
        cancelled = self.brokers.cancel(order_id, account_id)
        if cancelled:
            try:
                self.oms.cancel_order(order_id, datetime.now(timezone.utc), "operator cancel")
            except Exception:  # noqa: BLE001
                pass
        if self.audit:
            self.audit.record(
                actor="system",
                role="system",
                action="execution.cancel",
                target=order_id,
                details={"account_id": account_id, "cancelled": cancelled},
            )
        return cancelled

    def snapshot(self) -> Dict[str, Any]:
        return {
            "risk": self.risk.snapshot(),
            "orders": len(self.orders_repo.list(limit=1000)),
            "fills": len(self.orders_repo.list_fills(limit=1000)),
        }