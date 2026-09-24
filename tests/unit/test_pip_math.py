"""
Unit tests for precision pip math, CurrencyPair specifications, LotSize arithmetic,
and account currency conversion.
"""

from decimal import Decimal
import pytest
from datetime import datetime, timezone

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    Fill,
    LotSize,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PipCalculator,
    Position,
    UrgencyLevel,
)


class TestCurrencyPairMath:
    """Test CurrencyPair initialization, pip size, and rounding."""

    def test_eurusd_standard_5_decimal(self):
        pair = CurrencyPair.from_symbol("EURUSD")
        assert pair.base_currency == "EUR"
        assert pair.quote_currency == "USD"
        assert pair.symbol == "EURUSD"
        assert pair.price_precision == 5
        assert pair.pip_decimal_places == 4
        assert pair.pip_size == Decimal("0.0001")
        assert pair.pipette_size == Decimal("0.00001")
        assert not pair.is_jpy_cross

        # Test price diff to pips
        price_diff = Decimal("0.00255")
        pips = pair.to_pips(price_diff)
        assert pips == Decimal("25.5")

        # Test pips to price
        assert pair.to_price(Decimal("25.5")) == Decimal("0.00255")

        # Test price rounding
        assert pair.round_price(Decimal("1.085016")) == Decimal("1.08502")

    def test_gbpusd_formatting_variants(self):
        pair1 = CurrencyPair.from_symbol("GBP/USD")
        pair2 = CurrencyPair.from_symbol("gbp_usd")
        assert pair1.symbol == "GBPUSD"
        assert pair2.symbol == "GBPUSD"
        assert pair1.pip_size == Decimal("0.0001")

    def test_usdjpy_3_decimal(self):
        pair = CurrencyPair.from_symbol("USDJPY")
        assert pair.base_currency == "USD"
        assert pair.quote_currency == "JPY"
        assert pair.symbol == "USDJPY"
        assert pair.price_precision == 3
        assert pair.pip_decimal_places == 2
        assert pair.pip_size == Decimal("0.01")
        assert pair.pipette_size == Decimal("0.001")
        assert pair.is_jpy_cross

        # 50 pips in USDJPY = 0.50 price difference
        price_diff = Decimal("0.500")
        assert pair.to_pips(price_diff) == Decimal("50.0")
        assert pair.to_price(Decimal("50.0")) == Decimal("0.500")
        assert pair.round_price(Decimal("154.2568")) == Decimal("154.257")

    def test_invalid_symbol_raises(self):
        with pytest.raises(ValueError):
            CurrencyPair.from_symbol("INVALID")
        with pytest.raises(ValueError):
            CurrencyPair.from_symbol("US")


class TestLotSizeMath:
    """Test LotSize conversions and arithmetic."""

    def test_standard_mini_micro_lots(self):
        standard = LotSize.from_lots(1.0)
        assert standard.units == 100_000
        assert standard.standard_lots == Decimal("1.0")
        assert standard.mini_lots == Decimal("10.0")
        assert standard.micro_lots == Decimal("100.0")
        assert standard.nano_lots == Decimal("1000.0")

        mini = LotSize.from_lots(0.1)
        assert mini.units == 10_000
        assert mini.standard_lots == Decimal("0.1")

        micro = LotSize.from_lots(0.01)
        assert micro.units == 1_000

    def test_from_units(self):
        lot = LotSize.from_units(250_000)
        assert lot.standard_lots == Decimal("2.5")
        assert lot.units == 250_000

    def test_lot_arithmetic(self):
        l1 = LotSize.from_lots(0.5)  # 50,000 units
        l2 = LotSize.from_lots(0.3)  # 30,000 units
        added = l1 + l2
        assert added.units == 80_000
        assert added.standard_lots == Decimal("0.8")

        subbed = l1 - l2
        assert subbed.units == 20_000

        scaled = l2 * Decimal("2")
        assert scaled.units == 60_000

        assert l1 > l2
        assert l2 < l1

    def test_invalid_lots_raise(self):
        with pytest.raises(ValueError):
            LotSize.from_lots(0)
        with pytest.raises(ValueError):
            LotSize.from_lots(-1.5)
        with pytest.raises(ValueError):
            LotSize.from_units(-100)


