"""
Base Adapter for Market Data Feeds.
Defines the interface for all market data adapters (FIX, WebSocket, REST, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent


class MarketDataAdapter(ABC):
    """
    Abstract base class for market data adapters.
    """

    def __init__(self, symbols: List[str], timeframes: List[Timeframe]):
        """
        Initialize the adapter.

        :param symbols: List of currency pairs to subscribe to (e.g., ['EUR/USD', 'GBP/USD']).
        :param timeframes: List of timeframes to produce bars for (e.g., [Timeframe.M1, Timeframe.M5]).
        """
        self.symbols = [s.upper().replace("/", "").replace("_", "") for s in symbols]
        self.timeframes = timeframes
        self._is_connected = False
        # Callback functions for different event types
        self.on_bar_callback: Optional[Callable[[BarEvent], None]] = None
        self.on_tick_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_error_callback: Optional[Callable[[Exception], None]] = None
        self.on_close_callback: Optional[Callable[[], None]] = None

    @abstractmethod
    def connect(self) -> None:
        """
        Establish connection to the market data feed.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """
        Close connection to the market data feed.
        """
        pass

    @abstractmethod
    def subscribe_bars(self, symbols: List[str], timeframes: List[Timeframe]) -> None:
        """
        Subscribe to bar (candlestick) updates for given symbols and timeframes.

        :param symbols: List of symbols to subscribe to.
        :param timeframes: List of timeframes to subscribe to.
        """
        pass

    @abstractmethod
    def subscribe_ticks(self, symbols: List[str]) -> None:
        """
        Subscribe to tick (quote) updates for given symbols.

        :param symbols: List of symbols to subscribe to.
        """
        pass

    def set_bar_callback(self, callback: Callable[[BarEvent], None]) -> None:
        """Set the callback function to be called when a new bar is received."""
        self.on_bar_callback = callback

    def set_tick_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Set the callback function to be called when a new tick is received."""
        self.on_tick_callback = callback

    def set_error_callback(self, callback: Callable[[Exception], None]) -> None:
        """Set the callback function to be called when an error occurs."""
        self.on_error_callback = callback

    def set_close_callback(self, callback: Callable[[], None]) -> None:
        """Set the callback function to be called when the connection is closed."""
        self.on_close_callback = callback

    def _emit_bar(self, event: BarEvent) -> None:
        """Emit a bar event to the registered callback."""
        if self.on_bar_callback:
            try:
                self.on_bar_callback(event)
            except Exception as e:
                if self.on_error_callback:
                    self.on_error_callback(e)

    def _emit_tick(self, tick: Dict[str, Any]) -> None:
        """Emit a tick event to the registered callback."""
        if self.on_tick_callback:
            try:
                self.on_tick_callback(tick)
            except Exception as e:
                if self.on_error_callback:
                    self.on_error_callback(e)

    def _emit_error(self, exc: Exception) -> None:
        """Emit an error to the registered callback."""
        if self.on_error_callback:
            try:
                self.on_error_callback(exc)
            except Exception:
                # Avoid error in error callback
                pass

    def _emit_close(self) -> None:
        """Emit a close event to the registered callback."""
        if self.on_close_callback:
            try:
                self.on_close_callback()
            except Exception as e:
                if self.on_error_callback:
                    self.on_error_callback(e)

    @property
    def is_connected(self) -> bool:
        """Return True if the adapter is connected to the feed."""
        return self._is_connected

    def _set_connected(self, connected: bool) -> None:
        """Set the connection status."""
        self._is_connected = connected