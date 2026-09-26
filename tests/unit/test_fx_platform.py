"""Tests for the all-FX registry, regime engine, and target allocator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from forex_platform.core.instruments import FXInstrumentRegistry
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.portfolio_engine.regime import MarketRegime, MarketRegimeEngine
from forex_platform.portfolio_engine.target_allocator import (
    TargetPosition,
    TargetPositionAllocator,
)
from forex_platform.strategy_engine.base import BarEvent


class TestFXInstrumentRegistry:
    def test_registry_contains_all_28_conventional_pairs(self):
        summary = FXInstrumentRegistry.summary()
        assert summary["total_pairs"] == 28
        assert summary["majors"] == 7
        assert summary["crosses"] == 21
        assert len(set(FXInstrumentRegistry.SYMBOLS)) == 28

    def test_registry_normalizes_and_rejects_non_fx(self):
        assert FXInstrumentRegistry.get("eur/usd").symbol == "EURUSD"
        assert FXInstrumentRegistry.is_supported("AUDCAD")
        assert not FXInstrumentRegistry.is_supported("XAUUSD")
        with pytest.raises(ValueError):
            FXInstrumentRegistry.get("XAUUSD")

    def test_currency_lookup(self):
        symbols = FXInstrumentRegistry.symbols_for_currency("EUR")
        assert "EURUSD" in symbols
        assert "EURJPY" in symbols
        assert all("EUR" in symbol for symbol in symbols)


class TestRegimeEngine:
    def _bars(self, count: int, step: float) -> list[BarEvent]:
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        bars = []
        price = Decimal("1.0800")
        for i in range(count):
            close = price + Decimal(str(step))
            bars.append(BarEvent(
                symbol="EURUSD", timeframe=Timeframe.M15,
                timestamp=start + timedelta(minutes=15 * i),
                open=price, high=close + Decimal("0.0002"),
                low=price - Decimal("0.0002"), close=close,
                volume=Decimal("100"), spread=Decimal("1"),
            ))
            price = close
        return bars

    def test_insufficient_data_is_unknown(self):
        result = MarketRegimeEngine.classify("EURUSD", self._bars(10, 0.0001))
        assert result.regime == MarketRegime.UNKNOWN
        assert not MarketRegimeEngine.allows_trading(result.regime)

    def test_directional_series_is_trending(self):
        result = MarketRegimeEngine.classify("EURUSD", self._bars(40, 0.0008))
        assert result.regime == MarketRegime.TRENDING
        assert result.confidence > 0


class TestTargetPositionAllocator:
    def test_unknown_regime_fails_closed(self):
        decision = TargetPositionAllocator().allocate(
            [TargetPosition("EURUSD", 100_000)], regime=MarketRegime.UNKNOWN
        )
        assert decision.approved is False
        assert decision.intents == []
        assert decision.rejected == ["regime"]

    def test_target_delta_becomes_buy_intent(self):
        decision = TargetPositionAllocator().allocate(
            [TargetPosition("EURUSD", 100_000, strategy_id="trend")],
            regime=MarketRegime.TRENDING,
        )
        assert decision.approved is True
        assert len(decision.intents) == 1
        assert decision.intents[0].lot_size.units == 100_000
        assert decision.intents[0].side.value == "BUY"

    def test_symbol_cap_rejects_oversized_target(self):
        decision = TargetPositionAllocator(max_symbol_units=50_000).allocate(
            [TargetPosition("EURUSD", 100_000)], regime=MarketRegime.TRENDING
        )
        assert decision.approved is False
        assert "symbol_cap:EURUSD" in decision.rejected

    def test_hedge_direction_reduces_currency_delta(self):
        intent = TargetPositionAllocator().hedge_currency_delta("EUR", 100_000)
        assert intent is not None
        assert intent.symbol == "EURUSD"
        assert intent.side.value == "SELL"

    def test_submit_targets_reuses_submit_intent(self):
        from forex_platform.production.execution import ProductionExecutionService

        service = object.__new__(ProductionExecutionService)
        service.oms = SimpleNamespace(get_open_positions=lambda: [])
        service.audit = None
        calls = []
        service.submit_intent = lambda intent, **kwargs: calls.append((intent, kwargs)) or SimpleNamespace(
            model_dump=lambda mode: {"approved": True, "intent_id": intent.intent_id}
        )
        result = service.submit_targets(
            [TargetPosition("EURUSD", 100_000, strategy_id="trend")],
            regime=MarketRegime.TRENDING,
        )
        assert result["approved"] is True
        assert len(calls) == 1
        assert calls[0][0].symbol == "EURUSD"
