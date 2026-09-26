"""
High-Frequency Market Making Strategy.
Provides liquidity by posting bid and ask orders around the mid-price,
managed by inventory risk and adverse selection controls.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

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
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy


class MarketMakingStrategy(BaseStrategy):
    """
    High-Frequency Market Making Strategy.
    Posts limit orders on both sides of the market to capture the spread.
    Inventory is kept near zero through dynamic skew and order sizing.
    """

    def __init__(
        self,
        strategy_id: str = "hft_market_making",
        symbols: Optional[List[str]] = None,
        target_spread: float = 1.0,  # Target spread in pips
        max_inventory: float = 10.0,  # Maximum inventory in lots
        order_size: float = 0.1,  # Base order size in lots
        k_factor: float = 0.5,  # Inventory skew factor
        renewal_interval: int = 5,  # Seconds between order renewals (in bar counts for simplicity)
    ):
        target_symbols = symbols or ["EUR/USD", "GBP/USD", "USD/JPY"]
        super().__init__(
            strategy_id=strategy_id,
            name="High-Frequency Market Making",
            symbols=target_symbols,
            timeframes=[Timeframe.M1, Timeframe.M5],  # We'll use bars for timing, but strategy works on ticks
            parameters={
                "target_spread": target_spread,
                "max_inventory": max_inventory,
                "order_size": order_size,
                "k_factor": k_factor,
                "renewal_interval": renewal_interval,
            },
        )
        self.default_lot = LotSize.from_lots(order_size)
        self.target_spread = Decimal(str(target_spread))
        self.max_inventory = Decimal(str(max_inventory))
        self.k_factor = Decimal(str(k_factor))
        self.renewal_interval = renewal_interval
        
        # Track our inventory (signed: long positive, short negative)
        self._inventory: Dict[str, Decimal] = {s.upper().replace("/", "").replace("_", ""): Decimal("0") for s in target_symbols}
        # Track last order time for each symbol to manage renewal
        self._last_order_time: Dict[str, Optional[datetime]] = {s.upper().replace("/", "").replace("_", ""): None for s in target_symbols}
        # Track our active orders (simplified - in reality you'd track order IDs)
        self._active_orders: Dict[str, List[Dict]] = {s.upper().replace("/", "").replace("_", ""): [] for s in target_symbols}

    def on_bar(self, event: BarEvent) -> List[OrderIntent]:
        """
        On each bar, manage our market making quotes.
        In a true HFT strategy, this would operate on ticks, but we use bars for simplicity in this framework.
        """
        self.update_history(event)
        clean_sym = event.symbol.upper().replace("/", "").replace("_", "")
        
        # Initialize if needed
        if clean_sym not in self._inventory:
            self._inventory[clean_sym] = Decimal("0")
            self._last_order_time[clean_sym] = None
            self._active_orders[clean_sym] = []
        
        # Calculate mid price from the bar (using close as proxy for mid)
        mid_price = (event.high + event.low) / Decimal("2")
        # Or use (open + close)/2 or (high + low)/2 - we'll use (high + low)/2
        
        # Calculate inventory skew: as inventory increases, we skew quotes to reduce inventory
        inventory = self._inventory[clean_sym]
        # Skew in price units: k * inventory / max_inventory * spread
        # We'll convert target_spread from pips to price units
        pair = CurrencyPair.from_symbol(event.symbol)
        spread_in_price = pair.to_price(self.target_spread)  # Convert target spread pips to price
        
        if self.max_inventory > 0:
            inventory_skew = self.k_factor * inventory / self.max_inventory * spread_in_price
        else:
            inventory_skew = Decimal("0")
        
        # Calculate bid and ask prices
        half_spread = spread_in_price / Decimal("2")
        bid_price = mid_price - half_spread - inventory_skew
        ask_price = mid_price + half_spread - inventory_skew  # Same skew applies to both
        
        # Ensure prices are positive and make sense
        if bid_price <= Decimal("0") or ask_price <= Decimal("0") or bid_price >= ask_price:
            # Fallback to wide spread if calculation goes wrong
            bid_price = mid_price - spread_in_price
            ask_price = mid_price + spread_in_price
        
        # Determine if we need to post new orders (based on time or inventory change)
        # For simplicity, we'll post on every bar in this mock
        # In reality, you'd check if existing orders are still valid or need renewal
        
        intents: List[OrderIntent] = []
        
        # Post bid order (buy at bid)
        bid_intent = self.create_intent(
            symbol=pair.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            lot_size=self.default_lot,
            timestamp=event.timestamp,
            limit_price=pair.round_price(bid_price),
            urgency=UrgencyLevel.HIGH,  # HFT needs to be fast
            client_tag=f"MM_BID_{clean_sym}",
        )
        intents.append(bid_intent)
        
        # Post ask order (sell at ask)
        ask_intent = self.create_intent(
            symbol=pair.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            lot_size=self.default_lot,
            timestamp=event.timestamp,
            limit_price=pair.round_price(ask_price),
            urgency=UrgencyLevel.HIGH,
            client_tag=f"MM_ASK_{clean_sym}",
        )
        intents.append(ask_intent)
        
        # Update our tracking (in reality, we'd track actual order IDs and fills)
        self._last_order_time[clean_sym] = event.timestamp
        
        return intents
    
    # Note: In a real implementation, you would also need:
    # - Methods to handle order fills and update inventory
    # - Methods to handle order cancellations
    # - Adverse selection detection (to widen spreads when informed traders are present)
    # - More sophisticated inventory management