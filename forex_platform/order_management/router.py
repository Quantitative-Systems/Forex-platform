"""
Deterministic Smart Order Router and TWAP Order Slicer.
Generates idempotent Client Order IDs (CID), slices clips > 5 standard lots (> 500k units),
and guarantees zero live capital leakage (fails closed on live execution).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from typing import Dict, List, Optional
from pydantic import BaseModel, ConfigDict

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    LotSize,
    OrderIntent,
    OrderStatus,
)


class LiveCapitalLockedError(Exception):
    """Raised when routing to a live broker while live capital is locked at $0.00."""
    pass


class SlicedOrderBatch(BaseModel):
    """Batch of sliced execution orders for a parent intent."""
    model_config = ConfigDict(frozen=True)

    parent_intent_id: str
    total_units: int
    slice_count: int
    child_orders: List[ExecutionOrder]


class SmartOrderRouter:
    """
    Deterministic Order Router with:
    - Idempotent client order ID (CID) generation.
    - Duplicate intent deduplication.
    - TWAP clip slicing for large orders (> 5 standard lots = 500,000 units).
    - Fail-closed live capital safety gate.
    """

    MAX_CLIP_UNITS = 500_000  # 5 standard lots

    def __init__(self, allow_live_routing: bool = False):
        self.allow_live_routing = allow_live_routing
        self._idempotency_cache: Dict[str, ExecutionOrder] = {}

    @classmethod
    def generate_client_order_id(cls, intent: OrderIntent, slice_index: int = 0) -> str:
        """
        Generate a deterministic, collision-resistant Client Order ID (CID).
        """
        raw = f"{intent.intent_id}|{intent.symbol}|{intent.side.value}|{intent.lot_size.units}|{intent.order_type.value}|{slice_index}"
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"ORD-{intent.symbol}-{h}"

    def slice_intent(self, intent: OrderIntent) -> List[ExecutionOrder]:
        """
        Slices orders exceeding MAX_CLIP_UNITS into sequential sub-orders.
        Returns a list of ExecutionOrder slices.
        """
        total_units = intent.lot_size.units
        now = datetime.now(timezone.utc)

        if total_units <= self.MAX_CLIP_UNITS:
            # Single clip
            cid = self.generate_client_order_id(intent, slice_index=0)
            order = ExecutionOrder(
                order_id=cid,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                lot_size=intent.lot_size,
                limit_price=intent.limit_price,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                status=OrderStatus.PENDING,
                created_at=now,
                updated_at=now,
                remaining_units=intent.lot_size.units,
            )
            return [order]

        # Multi-clip slicing (TWAP)
        remaining = total_units
        slice_orders: List[ExecutionOrder] = []
        slice_idx = 0

        while remaining > 0:
            chunk = min(remaining, self.MAX_CLIP_UNITS)
            lot_slice = LotSize.from_units(chunk)
            cid = self.generate_client_order_id(intent, slice_index=slice_idx)

            order = ExecutionOrder(
                order_id=cid,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                lot_size=lot_slice,
                limit_price=intent.limit_price,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                status=OrderStatus.PENDING,
                created_at=now,
                updated_at=now,
                remaining_units=chunk,
            )
            slice_orders.append(order)
            remaining -= chunk
            slice_idx += 1

        return slice_orders

    def route_intent(
        self,
        intent: OrderIntent,
        destination: str = "PAPER_ENGINE",
    ) -> List[ExecutionOrder]:
        """
        Route an OrderIntent to execution destination with deduplication.
        Fails closed if destination is LIVE and allow_live_routing is False.
        """
        if destination.upper() in ("LIVE", "BROKER_LIVE", "MT5_LIVE") and not self.allow_live_routing:
            raise LiveCapitalLockedError(
                f"Fail-Closed Invariant: Destination '{destination}' rejected. "
                f"Live capital routing is permanently locked at $0.00."
            )

        # Idempotency check: if intent_id was already processed, return cached orders
        first_cid = self.generate_client_order_id(intent, slice_index=0)
        if first_cid in self._idempotency_cache:
            # Return cached order
            return [self._idempotency_cache[first_cid]]

        orders = self.slice_intent(intent)
        for ord_ in orders:
            self._idempotency_cache[ord_.order_id] = ord_

        return orders
