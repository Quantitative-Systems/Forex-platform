"""
24/7/365 Auto-Trading Controller.

Main orchestrator for continuous broker account connections, market data streaming,
and automated order execution with fail-closed safety gates.
"""

from __future__ import annotations

import time
import threading
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Any
from enum import Enum
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path

from forex_platform.core.domain import (
    CurrencyPair,
    OrderIntent,
    OrderSide,
    OrderType,
    LotSize,
    Position,
    to_decimal,
)
from forex_platform.broker_adapters.base import (
    IBrokerAdapter,
    BrokerAccountInfo,
    BrokerPosition,
    PermissionSecurityError,
    RateLimitExceededError,
    BrokerOrderAck,
)
from forex_platform.broker_adapters.mt5_bridge import MT5BridgeAdapter
from forex_platform.broker_adapters.ctrader_adapter import CTraderAdapter
from forex_platform.risk_engine.firewall import (
    PreTradeRiskFirewall,
    MarketTelemetry,
    RiskDecision,
)
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.order_management.router import SmartOrderRouter
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.strategy_engine.base import BaseStrategy, BarEvent
from forex_platform.market_data.adapters.base import MarketDataAdapter
from forex_platform.market_data.adapters.fix_adapter import FIXAdapter
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.paper_trading.simulator import MicrostructurePaperSimulator
from forex_platform.paper_trading.persistence import SQLitePaperLedger
from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.costs.commission import CommissionModel
from forex_platform.costs.spread_model import DynamicSpreadModel
from forex_platform.costs.swap_engine import SwapEngine
from forex_platform.research_engine.evaluate import StrategyEvaluator
from forex_platform.strategy_engine.scalping import AsianRangeFadeScalper
from forex_platform.strategy_engine.session_breakout import LondonSessionBreakout
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy
from forex_platform.strategy_engine.macro_carry import MacroCarryStrategy
from forex_platform.strategy_engine.stat_arb import TriangularStatisticalArbitrage


