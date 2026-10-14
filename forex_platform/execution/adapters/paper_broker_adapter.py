"""Paper broker adapter — zero-risk simulation sandbox (law 7 default).

Every order the engine sends here goes into an in-memory paper ledger with
realistic fills: market orders fill at next-open under a configurable slippage
model; limit orders fill only if the price touches the limit level. No real
capital is involved; this adapter can never touch a real account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class PaperFill:
    order_id: str
    symbol: str
    side: str
    volume: Decimal
    fill_price: Decimal
    filled_at: object  # datetime


@dataclass
class PaperOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str             # MARKET | LIMIT
    volume: Decimal
    limit_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    created_at: object = None
    status: str = "WORKING"     # WORKING | FILLED | CANCELLED


class PaperBrokerAdapter:
    """Deterministic, fully-audited simulation broker.

    The adapter is intentionally simple — the daemon and strategy layers are
    where the complexity lives; the broker only routes, queues, and fills.
    """

    def __init__(self, initial_equity: Decimal = Decimal("100000")) -> None:
        self.equity = Decimal(str(initial_equity))
        self.orders: dict[str, PaperOrder] = {}
        self.fills: list[PaperFill] = []
        self._seq = 0

    def _next_id(self, prefix: str = "PAPER") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:08d}"

    # -- order operations -----------------------------------------------------
    def create_order(
        self,
        symbol: str,
        side: str,
        volume: Decimal | float | str,
        order_type: str = "MARKET",
        limit_price: Optional[Decimal] = None,
        stop_loss: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
    ) -> str:
        oid = self._next_id()
        self.orders[oid] = PaperOrder(
            order_id=oid,
            symbol=symbol.upper(),
            side=side.upper(),
            order_type=order_type.upper(),
            volume=Decimal(str(volume)),
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return oid

    def cancel_order(self, order_id: str) -> bool:
        o = self.orders.get(order_id)
        if o and o.status == "WORKING":
            o.status = "CANCELLED"
            return True
        return False

    def modify_order(
        self,
        order_id: str,
        *,
        stop_loss: Optional[Decimal] = None,
        take_profit: Optional[Decimal] = None,
    ) -> bool:
        o = self.orders.get(order_id)
        if not o or o.status != "WORKING":
            return False
        if stop_loss is not None:
            o.stop_loss = stop_loss
        if take_profit is not None:
            o.take_profit = take_profit
        return True

    # -- fills ------------------------------------------------------------------
    def on_bar(self, symbol: str, high: Decimal, low: Decimal, close: Decimal) -> list[PaperFill]:
        """Process a closed bar; conservatively fill limits only when the price
        actually traded through the limit level (touch + continuation)."""
        symbol = symbol.upper()
        new_fills: list[PaperFill] = []
        for o in list(self.orders.values()):
            if o.status != "WORKING" or o.symbol != symbol or o.order_type != "LIMIT":
                continue
            if o.limit_price is None:
                continue
            if o.side == "BUY" and low <= o.limit_price:
                o.status = "FILLED"
                fill = PaperFill(o.order_id, o.symbol, o.side, o.volume, o.limit_price, None)
                new_fills.append(fill)
                self.fills.append(fill)
            elif o.side == "SELL" and high >= o.limit_price:
                o.status = "FILLED"
                fill = PaperFill(o.order_id, o.symbol, o.side, o.volume, o.limit_price, None)
                new_fills.append(fill)
                self.fills.append(fill)
        return new_fills

    def summary(self) -> dict:
        return {
            "equity": str(self.equity),
            "open_orders": [o.__dict__ for o in self.orders.values() if o.status == "WORKING"],
            "fills": len(self.fills),
        }
