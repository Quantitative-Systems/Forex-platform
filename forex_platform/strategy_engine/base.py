"""
Universal Base Strategy Contract and Event Definitions.
Enforces parameter schemas, timeframe requirements, and causal bar processing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderType,
    UrgencyLevel,
    to_decimal,
)
from forex_platform.market_data.causal_aligner import Timeframe


class BarEvent(BaseModel):
    """
    Standardized market bar event delivered to strategies upon bar close.
    """
    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    spread: Decimal = Decimal("1.0")
    htf_data: Dict[str, Any] = Field(default_factory=dict)


class BaseStrategy(ABC):
    """
    Abstract base class for all Forex strategy plugins.
    """

    def __init__(
        self,
        strategy_id: str,
        name: str,
        symbols: List[str],
        timeframes: List[Timeframe],
        parameters: Optional[Dict[str, Any]] = None,
    ):
        self.strategy_id = strategy_id
        self.name = name
        self.symbols = [s.upper().replace("/", "").replace("_", "") for s in symbols]
        self.timeframes = timeframes
        self.parameters = parameters or {}
        # Historical bar buffer per symbol: symbol -> list[BarEvent]
        self._history: Dict[str, List[BarEvent]] = {s: [] for s in self.symbols}
        self._max_history = int(self.parameters.get("max_history_bars", 200))

    def update_history(self, event: BarEvent) -> None:
        """Buffer incoming bar event maintaining max_history limit."""
        sym = event.symbol.upper().replace("/", "").replace("_", "")
        if sym not in self._history:
            self._history[sym] = []
        self._history[sym].append(event)
        if len(self._history[sym]) > self._max_history:
            self._history[sym].pop(0)

    def get_history(self, symbol: str) -> List[BarEvent]:
        sym = symbol.upper().replace("/", "").replace("_", "")
        return list(self._history.get(sym, []))

    @abstractmethod
    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        """
        Invoked upon bar completion. Must emit a list of OrderIntent instances (or empty list).
        Must be strictly causal: no looking ahead into future bars.
        """
        pass

    def create_intent(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        lot_size: LotSize,
        timestamp: datetime,
        limit_price: Optional[Decimal] = None,
        stop_loss: Optional[Optional[Decimal]] = None,
        take_profit: Optional[Decimal] = None,
        urgency: UrgencyLevel = UrgencyLevel.MEDIUM,
        client_tag: Optional[str] = None,
    ) -> OrderIntent:
        """Helper to construct validated OrderIntent."""
        intent_id = f"INT-{self.strategy_id}-{symbol}-{int(timestamp.timestamp())}"
        tag = f"{self.strategy_id}:{client_tag}" if client_tag else self.strategy_id
        return OrderIntent(
            intent_id=intent_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            lot_size=lot_size,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            urgency=urgency,
            timestamp=timestamp,
            client_tag=tag,
        )

    # -------------------------------------------------------------------------
    # Technical Indicator Utilities (Zero-lookahead, pure causal math)
    # -------------------------------------------------------------------------
    @staticmethod
    def calculate_sma(closes: List[Decimal], period: int) -> Optional[Decimal]:
        if len(closes) < period or period <= 0:
            return None
        window = closes[-period:]
        avg = sum(window, Decimal("0.0")) / Decimal(str(period))
        return avg

    @staticmethod
    def calculate_ema(closes: List[Decimal], period: int) -> Optional[Decimal]:
        if len(closes) < period or period <= 0:
            return None
        k = Decimal("2.0") / Decimal(str(period + 1))
        # Initialize EMA with SMA of first 'period' bars
        ema = sum(closes[:period], Decimal("0.0")) / Decimal(str(period))
        for price in closes[period:]:
            ema = (price * k) + (ema * (Decimal("1.0") - k))
        return ema

    @staticmethod
    def calculate_atr(bars: List[BarEvent], period: int = 14) -> Optional[Decimal]:
        if len(bars) < period + 1 or period <= 0:
            return None
        tr_list: List[Decimal] = []
        for i in range(1, len(bars)):
            curr = bars[i]
            prev = bars[i - 1]
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close),
            )
            tr_list.append(tr)

        if len(tr_list) < period:
            return None
        # Return simple average of last 'period' true ranges
        window = tr_list[-period:]
        return sum(window, Decimal("0.0")) / Decimal(str(period))
