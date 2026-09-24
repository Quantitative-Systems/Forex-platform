"""
Canonical broker connectivity interfaces, non-custodial permission auditing,
and token-bucket rate limiting for Forex Platform.
"""

from __future__ import annotations

import time
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field, field_validator

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    to_decimal,
)


class PermissionSecurityError(PermissionError):
    """
    Raised when broker credentials or account configuration contain non-trade,
    custodial, withdrawal, or administrative permissions.
    Forex Platform is strictly non-custodial: execution algorithms may only place
    and cancel market/limit orders and read telemetry.
    """
    pass


class RateLimitExceededError(RuntimeError):
    """Raised when broker request throughput exceeds configured rate limits."""
    pass


class TokenBucketRateLimiter:
    """
    Thread-safe Token Bucket Rate Limiter enforcing broker request throughput limits.
    Prevents API banning and high-frequency messaging violations.
    """

    def __init__(self, capacity: int = 50, refill_rate: float = 50.0):
        """
        :param capacity: Maximum burst capacity of tokens.
        :param refill_rate: Tokens added per second.
        """
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("Rate limiter capacity and refill rate must be positive.")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now

    def acquire(self, tokens: int = 1) -> bool:
        """Attempt to acquire tokens without blocking. Returns True if acquired, False otherwise."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def consume(self, tokens: int = 1) -> None:
        """Acquire tokens or raise RateLimitExceededError."""
        if not self.acquire(tokens):
            raise RateLimitExceededError(
                f"Broker rate limit exceeded: insufficient tokens ({self._tokens:.2f} available, {tokens} required)."
            )

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


class BrokerPosition(BaseModel):
    """Normalized position record retrieved from broker gateway."""
    model_config = ConfigDict(frozen=True)

    ticket_id: str
    symbol: str
    side: OrderSide
    units: int
    open_price: Decimal
    unrealized_pnl: Decimal = Decimal("0.0")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("open_price", "unrealized_pnl", mode="before")
    @classmethod
    def convert_decimal(cls, v: Any) -> Decimal:
        return to_decimal(v)


class BrokerAccountInfo(BaseModel):
    """Normalized account summary returned from broker terminal."""
    model_config = ConfigDict(frozen=True)

    account_id: str
    broker_name: str
    currency: str = "USD"
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    is_live: bool = False
    permissions: List[str] = Field(default_factory=list)

    @field_validator("balance", "equity", "margin", "free_margin", mode="before")
    @classmethod
    def convert_decimal(cls, v: Any) -> Decimal:
        return to_decimal(v)


class BrokerOrderAck(BaseModel):
    """Acknowledgment payload returned from broker order placement."""
    model_config = ConfigDict(frozen=True)

    order_id: str
    broker_ticket: str
    status: OrderStatus
    timestamp: datetime
    message: str = "Order acknowledged"


class IBrokerAdapter(ABC):
    """
    Canonical broker gateway adapter contract.
    Enforces non-custodial security audits, token-bucket throttling, and fail-closed safety.
    """

    FORBIDDEN_PERMISSIONS: Set[str] = {
        "WITHDRAWAL",
        "WITHDRAW",
        "TRANSFER",
        "FUNDS_TRANSFER",
        "EXTERNAL_TRANSFER",
        "MANAGE_FUNDS",
        "FULL_ACCESS",
        "ADMIN",
        "DEPOSIT",
    }

    def __init__(
        self,
        account_id: str,
        broker_name: str,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
        is_live: bool = False,
    ):
        self.account_id = account_id
        self.broker_name = broker_name
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(capacity=50, refill_rate=50.0)
        self.is_live = is_live
        # Zero Live Capital Invariant: live capital permanently locked at $0.00
        self.live_capital: Decimal = Decimal("0.00")
        self._is_connected: bool = False

    def audit_permissions(self, permissions: List[str]) -> None:
        """
        Audit broker permissions and reject any non-trade or custodial rights.
        Fails closed with PermissionSecurityError.
        """
        for perm in permissions:
            normalized = perm.strip().upper().replace(" ", "_")
            if normalized in self.FORBIDDEN_PERMISSIONS:
                raise PermissionSecurityError(
                    f"CRITICAL SECURITY VIOLATION: Non-custodial invariant breach! "
                    f"Broker credentials contain forbidden permission '{perm}'. "
                    f"Only trade execution permissions (e.g. TRADE, MARKET_DATA) are permitted."
                )

    def verify_live_capital_guard(self) -> None:
        """
        Zero Live Capital Invariant enforcement:
        Live order routing is strictly locked at $0.00 and fails closed.
        """
        if self.is_live or self.live_capital > Decimal("0.00"):
            raise PermissionSecurityError(
                f"LIVE CAPITAL LOCKED: Live order routing is blocked by safety invariant. "
                f"Live capital locked at ${self.live_capital:.2f}. Execution must fail closed."
            )

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to broker terminal / gateway."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Terminate connection to broker terminal / gateway."""
        pass

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @abstractmethod
    def send_order(self, intent: OrderIntent) -> BrokerOrderAck:
        """Submit an order intent to the broker gateway."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an active order on the broker gateway."""
        pass

    @abstractmethod
    def get_positions(self) -> List[BrokerPosition]:
        """Fetch all currently open positions from the broker."""
        pass

    @abstractmethod
    def get_account_info(self) -> BrokerAccountInfo:
        """Retrieve latest account balance, equity, and margin telemetry."""
        pass