class TestPipCalculator:
    """Test pip values and PnL conversions across accounts."""

    def test_eurusd_pip_value_usd_account(self):
        eurusd = CurrencyPair.from_symbol("EURUSD")
        lot = LotSize.from_lots(1.0)  # 100,000 units

        # 100,000 units * 0.0001 = $10.00 USD
        pip_val = PipCalculator.pip_value_in_quote(eurusd, lot)
        assert pip_val == Decimal("10.0000")

        # In USD account (quote currency = account currency)
        pip_val_acc = PipCalculator.pip_value(eurusd, lot, quote_to_account_rate=Decimal("1.0"))
        assert pip_val_acc == Decimal("10.0000")

        # Mini lot: 10,000 units * 0.0001 = $1.00 USD
        mini = LotSize.from_lots(0.1)
        assert PipCalculator.pip_value(eurusd, mini) == Decimal("1.0000")

    def test_usdjpy_pip_value_usd_account(self):
        usdjpy = CurrencyPair.from_symbol("USDJPY")
        lot = LotSize.from_lots(1.0)  # 100,000 units

        # 100,000 * 0.01 = 1,000 JPY
        pip_val_jpy = PipCalculator.pip_value_in_quote(usdjpy, lot)
        assert pip_val_jpy == Decimal("1000.00")

        # At USD/JPY rate 150.00, JPY to USD rate is 1 / 150.00
        rate = Decimal("1.0") / Decimal("150.00")
        pip_val_usd = PipCalculator.pip_value(usdjpy, lot, quote_to_account_rate=rate)
        # 1000 / 150 = 6.66666...
        expected = Decimal("1000") / Decimal("150")
        assert pip_val_usd == expected

    def test_cross_pair_eurgbp_usd_account(self):
        eurgbp = CurrencyPair.from_symbol("EURGBP")
        lot = LotSize.from_lots(1.0)  # 100,000 units

        # 100,000 * 0.0001 = 10 GBP
        pip_val_gbp = PipCalculator.pip_value_in_quote(eurgbp, lot)
        assert pip_val_gbp == Decimal("10.0000")

        # Account is USD. Conversion from GBP to USD via GBP/USD rate (e.g. 1.2500)
        gbp_usd_rate = Decimal("1.2500")
        pip_val_usd = PipCalculator.pip_value(eurgbp, lot, quote_to_account_rate=gbp_usd_rate)
        # 10 GBP * 1.2500 = $12.50 USD
        assert pip_val_usd == Decimal("12.50000000")

    def test_pnl_calculation_buy_and_sell(self):
        eurusd = CurrencyPair.from_symbol("EURUSD")
        lot = LotSize.from_lots(1.0)  # 100,000 units

        # Long trade: 1.08500 to 1.08750 (gain of 25 pips = $250.00)
        pnl_long = PipCalculator.calculate_pnl(
            side=OrderSide.BUY,
            entry_price=Decimal("1.08500"),
            exit_price=Decimal("1.08750"),
            lot_size=lot,
            pair=eurusd,
        )
        assert pnl_long == Decimal("250.00000")

        # Short trade: 1.08750 to 1.08500 (gain of 25 pips = $250.00)
        pnl_short = PipCalculator.calculate_pnl(
            side=OrderSide.SELL,
            entry_price=Decimal("1.08750"),
            exit_price=Decimal("1.08500"),
            lot_size=lot,
            pair=eurusd,
        )
        assert pnl_short == Decimal("250.00000")

        # Losing trade: Long EURUSD 1.08750 to 1.08500 (-$250.00)
        pnl_loss = PipCalculator.calculate_pnl(
            side=OrderSide.BUY,
            entry_price=Decimal("1.08750"),
            exit_price=Decimal("1.08500"),
            lot_size=lot,
            pair=eurusd,
        )
        assert pnl_loss == Decimal("-250.00000")


class TestTradingDomainContracts:
    """Test OrderIntent, ExecutionOrder, Fill, and Position."""

    def test_order_intent_creation(self):
        now = datetime.now(timezone.utc)
        lot = LotSize.from_lots(1.5)
        intent = OrderIntent(
            intent_id="intent-001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            lot_size=lot,
            limit_price=Decimal("1.08200"),
            stop_loss=Decimal("1.07900"),
            take_profit=Decimal("1.08800"),
            urgency=UrgencyLevel.HIGH,
            timestamp=now,
        )
        assert intent.side == OrderSide.BUY
        assert intent.limit_price == Decimal("1.08200")
        assert intent.lot_size.units == 150_000

    def test_position_lifecycle(self):
        now = datetime.now(timezone.utc)
        pos = Position(
            position_id="pos-001",
            symbol="EURUSD",
            side=OrderSide.BUY,
            units=100_000,
            average_entry_price=Decimal("1.08000"),
            current_price=Decimal("1.08000"),
            opened_at=now,
            updated_at=now,
        )
        assert pos.is_open
        assert pos.unrealized_pnl == Decimal("0.0")

        # Market price increases by 20 pips to 1.08200
        pos.update_market_price(Decimal("1.08200"))
        # 100,000 * 0.00200 = 200.00
        assert pos.unrealized_pnl == Decimal("200.00000")

        # Close position at 1.08300 (30 pips gain = $300.00)
        realized = pos.close(Decimal("1.08300"))
        assert realized == Decimal("300.00000")
        assert pos.realized_pnl == Decimal("300.00000")
        assert not pos.is_open
        assert pos.units == 0