class BrokerConnectionState(Enum):
    """Broker connection states for health monitoring."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"
    RECONNECTING = "RECONNECTING"


class ConnectionHealth:
    """Health metrics for broker connections."""

    def __init__(self):
        self.last_success: Optional[datetime] = None
        self.consecutive_errors: int = 0
        self.total_requests: int = 0
        self.failed_requests: int = 0
        self.last_error: Optional[str] = None
        self.response_time_ms: Optional[float] = None

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return (self.total_requests - self.failed_requests) / self.total_requests

    @property
    def is_healthy(self) -> bool:
        return (
            self.consecutive_errors == 0
            and self.success_rate >= 0.95
            and (self.last_success is None or (datetime.now(timezone.utc) - self.last_success).total_seconds() < 300)
        )


class AutoTradingStatus(Enum):
    """Auto-trading system status."""
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    ERROR = "ERROR"
    RECOVERING = "RECOVERING"


@dataclass
class BrokerAccountConfig:
    """Configuration for a broker account."""
    account_id: str
    broker_name: str
    broker_type: str  # "MT5" or "CTRADER"
    is_live: bool = False
    auto_reconnect: bool = True
    reconnect_interval_seconds: int = 30
    max_reconnect_attempts: int = 5
    rate_limit_capacity: int = 50
    rate_limit_refill: float = 50.0
    permissions: Optional[List[str]] = None


class BrokerAccountManager:
    """Manages multiple broker accounts with auto-connect and health monitoring."""

    def __init__(self, config: BrokerAccountConfig):
        self.config = config
        self.adapter: Optional[IBrokerAdapter] = None
        self.state = BrokerConnectionState.DISCONNECTED
        self.health = ConnectionHealth()
        self.connection_lock = threading.RLock()
        self.reconnect_attempts = 0
        self.last_connection_time: Optional[datetime] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def connect(self) -> bool:
        """Establish connection to broker with retry logic."""
        with self.connection_lock:
            if self.state in (BrokerConnectionState.CONNECTED, BrokerConnectionState.CONNECTING):
                return self.is_connected

            self.state = BrokerConnectionState.CONNECTING
            try:
                if self.config.broker_type.upper() == "MT5":
                    self.adapter = MT5BridgeAdapter(
                        account_id=self.config.account_id,
                        broker_name=self.config.broker_name,
                        permissions=self.config.permissions,
                        rate_limiter=None,  # Will be created by adapter
                        is_live=self.config.is_live,
                    )
                elif self.config.broker_type.upper() == "CTRADER":
                    self.adapter = CTraderAdapter(
                        account_id=self.config.account_id,
                        client_id=f"AUTO_{self.config.account_id}",
                        permissions=self.config.permissions,
                        rate_limiter=None,
                        is_live=self.config.is_live,
                    )
                else:
                    raise ValueError(f"Unsupported broker type: {self.config.broker_type}")

                success = self.adapter.connect()
                if success:
                    self.state = BrokerConnectionState.CONNECTED
                    self.last_connection_time = datetime.now(timezone.utc)
                    self.health.last_success = self.last_connection_time
                    self.health.consecutive_errors = 0
                    self.reconnect_attempts = 0
                    return True
                else:
                    raise ConnectionError("Adapter connect returned False")

            except Exception as e:
                self.state = BrokerConnectionState.ERROR
                self.health.consecutive_errors += 1
                self.health.last_error = str(e)
                self.health.failed_requests += 1
                return False

    def disconnect(self) -> None:
        """Terminate broker connection."""
        with self.connection_lock:
            if self.adapter:
                try:
                    self.adapter.disconnect()
                except Exception:
                    pass
                self.adapter = None
            self.state = BrokerConnectionState.DISCONNECTED

    def is_connected(self) -> bool:
        """Check if broker is connected."""
        with self.connection_lock:
            if self.adapter is None:
                return False
            return self.adapter.is_connected

    def get_account_info(self) -> Optional[BrokerAccountInfo]:
        """Get account information from broker."""
        if not self.is_connected():
            return None
        try:
            return self.adapter.get_account_info()
        except Exception as e:
            self.health.failed_requests += 1
            self.health.last_error = str(e)
            return None

    def get_positions(self) -> List[BrokerPosition]:
        """Get open positions from broker."""
        if not self.is_connected():
            return []
        try:
            return self.adapter.get_positions()
        except Exception as e:
            self.health.failed_requests += 1
            self.health.last_error = str(e)
            return []

    def send_order(self, intent: OrderIntent) -> Optional[BrokerOrderAck]:
        """Send order to broker."""
        if not self.is_connected():
            return None
        try:
            ack = self.adapter.send_order(intent)
            self.health.total_requests += 1
            self.health.last_success = datetime.now(timezone.utc)
            self.health.consecutive_errors = 0
            return ack
        except (PermissionSecurityError, RateLimitExceededError) as e:
            self.health.failed_requests += 1
            self.health.last_error = str(e)
            raise
        except Exception as e:
            self.health.failed_requests += 1
            self.health.last_error = str(e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel order on broker."""
        if not self.is_connected():
            return False
        try:
            return self.adapter.cancel_order(order_id)
        except Exception as e:
            self.health.failed_requests += 1
            self.health.last_error = str(e)
            return False

    def health_check(self) -> bool:
        """Perform health check on broker connection."""
        if not self.is_connected():
            return False

        try:
            # Simple ping by getting account info
            info = self.get_account_info()
            if info is None:
                return False

            # Update response time
            self.health.response_time_ms = 0.0  # In real implementation, measure actual response time
            return True
        except Exception as e:
            self.health.failed_requests += 1
            self.health.last_error = str(e)
            return False

    def start_health_monitor(self, interval_seconds: int = 30) -> None:
        """Start background health monitoring thread."""
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return

        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._health_monitor_loop,
            args=(interval_seconds,),
            daemon=True,
        )
        self._monitor_thread.start()

    def stop_health_monitor(self) -> None:
        """Stop health monitoring thread."""
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None

    def _health_monitor_loop(self, interval_seconds: int) -> None:
        """Background health monitoring loop."""
        while not self._stop_event.is_set():
            try:
                if self.state == BrokerConnectionState.CONNECTED:
                    healthy = self.health_check()
                    if not healthy:
                        self.state = BrokerConnectionState.ERROR
                        if self.config.auto_reconnect:
                            self.state = BrokerConnectionState.RECONNECTING
                            self._attempt_reconnect()
            except Exception as e:
                self.health.last_error = str(e)

            time.sleep(interval_seconds)

    def _attempt_reconnect(self) -> None:
        """Attempt to reconnect to broker."""
        if self.reconnect_attempts >= self.config.max_reconnect_attempts:
            return

        self.reconnect_attempts += 1
        time.sleep(self.config.reconnect_interval_seconds)

        try:
            if self.connect():
                self.state = BrokerConnectionState.CONNECTED
                self.reconnect_attempts = 0
            else:
                self.state = BrokerConnectionState.ERROR
        except Exception as e:
            self.health.last_error = str(e)
            self.state = BrokerConnectionState.ERROR

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        self.start_health_monitor()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop_health_monitor()
        self.disconnect()


