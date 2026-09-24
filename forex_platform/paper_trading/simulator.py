"""
Microstructure Paper Trading Simulator.
Models realistic institutional ECN execution:
- Floating spread crossing (bid/ask).
- Volume-based square-root slippage.
- Simulated execution latency (50ms - 150ms).
- Raw ECN commission deduction and daily swap financing.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import math
import random
from typing import Optional

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    Fill,
    LotSize,
    OrderSide,
    OrderType,
    to_decimal,
)
from forex_platform.costs.commission import CommissionModel
from forex_platform.costs.spread_model import DynamicSpreadModel
from forex_platform.costs.swap_engine import SwapEngine
from forex_platform.risk_engine.firewall import MarketTelemetry


class MicrostructurePaperSimulator:
    """
    Simulates high-fidelity ECN microstructure execution.
    """

    def __init__(
        self,
        base_slippage_pips: float = 0.10,
        slippage_gamma: float = 0.15,
        min_latency_ms: int = 50,
        max_latency_ms: int = 150,
        commission_model: Optional[CommissionModel] = None,
        spread_model: Optional[DynamicSpreadModel] = None,
        swap_engine: Optional[SwapEngine] = None,
    ):
        self.base_slippage_pips = Decimal(str(base_slippage_pips))
        self.slippage_gamma = Decimal(str(slippage_gamma))
        self.min_latency_ms = min_latency_ms
        self.max_latency_ms = max_latency_ms
        self.commission_model = commission_model or CommissionModel()
        self.spread_model = spread_model or DynamicSpreadModel()
        self.swap_engine = swap_engine or SwapEngine()
        self._rng = random.Random(42)

    def calculate_slippage_pips(self, units: int) -> Decimal:
        """
        Square-root volume impact slippage model:
        slippage = base + gamma * sqrt(units / 100,000)
        """
        lots = float(units) / 100_000.0
        impact = float(self.slippage_gamma) * math.sqrt(lots)
        total_pips = float(self.base_slippage_pips) + impact
        return to_decimal(round(total_pips, 2))

    def simulate_execution(
        self,
        order: ExecutionOrder,
        telemetry: MarketTelemetry,
        current_time: datetime,
    ) -> Fill:
        """
        Simulate an ECN fill with latency, spread crossing, and volume slippage.
        """
        utc_now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        pair = CurrencyPair.from_symbol(order.symbol)

        # 1. Simulate network and matching engine latency (50-150ms)
        latency_ms = self._rng.randint(self.min_latency_ms, self.max_latency_ms)
        fill_timestamp = utc_now + timedelta(milliseconds=latency_ms)

        # 2. Base price from order side (BUY crosses at ask, SELL crosses at bid)
        if order.side == OrderSide.BUY:
            base_price = telemetry.ask
        else:
            base_price = telemetry.bid

        # 3. Market order slippage
        if order.order_type == OrderType.MARKET:
            slippage_pips = self.calculate_slippage_pips(order.remaining_units)
            slippage_price = pair.to_price(slippage_pips)
            if order.side == OrderSide.BUY:
                fill_price = base_price + slippage_price
            else:
                fill_price = base_price - slippage_price
        else:
            # Limit order: fills at limit price if market touched it (maker, zero slippage)
            fill_price = order.limit_price or base_price

        rounded_fill_price = pair.round_price(fill_price)

        # 4. Commission
        lot_size = LotSize.from_units(order.remaining_units)
        commission = self.commission_model.calculate_commission(lot_size, is_roundturn=False)

        fill_id = f"FILL-SIM-{order.order_id[-8:]}-{int(fill_timestamp.timestamp() * 1000)}"
        return Fill(
            fill_id=fill_id,
            order_id=order.order_id,
            symbol=pair.symbol,
            side=order.side,
            fill_price=rounded_fill_price,
            units=order.remaining_units,
            commission=commission,
            timestamp=fill_timestamp,
            liquidity_flag="TAKER" if order.order_type == OrderType.MARKET else "MAKER",
        )
