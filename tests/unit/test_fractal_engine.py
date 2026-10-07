from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import polars as pl

from forex_platform.fractal_engine.hypothesis_engine import FractalHypothesisEngine
from forex_platform.fractal_engine.research import (
    benjamini_hochberg,
    run_campaign,
    run_set_trade_matrix,
)
from forex_platform.fractal_engine.state_engine import (
    CanonicalTimeframeState,
    StructuralRange,
    UniversalTimeframeStateEngine,
    aggregate_bars,
)
from forex_platform.fractal_engine.state_graph import (
    CausalMovementLedger,
    FractalStateGraph,
    build_set_views,
)
from forex_platform.fractal_engine.timeframes import (
    CANONICAL_LADDER,
    TIMEFRAME_SETS,
    CanonicalTimeframe,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.provenance import DataProvenance
from forex_platform.market_model.contracts import MarketPhase
from forex_platform.market_model.zones.liquidity import (
    LiquidityKind,
    detect_liquidity_pools,
)


def make_bars(count: int, start: datetime | None = None) -> pl.DataFrame:
    start = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    previous = 1.1
    for i in range(count):
        close = 1.1 + i * 0.00001 + (0.0005 if (i // 8) % 2 else -0.0005)
        open_price = previous
        rows.append({
            "timestamp": start + timedelta(minutes=15 * i),
            "open": open_price,
            "high": max(open_price, close) + 0.0002,
            "low": min(open_price, close) - 0.0002,
            "close": close,
            "volume": 100.0,
            "spread": 1.0,
        })
        previous = close
    return pl.DataFrame(rows)


def test_trade_matrix_requires_100_trades_for_each_of_20_real_data_cells() -> None:
    symbols = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD")
    audits = {symbol: {"reason": "no qualifying real M1 history"} for symbol in symbols}

    matrix = run_set_trade_matrix(symbols, {}, audits)

    assert matrix["family_size"] == 20
    assert matrix["cells_tested"] == 0
    assert len(matrix["cells"]) == 20
    assert {cell["required_minimum_trades"] for cell in matrix["cells"]} == {100}
    assert {cell["status"] for cell in matrix["cells"]} == {"NOT_RUN_INSUFFICIENT_REAL_DATA"}


def make_state(
    timeframe: str,
    timestamp: datetime,
    *,
    index: int = 0,
    trend: int | None = 1,
    phase: MarketPhase = MarketPhase.CONTINUATION,
    location: str = "PREMIUM",
    transition: tuple[str, ...] = ("PHASE_TRANSITION",),
) -> CanonicalTimeframeState:
    signature = f"{trend or 0}|{phase.value}|{location}|R"
    return CanonicalTimeframeState(
        state_id=f"EURUSD|{timeframe}|{timestamp.isoformat()}|{index}",
        symbol="EURUSD",
        timeframe=timeframe,
        timestamp=timestamp,
        bar_index=index,
        current_price=Decimal("1.1000"),
        structural_trend=trend,
        swing_state="HH_HL" if trend == 1 else "LH_LL" if trend == -1 else "MIXED",
        phase=phase,
        location=location,
        range_location=Decimal("0.75") if location == "PREMIUM" else Decimal("0.25"),
        structural_range=StructuralRange(
            low=Decimal("1.0"), high=Decimal("1.2"),
            low_swing_index=0, high_swing_index=1,
            low_timestamp=timestamp, high_timestamp=timestamp,
        ),
        range_ambiguous=False,
        range_candidate_count=1,
        swings=(),
        breaks=(),
        key_levels=(),
        zones=(),
        state_valid=True,
        validity_reason="test fixture",
        invalidation_condition="below 1.0" if trend == 1 else None,
        state_signature=signature,
        zone_ids=(),
        transition=transition,
        duration_bars=1,
        session_regime="LONDON_SOLO",
        active_sessions=("LONDON",),
    )


def test_frozen_ladder_and_set_windows_are_exact() -> None:
    assert [tf.value for tf in CANONICAL_LADDER] == ["1M", "1W", "1D", "4H", "1H", "15M", "3M"]
    assert [[tf.value for tf in values] for values in TIMEFRAME_SETS.values()] == [
        ["1M", "1W", "1D"], ["1W", "1D", "4H"], ["1D", "4H", "1H"],
        ["4H", "1H", "15M"], ["1H", "15M", "3M"],
    ]


def test_aggregation_emits_only_closed_target_candles() -> None:
    bars = make_bars(7)
    h1 = aggregate_bars(bars, "1H", Timeframe.M15)
    assert h1.height == 1
    assert h1["timestamp"][0] == datetime(2024, 1, 1, 1, tzinfo=timezone.utc)
    assert h1["source_bar_count"][0] == 4
    assert aggregate_bars(bars, "3M", Timeframe.M15).is_empty()


def test_state_engine_uses_one_causal_state_history_and_set5_needs_m1() -> None:
    bars = make_bars(96)
    engine = UniversalTimeframeStateEngine("EURUSD", Timeframe.M15, swing_lookback=2)
    histories = engine.build(bars)
    assert set(histories) == {"1M", "1W", "1D", "4H", "1H", "15M"}
    assert "3M" not in histories
    assert histories["15M"]
    state = histories["15M"][20]
    assert state.timestamp == bars["timestamp"][20] + timedelta(minutes=15)
    assert all(swing.index + 2 <= state.bar_index for swing in state.swings)


def test_future_append_does_not_rewrite_past_state() -> None:
    bars = make_bars(80)
    full = UniversalTimeframeStateEngine("EURUSD", Timeframe.M15, swing_lookback=2).build(bars)
    prefix = UniversalTimeframeStateEngine("EURUSD", Timeframe.M15, swing_lookback=2).build(bars.head(41))
    past_full = full["15M"][30]
    past_prefix = prefix["15M"][30]
    assert past_full.state_id == past_prefix.state_id
    assert past_full.state_signature == past_prefix.state_signature
    assert past_full.transition == past_prefix.transition
    assert [(s.index, s.price, s.swing_type) for s in past_full.swings] == [
        (s.index, s.price, s.swing_type) for s in past_prefix.swings
    ]


def test_streaming_and_batch_snapshots_match() -> None:
    bars = make_bars(48)
    batch_engine = UniversalTimeframeStateEngine("EURUSD", Timeframe.M15, swing_lookback=2)
    batch = batch_engine.build(bars)
    stream_engine = UniversalTimeframeStateEngine("EURUSD", Timeframe.M15, swing_lookback=2)
    for row in bars.iter_rows(named=True):
        stream_engine.update(row)
    for timeframe in ("15M", "1H", "4H"):
        streamed = stream_engine.get_history(timeframe)
        assert [state.state_id for state in streamed] == [state.state_id for state in batch[timeframe]]
        assert [state.state_signature for state in streamed] == [state.state_signature for state in batch[timeframe]]


def test_overlapping_set_roles_reference_the_same_state_object() -> None:
    timestamp = datetime(2024, 1, 2, tzinfo=timezone.utc)
    histories = {
        timeframe.value: [make_state(timeframe.value, timestamp, index=i)]
        for i, timeframe in enumerate(CANONICAL_LADDER)
    }
    views = {
        key: build_set_views(histories, key, timestamps=[timestamp])
        for key in ("SET_1", "SET_2", "SET_3", "SET_4", "SET_5")
    }
    assert views["SET_1"][0].mtf is views["SET_2"][0].htf
    assert views["SET_1"][0].ltf is views["SET_2"][0].mtf is views["SET_3"][0].htf
    assert views["SET_2"][0].ltf is views["SET_3"][0].mtf is views["SET_4"][0].htf
    assert views["SET_3"][0].ltf is views["SET_4"][0].mtf is views["SET_5"][0].htf
    assert views["SET_4"][0].ltf is views["SET_5"][0].mtf
    graph = FractalStateGraph(histories)
    refs = graph.references_by_set({key: value for key, value in views.items()})
    assert sum(len(sets) > 1 for sets in refs.values()) == 5


def test_movement_ledger_keeps_one_id_for_same_directional_leg() -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    states = [
        make_state("3M", start + timedelta(minutes=3 * i), index=i, trend=trend)
        for i, trend in enumerate((1, 1, -1, -1))
    ]
    ledger = CausalMovementLedger(states)
    assert ledger.for_state(states[0]) == ledger.for_state(states[1])
    assert ledger.for_state(states[2]) != ledger.for_state(states[1])
    assert len(ledger.episodes) == 2


def test_hypothesis_uses_only_prior_observations_and_never_executes() -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    views = []
    from forex_platform.fractal_engine.state_graph import SetStateView
    for i in range(5):
        timestamp = start + timedelta(hours=i)
        htf = make_state("4H", timestamp, index=i)
        mtf = make_state("1H", timestamp, index=i, phase=MarketPhase.PULLBACK)
        ltf = make_state("15M", timestamp, index=i, location="DISCOUNT")
        views.append(SetStateView("SET_4", timestamp, htf, mtf, ltf))
    model = FractalHypothesisEngine(min_observations=2)
    model.fit_observations(views)
    result = model.infer(views[-1])
    assert result.status == "CONDITIONAL_HYPOTHESIS"
    assert result.historical_sample_count == 4
    assert result.expected_path is not None
    assert result.execution_eligibility == "OBSERVATION_ONLY"


def test_fdr_and_null_test_report_finite_resolution() -> None:
    assert benjamini_hochberg([0.01, 0.04, None, 0.02]) == [0.03, 0.04, None, 0.03]
    from forex_platform.fractal_engine.research import _circular_shift_test
    p_value, count, _mi = _circular_shift_test(["a", "b"] * 8, ["a", "b"] * 8)
    assert count > 0
    assert p_value is not None and p_value >= 1 / (count + 1)


def test_liquidity_pool_detector_returns_equal_high_cluster() -> None:
    from forex_platform.market_model.contracts import SwingPoint, SwingScope, SwingType
    timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    swings = [
        SwingPoint(index=i, timestamp=timestamp + timedelta(hours=i),
                   price=Decimal(price), swing_type=SwingType.HIGH, scope=SwingScope.EXTERNAL)
        for i, price in enumerate(("1.1000", "1.1001"))
    ]
    pools = detect_liquidity_pools(swings, tolerance=Decimal("0.0002"))
    assert len(pools) == 1
    assert pools[0].kind == LiquidityKind.EQH
    assert pools[0].price == Decimal("1.10005")


def test_campaign_fails_closed_for_synthetic_data_and_writes_smoke_report(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    bars = make_bars(80)
    HistoricalECNFetcher.cache_to_parquet(
        bars, "EURUSD", Timeframe.M15, cache_dir,
        provenance=DataProvenance.SYNTHETIC, source="test synthetic",
    )
    result = run_campaign(
        cache_dir=cache_dir,
        report_path=tmp_path / "report.md",
        results_path=tmp_path / "results.json",
    )
    assert result["overall_verdict"] == "INSUFFICIENT DATA"
    assert result["eligible_symbols"] == []
    assert result["smoke_test"]["provenance"] == "SYNTHETIC"
    assert "software smoke test only" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert (tmp_path / "results.json").exists()