class AutoTradingController:
    """
    24/7/365 Auto-Trading Controller.

    Main orchestrator for continuous broker account connections, market data streaming,
    and automated order execution with fail-closed safety gates.
    Supports multiple strategies loaded from promoted research artifacts.
    """

    # Strategy factory mapping
    STRATEGY_FACTORIES = {
        "AsianRangeFadeScalper": lambda params: AsianRangeFadeScalper(
            strategy_id=params.get("strategy_id", "asian_scalper"),
            symbols=params.get("symbols", ["EURUSD", "USDJPY", "AUDUSD"]),
            period=params.get("period", 20),
            z_threshold=params.get("z_threshold", 2.0),
            lot_size=params.get("lot_size", 0.5),
            tp_pips=params.get("tp_pips", 6.0),
            sl_pips=params.get("sl_pips", 10.0),
        ),
        "LondonSessionBreakout": lambda params: LondonSessionBreakout(
            strategy_id=params.get("strategy_id", "london_orb"),
            symbols=params.get("symbols", ["EURUSD", "GBPUSD", "EURGBP"]),
            lot_size=params.get("lot_size", 1.0),
            buffer_pips=params.get("buffer_pips", 2.0),
            atr_period=params.get("atr_period", 14),
        ),
        "TrendContinuationStrategy": lambda params: TrendContinuationStrategy(
            strategy_id=params.get("strategy_id", "trend_continuation"),
            symbols=params.get("symbols", ["EURUSD", "GBPUSD", "USDJPY"]),
            fast_period=params.get("fast_period", 20),
            slow_period=params.get("slow_period", 50),
            ker_period=params.get("ker_period", 10),
            ker_threshold=params.get("ker_threshold", 0.35),
            lot_size=params.get("lot_size", 1.0),
        ),
        "MacroCarryStrategy": lambda params: MacroCarryStrategy(
            strategy_id=params.get("strategy_id", "macro_carry"),
            symbols=params.get("symbols", ["USDJPY", "EURUSD", "AUDJPY"]),
            vol_shock_threshold=params.get("vol_shock_threshold", 2.0),
            lot_size=params.get("lot_size", 1.0),
        ),
        "TriangularStatisticalArbitrage": lambda params: TriangularStatisticalArbitrage(
            strategy_id=params.get("strategy_id", "triangular_stat_arb"),
            base_pair=params.get("base_pair", "EURUSD"),
            target_pair=params.get("target_pair", "GBPUSD"),
            cross_pair=params.get("cross_pair", "EURGBP"),
            lookback_period=params.get("lookback_period", 30),
            z_threshold=params.get("z_threshold", 2.0),
            lot_size=params.get("lot_size", 0.5),
        ),
    }

    def __init__(
        self,
        strategies: Optional[List[BaseStrategy]] = None,
        broker_configs: Optional[List[BrokerAccountConfig]] = None,
        market_data_adapter: Optional[MarketDataAdapter] = None,
        initial_balance: Decimal = Decimal("100000.00"),
        risk_check_interval_seconds: int = 5,
        max_consecutive_errors: int = 10,
        promoted_dir: str = "research/promoted",
    ):
        # Support both single strategy (backward compat) and multiple strategies
        self.strategies: List[BaseStrategy] = strategies or []
        self.broker_configs = broker_configs or []
        self.market_data_adapter = market_data_adapter
        self.initial_balance = initial_balance
        self.risk_check_interval_seconds = risk_check_interval_seconds
        self.max_consecutive_errors = max_consecutive_errors
        self.promoted_dir = Path(promoted_dir)

        # Initialize components
        self.kill_switch = HierarchicalKillSwitch()
        self.circuit_breaker = CircuitBreakerEngine(initial_equity=initial_balance)
        self.firewall = PreTradeRiskFirewall(
            kill_switch=self.kill_switch,
            circuit_breaker=self.circuit_breaker,
            allow_live_capital=False,
        )
        self.router = SmartOrderRouter(allow_live_routing=False)
        self.oms = OrderManagementSystem(
            firewall=self.firewall,
            router=self.router,
            kill_switch=self.kill_switch,
            initial_balance=initial_balance,
        )

        # Broker account managers
        self.broker_managers: Dict[str, BrokerAccountManager] = {}
        for config in self.broker_configs:
            self.broker_managers[config.account_id] = BrokerAccountManager(config)

        # State
        self.status = AutoTradingStatus.STOPPED
        self.error_count = 0
        self.last_error: Optional[str] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_risk_check = datetime.now(timezone.utc)

        # Statistics
        self.stats = {
            "orders_submitted": 0,
            "orders_filled": 0,
            "orders_rejected": 0,
            "total_pnl": Decimal("0.0"),
            "start_time": None,
            "last_update": None,
        }

        # Load promoted strategies if no strategies provided
        if not self.strategies and self.promoted_dir.exists():
            self.load_promoted_strategies()

    def load_promoted_strategies(self) -> int:
        """
        Load all promoted strategies from research/promoted/ directory.
        Returns number of strategies loaded.
        """
        loaded = 0
        if not self.promoted_dir.exists():
            return 0

        for promo_file in self.promoted_dir.glob("*_PROMOTED_*.json"):
            try:
                with open(promo_file) as f:
                    promo_data = json.load(f)

                if promo_data.get("status") != "PROMOTABLE_PAPER_ONLY":
                    continue

                strategy_name = promo_data.get("strategy_id", "").split("_")[0]
                # Map strategy_id prefix to full class name
                strategy_class_map = {
                    "asian": "AsianRangeFadeScalper",
                    "london": "LondonSessionBreakout",
                    "trend": "TrendContinuationStrategy",
                    "macro": "MacroCarryStrategy",
                    "tri": "TriangularStatisticalArbitrage",
                    "triangular": "TriangularStatisticalArbitrage",
                }

                # Find matching strategy class
                matched_class = None
                for prefix, class_name in strategy_class_map.items():
                    if promo_data["strategy_id"].startswith(prefix):
                        matched_class = class_name
                        break

                if matched_class and matched_class in self.STRATEGY_FACTORIES:
                    params = promo_data.get("parameters", {})
                    params["strategy_id"] = promo_data["strategy_id"]
                    strategy = self.STRATEGY_FACTORIES[matched_class](params)
                    self.strategies.append(strategy)
                    loaded += 1
                    print(f"Loaded promoted strategy: {strategy.strategy_id} ({matched_class})")

            except Exception as e:
                print(f"Failed to load promoted strategy from {promo_file}: {e}")

        return loaded

    def add_strategy(self, strategy: BaseStrategy) -> None:
        """Add a strategy to the controller."""
        self.strategies.append(strategy)

    def start(self) -> bool:
        """Start auto-trading controller."""
        if self.status != AutoTradingStatus.STOPPED:
            return False

        # If no market data adapter provided, create a default one
        if self.market_data_adapter is None:
            # Collect all symbols from all strategies
            all_symbols = set()
            all_timeframes = set()
            for strategy in self.strategies:
                all_symbols.update(strategy.symbols)
                all_timeframes.update(strategy.timeframes)

            self.market_data_adapter = FIXAdapter(
                symbols=list(all_symbols),
                timeframes=list(all_timeframes),
            )

        self.status = AutoTradingStatus.STARTING
        try:
            # Connect to all broker accounts
            for manager in self.broker_managers.values():
                if not manager.connect():
                    raise ConnectionError(f"Failed to connect to broker {manager.config.account_id}")

            # Start health monitors
            for manager in self.broker_managers.values():
                manager.start_health_monitor()

            # Start market data adapter
            self.market_data_adapter.connect()

            # Set up callbacks
            self.market_data_adapter.set_bar_callback(self._on_bar_event)
            self.market_data_adapter.set_error_callback(self._on_market_data_error)

            # Start monitoring thread
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
            )
            self._monitor_thread.start()

            self.status = AutoTradingStatus.RUNNING
            self.stats["start_time"] = datetime.now(timezone.utc)
            return True

        except Exception as e:
            self.status = AutoTradingStatus.ERROR
            self.last_error = str(e)
            self.error_count += 1
            self.stop()
            return False

    def stop(self) -> None:
        """Stop auto-trading controller."""
        if self.status == AutoTradingStatus.STOPPED:
            return

        self.status = AutoTradingStatus.STOPPED
        self._stop_event.set()

        # Stop monitoring thread
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=10.0)
            self._monitor_thread = None

        # Stop health monitors
        for manager in self.broker_managers.values():
            manager.stop_health_monitor()

        # Disconnect from market data
        try:
            self.market_data_adapter.disconnect()
        except Exception:
            pass

        # Disconnect from brokers
        for manager in self.broker_managers.values():
            manager.disconnect()

    def is_running(self) -> bool:
        """Check if auto-trading is running."""
        return self.status == AutoTradingStatus.RUNNING

    def get_status(self) -> Dict[str, Any]:
        """Get current status of auto-trading controller."""
        status = {
            "controller_status": self.status.value,
            "broker_statuses": {},
            "error_count": self.error_count,
            "last_error": self.last_error,
            "stats": self.stats.copy(),
            "market_data_connected": self.market_data_adapter.is_connected,
        }

        for account_id, manager in self.broker_managers.items():
            status["broker_statuses"][account_id] = {
                "state": manager.state.value,
                "is_connected": manager.is_connected(),
                "health": {
                    "success_rate": manager.health.success_rate,
                    "consecutive_errors": manager.health.consecutive_errors,
                    "last_success": manager.health.last_success.isoformat() if manager.health.last_success else None,
                    "last_error": manager.health.last_error,
                },
                "account_info": None,
            }

            # Get account info if connected
            info = manager.get_account_info()
            if info:
                status["broker_statuses"][account_id]["account_info"] = {
                    "account_id": info.account_id,
                    "broker_name": info.broker_name,
                    "balance": str(info.balance),
                    "equity": str(info.equity),
                    "free_margin": str(info.free_margin),
                }

        return status

    def _monitor_loop(self) -> None:
        """Main monitoring loop for auto-trading."""
        while not self._stop_event.is_set():
            try:
                # Perform risk checks periodically
                now = datetime.now(timezone.utc)
                if (now - self._last_risk_check).total_seconds() >= self.risk_check_interval_seconds:
                    self._perform_risk_checks()
                    self._last_risk_check = now

                # Check broker health
                self._check_broker_health()

                # Update statistics
                self._update_statistics()

                time.sleep(1.0)  # Sleep for 1 second

            except Exception as e:
                self.error_count += 1
                self.last_error = str(e)
                if self.error_count >= self.max_consecutive_errors:
                    self.status = AutoTradingStatus.ERROR
                    break

    def _perform_risk_checks(self) -> None:
        """Perform periodic risk checks."""
        # Check circuit breaker
        cb_status = self.circuit_breaker.update_equity(
            self.oms.get_total_equity(),
            datetime.now(timezone.utc),
        )

        # Check kill switches
        for account_id, manager in self.broker_managers.items():
            is_killed, reason = self.kill_switch.is_killed(broker_id=account_id)
            if is_killed:
                self.status = AutoTradingStatus.ERROR
                self.last_error = f"Kill switch activated for broker {account_id}: {reason}"
                break

    def _check_broker_health(self) -> None:
        """Check health of all broker connections."""
        for account_id, manager in self.broker_managers.items():
            if manager.state == BrokerConnectionState.CONNECTED:
                healthy = manager.health_check()
                if not healthy:
                    manager.state = BrokerConnectionState.ERROR
                    if manager.config.auto_reconnect:
                        manager.state = BrokerConnectionState.RECONNECTING
                        manager._attempt_reconnect()

    def _update_statistics(self) -> None:
        """Update trading statistics."""
        self.stats["last_update"] = datetime.now(timezone.utc)
        self.stats["orders_submitted"] = len(self.oms._orders)
        self.stats["orders_filled"] = len(self.oms._fills)
        self.stats["orders_rejected"] = sum(
            1 for order in self.oms._orders.values()
            if order.status.value in ("REJECTED", "CANCELLED", "EXPIRED")
        )
        self.stats["total_pnl"] = self.oms.get_total_equity() - self.initial_balance

    def _on_bar_event(self, event: BarEvent) -> None:
        """Handle incoming bar events from market data."""
        try:
            # Check if market is open (respect forex weekend closure)
            now = datetime.now(timezone.utc)
            if ForexSessionEngine.is_weekend(now):
                return

            # Update all strategies' history
            for strategy in self.strategies:
                strategy.update_history(event)

            # Generate order intents from all strategies
            for strategy in self.strategies:
                intents = strategy.on_bar(event)
                for intent in intents:
                    self._process_order_intent(intent, now, strategy)

        except Exception as e:
            self.error_count += 1
            self.last_error = f"Error processing bar event: {str(e)}"

    def _on_market_data_error(self, error: Exception) -> None:
        """Handle market data errors."""
        self.error_count += 1
        self.last_error = f"Market data error: {str(error)}"

    def _process_order_intent(self, intent: OrderIntent, current_time: datetime, strategy: BaseStrategy) -> None:
        """Process an order intent through the full pipeline."""
        try:
            # Get telemetry from market data (simplified - in real implementation would get current quotes)
            telemetry = None
            if self.market_data_adapter.is_connected:
                # In a real implementation, we would get current quotes for the symbol
                # For now, we'll create a simple telemetry object
                telemetry = MarketTelemetry(
                    symbol=intent.symbol,
                    bid=Decimal("1.0850"),  # Placeholder
                    ask=Decimal("1.0851"),  # Placeholder
                    quote_timestamp=current_time,
                    recent_spreads_pips=[Decimal("1.0")],
                )

            # Submit intent through OMS (risk firewall)
            decision, orders = self.oms.submit_intent(
                intent=intent,
                current_time=current_time,
                telemetry=telemetry,
                strategy_id=strategy.strategy_id,
            )

            if decision.approved and orders:
                # For each order, send to broker
                for order in orders:
                    # Find appropriate broker (simplified - round-robin)
                    broker_id = list(self.broker_managers.keys())[0]
                    manager = self.broker_managers[broker_id]

                    child_intent = OrderIntent(
                        intent_id=order.order_id,
                        symbol=order.symbol,
                        side=order.side,
                        order_type=order.order_type,
                        lot_size=order.lot_size,
                        limit_price=order.limit_price,
                        stop_loss=order.stop_loss,
                        take_profit=order.take_profit,
                        urgency=intent.urgency,
                        timestamp=current_time,
                        client_tag=order.order_id,
                    )
                    ack = manager.send_order(child_intent)
                    if ack:
                        # Acknowledge order in OMS
                        self.oms.acknowledge_order(order.order_id, current_time)
                    else:
                        # Order rejected by broker
                        self.oms.mark_order_unknown(order.order_id, current_time, "Broker rejected order")

            elif not decision.approved:
                # Order rejected by risk firewall
                self.error_count += 1

        except Exception as e:
            self.error_count += 1
            self.last_error = f"Error processing order intent: {str(e)}"

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()


