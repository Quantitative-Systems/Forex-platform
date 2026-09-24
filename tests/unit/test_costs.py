"""
Unit tests for economic friction models:
Dynamic spread regimes, raw ECN commission calculations, and rollover/triple-swap models.
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from forex_platform.core.domain import CurrencyPair, LotSize, OrderSide, Position
from forex_platform.costs.commission import CommissionModel
from forex_platform.costs.spread_model import DynamicSpreadModel, MarketClosedError
from forex_platform.costs.swap_engine import SwapEngine


class TestDynamicSpreadModel:
    """Test session-aware dynamic spread calculation across liquidity regimes."""

    def setup_method(self):
        self.model = DynamicSpreadModel()
        self.eurusd = CurrencyPair.from_symbol("EURUSD")

    def test_london_ny_overlap_baseline(self):
        # Tuesday 14:00 UTC -> London + NY peak overlap (1.0x baseline)
        dt = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        mult = self.model.get_spread_multiplier(dt)
        assert mult == Decimal("1.0")

        spread = self.model.calculate_spread(self.eurusd, dt)
        assert spread == Decimal("0.80")  # Baseline EURUSD spread

    def test_single_western_session(self):
        # Tuesday 10:00 UTC -> London solo (1.3x)
        dt = datetime(2026, 1, 6, 10, 0, 0, tzinfo=timezone.utc)
        mult = self.model.get_spread_multiplier(dt)
        assert mult == Decimal("1.3")

        spread = self.model.calculate_spread(self.eurusd, dt)
        # 0.8 * 1.3 = 1.04
        assert spread == Decimal("1.04")

    def test_asian_session_widening(self):
        # Tuesday 03:00 UTC -> Sydney + Tokyo (3.5x widening, within 3x-5x requirement)
        dt = datetime(2026, 1, 6, 3, 0, 0, tzinfo=timezone.utc)
        mult = self.model.get_spread_multiplier(dt)
        assert Decimal("3.0") <= mult <= Decimal("5.0")

        spread = self.model.calculate_spread(self.eurusd, dt)
        # 0.8 * 3.5 = 2.80
        assert spread == Decimal("2.80")

    def test_rollover_window_widening(self):
        # Tuesday 21:00 UTC -> Daily rollover (15.0x widening, within 10x-20x requirement)
        dt = datetime(2026, 1, 6, 21, 0, 0, tzinfo=timezone.utc)
        mult = self.model.get_spread_multiplier(dt)
        assert Decimal("10.0") <= mult <= Decimal("20.0")

        spread = self.model.calculate_spread(self.eurusd, dt)
        # 0.8 * 15.0 = 12.00 pips
        assert spread == Decimal("12.00")

    def test_weekend_market_closed(self):
        # Saturday 12:00 UTC -> Weekend
        dt = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(MarketClosedError):
            self.model.calculate_spread(self.eurusd, dt)

    def test_bid_ask_generation(self):
        # Tuesday 14:00 UTC mid = 1.08500
        dt = datetime(2026, 1, 6, 14, 0, 0, tzinfo=timezone.utc)
        bid, ask = self.model.get_bid_ask(self.eurusd, Decimal("1.08500"), dt)

        # Baseline spread = 0.8 pips = 0.00008
        # Half spread = 0.00004
        # Bid = 1.08500 - 0.00004 = 1.08496
        # Ask = 1.08500 + 0.00004 = 1.08504
        assert bid == Decimal("1.08496")
        assert ask == Decimal("1.08504")
        assert ask - bid == Decimal("0.00008")


class TestCommissionModel:
    """Test raw ECN commission calculations."""

    def setup_method(self):
        self.model = CommissionModel(roundturn_per_standard_lot=Decimal("7.00"))

    def test_standard_lot_commission(self):
        lot = LotSize.from_lots(1.0)  # 100,000 units
        # Single side (half-turn): $3.50
        comm_half = self.model.calculate_commission(lot, is_roundturn=False)
        assert comm_half == Decimal("3.5000")

        # Round turn (full cycle): $7.00
        comm_rt = self.model.calculate_commission(lot, is_roundturn=True)
        assert comm_rt == Decimal("7.0000")

    def test_fractional_lot_proportionality(self):
        # 0.1 mini lot (10,000 units)
        mini = LotSize.from_lots(0.1)
        assert self.model.calculate_commission(mini, is_roundturn=False) == Decimal("0.3500")
        assert self.model.calculate_commission(mini, is_roundturn=True) == Decimal("0.7000")

        # 0.01 micro lot (1,000 units)
        micro = LotSize.from_lots(0.01)
        assert self.model.calculate_commission(micro, is_roundturn=False) == Decimal("0.0350")
        assert self.model.calculate_commission(micro, is_roundturn=True) == Decimal("0.0700")

    def test_custom_commission_rate(self):
        custom_model = CommissionModel(roundturn_per_standard_lot=Decimal("5.00"))
        lot = LotSize.from_lots(1.0)
        assert custom_model.calculate_commission(lot, is_roundturn=False) == Decimal("2.5000")
        assert custom_model.calculate_commission(lot, is_roundturn=True) == Decimal("5.0000")


class TestSwapEngine:
    """Test overnight financing and Wednesday triple-swap settlement."""

    def setup_method(self):
        self.eurusd = CurrencyPair.from_symbol("EURUSD")
        self.lot = LotSize.from_lots(1.0)  # 100,000 units ($10.00/pip)

    def test_regular_daily_swap(self):
        # Tuesday 21:00 UTC rollover (weekday 1)
        tue_dt = datetime(2026, 1, 6, 21, 0, 0, tzinfo=timezone.utc)
        assert SwapEngine.get_swap_multiplier(tue_dt) == 1

        # Long swap: -0.6 pips per day
        swap_long = SwapEngine.calculate_swap(
            pair=self.eurusd,
            lot_size=self.lot,
            side=OrderSide.BUY,
            rollover_dt=tue_dt,
            long_swap_pips=Decimal("-0.6"),
            short_swap_pips=Decimal("0.2"),
        )
        # -0.6 pips * 1 * $10.00/pip = -$6.00 USD
        assert swap_long == Decimal("-6.0000")

        # Short swap: +0.2 pips per day
        swap_short = SwapEngine.calculate_swap(
            pair=self.eurusd,
            lot_size=self.lot,
            side=OrderSide.SELL,
            rollover_dt=tue_dt,
            long_swap_pips=Decimal("-0.6"),
            short_swap_pips=Decimal("0.2"),
        )
        # +0.2 pips * 1 * $10.00/pip = +$2.00 USD
        assert swap_short == Decimal("2.0000")

    def test_wednesday_triple_swap(self):
        # Wednesday 21:00 UTC rollover (weekday 2)
        wed_dt = datetime(2026, 1, 7, 21, 0, 0, tzinfo=timezone.utc)
        assert SwapEngine.get_swap_multiplier(wed_dt) == 3

        # Long swap with 3x multiplier
        swap_long_triple = SwapEngine.calculate_swap(
            pair=self.eurusd,
            lot_size=self.lot,
            side=OrderSide.BUY,
            rollover_dt=wed_dt,
            long_swap_pips=Decimal("-0.6"),
            short_swap_pips=Decimal("0.2"),
        )
        # -0.6 * 3 * $10.00 = -$18.00 USD
        assert swap_long_triple == Decimal("-18.0000")

        # Short swap with 3x multiplier
        swap_short_triple = SwapEngine.calculate_swap(
            pair=self.eurusd,
            lot_size=self.lot,
            side=OrderSide.SELL,
            rollover_dt=wed_dt,
            long_swap_pips=Decimal("-0.6"),
            short_swap_pips=Decimal("0.2"),
        )
        # +0.2 * 3 * $10.00 = +$6.00 USD
        assert swap_short_triple == Decimal("6.0000")

    def test_apply_rollover_to_position(self):
        pos = Position(
            position_id="pos-swap-01",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=100_000,
            average_entry_price=Decimal("1.08500"),
            current_price=Decimal("1.08500"),
            opened_at=datetime(2026, 1, 7, 10, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 7, 10, 0, 0, tzinfo=timezone.utc),
        )

        wed_dt = datetime(2026, 1, 7, 21, 0, 0, tzinfo=timezone.utc)
        applied = SwapEngine.apply_rollover_to_position(
            position=pos,
            pair=self.eurusd,
            rollover_dt=wed_dt,
            long_swap_pips=Decimal("-0.5"),
            short_swap_pips=Decimal("0.1"),
        )
        # -0.5 * 3 * $10.00 = -$15.00
        assert applied == Decimal("-15.0000")
        assert pos.total_swap == Decimal("-15.0000")
