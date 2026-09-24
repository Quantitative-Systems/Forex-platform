"""
Position and State Reconciliation Engine.
Continuously reconciles local Order Management System (OMS) positions against
remote broker gateway positions. Any detected discrepancy (phantom positions,
lost positions, size or side mismatches) immediately halts trading, arms the
global kill switch, and triggers SAFE_MODE.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.broker_adapters.base import BrokerPosition, IBrokerAdapter
from forex_platform.core.domain import OrderSide, Position
from forex_platform.order_management.oms import OrderManagementSystem
from forex_platform.risk_engine.circuit_breakers import CircuitBreakerEngine
from forex_platform.risk_engine.kill_switches import HierarchicalKillSwitch

logger = logging.getLogger(__name__)


class DiscrepancyType(str, Enum):
    PHANTOM_POSITION = "PHANTOM_POSITION"  # Present on broker but absent in OMS
    LOST_POSITION = "LOST_POSITION"        # Present in OMS but absent on broker
    SIDE_MISMATCH = "SIDE_MISMATCH"        # Conflicting order sides (e.g. BUY vs SELL)
    SIZE_MISMATCH = "SIZE_MISMATCH"        # Mismatch in held volume/units


class ReconciliationDiscrepancy(BaseModel):
    """Detailed record of a detected state mismatch between OMS and broker."""
    model_config = ConfigDict(frozen=True)

    discrepancy_type: DiscrepancyType
    symbol: str
    oms_units: int
    broker_units: int
    oms_side: Optional[OrderSide]
    broker_side: Optional[OrderSide]
    description: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReconciliationReport(BaseModel):
    """Summary of a reconciliation execution cycle."""
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    is_clean: bool
    discrepancies: List[ReconciliationDiscrepancy]
    matched_positions_count: int
    summary_message: str


class PositionReconciler:
    """
    Automated 5-second asynchronous reconciliation daemon and one-shot validator.
    Ensures mathematical and state consistency across the platform.
    """

    def __init__(
        self,
        oms: OrderManagementSystem,
        broker: IBrokerAdapter,
        kill_switch: HierarchicalKillSwitch,
        circuit_breaker: Optional[CircuitBreakerEngine] = None,
        interval_seconds: float = 5.0,
    ):
        self.oms = oms
        self.broker = broker
        self.kill_switch = kill_switch
        self.circuit_breaker = circuit_breaker
        self.interval_seconds = interval_seconds

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._latest_report: Optional[ReconciliationReport] = None
        self._reconciliation_count: int = 0

    @property
    def latest_report(self) -> Optional[ReconciliationReport]:
        return self._latest_report

    @property
    def reconciliation_count(self) -> int:
        return self._reconciliation_count

    def reconcile_once(self) -> ReconciliationReport:
        """
        Execute a single deterministic reconciliation pass.
        Fails closed by activating global kill switch and safe mode on any discrepancy.
        """
        now = datetime.now(timezone.utc)
        discrepancies: List[ReconciliationDiscrepancy] = []

        # 1. Gather OMS positions and compute net units per symbol
        # Convention: BUY is positive units, SELL is negative units
        oms_positions = self.oms.get_open_positions()
        oms_net: Dict[str, int] = {}
        oms_side_map: Dict[str, OrderSide] = {}
        for pos in oms_positions:
            clean_sym = pos.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
            sign = 1 if pos.side == OrderSide.BUY else -1
            oms_net[clean_sym] = oms_net.get(clean_sym, 0) + (pos.units * sign)
            oms_side_map[clean_sym] = pos.side

        # 2. Gather Broker positions and compute net units per symbol
        broker_positions = self.broker.get_positions()
        broker_net: Dict[str, int] = {}
        broker_side_map: Dict[str, OrderSide] = {}
        for b_pos in broker_positions:
            clean_sym = b_pos.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
            sign = 1 if b_pos.side == OrderSide.BUY else -1
            broker_net[clean_sym] = broker_net.get(clean_sym, 0) + (b_pos.units * sign)
            broker_side_map[clean_sym] = b_pos.side

        # 3. Compare all symbols across OMS and Broker
        all_symbols = set(oms_net.keys()).union(set(broker_net.keys()))
        matched_count = 0

        for sym in sorted(all_symbols):
            o_units = oms_net.get(sym, 0)
            b_units = broker_net.get(sym, 0)

            # Case A: Position in OMS but completely absent in Broker (Lost Position)
            if o_units != 0 and b_units == 0:
                discrepancy = ReconciliationDiscrepancy(
                    discrepancy_type=DiscrepancyType.LOST_POSITION,
                    symbol=sym,
                    oms_units=abs(o_units),
                    broker_units=0,
                    oms_side=oms_side_map.get(sym),
                    broker_side=None,
                    description=f"CRITICAL: OMS holds {abs(o_units)} units of {sym}, but position is absent on broker.",
                    timestamp=now,
                )
                discrepancies.append(discrepancy)

            # Case B: Position on Broker but completely absent in OMS (Phantom Position)
            elif o_units == 0 and b_units != 0:
                discrepancy = ReconciliationDiscrepancy(
                    discrepancy_type=DiscrepancyType.PHANTOM_POSITION,
                    symbol=sym,
                    oms_units=0,
                    broker_units=abs(b_units),
                    oms_side=None,
                    broker_side=broker_side_map.get(sym),
                    description=f"CRITICAL: Phantom position detected! Broker holds {abs(b_units)} units of {sym} missing in OMS.",
                    timestamp=now,
                )
                discrepancies.append(discrepancy)

            # Case C: Position present on both
            elif o_units != 0 and b_units != 0:
                o_side = OrderSide.BUY if o_units > 0 else OrderSide.SELL
                b_side = OrderSide.BUY if b_units > 0 else OrderSide.SELL

                # Side mismatch
                if o_side != b_side:
                    discrepancy = ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.SIDE_MISMATCH,
                        symbol=sym,
                        oms_units=abs(o_units),
                        broker_units=abs(b_units),
                        oms_side=o_side,
                        broker_side=b_side,
                        description=f"CRITICAL: Position side mismatch for {sym}: OMS is {o_side.value}, Broker is {b_side.value}.",
                        timestamp=now,
                    )
                    discrepancies.append(discrepancy)

                # Size mismatch
                elif abs(o_units) != abs(b_units):
                    discrepancy = ReconciliationDiscrepancy(
                        discrepancy_type=DiscrepancyType.SIZE_MISMATCH,
                        symbol=sym,
                        oms_units=abs(o_units),
                        broker_units=abs(b_units),
                        oms_side=o_side,
                        broker_side=b_side,
                        description=f"CRITICAL: Size mismatch for {sym}: OMS has {abs(o_units)} units, Broker has {abs(b_units)} units.",
                        timestamp=now,
                    )
                    discrepancies.append(discrepancy)
                else:
                    matched_count += 1
            else:
                matched_count += 1

        is_clean = len(discrepancies) == 0

        # Fail-closed enforcement on any discrepancy
        if not is_clean:
            reason = (
                f"RECONCILIATION_BREACH: {len(discrepancies)} state discrepancies detected "
                f"between OMS and broker {self.broker.broker_name}. Platform trading frozen."
            )
            logger.critical(reason)
            # 1. Arm global kill switch
            self.kill_switch.arm_global(reason=reason)

            # 2. Trip circuit breaker into SAFE_MODE
            if self.circuit_breaker is not None:
                self.circuit_breaker._safe_mode = True
                self.circuit_breaker._halt_reason = reason

            summary_msg = f"FAILED: {len(discrepancies)} discrepancies. Trading halted immediately."
        else:
            summary_msg = f"CLEAN: All {matched_count} positions perfectly reconciled."

        report = ReconciliationReport(
            timestamp=now,
            is_clean=is_clean,
            discrepancies=discrepancies,
            matched_positions_count=matched_count,
            summary_message=summary_msg,
        )
        self._latest_report = report
        self._reconciliation_count += 1
        return report

    def start(self) -> None:
        """Start the asynchronous background reconciliation daemon."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ReconcilerDaemon")
        self._thread.start()

    def stop(self) -> None:
        """Stop the background reconciliation daemon."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.reconcile_once()
            except Exception as e:
                logger.exception("Error during background reconciliation: %s", e)
                # Fail-closed on reconciliation exception
                self.kill_switch.arm_global(f"RECONCILIATION_EXCEPTION: {str(e)}")
            time.sleep(self.interval_seconds)
