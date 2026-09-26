"""
Quote telemetry for pre-trade risk checks.

Live modes require a real quote from a connected broker gateway. Paper/demo
modes may use deterministic reference quotes, which are labeled as PAPER so
they can never be mistaken for executable market data in an audit trail.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from forex_platform.core.domain import to_decimal
from forex_platform.production.brokers import BrokerRegistry
from forex_platform.production.observability import get_logger
from forex_platform.production.settings import Settings
from forex_platform.risk_engine.firewall import MarketTelemetry

logger = get_logger(__name__)


PAPER_REFERENCE_PRICES: Dict[str, Decimal] = {
    "EURUSD": Decimal("1.10000"),
    "GBPUSD": Decimal("1.27000"),
    "USDJPY": Decimal("150.000"),
    "AUDUSD": Decimal("0.66000"),
    "USDCAD": Decimal("1.35000"),
    "USDCHF": Decimal("0.88000"),
    "NZDUSD": Decimal("0.60000"),
    "EURGBP": Decimal("0.86000"),
    "EURJPY": Decimal("165.000"),
    "GBPJPY": Decimal("190.000"),
    "AUDJPY": Decimal("99.000"),
    "EURCHF": Decimal("0.97000"),
}


class MarketDataService:
    """Resolves pre-trade telemetry from brokers or labeled paper references."""

    def __init__(self, settings: Settings, brokers: BrokerRegistry) -> None:
        self.settings = settings
        self.brokers = brokers
        self._paper_spreads: Dict[str, List[Decimal]] = {}

    # ------------------------------------------------------------------
    # Real broker quotes
    # ------------------------------------------------------------------
    def _from_broker(self, symbol: str) -> Optional[MarketTelemetry]:
        for spec in self.brokers.list_specs():
            adapter = self.brokers.get_adapter(spec.account_id)
            if adapter is None or not adapter.is_connected:
                continue
            get_tick = getattr(adapter, "get_tick", None)
            if get_tick is None:
                continue
            try:
                payload = get_tick(symbol)
            except Exception as exc:  # noqa: BLE001 - try the next route
                logger.warning("Quote fetch failed on %s: %s", spec.account_id, exc)
                continue
            tick = payload.get("ticks", [payload])[0] if isinstance(payload, dict) else None
            if not tick or tick.get("bid") is None or tick.get("ask") is None:
                continue
            timestamp = tick.get("timestamp")
            quote_time = (
                datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if timestamp
                else datetime.now(timezone.utc)
            )
            if quote_time.tzinfo is None:
                quote_time = quote_time.replace(tzinfo=timezone.utc)
            return MarketTelemetry(
                symbol=symbol,
                bid=to_decimal(tick["bid"]),
                ask=to_decimal(tick["ask"]),
                quote_timestamp=quote_time,
                recent_spreads_pips=[],
            )
        return None

    # ------------------------------------------------------------------
    # Labeled paper quotes
    # ------------------------------------------------------------------
    def _paper_reference(self, symbol: str) -> MarketTelemetry:
        clean = symbol.upper()
        reference = PAPER_REFERENCE_PRICES.get(clean)
        if reference is None:
            digest = hashlib.sha256(clean.encode("utf-8")).digest()
            reference = Decimal("1.00000") + Decimal(int.from_bytes(digest[:2], "big")) / Decimal("100000")
        half_spread = reference * Decimal("0.00005")  # 0.5 pip on a 1.0-ish price
        spreads = self._paper_spreads.setdefault(clean, [])
        spread_pips = (half_spread * Decimal("2")) / (Decimal("0.0001") if len(clean) == 6 else Decimal("0.01"))
        spreads.append(spread_pips)
        if len(spreads) > 20:
            spreads.pop(0)
        return MarketTelemetry(
            symbol=clean,
            bid=reference - half_spread,
            ask=reference + half_spread,
            quote_timestamp=datetime.now(timezone.utc),
            recent_spreads_pips=list(spreads),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def telemetry_for(self, symbol: str, mode: Optional[str] = None) -> Optional[MarketTelemetry]:
        mode_upper = (mode or self.settings.default_execution_mode).upper()
        real = self._from_broker(symbol)
        if real is not None:
            return real
        if mode_upper == "LIVE":
            return None
        return self._paper_reference(symbol)

    def quote(self, symbol: str, mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
        mode_upper = (mode or self.settings.default_execution_mode).upper()
        real = self._from_broker(symbol)
        source = "BROKER"
        telemetry = real
        if telemetry is None:
            if mode_upper == "LIVE":
                return None
            telemetry = self._paper_reference(symbol)
            source = "PAPER"
        return {
            "symbol": telemetry.symbol,
            "bid": str(telemetry.bid),
            "ask": str(telemetry.ask),
            "spread_pips": str(telemetry.spread_pips),
            "quote_timestamp": telemetry.quote_timestamp.isoformat(),
            "source": source,
        }
