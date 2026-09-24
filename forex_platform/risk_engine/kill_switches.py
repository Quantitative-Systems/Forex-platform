"""
Hierarchical atomic kill switches for Forex Platform.
Supports 5 granular levels: Global, Tenant, Broker, Currency Pair, and Strategy.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple


class HierarchicalKillSwitch:
    """
    Thread-safe hierarchical kill switch manager.
    Levels:
    1. Global: Halts the entire platform.
    2. Tenant: Halts a specific tenant/account.
    3. Broker: Halts a specific broker gateway.
    4. Pair/Symbol: Halts trading in a specific currency pair (e.g. 'EURUSD').
    5. Strategy: Halts a specific strategy identifier.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._global_killed: bool = False
        self._global_reason: Optional[str] = None
        self._tenant_switches: Dict[str, str] = {}
        self._broker_switches: Dict[str, str] = {}
        self._pair_switches: Dict[str, str] = {}
        self._strategy_switches: Dict[str, str] = {}

    def arm_global(self, reason: str = "Global emergency kill switch activated") -> None:
        with self._lock:
            self._global_killed = True
            self._global_reason = reason

    def disarm_global(self) -> None:
        with self._lock:
            self._global_killed = False
            self._global_reason = None

    def arm_tenant(self, tenant_id: str, reason: str) -> None:
        with self._lock:
            self._tenant_switches[tenant_id] = reason

    def disarm_tenant(self, tenant_id: str) -> None:
        with self._lock:
            self._tenant_switches.pop(tenant_id, None)

    def arm_broker(self, broker_id: str, reason: str) -> None:
        with self._lock:
            self._broker_switches[broker_id] = reason

    def disarm_broker(self, broker_id: str) -> None:
        with self._lock:
            self._broker_switches.pop(broker_id, None)

    def arm_pair(self, symbol: str, reason: str) -> None:
        clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        with self._lock:
            self._pair_switches[clean] = reason

    def disarm_pair(self, symbol: str) -> None:
        clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        with self._lock:
            self._pair_switches.pop(clean, None)

    def arm_strategy(self, strategy_id: str, reason: str) -> None:
        with self._lock:
            self._strategy_switches[strategy_id] = reason

    def disarm_strategy(self, strategy_id: str) -> None:
        with self._lock:
            self._strategy_switches.pop(strategy_id, None)

    def disarm_all(self) -> None:
        with self._lock:
            self._global_killed = False
            self._global_reason = None
            self._tenant_switches.clear()
            self._broker_switches.clear()
            self._pair_switches.clear()
            self._strategy_switches.clear()

    def is_killed(
        self,
        tenant_id: Optional[str] = None,
        broker_id: Optional[str] = None,
        symbol: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if any hierarchical switch is armed.
        Evaluates Global -> Tenant -> Broker -> Pair -> Strategy.
        Returns (is_killed, reason).
        """
        with self._lock:
            if self._global_killed:
                return True, f"GLOBAL_KILL: {self._global_reason}"

            if tenant_id and tenant_id in self._tenant_switches:
                return True, f"TENANT_KILL [{tenant_id}]: {self._tenant_switches[tenant_id]}"

            if broker_id and broker_id in self._broker_switches:
                return True, f"BROKER_KILL [{broker_id}]: {self._broker_switches[broker_id]}"

            if symbol:
                clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
                if clean in self._pair_switches:
                    return True, f"PAIR_KILL [{clean}]: {self._pair_switches[clean]}"

            if strategy_id and strategy_id in self._strategy_switches:
                return True, f"STRATEGY_KILL [{strategy_id}]: {self._strategy_switches[strategy_id]}"

            return False, None
