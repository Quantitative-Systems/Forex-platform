from datetime import datetime, timezone
from decimal import Decimal

import polars as pl
import pytest

from forex_platform.core.domain import CurrencyPair, LotSize
from forex_platform.discovery.discovery_loop import (
    CandidateHypothesis,
    ContinuousDiscoveryLoop,
    PromotionStatus,
)
from forex_platform.fractal_engine.timeframes import TIMEFRAME_SETS
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.provenance import DataProvenance
from forex_platform.research_engine.backtester import EventDrivenBacktester
from forex_platform.strategy_engine.base import BarEvent
from forex_platform.strategy_engine.fractal_institutional import InstitutionalFractalStrategy


def test_candidate_uses_all_five_sets_on_a_single_m1_feed() -> None:
    strategy = InstitutionalFractalStrategy()

    assert strategy.timeframe_sets == tuple(TIMEFRAME_SETS)
    assert strategy.timeframes == [Timeframe.M1]
    assert set(strategy.timeframe_sets) == {"SET_1", "SET_2", "SET_3", "SET_4", "SET_5"}
    assert strategy.minimum_net_reward_risk == Decimal("4")


def test_strategy_cannot_lower_frozen_four_r_floor() -> None:
    with pytest.raises(ValueError, match="frozen 4R floor"):
        InstitutionalFractalStrategy(minimum_net_reward_risk=Decimal("3.99"))


def test_strategy_can_be_isolated_to_one_set_for_fair_cell_tests() -> None:
    isolated = InstitutionalFractalStrategy(selected_set="SET 4")
    all_sets = InstitutionalFractalStrategy()

    assert isolated.selected_set == "SET_4"
    assert isolated.timeframe_sets == all_sets.timeframe_sets
    with pytest.raises(ValueError, match="unknown timeframe set"):
        InstitutionalFractalStrategy(selected_set="SET_6")


def test_discovery_registry_can_instantiate_the_fractal_candidate() -> None:
    loop = ContinuousDiscoveryLoop()
    candidate = CandidateHypothesis(
        candidate_id="fractal-test",
        strategy_name="InstitutionalFractalStrategy",
        symbol="GBPUSD",
        timeframe=Timeframe.M1,
    )

    strategy = loop.instantiate_strategy(candidate)

    assert isinstance(strategy, InstitutionalFractalStrategy)
    assert strategy.strategy_id == "fractal-test"
    assert strategy.symbols == ["GBPUSD"]


def test_strategy_rejects_non_m1_events() -> None:
    strategy = InstitutionalFractalStrategy()
    event = BarEvent(
        symbol="EURUSD",
        timeframe=Timeframe.M15,
        timestamp=datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc),
        open=Decimal("1.1000"), high=Decimal("1.1002"),
        low=Decimal("1.0998"), close=Decimal("1.1001"),
        volume=Decimal("10"),
    )

    with pytest.raises(ValueError, match="requires completed M1"):
        strategy.on_bar(event)


def test_position_size_rounds_down_beneath_the_cash_risk_limit() -> None:
    size = InstitutionalFractalStrategy.size_for_risk(
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.0025"),
        stop_pips=Decimal("25"),
        pip_value_per_lot=Decimal("10"),
        lot_step=Decimal("0.01"),
        maximum_lots=Decimal("1"),
    )

    assert size == LotSize.from_lots("1.00")
    modeled_risk = Decimal(size.standard_lots) * Decimal("25") * Decimal("10")
    assert modeled_risk <= Decimal("100000") * Decimal("0.0025")


def test_position_size_fails_closed_below_minimum_lot() -> None:
    size = InstitutionalFractalStrategy.size_for_risk(
        equity=Decimal("1000"),
        risk_fraction=Decimal("0.001"),
        stop_pips=Decimal("80"),
        pip_value_per_lot=Decimal("10"),
    )

    assert size is None


def test_non_usd_quote_requires_an_explicit_conversion_rate() -> None:
    strategy = InstitutionalFractalStrategy()
    eur_gbp = strategy._quote_to_account_rate(CurrencyPair.from_symbol("EURGBP"), Decimal("0.85"))
    assert eur_gbp is None

    strategy.update_account_state(
        equity=Decimal("100000"),
        daily_realized_pnl=Decimal("0"),
        quote_to_account_rates={"GBP": Decimal("1.27")},
    )
    assert strategy._quote_to_account_rate(CurrencyPair.from_symbol("EURGBP"), Decimal("0.85")) == Decimal("1.27")


def test_backtester_converts_usd_base_quote_pnl_and_fails_closed_on_cross() -> None:
    strategy = InstitutionalFractalStrategy()
    usd_jpy = EventDrivenBacktester(strategy, CurrencyPair.from_symbol("USDJPY"))
    eur_gbp = EventDrivenBacktester(strategy, CurrencyPair.from_symbol("EURGBP"))

    assert usd_jpy.quote_to_account_rate({}, Decimal("150")) == Decimal(1) / Decimal("150")
    assert eur_gbp.quote_to_account_rate({"quote_to_account_rate": "1.27"}, Decimal("0.85")) == Decimal("1.27")
    with pytest.raises(ValueError, match="Missing GBP-to-USD conversion"):
        eur_gbp.quote_to_account_rate({}, Decimal("0.85"))


def test_discovery_rejects_synthetic_fractal_candidate_before_backtesting() -> None:
    candidate = CandidateHypothesis(
        candidate_id="candidate-smoke",
        strategy_name="InstitutionalFractalStrategy",
        symbol="EURUSD",
        timeframe=Timeframe.M1,
        data_provenance=DataProvenance.SYNTHETIC,
    )

    result = ContinuousDiscoveryLoop().evaluate_candidate(candidate, pl.DataFrame())

    assert result.status == PromotionStatus.REJECTED_GATES
    assert "real vendor or broker-exported data is required" in result.error_message


def test_discovery_rejects_fractal_candidate_without_m1_resolution() -> None:
    candidate = CandidateHypothesis(
        candidate_id="candidate-wrong-timeframe",
        strategy_name="InstitutionalFractalStrategy",
        symbol="EURUSD",
        timeframe=Timeframe.M15,
        data_provenance=DataProvenance.SYNTHETIC,
    )

    result = ContinuousDiscoveryLoop().evaluate_candidate(candidate, pl.DataFrame())

    assert result.status == PromotionStatus.REJECTED_GATES
    assert "requires M1 source bars" in result.error_message


def test_daily_loss_circuit_stops_new_strategy_intents() -> None:
    strategy = InstitutionalFractalStrategy(account_equity=Decimal("100000"))
    strategy.update_account_state(
        equity=Decimal("100000"),
        daily_realized_pnl=Decimal("-2000"),
    )
    event = BarEvent(
        symbol="EURUSD",
        timeframe=Timeframe.M1,
        timestamp=datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc),
        open=Decimal("1.1000"), high=Decimal("1.1002"),
        low=Decimal("1.0998"), close=Decimal("1.1001"),
        volume=Decimal("10"), spread=Decimal("1"),
    )

    assert strategy.on_bar(event) == []
    assert strategy.last_decision["EURUSD"] == "DAILY_LOSS_LIMIT"
