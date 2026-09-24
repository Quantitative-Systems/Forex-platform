"""
Order lifecycle state machine and transition validation.
Enforces strict deterministic progression:
CREATED -> RISK_CHECKED -> SUBMITTED -> ACKED -> FILLED.
UNKNOWN state immediately locks the currency pair and emits a critical reconciliation alert.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field


class OrderLifecycleState(str, Enum):
    CREATED = "CREATED"
    RISK_CHECKED = "RISK_CHECKED"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class IllegalStateTransitionError(Exception):
    """Raised when an illegal transition is attempted."""
    pass


class ReconciliationAlert(BaseModel):
    """Alert emitted when an order enters UNKNOWN state."""
    model_config = ConfigDict(frozen=True)

    order_id: str
    symbol: str
    previous_state: OrderLifecycleState
    timestamp: datetime
    reason: str
    severity: str = "CRITICAL"


class OrderStateMachine:
    """
    Finite State Machine managing order lifecycle transitions.
    """

    # Permitted state transition map
    LEGAL_TRANSITIONS: Dict[OrderLifecycleState, Set[OrderLifecycleState]] = {
        OrderLifecycleState.CREATED: {
            OrderLifecycleState.RISK_CHECKED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.FAILED,
        },
        OrderLifecycleState.RISK_CHECKED: {
            OrderLifecycleState.SUBMITTED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.FAILED,
        },
        OrderLifecycleState.SUBMITTED: {
            OrderLifecycleState.ACKED,
            OrderLifecycleState.PARTIALLY_FILLED,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.REJECTED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
        },
        OrderLifecycleState.ACKED: {
            OrderLifecycleState.PARTIALLY_FILLED,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.FAILED,
            OrderLifecycleState.UNKNOWN,
        },
        OrderLifecycleState.PARTIALLY_FILLED: {
            OrderLifecycleState.PARTIALLY_FILLED,
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.EXPIRED,
            OrderLifecycleState.UNKNOWN,
        },
        # Terminal states have no forward transitions
        OrderLifecycleState.FILLED: set(),
        OrderLifecycleState.REJECTED: set(),
        OrderLifecycleState.CANCELLED: set(),
        OrderLifecycleState.EXPIRED: set(),
        OrderLifecycleState.FAILED: set(),
        # UNKNOWN can only transition through manual/automated reconciliation
        OrderLifecycleState.UNKNOWN: {
            OrderLifecycleState.FILLED,
            OrderLifecycleState.CANCELLED,
            OrderLifecycleState.FAILED,
        },
    }

    TERMINAL_STATES = {
        OrderLifecycleState.FILLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.FAILED,
    }

    def __init__(
        self,
        order_id: str,
        symbol: str,
        initial_state: OrderLifecycleState = OrderLifecycleState.CREATED,
        on_reconciliation_alert: Optional[Callable[[ReconciliationAlert], None]] = None,
    ):
        self.order_id = order_id
        self.symbol = symbol
        self._current_state = initial_state
        self._state_history: List[tuple[OrderLifecycleState, datetime, str]] = [
            (initial_state, datetime.now(timezone.utc), "Initialization")
        ]
        self._on_reconciliation_alert = on_reconciliation_alert

    @property
    def current_state(self) -> OrderLifecycleState:
        return self._current_state

    @property
    def is_terminal(self) -> bool:
        return self._current_state in self.TERMINAL_STATES

    def transition_to(
        self,
        target_state: OrderLifecycleState,
        timestamp: datetime,
        reason: str = "",
    ) -> OrderLifecycleState:
        """
        Validate and execute transition from current state to target state.
        Raises IllegalStateTransitionError if transition is illegal.
        If target is UNKNOWN, locks pair and emits reconciliation alert.
        """
        if self._current_state == target_state:
            # Idempotent re-affirmation of same state
            return self._current_state

        allowed = self.LEGAL_TRANSITIONS.get(self._current_state, set())
        if target_state not in allowed:
            raise IllegalStateTransitionError(
                f"Illegal transition for order {self.order_id}: cannot transition "
                f"from {self._current_state.value} to {target_state.value}."
            )

        prev_state = self._current_state
        self._current_state = target_state
        utc_ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        self._state_history.append((target_state, utc_ts, reason))

        # Invariant: UNKNOWN state locks pair and alerts
        if target_state == OrderLifecycleState.UNKNOWN:
            alert = ReconciliationAlert(
                order_id=self.order_id,
                symbol=self.symbol,
                previous_state=prev_state,
                timestamp=utc_ts,
                reason=reason or "Order entered UNKNOWN state (possible connection/broker timeout)",
                severity="CRITICAL",
            )
            if self._on_reconciliation_alert:
                self._on_reconciliation_alert(alert)

        return self._current_state

    def get_history(self) -> List[tuple[OrderLifecycleState, datetime, str]]:
        return list(self._state_history)
