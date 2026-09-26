"""
FIX Adapter for Market Data Feeds.
A mock implementation that simulates receiving market data via FIX.
In a production environment, this would use a FIX engine like QuickFIX/J.
"""

import random
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from forex_platform.market_data.adapters.base import MarketDataAdapter
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent


class FIXAdapter(MarketDataAdapter):
    """
    Mock FIX adapter that simulates market data feeds.
    """

    def __init__(self, symbols: List[str], timeframes: List[Timeframe]):
        super().__init__(symbols, timeframes)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Last prices for each symbol to generate realistic ticks
        self._last_prices: Dict[str, Decimal] = {}
        # Initialize last prices with some reasonable values
        for symbol in symbols:
            clean_symbol = symbol.upper().replace("/", "").replace("_", "")
            # Set initial price based on symbol
            if "EUR" in clean_symbol and "USD" in clean_symbol:
                self._last_prices[clean_symbol] = Decimal("1.0800")
            elif "GBP" in clean_symbol and "USD" in clean_symbol:
                self._last_prices[clean_symbol] = Decimal("1.2600")
            elif "USD" in clean_symbol and "JPY" in clean_symbol:
                self._last_prices[clean_symbol] = Decimal("149.50")
            else:
                self._last_prices[clean_symbol] = Decimal("1.0000")

    def connect(self) -> None:
        """Simulate connecting to the FIX gateway."""
        if self._is_connected:
            return
        self._is_connected = True
        # Start the simulation thread
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._simulate_market_data, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        """Simulate disconnecting from the FIX gateway."""
        if not self._is_connected:
            return
        self._is_connected = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def subscribe_bars(self, symbols: List[str], timeframes: List[Timeframe]) -> None:
        """
        Subscribe to bar updates. In this mock, we generate bars from ticks.
        In a real FIX adapter, you would subscribe to market data and then aggregate ticks into bars.
        """
        # For simplicity, we'll just note the subscription and rely on the tick simulation
        # to generate bars internally. A real implementation would be more complex.
        pass

    def subscribe_ticks(self, symbols: List[str]) -> None:
        """
        Subscribe to tick updates. In this mock, we start generating ticks for the given symbols.
        """
        # In a real FIX adapter, you would send a market data subscription request.
        # For our mock, we just ensure we are connected and the simulation is running.
        if not self._is_connected:
            self.connect()

    def _simulate_market_data(self) -> None:
        """Simulate market data by generating random ticks and aggregating them into bars."""
        # We'll simulate ticks at random intervals between 100ms and 500ms
        # We'll also simulate bar completion every minute (for M1 timeframe)
        # This is a simplified simulation for demonstration purposes.

        # Data structures for bar building
        # We'll build bars for each symbol and timeframe we are subscribed to
        # For simplicity, we'll only build M1 bars in this mock
        bar_data: Dict[str, Dict[Timeframe, Dict[str, Any]]] = {}
        for symbol in self.symbols:
            clean_symbol = symbol.upper().replace("/", "").replace("_", "")
            bar_data[clean_symbol] = {}
            for tf in self.timeframes:
                bar_data[clean_symbol][tf] = {
                    "open": None,
                    "high": Decimal("-Infinity"),
                    "low": Decimal("Infinity"),
                    "close": None,
                    "volume": Decimal("0"),
                    "start_time": None,
                    "tick_count": 0,
                }

        last_bar_time: Dict[str, Dict[Timeframe, datetime]] = {}
        for symbol in self.symbols:
            clean_symbol = symbol.upper().replace("/", "").replace("_", "")
            last_bar_time[clean_symbol] = {tf: datetime.min for tf in self.timeframes}

        while not self._stop_event.is_set():
            # Simulate a tick for each subscribed symbol
            for symbol in self.symbols:
                clean_symbol = symbol.upper().replace("/", "").replace("_", "")
                # Generate a random price change (mean reversion around last price)
                last_price = self._last_prices[clean_symbol]
                # Random change in pips (1 pip = 0.0001 for most pairs, 0.01 for JPY pairs)
                if "JPY" in clean_symbol:
                    pip_size = Decimal("0.01")
                else:
                    pip_size = Decimal("0.0001")
                # Random change between -2 and +2 pips
                change_in_pips = random.uniform(-2, 2)
                price_change = Decimal(str(change_in_pips)) * pip_size
                # Ensure price doesn't go negative
                new_price = max(last_price + price_change, Decimal("0.0001"))
                self._last_prices[clean_symbol] = new_price

                # Generate a tick
                tick = {
                    "symbol": clean_symbol,
                    "bid": new_price - Decimal("0.00005"),  # Simplified spread
                    "ask": new_price + Decimal("0.00005"),
                    "timestamp": datetime.utcnow(),
                }

                # Emit tick to callback
                self._emit_tick(tick)

                # Update bar data for each timeframe
                now = datetime.utcnow()
                for tf in self.timeframes:
                    bar_info = bar_data[clean_symbol][tf]
                    # Determine the start of the current bar for this timeframe
                    if tf == Timeframe.M1:
                        bar_start = now.replace(second=0, microsecond=0)
                    elif tf == Timeframe.M5:
                        bar_start = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
                    elif tf == Timeframe.H1:
                        bar_start = now.replace(minute=0, second=0, microsecond=0)
                    else:
                        # Default to M1 for unknown timeframes
                        bar_start = now.replace(second=0, microsecond=0)

                    # If we are starting a new bar, emit the completed bar
                    if bar_info["start_time"] is None or bar_start > bar_info["start_time"]:
                        # Emit the completed bar if we have data
                        if bar_info["open"] is not None and bar_info["tick_count"] > 0:
                            bar_event = BarEvent(
                                symbol=clean_symbol,
                                timeframe=tf,
                                timestamp=bar_info["start_time"],
                                open=bar_info["open"],
                                high=bar_info["high"],
                                low=bar_info["low"],
                                close=bar_info["close"],
                                volume=bar_info["volume"],
                                spread=Decimal("0.0001"),  # Simplified spread
                            )
                            self._emit_bar(bar_event)

                        # Reset for the new bar
                        bar_info["open"] = new_price
                        bar_info["high"] = new_price
                        bar_info["low"] = new_price
                        bar_info["close"] = new_price
                        bar_info["volume"] = Decimal("1")  # Simulate one unit volume per tick
                        bar_info["start_time"] = bar_start
                        bar_info["tick_count"] = 1
                    else:
                        # Update the current bar
                        if bar_info["open"] is None:
                            bar_info["open"] = new_price
                        bar_info["high"] = max(bar_info["high"], new_price)
                        bar_info["low"] = min(bar_info["low"], new_price)
                        bar_info["close"] = new_price
                        bar_info["volume"] += Decimal("1")
                        bar_info["tick_count"] += 1

            # Sleep for a random interval to simulate real-time ticks
            time.sleep(random.uniform(0.1, 0.5))

        # Emit any remaining bars when stopping
        for symbol in self.symbols:
            clean_symbol = symbol.upper().replace("/", "").replace("_", "")
            for tf in self.timeframes:
                bar_info = bar_data[clean_symbol][tf]
                if bar_info["open"] is not None and bar_info["tick_count"] > 0:
                    bar_event = BarEvent(
                        symbol=clean_symbol,
                        timeframe=tf,
                        timestamp=bar_info["start_time"],
                        open=bar_info["open"],
                        high=bar_info["high"],
                        low=bar_info["low"],
                        close=bar_info["close"],
                        volume=bar_info["volume"],
                        spread=Decimal("0.0001"),
                    )
                    self._emit_bar(bar_event)