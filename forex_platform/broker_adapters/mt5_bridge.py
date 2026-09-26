"""
MetaTrader 5 (MT5) IPC Bridge Adapter.
Supports major institutional and retail ECN brokers: Vantage, HF Markets, IC Markets.
Provides broker symbol suffix normalization and non-custodial security enforcement.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from forex_platform.broker_adapters.base import (
    BrokerAccountInfo,
    BrokerOrderAck,
    BrokerPosition,
    IBrokerAdapter,
    PermissionSecurityError,
    TokenBucketRateLimiter,
)
from forex_platform.core.domain import (
    CurrencyPair,
    OrderIntent,
    OrderSide,
    OrderStatus,
    to_decimal,
)


class MT5BridgeAdapter(IBrokerAdapter):
    """
    Bridge connecting Forex Platform to MetaTrader 5 terminal instances via IPC socket/protocol.
    Handles broker symbol suffix variations (e.g., Vantage .pro, HF Markets +, IC Markets .raw).
    """

    # Broker symbol suffix presets
    BROKER_SUFFIX_MAP: Dict[str, str] = {
        "VANTAGE": ".pro",
        "HF_MARKETS": "+",
        "HFMARKETS": "+",
        "IC_MARKETS": ".raw",
        "ICMARKETS": ".raw",
        "STANDARD": "",
    }

    # Known common broker suffixes for stripping
    COMMON_SUFFIX_PATTERN = re.compile(r"(\.pro|\.raw|\.r|\.m|\.ecn|\+|\_i|\_sb|\.sb)$", re.IGNORECASE)

    def __init__(
        self,
        account_id: str,
        broker_name: str = "IC_MARKETS",
        permissions: Optional[List[str]] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
        is_live: bool = False,
        custom_suffix: Optional[str] = None,
    ):
        super().__init__(
            account_id=account_id,
            broker_name=broker_name.upper(),
            rate_limiter=rate_limiter,
            is_live=is_live,
        )
        self.permissions = permissions or ["TRADE", "READ_ONLY", "MARKET_DATA"]
        self.audit_permissions(self.permissions)

        # Suffix handling
        if custom_suffix is not None:
            self._symbol_suffix = custom_suffix
        else:
            self._symbol_suffix = self.BROKER_SUFFIX_MAP.get(self.broker_name, "")

        # Internal state for bridge simulation and IPC session
        self._open_positions: Dict[str, BrokerPosition] = {}
        self._balance = Decimal("100000.00")
        self._margin = Decimal("0.00")

    @classmethod
    def normalize_symbol(cls, raw_symbol: str) -> str:
        """
        Normalize broker-specific symbol to standard 6-character currency pair.
        Examples:
            'EURUSD.pro' -> 'EURUSD'
            'EURUSD+'    -> 'EURUSD'
            'USDJPY.raw' -> 'USDJPY'
            'GBP_USD'    -> 'GBPUSD'
        """
        cleaned = raw_symbol.strip()
        # Strip common trailing suffixes
        cleaned = cls.COMMON_SUFFIX_PATTERN.sub("", cleaned)
        # Remove delimiters
        cleaned = cleaned.upper().replace("/", "").replace("_", "").replace("-", "").replace(".", "")
        if len(cleaned) < 6:
            raise ValueError(f"Cannot normalize symbol '{raw_symbol}': expected minimum 6 characters.")
        return cleaned[:6]

    def denormalize_symbol(self, canonical_symbol: str) -> str:
        """
        Map 6-character canonical symbol to broker-specific symbol string.
        Example: 'EURUSD' -> 'EURUSD.raw' for IC Markets.
        """
        clean = canonical_symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        return f"{clean}{self._symbol_suffix}"

    def connect(self) -> bool:
        self.rate_limiter.consume(1)
        self.audit_permissions(self.permissions)
        self._is_connected = True
        return True

    def disconnect(self) -> None:
        self._is_connected = False

    def send_order(self, intent: OrderIntent) -> BrokerOrderAck:
        if not self._is_connected:
            raise ConnectionError(f"MT5 bridge to {self.broker_name} is not connected.")

        self.rate_limiter.consume(1)
        # Fail closed on any live capital route
        self.verify_live_capital_guard()

        broker_symbol = self.denormalize_symbol(intent.symbol)
        canonical_symbol = self.normalize_symbol(broker_symbol)
        ticket = f"MT5_{uuid.uuid4().hex[:10].upper()}"

        # Estimate execution price (for demo/paper bridge)
        fill_price = intent.limit_price or Decimal("1.10000")

        # Record position
        new_pos = BrokerPosition(
            ticket_id=ticket,
            symbol=canonical_symbol,
            side=intent.side,
            units=intent.lot_size.units,
            open_price=fill_price,
            unrealized_pnl=Decimal("0.0"),
            timestamp=datetime.now(timezone.utc),
        )
        self._open_positions[ticket] = new_pos

        return BrokerOrderAck(
            order_id=intent.intent_id,
            broker_ticket=ticket,
            status=OrderStatus.FILLED,
            timestamp=datetime.now(timezone.utc),
            message=f"Order executed on MT5 terminal ({self.broker_name}) as {broker_symbol}",
            filled_units=intent.lot_size.units,
            fill_price=fill_price,
        )

    def cancel_order(self, order_id: str) -> bool:
        if not self._is_connected:
            raise ConnectionError(f"MT5 bridge to {self.broker_name} is not connected.")
        self.rate_limiter.consume(1)
        # In this bridge, tickets can be closed
        if order_id in self._open_positions:
            del self._open_positions[order_id]
            return True
        return False

    def get_positions(self) -> List[BrokerPosition]:
        if not self._is_connected:
            raise ConnectionError(f"MT5 bridge to {self.broker_name} is not connected.")
        self.rate_limiter.consume(1)
        return list(self._open_positions.values())

    def get_account_info(self) -> BrokerAccountInfo:
        if not self._is_connected:
            raise ConnectionError(f"MT5 bridge to {self.broker_name} is not connected.")
        self.rate_limiter.consume(1)
        unrealized = sum((p.unrealized_pnl for p in self._open_positions.values()), Decimal("0.0"))
        equity = self._balance + unrealized
        free_margin = max(Decimal("0.0"), equity - self._margin)

        return BrokerAccountInfo(
            account_id=self.account_id,
            broker_name=self.broker_name,
            currency="USD",
            balance=self._balance,
            equity=equity,
            margin=self._margin,
            free_margin=free_margin,
            is_live=self.is_live,
            permissions=list(self.permissions),
        )

    # Test/Simulation inspection helpers
    def inject_remote_position(self, position: BrokerPosition) -> None:
        """Inject a remote broker position directly for testing reconciliation."""
        self._open_positions[position.ticket_id] = position

    def clear_positions(self) -> None:
        self._open_positions.clear()