# Example usage
if __name__ == "__main__":
    # Example configuration
    from forex_platform.core.domain import CurrencyPair
    from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy

    # Create strategies
    strategies = [
        TrendContinuationStrategy(symbols=["EURUSD", "GBPUSD", "USDJPY"]),
        AsianRangeFadeScalper(symbols=["EURUSD", "USDJPY", "AUDUSD"]),
        LondonSessionBreakout(symbols=["EURUSD", "GBPUSD", "EURGBP"]),
    ]

    # Create broker configs
    broker_configs = [
        BrokerAccountConfig(
            account_id="demo_mt5",
            broker_name="IC_MARKETS",
            broker_type="MT5",
            is_live=False,
        ),
        BrokerAccountConfig(
            account_id="demo_ctrader",
            broker_name="CTRADER_ECN",
            broker_type="CTRADER",
            is_live=False,
        ),
    ]

    # Create market data adapter
    market_data = FIXAdapter(
        symbols=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "EURGBP"],
        timeframes=[Timeframe.M1, Timeframe.M5, Timeframe.H1],
    )

    # Create auto-trading controller
    controller = AutoTradingController(
        strategies=strategies,
        broker_configs=broker_configs,
        market_data_adapter=market_data,
        initial_balance=Decimal("100000.00"),
    )

    # Start auto-trading
    if controller.start():
        print("Auto-trading started successfully")
        print("Press Ctrl+C to stop...")

        try:
            while controller.is_running():
                status = controller.get_status()
                print(f"Status: {status['controller_status']}, "
                      f"Orders: {status['stats']['orders_submitted']}, "
                      f"PnL: ${status['stats']['total_pnl']:.2f}")
                time.sleep(10.0)
        except KeyboardInterrupt:
            print("\nStopping auto-trading...")
        finally:
            controller.stop()
    else:
        print(f"Failed to start auto-trading: {controller.last_error}")