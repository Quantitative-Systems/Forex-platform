"""
24/7/365 Auto-Trading Infrastructure.

Provides broker account auto-connect, health monitoring, continuous trading
supervision, and fail-closed risk gating for automated Forex execution.
"""

from forex_platform.auto_trading.controller import (
    AutoTradingController,
    AutoTradingStatus,
    BrokerConnectionState,
    ConnectionHealth,
)

__all__ = [
    "AutoTradingController",
    "AutoTradingStatus",
    "BrokerConnectionState",
    "ConnectionHealth",
]
