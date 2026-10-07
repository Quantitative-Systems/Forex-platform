"""Automated, provenance-gated fractal research campaign.

Synthetic history is a software smoke test only. Statistical results are
qualified only from quality-passed, labelled real history.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import statistics
from typing import Any, Optional, Sequence

import numpy as np
import polars as pl

from forex_platform.fractal_engine.hypothesis_engine import FractalHypothesisEngine
from forex_platform.fractal_engine.state_engine import CanonicalTimeframeState, UniversalTimeframeStateEngine
from forex_platform.fractal_engine.state_graph import CausalMovementLedger, FractalStateGraph, SetStateView, build_set_views
from forex_platform.fractal_engine.timeframes import (
    ADJACENT_PAIRS, CANONICAL_LADDER, NON_ADJACENT_PAIRS, TIMEFRAME_SETS, CanonicalTimeframe,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_data.provenance import DataProvenance, fingerprint_dataframe, is_qualifying_real_data
from forex_platform.market_data.quality import DataQualityAuditor
from forex_platform.market_model.contracts import MarketPhase

REQUIRED_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD")
MINIMUM_YEARS = 2.0
BOOTSTRAP_REPLICATES = 500
PERMUTATION_LIMIT = 999
LOG = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _wilson(k: int, n: int) -> Optional[list[float]]:
    if not n:
        return None
    z = 1.959963984540054
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - m), min(1.0, c + m)]


def _block_bootstrap(values: Sequence[int], seed: int) -> Optional[list[float]]:
    if not values:
        return None
    if len(values) == 1:
        return [float(values[0]), float(values[0])]
    sample = np.asarray(values, dtype=float)
    n, block = len(sample), max(1, round(math.sqrt(len(sample))))
    rng = np.random.default_rng(seed)
    means = np.empty(BOOTSTRAP_REPLICATES)
    for r in range(BOOTSTRAP_REPLICATES):
        draw: list[float] = []
        while len(draw) < n:
            start = int(rng.integers(0, n))
            draw.extend(sample[(start + j) % n] for j in range(block))
        means[r] = np.mean(draw[:n])
    return [float(np.quantile(means, .025)), float(np.quantile(means, .975))]


def _mi(x: Sequence[str], y: Sequence[str]) -> float:
    if not x or len(x) != len(y):
        return 0.0
    n, cx, cy, cxy = len(x), Counter(x), Counter(y), Counter(zip(x, y))
    return sum((count / n) * math.log2((count * n) / (cx[a] * cy[b]))
               for (a, b), count in cxy.items())


def _circular_shift_test(x: Sequence[str], y: Sequence[str]) -> tuple[Optional[float], int, float]:
    n, observed = len(x), _mi(x, y)
    if n < 8:
        return None, 0, observed
    offsets = list(range(1, n))
    if len(offsets) > PERMUTATION_LIMIT:
        offsets = [offsets[i] for i in np.linspace(0, len(offsets) - 1, PERMUTATION_LIMIT, dtype=int)]
    exceed = sum(_mi([x[(i + offset) % n] for i in range(n)], y) >= observed - 1e-15 for offset in offsets)
    return (1 + exceed) / (1 + len(offsets)), len(offsets), observed


def benjamini_hochberg(p_values: Sequence[Optional[float]]) -> list[Optional[float]]:
    """Benjamini-Hochberg q-values, retaining original order."""
    valid = sorted(((i, p) for i, p in enumerate(p_values) if p is not None), key=lambda item: item[1])
    result: list[Optional[float]] = [None] * len(p_values)
    running, total = 1.0, len(valid)
    for rank in range(total, 0, -1):
        index, value = valid[rank - 1]
        running = min(running, value * total / rank)
        result[index] = min(1.0, running)
    return result


def _condition(state: CanonicalTimeframeState) -> str:
    return "|".join((state.trend_label, state.phase.value.upper(), state.location, state.swing_state))


def _next_destination(state: CanonicalTimeframeState) -> Optional[float]:
    direction = state.structural_trend
    if direction not in (-1, 1):
        return None
    wanted = "high" if direction > 0 else "low"
    levels = [v for v in state.key_levels if v.swing_type == wanted and v.weak
              and (v.price > state.current_price if direction > 0 else v.price < state.current_price)]
    if levels:
        selected = min(levels, key=lambda v: v.price) if direction > 0 else max(levels, key=lambda v: v.price)
        return float(selected.price)
    if state.structural_range:
        price = state.structural_range.high if direction > 0 else state.structural_range.low
        if (direction > 0 and price > state.current_price) or (direction < 0 and price < state.current_price):
            return float(price)
    return None


def _invalidation_price(state: CanonicalTimeframeState) -> Optional[float]:
    direction = state.structural_trend
    if direction not in (-1, 1):
        return None
    wanted = "low" if direction > 0 else "high"
    levels = [v for v in state.key_levels if v.swing_type == wanted and v.protected]
    if levels:
        return float(levels[-1].price)
    if state.structural_range:
        return float(state.structural_range.low if direction > 0 else state.structural_range.high)
    return None


def _binary_metric(values: Sequence[Optional[int]], seed: int) -> dict[str, Any]:
    sample = [int(v) for v in values if v is not None]
    return {
        "n": len(sample),
        "count": sum(sample),
        "probability": sum(sample) / len(sample) if sample else None,
        "wilson_95_ci": _wilson(sum(sample), len(sample)),
        "moving_block_bootstrap_95_ci": _block_bootstrap(sample, seed),
        "bootstrap_replicates": BOOTSTRAP_REPLICATES if sample else 0,
        "bootstrap_block_length": max(1, round(math.sqrt(len(sample)))) if sample else None,
    }


def analyze_scale_relationship(
    symbol: str,
    source_tf: CanonicalTimeframe,
    child_tf: CanonicalTimeframe,
    source_states: Sequence[CanonicalTimeframeState],
    child_states: Sequence[CanonicalTimeframeState],
    seed: int = 17,
) -> dict[str, Any]:
    """Relate each distinct source signature to the first later child close."""
    source = sorted(source_states, key=lambda s: s.timestamp)
    child = sorted(child_states, key=lambda s: s.timestamp)
    child_times = [s.timestamp for s in child]
    observations: list[dict[str, Any]] = []
    prior_source_signature: Optional[str] = None
    for source_index, parent in enumerate(source):
        if parent.state_signature == prior_source_signature:
            continue
        prior_source_signature = parent.state_signature
        next_i = bisect_right(child_times, parent.timestamp)
        if next_i >= len(child):
            continue
        prior_i = next_i - 1
        outcome = child[next_i]
        prior_child = child[prior_i] if prior_i >= 0 else None
        direction = parent.structural_trend or 0
        direction_class = (
            "REVERSAL" if direction and outcome.structural_trend == -direction
            else "ALIGNED" if direction and outcome.structural_trend == direction
            else "NEUTRAL_OR_UNRESOLVED"
        )
        horizon = source[source_index + 1].timestamp if source_index + 1 < len(source) else child[-1].timestamp
        future = [state for state in child[next_i:] if state.timestamp <= horizon]
        destination, invalidation = _next_destination(parent), _invalidation_price(parent)
        reached = None if destination is None else int(any(
            float(state.current_price) >= destination if direction > 0 else float(state.current_price) <= destination
            for state in future
        ))
        invalidated = None if invalidation is None else int(any(
            float(state.current_price) < invalidation if direction > 0 else float(state.current_price) > invalidation
            for state in future
        ))
        observations.append({
            "timestamp": parent.timestamp,
            "source": _condition(parent),
            "outcome": _condition(outcome),
            "phase": outcome.phase.value.upper(),
            "direction": direction_class,
            "persistence": int("PERSISTENCE" in outcome.transition),
            "structural_break": int("STRUCTURAL_TRANSITION" in outcome.transition),
            "phase_transition": int("PHASE_TRANSITION" in outcome.transition),
            "zone_migration": int("ZONE_MIGRATION" in outcome.transition),
            "premium_to_discount": int(bool(prior_child and prior_child.location == "PREMIUM" and outcome.location == "DISCOUNT")),
            "discount_to_premium": int(bool(prior_child and prior_child.location == "DISCOUNT" and outcome.location == "PREMIUM")),
            "destination_reached": reached,
            "invalidated": invalidated,
            "child_duration": outcome.duration_bars,
        })
    x, y = [v["source"] for v in observations], [v["outcome"] for v in observations]
    p_value, null_n, mi_bits = _circular_shift_test(x, y)
    metrics = {
        name: _binary_metric([row[field] for row in observations], seed + number)
        for number, (name, field) in enumerate((
            ("state_persistence", "persistence"), ("structural_break", "structural_break"),
            ("phase_transition", "phase_transition"), ("zone_migration", "zone_migration"),
            ("premium_to_discount", "premium_to_discount"), ("discount_to_premium", "discount_to_premium"),
            ("destination_reach", "destination_reached"), ("invalidation", "invalidated"),
        ))
    }
    for row in observations:
        row["pullback"] = int(row["phase"] == "PULLBACK")
        row["continuation"] = int(row["phase"] == "CONTINUATION")
        row["reversal"] = int(row["direction"] == "REVERSAL")
    for name, field in (("pullback", "pullback"), ("continuation", "continuation"), ("reversal", "reversal")):
        metrics[name] = _binary_metric([row[field] for row in observations], seed + len(metrics))
    n = len(observations)
    cut1, cut2 = int(n * .6), int(n * .8)
    splits = {}
    for label, subset in (("DISCOVERY_60", observations[:cut1]),
                          ("VALIDATION_20", observations[cut1:cut2]),
                          ("CONFIRMATION_OOS_20", observations[cut2:])):
        splits[label] = {
            "n": len(subset),
            "pullback_probability": sum(r["phase"] == "PULLBACK" for r in subset) / len(subset) if subset else None,
            "continuation_probability": sum(r["phase"] == "CONTINUATION" for r in subset) / len(subset) if subset else None,
            "reversal_probability": sum(r["direction"] == "REVERSAL" for r in subset) / len(subset) if subset else None,
        }
    transitions = Counter(y)
    durations = [r["child_duration"] for r in observations]
    return {
        "symbol": symbol, "source_timeframe": source_tf.value, "child_timeframe": child_tf.value,
        "n": n, "conditional_outcomes": metrics,
        "most_observed_child_states": [
            {"outcome": label, "count": count, "probability": count / n}
            for label, count in transitions.most_common(10)
        ],
        "information": {
            "mutual_information_bits": mi_bits, "conditional_entropy_reduction_bits": mi_bits,
            "circular_shift_null_p": p_value, "null_replicates": null_n,
            "null_resolution": 1 / (null_n + 1) if null_n else None,
            "interpretation": "descriptive; serial dependence and sparse state combinations limit inference",
        },
        "transition_duration_child_bars": {
            "n": len(durations), "median": statistics.median(durations) if durations else None,
            "mean": statistics.mean(durations) if durations else None,
        },
        "chronological_splits": splits,
    }


def _inspect_dataset(symbol: str, cache_dir: Path, min_years: float):
    candidates, audit_rows = [], []
    for tf in (Timeframe.M1, Timeframe.M15):
        path = HistoricalECNFetcher.get_cache_path(symbol, tf, cache_dir)
        if not path.exists():
            audit_rows.append({"timeframe": tf.value, "status": "MISSING", "path": str(path)})
            continue
        provenance, source = HistoricalECNFetcher.load_provenance(symbol, tf, cache_dir)
        try:
            bars = MarketDataLoader.normalize_and_validate(
                pl.read_parquet(path), expected_interval=tf.duration, enforce_monotonicity=True
            )
            start, end = _utc(bars["timestamp"][0]), _utc(bars["timestamp"][-1])
            years = (end - start).total_seconds() / (365.25 * 86400)
            quality = DataQualityAuditor.audit(bars, symbol=symbol, expected_interval=tf.duration)
            good = is_qualifying_real_data(provenance) and years >= min_years and quality.is_valid
            reasons = []
            if not is_qualifying_real_data(provenance):
                reasons.append(f"provenance is {provenance.value}")
            if years < min_years:
                reasons.append(f"coverage {years:.2f} years is below {min_years:.2f}")
            if not quality.is_valid:
                reasons.append(f"quality flags: {quality.midweek_drops_count} midweek gaps, {quality.anomalies_count} anomalies")
            item = {
                "timeframe": tf.value, "status": "ELIGIBLE" if good else "NOT_ELIGIBLE",
                "path": str(path), "provenance": provenance.value, "source": source,
                "rows": bars.height, "start_utc": start.isoformat(), "end_utc": end.isoformat(),
                "coverage_years": years, "quality_passed": quality.is_valid,
                "midweek_gaps": quality.midweek_drops_count, "weekend_gaps": quality.weekend_gaps_count,
                "anomalies": quality.anomalies_count, "fingerprint": fingerprint_dataframe(bars).as_dict(),
                "reasons": reasons,
            }
            audit_rows.append(item)
            if good:
                candidates.append((tf, bars, provenance, source, years))
        except Exception as exc:
            audit_rows.append({"timeframe": tf.value, "status": "INVALID", "path": str(path), "reason": str(exc)})
    if candidates:
        selected = next((v for v in candidates if v[0] == Timeframe.M1), candidates[0])
        return {
            "symbol": symbol, "status": "ELIGIBLE", "selected_timeframe": selected[0].value,
            "datasets": audit_rows, "reason": "real provenance, coverage and quality gates passed",
        }, {"timeframe": selected[0], "bars": selected[1], "provenance": selected[2], "source": selected[3]}
    return {
        "symbol": symbol, "status": "INSUFFICIENT_DATA", "selected_timeframe": None,
        "datasets": audit_rows, "reason": "no real, quality-passed source with required continuous coverage",
    }, None


def run_set_trade_matrix(
    symbols: Sequence[str],
    selected_data: dict[str, dict[str, Any]],
    audits: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate one isolated research candidate per asset/set cell.

    Each available real M1 dataset is evaluated with the platform's G1-G8,
    chronological walk-forward, rolling-window, and doubled-cost machinery.
    Passing cells remain blocked from promotion until they clear family-level
    multiplicity adjustment and broader cross-asset governance review.
    """
    from forex_platform.discovery.discovery_loop import (
        CandidateHypothesis,
        ContinuousDiscoveryLoop,
        PromotionStatus,
    )
    from forex_platform.research_engine.evaluate import StrategyEvaluator

    cells: list[dict[str, Any]] = []
    for symbol in symbols:
        selected = selected_data.get(symbol)
        audit = audits[symbol]
        for set_key in TIMEFRAME_SETS:
            base = {
                "symbol": symbol,
                "set": set_key,
                "required_minimum_trades": 100,
                "trade_counts": None,
                "performance_metrics": None,
                "gate_results": None,
                "multiplicity_adjustment": "PENDING_ACROSS_20_CELLS",
            }
            if selected is None:
                cells.append({
                    **base,
                    "status": "NOT_RUN_INSUFFICIENT_REAL_DATA",
                    "reason": audit.get("reason", "no eligible real market history"),
                })
                continue
            if selected["timeframe"] != Timeframe.M1:
                cells.append({
                    **base,
                    "status": "NOT_RUN_M1_REQUIRED",
                    "reason": "The selected dataset cannot construct the full ladder through 3M; M1 source bars are required.",
                    "source_timeframe": selected["timeframe"].value,
                })
                continue

            candidate = CandidateHypothesis(
                candidate_id=f"fractal_{symbol.lower()}_{set_key.lower()}",
                strategy_name="InstitutionalFractalStrategy",
                symbol=symbol,
                timeframe=Timeframe.M1,
                parameters={"selected_set": set_key},
                data_provenance=selected["provenance"],
            )
            loop = ContinuousDiscoveryLoop(
                evaluator=StrategyEvaluator(
                    min_dev_trades=100,
                    min_val_trades=30,
                    min_oos_trades=30,
                    persist_artifacts=False,
                ),
                require_real_data=True,
            )
            result = loop.evaluate_candidate(
                candidate,
                selected["bars"],
                allow_promotion=False,
            )
            total_trades = result.trade_counts.get("total", 0)
            if result.status == PromotionStatus.EXECUTION_FAILED:
                cell_status = "EXECUTION_FAILED"
            elif total_trades < 100:
                cell_status = "INSUFFICIENT_TRADES"
            elif result.status == PromotionStatus.VALIDATION_PENDING:
                cell_status = "GATES_PASSED_PENDING_MULTIPLICITY_REVIEW"
            else:
                cell_status = "GATES_FAILED"
            cells.append({
                **base,
                "status": cell_status,
                "reason": result.error_message,
                "trade_counts": result.trade_counts or None,
                "performance_metrics": result.performance_metrics or None,
                "gate_results": (
                    {key: value.model_dump() for key, value in result.gate_report.gate_results.items()}
                    if result.gate_report else None
                ),
                "failed_gate": result.gate_report.failed_gate if result.gate_report else None,
                "family_corrected": False,
            })
    p_values = [
        (cell.get("performance_metrics") or {}).get("oos_block_bootstrap_positive_mean_p_value")
        for cell in cells
    ]
    q_values = benjamini_hochberg(p_values)
    for cell, q_value in zip(cells, q_values):
        cell["family_adjusted_q_value"] = q_value
        cell["family_corrected"] = q_value is not None and q_value <= 0.05
        cell["multiplicity_adjustment"] = (
            "BENJAMINI_HOCHBERG_FDR" if q_value is not None else "NO_VALID_OOS_TEST"
        )
        if cell["status"] == "GATES_PASSED_PENDING_MULTIPLICITY_REVIEW":
            if q_value is None or q_value > 0.05:
                cell["status"] = "REJECTED_MULTIPLE_TESTING"
            elif (cell.get("performance_metrics") or {}).get(
                "oos_expectancy_after_top_winner_removal", 0
            ) <= 0:
                cell["status"] = "REJECTED_TOP_WINNER_CONCENTRATION"
            else:
                cell["status"] = "GATES_AND_FDR_PASSED_PENDING_CROSS_ASSET_REVIEW"
    return {
        "minimum_trades_per_asset_set_cell": 100,
        "cells_tested": sum(cell["status"] not in {
            "NOT_RUN_INSUFFICIENT_REAL_DATA", "NOT_RUN_M1_REQUIRED"
        } for cell in cells),
        "family_size": len(cells),
        "valid_oos_hypothesis_tests": sum(value is not None for value in p_values),
        "multiplicity_adjustment": "Benjamini-Hochberg across OOS centered moving-block-bootstrap tests for the asset/set family; q<=0.05 required.",
        "cells": cells,
    }


def _smoke_dataset(cache_dir: Path):
    for symbol in REQUIRED_SYMBOLS:
        for tf in (Timeframe.M15, Timeframe.M1):
            path = HistoricalECNFetcher.get_cache_path(symbol, tf, cache_dir)
            if not path.exists():
                continue
            try:
                bars = MarketDataLoader.normalize_and_validate(
                    pl.read_parquet(path), expected_interval=tf.duration, enforce_monotonicity=True
                )
                provenance, source = HistoricalECNFetcher.load_provenance(symbol, tf, cache_dir)
                return symbol, tf, bars, provenance, source
            except Exception:
                continue
    return None


def _identity_audit(set_views: dict[str, Sequence[SetStateView]]) -> dict[str, Any]:
    seen: dict[str, CanonicalTimeframeState] = {}
    references: dict[str, set[str]] = {}
    identity_ok = True
    for set_key, views in set_views.items():
        for view in views:
            for state in view.states:
                if state is None:
                    continue
                prior = seen.get(state.state_id)
                if prior is not None and prior is not state:
                    identity_ok = False
                seen[state.state_id] = state
                references.setdefault(state.state_id, set()).add(set_key)
    return {
        "one_object_per_state_id": identity_ok,
        "unique_state_objects_referenced": len(seen),
        "shared_state_ids_referenced_by_multiple_sets": sum(len(v) > 1 for v in references.values()),
    }


def _set_coverage(set_views: dict[str, Sequence[SetStateView]]) -> dict[str, Any]:
    output = {}
    for key, views in set_views.items():
        full = sum(all(state is not None for state in view.states) for view in views)
        output[key] = {
            "views": len(views), "full_role_views": full,
            "coverage_rate": full / len(views) if views else 0.0,
            "status": "FULL_COVERAGE" if views and full == len(views) else "PARTIAL_COVERAGE" if full else "INSUFFICIENT_SCALE_DATA",
        }
    return output


def _nested_counts(set_views: dict[str, Sequence[SetStateView]]) -> dict[str, Any]:
    result = {}
    for key, views in set_views.items():
        complete = [v for v in views if all(s is not None for s in v.states)]
        stages, count, prior_move = 0, 0, None
        for view in complete:
            htf, mtf, ltf = view.states
            assert htf is not None and mtf is not None and ltf is not None
            if view.movement_id != prior_move:
                stages, prior_move = 0, view.movement_id
            if stages == 0 and htf.phase == MarketPhase.CONTINUATION and htf.structural_trend in (-1, 1):
                stages = 1
            if stages == 1 and mtf.phase == MarketPhase.PULLBACK:
                stages = 2
            if stages == 2 and htf.structural_trend and ltf.structural_trend == -htf.structural_trend:
                stages = 3
            if stages == 3 and htf.structural_trend and ltf.structural_trend == htf.structural_trend and mtf.phase == MarketPhase.CONTINUATION:
                count += 1
                stages = 0
        result[key] = {
            "complete_views": len(complete),
            "nested_continuation_pullback_reversal_resumption_sequences": count,
            "distinct_movement_count": len({v.movement_id for v in complete if v.movement_id}),
            "interpretation": "descriptive sequence counter, not a trade or independent-sample count",
        }
    return result


def _baseline_artifact(root: Path) -> dict[str, Any]:
    path = root / "research" / "results" / "experiments.json"
    if not path.exists():
        return {"artifact": str(path), "status": "MISSING"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        experiments = list((payload.get("experiments") or {}).values())
        return {
            "artifact": str(path), "status": "EXISTING_ARTIFACT",
            "experiments": len(experiments),
            "provenance_counts": dict(Counter(v.get("data_provenance", "UNKNOWN") for v in experiments)),
            "verdict_counts": dict(Counter(v.get("verdict", "UNKNOWN") for v in experiments)),
            "survivors": sum(v.get("verdict") == "SURVIVED" for v in experiments),
            "warning": "prior short/synthetic sweep is not a valid real-data baseline comparison",
        }
    except Exception as exc:
        return {"artifact": str(path), "status": "INVALID", "reason": str(exc)}


def run_campaign(
    *,
    cache_dir: Path | str = Path("data/cache"),
    report_path: Path | str = Path("research/FRACTAL_RESEARCH_REPORT.md"),
    results_path: Path | str = Path("research/results/fractal_research.json"),
    symbols: Sequence[str] = REQUIRED_SYMBOLS,
    min_years: float = MINIMUM_YEARS,
    run_smoke: bool = True,
) -> dict[str, Any]:
    """Run data qualification, canonical state research, and final reporting."""
    cache_dir, report_path, results_path = Path(cache_dir), Path(report_path), Path(results_path)
    symbols = tuple(dict.fromkeys(s.upper().replace("/", "").replace("_", "").replace("-", "") for s in symbols))
    audits, selected_data, relationships, symbol_results = {}, {}, [], {}
    for symbol in symbols:
        audit, selected = _inspect_dataset(symbol, cache_dir, min_years)
        audits[symbol] = audit
        if selected is not None:
            selected_data[symbol] = selected

    trade_matrix = run_set_trade_matrix(symbols, selected_data, audits)

    for symbol, selected in selected_data.items():
        engine = UniversalTimeframeStateEngine(symbol, selected["timeframe"], swing_lookback=5)
        histories = engine.build(selected["bars"])
        finest = next((tf.value for tf in reversed(CANONICAL_LADDER) if tf.value in histories), None)
        ledger = CausalMovementLedger(histories[finest]) if finest else None
        views = {}
        for set_key, ladder in TIMEFRAME_SETS.items():
            times = [s.timestamp for s in histories.get(ladder[2].value, ())]
            views[set_key] = build_set_views(histories, set_key, timestamps=times, movement_ledger=ledger)
        graph = FractalStateGraph(histories, ledger)
        hypotheses = {}
        for set_key, set_views in views.items():
            model = FractalHypothesisEngine(min_observations=20)
            observations = model.fit_observations(set_views)
            hypotheses[set_key] = {
                "transition_observations": len(observations),
                "latest_status": model.infer(set_views[-1]).status if set_views else "NO_VIEW",
                "execution_eligibility": "OBSERVATION_ONLY",
            }
        symbol_results[symbol] = {
            "source_timeframe": selected["timeframe"].value,
            "source_rows": selected["bars"].height,
            "source_provenance": selected["provenance"].value,
            "source": selected["source"],
            "canonical_state_counts": {key: len(value) for key, value in histories.items()},
            "available_timeframes": list(histories),
            "graph": {
                "unique_state_nodes": len(graph.nodes),
                "temporal_transition_edges": graph.temporal_edge_count,
                "scale_observation_edges": graph.scale_edge_count,
                "movement_episodes": len(ledger.episodes) if ledger else 0,
                "movement_identity_timeframe": finest,
            },
            "identity_audit": _identity_audit(views),
            "set_coverage": _set_coverage(views),
            "nested_pullback_sequences": _nested_counts(views),
            "conditional_hypotheses": hypotheses,
        }
        available = {CanonicalTimeframe(name) for name in histories}
        pairs = list(ADJACENT_PAIRS) + [p for p in NON_ADJACENT_PAIRS if p not in ADJACENT_PAIRS]
        for source_tf, child_tf in pairs:
            if source_tf in available and child_tf in available:
                relationships.append(analyze_scale_relationship(
                    symbol, source_tf, child_tf, histories[source_tf.value], histories[child_tf.value],
                    seed=sum(map(ord, symbol + source_tf.value + child_tf.value)),
                ))
    q_values = benjamini_hochberg([v["information"]["circular_shift_null_p"] for v in relationships])
    for result, q in zip(relationships, q_values):
        result["information"]["fdr_q_value_across_all_asset_scale_tests"] = q

    smoke_summary = None
    if run_smoke:
        smoke = _smoke_dataset(cache_dir)
        if smoke and smoke[0] not in selected_data:
            symbol, tf, bars, provenance, source = smoke
            engine = UniversalTimeframeStateEngine(symbol, tf, swing_lookback=5)
            histories = engine.build(bars)
            finest = next((v.value for v in reversed(CANONICAL_LADDER) if v.value in histories), None)
            ledger = CausalMovementLedger(histories[finest]) if finest else None
            views = {
                key: build_set_views(
                    histories, key,
                    timestamps=[s.timestamp for s in histories.get(ladder[2].value, ())],
                    movement_ledger=ledger,
                )
                for key, ladder in TIMEFRAME_SETS.items()
            }
            graph = FractalStateGraph(histories, ledger)
            smoke_summary = {
                "purpose": "software smoke test only; excluded from scientific conclusions",
                "symbol": symbol, "source_timeframe": tf.value, "provenance": provenance.value,
                "source": source, "source_rows": bars.height,
                "start_utc": _utc(bars["timestamp"][0]).isoformat(),
                "end_utc": _utc(bars["timestamp"][-1]).isoformat(),
                "canonical_state_counts": {k: len(v) for k, v in histories.items()},
                "graph_nodes": len(graph.nodes),
                "graph_temporal_edges": graph.temporal_edge_count,
                "graph_scale_edges": graph.scale_edge_count,
                "set_coverage": _set_coverage(views),
                "identity_audit": _identity_audit(views),
            }

    complete = len(selected_data) == len(symbols) and set(symbols) == set(REQUIRED_SYMBOLS)
    verdict = "INSUFFICIENT DATA" if not selected_data else (
        "CONDITIONAL / INSUFFICIENT TRADING EVIDENCE" if complete else "INSUFFICIENT DATA FOR CROSS-ASSET CONCLUSION"
    )
    root = Path.cwd()
    results = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_verdict": verdict,
        "scientific_conclusion": (
            "No real-market fractal edge can be established from the available data. "
            "Shared architecture identity is testable; predictability and tradability are not established."
        ),
        "configuration": {
            "symbols": list(symbols), "minimum_real_data_years": min_years,
            "required_provenance": ["REAL_VENDOR", "BROKER_EXPORT"], "swing_lookback_bars": 5,
            "source_observations": "distinct source signatures; next strictly later child close",
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "null_test": "circular shift with plus-one finite-sample correction",
            "multiple_testing": "Benjamini-Hochberg over tested asset-scale relationships",
            "live_capital_usd": 0.0,
        },
        "dataset_audit": audits, "eligible_symbols": list(selected_data),
        "set_trade_matrix": trade_matrix,
        "cross_asset_coverage_complete": complete,
        "symbol_results": symbol_results, "scale_relationships": relationships,
        "existing_baseline_artifact": _baseline_artifact(root),
        "smoke_test": smoke_summary,
        "capital_eligibility": {key: "OBSERVATION ONLY" for key in TIMEFRAME_SETS},
        "edge_registry": [],
        "known_limits": [
            "Transition frequencies are not trade expectancy.",
            "Synthetic or unknown provenance never qualifies for scientific statistics.",
            "Set 5 requires real M1 input; M15 is not upsampled.",
            "No candidate trades means no PnL, costs, drawdown or G1-G8 decision.",
            "The institutional fractal strategy is registered as a real-M1-only research candidate; it has not been empirically backtested or promoted.",
            "Candidate profitability, promotion gates and full execution friction remain untested because no eligible real M1 dataset is available.",
            "The candidate is not connected to paper or broker execution; calendar-news, volatility-based market impact, structural trailing, full bid/ask intrabar fills, portfolio aggregation and reconciliation are not validated in its backtest path.",
            "State updates use completed timeframe candles, not unfinished candles.",
            "No paper/live integration without independent validation; live capital remains $0.00.",
        ],
    }
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(results), encoding="utf-8")
    results["results_path"], results["report_path"] = str(results_path), str(report_path)
    return results


def _prob(metric: dict[str, Any]) -> str:
    value = metric.get("probability")
    return "n/a" if value is None else f"{value:.1%} (n={metric.get('n', 0)})"


def render_report(results: dict[str, Any]) -> str:
    """Render the comprehensive A-T research report."""
    lines = [
        "# Fractal Cross-Timeframe Research Report", "",
        f"- Generated: {results.get('generated_at_utc')}",
        f"- Verdict: **{results.get('overall_verdict')}**",
        "- Live capital: **$0.00**", "",
        "## A. Executive verdict", "",
        results.get("scientific_conclusion", ""),
        "The architecture identity result does not establish market predictability or economic edge.", "",
        "## B. Architecture audit", "",
        "| Existing view | Fractal research view |", "|---|---|",
        "| Timeframe sets are run through independent strategy paths. | One canonical state history is reused by role views over the same ladder. |",
        "| HTF context flows into MTF/LTF strategy logic. | Observed state, conditional path, execution eligibility and capital allocation are kept separate. |",
        "",
        "Canonical ladder: **1M -> 1W -> 1D -> 4H -> 1H -> 15M -> 3M**.", "",
        "## C. Existing build validation", "",
        "- Structure uses strict symmetric pivots, body-close BOS/CHOCH and protected/weak swing helpers. The new engine publishes pivots only after right-side confirmation.",
        "- Existing zone modules cover session levels, FVGs, order blocks, liquidity pools and sweeps. The new snapshots retain structural levels and causal live FVG/OB/liquidity zones.",
        "- The frozen phase vocabulary remains PULLBACK and CONTINUATION; neutral structure conservatively defaults to PULLBACK in the existing classifier.",
        "- The pre-existing institutional_phase_rider.py is not a runnable baseline: malformed indentation prevents import; its legacy timeframe enum also lacks the referenced monthly/weekly values, and the class calls an absent _get_history_df helper. It is excluded from this research. A separate institutional fractal candidate is registered in discovery, requires real M1 data, and remains research/paper-only pending full qualification.",
        "- The research candidate combines HTF continuation and range location, MTF pullback/location with directional FVG or order-block context, and a confirmed LTF break followed by a later zone retest. It deduplicates entries by shared 3M movement identity and applies risk, spread, a cost-adjusted minimum 4R filter, session/rollover and daily realized loss controls.",
        "- Existing contracts carry per-timeframe structure, swings, breaks, zones, phase and trend, but not canonical cross-set identity or independent range location.",
        "- Backtest, qualification, walk-forward, risk, paper, instrument, session, provenance and quality modules remain the platform's canonical implementations.",
        "- Execution accounting fix: bid-quoted bars now fill buys at ask and sells at bid, evaluate short exits on ask OHLC, model gap-through stop slippage, and liquidate open positions at test-window boundaries; regression coverage was added.",
        "- Optional HistData acquisition: the research command can download raw bid/ask tick archives, convert fixed-EST timestamps to UTC, aggregate separate bid/ask M1 OHLC, and preserve vendor provenance. Only very short no-quote gaps and the bounded Sunday reopen gap are carried forward; longer feed gaps stay visible to quality gates.",
        "- Defect fixed: liquidity-pool extraction did not return its populated list; a regression test now covers it.",
        "", "## D. Fractal state graph and set views", "",
        "| Set | HTF | MTF | LTF |", "|---|---|---|---|",
    ]
    for key, ladder in TIMEFRAME_SETS.items():
        lines.append(f"| {key.replace('_', ' ')} | {ladder[0].value} | {ladder[1].value} | {ladder[2].value} |")
    lines += ["", "Each closed candle state is immutable and reused by reference across every set role. Higher states align by a backward close-time lookup.", "", "## E. Cross-timeframe transition results", ""]
    if results["scale_relationships"]:
        lines += ["| Asset | Scale | N | Pullback | Continuation | Reversal | MI bits | Null p | FDR q |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for item in results["scale_relationships"]:
            m, info = item["conditional_outcomes"], item["information"]
            lines.append(
                f"| {item['symbol']} | {item['source_timeframe']}→{item['child_timeframe']} | {item['n']} "
                f"| {_prob(m['pullback'])} | {_prob(m['continuation'])} | {_prob(m['reversal'])} "
                f"| {info['mutual_information_bits']:.4f} | {info['circular_shift_null_p'] if info['circular_shift_null_p'] is not None else 'n/a'} "
                f"| {info.get('fdr_q_value_across_all_asset_scale_tests') if info.get('fdr_q_value_across_all_asset_scale_tests') is not None else 'n/a'} |"
            )
    else:
        lines.append("Not estimable: no symbol passed provenance, quality and coverage gates.")
    lines += [
        "", "Probabilities describe state transitions, not returns. CIs use Wilson and circular moving-block bootstrap; null p-values have finite resolution and are FDR adjusted.",
        "", "## F. Nested pullback results", "",
    ]
    if results["symbol_results"]:
        for symbol, data in results["symbol_results"].items():
            for key, stat in data["nested_pullback_sequences"].items():
                lines.append(f"- {symbol} {key}: {stat['nested_continuation_pullback_reversal_resumption_sequences']} sequences; descriptive, not a trade or independent-sample count.")
    else:
        lines.append("Not measurable on eligible real data. The synthetic smoke fixture is excluded.")
    lines += [
        "", "## G. Premium/discount results", "",
        "Each timeframe uses its own confirmed structural range. The exact midpoint is EQUILIBRIUM; lower/higher positions are DISCOUNT/PREMIUM. Competing ranges containing price are marked AMBIGUOUS. Incremental information is not established.",
        "", "## H. Set correlation results", "",
        "Sets overlap by definition. Canonical object identity is audited in the JSON; this does not measure statistical return correlation.",
        "", "## I. One movement / multiple sets", "",
        "Movement IDs identify confirmed structural legs on the finest available input. Representations are views of that ID. Trade opportunity and duplicated signal counts remain unmeasured because there is no eligible candidate signal stream.",
        "", "## J. Baseline versus fractal", "",
    ]
    baseline = results["existing_baseline_artifact"]
    lines.append(
        f"Existing sweep: {baseline.get('experiments', 0)} experiments, {baseline.get('survivors', 0)} survivors, "
        f"provenance {baseline.get('provenance_counts', {})}. This short/synthetic artifact is not a valid real-data comparison. "
        "No PnL expectancy, PF, win rate, drawdown, OOS or cost-adjusted baseline/fractal comparison is available."
    )
    lines += ["", "## K. Asset results", ""]
    for symbol, audit in results["dataset_audit"].items():
        lines.append(f"- {symbol}: {audit['status']} — {audit['reason']}.")
    lines += ["", "## L. Scale results", ""]
    if results["symbol_results"]:
        for symbol, data in results["symbol_results"].items():
            for key, stat in data["set_coverage"].items():
                lines.append(f"- {symbol} {key}: {stat['status']} ({stat['full_role_views']}/{stat['views']} full views).")
    else:
        lines.append("All five sets are observation-only; no empirical scale has capital eligibility.")
    lines += [
        "", "## M. Set 5 forensic result", "",
        "Not determinable from the available cache. M15 cannot construct 3M and no eligible 2-year four-pair M1 history was found. Set 5 remains observation/confirmation only.",
        "", "## N. Adversarial validation", "",
        "- Lookahead: stream updates use completed candles; as-of joins use state timestamps.",
        "- Swing leakage: right-side confirmation precedes publication and structural breaks.",
        "- Range leakage: premium/discount uses only confirmed swings; ambiguous ranges suppress the label.",
        "- Cross-set duplication: state snapshots are shared objects; movement IDs deduplicate representations by base structural leg.",
        "- Gap, Sunday open, rollover, spread, commissions, slippage, swaps, news, intrabar collision, target geometry, drift, outliers and regime concentration: not re-simulated in this state-only report; existing execution and risk tests remain separate.",
        "- The candidate is registered for discovery/backtesting only. Its entry cost screen uses bar spread, commission and a fixed slippage reserve; it is not wired to paper/broker execution and does not itself simulate calendar-news blackouts, volatility-based market impact, +2R structural trailing, bid/ask intrabar exits, portfolio heat/exposure or reconciliation.",
        "- No candidate is promoted; selection and winner-concentration claims are therefore unavailable. The institutional fractal candidate is registered but has not been evaluated on eligible data.",
        "", "## O. Statistical validation", "",
        f"Eligible symbols: {', '.join(results['eligible_symbols']) or 'none'}. Distinct source-signature N, chronological 60/20/20 splits, bootstrap, circular-shift null and BH FDR are reported when estimable. Trade expectancy, G1-G8 gates, walk-forward returns, cost shock and top-winner removal require candidate trades and are unavailable.",
        "", "## P. New edge registry", "", "No new candidate passed promotion; registry is empty.",
        "", "## Q. Final capital eligibility", "",
    ]
    lines += [f"- {key.replace('_', ' ')}: **OBSERVATION ONLY**." for key in TIMEFRAME_SETS]
    lines += ["- Live capital: **$0.00**.", "", "## R. Test status", "",
              "Unit tests cover timeframe definitions, canonical state identity, causal updates, premium/discount separation, movement deduplication, graph transitions, null-test resolution, real-data candidate gates, M1-only setup, five-set discovery registration, risk sizing, the frozen 4R floor, account-currency conversion, bid/ask fills, window-end liquidation and the liquidity-pool return regression.", "",
              "## S. Files and modules", "",
              "- forex_platform/fractal_engine/timeframes.py — ladder, sets and temporal boundaries.",
              "- forex_platform/fractal_engine/state_engine.py — causal state, swings, structural ranges and zones.",
              "- forex_platform/fractal_engine/state_graph.py — shared role views, graph and movement ledger.",
              "- forex_platform/fractal_engine/hypothesis_engine.py — time-filtered conditional paths.",
              "- forex_platform/fractal_engine/research.py — automated data gate, statistics and report.",
              "- research/FRACTAL_RESEARCH_REPORT.md and research/results/fractal_research.json — generated outputs.",
              "", "## T. Final scientific conclusion", "",
              "1. Sets are overlapping views of the specified ladder: **yes by definition**.",
              "2. One canonical state is consistent across set roles: **yes by architecture and identity audit**.",
              "3. Higher timeframe state predicts lower transitions: **not established**.",
              "4. Timeframe-relative premium/discount adds information: **not established**.",
              "5. Nested pullbacks are useful/common in real FX: **not established**.",
              "6. One base structural leg can be represented by several set views: **yes by movement identity**; duplicated trade signals remain unmeasured.",
              "7. Fractal layer improves edge over baseline: **not established**.",
              "8. Opportunity improves without expectancy degradation: **not established**.",
              "9. Set 5 information versus execution economics: **not determinable**.",
              "10. Economically tradable scales: **none established**.",
              "11. Cross-asset generalization: **not established**.",
              "12. Overall status: **INSUFFICIENT DATA**.",
              "13. Freeze the ladder, three-domain Market Model, state identity and current risk/execution invariants; keep all sets observation-only until independent real-data evidence passes promotion gates."]
    smoke = results.get("smoke_test")
    if smoke:
        lines += ["", "### Excluded engineering smoke run", "",
                  f"{smoke['symbol']} {smoke['source_timeframe']} ({smoke['provenance']}, {smoke['source_rows']} bars) was a software smoke test only, exercising the code path. It is excluded from all scientific conclusions."]
    lines += ["", "## U. Per-asset / per-set trading qualification", "",
              f"Minimum requested sample: **{results['set_trade_matrix']['minimum_trades_per_asset_set_cell']} trades per asset/set cell**. Cell tests use isolated set selection and, where eligible real M1 history exists, the platform G1-G8, walk-forward, rolling-window and doubled-cost gates. OOS moving-block tests receive Benjamini-Hochberg FDR correction across the valid cells; passing results remain research-only pending cross-asset review.",
              "", "| Asset | Set | Trades Dev/Val/OOS/Total | OOS expectancy | 2x-cost expectancy | FDR q | Status |", "|---|---|---:|---:|---:|---:|---|"]
    for cell in results["set_trade_matrix"]["cells"]:
        counts = cell.get("trade_counts") or {}
        metrics = cell.get("performance_metrics") or {}
        count_text = (
            f"{counts.get('development', 0)}/{counts.get('validation', 0)}/"
            f"{counts.get('oos', 0)}/{counts.get('total', 0)}"
            if counts else "—"
        )
        oos_exp = metrics.get("oos_expectancy")
        shock_exp = metrics.get("cost_shock_oos_expectancy")
        q_value = cell.get("family_adjusted_q_value")
        lines.append(
            f"| {cell['symbol']} | {cell['set'].replace('_', ' ')} | {count_text} "
            f"| {oos_exp if oos_exp is not None else '—'} "
            f"| {shock_exp if shock_exp is not None else '—'} "
            f"| {q_value if q_value is not None else '—'} | {cell['status']} |"
        )
    lines += ["", "No synthetic run is counted toward these trade minimums. A cell below 100 trades is insufficient sample; positive backtest metrics alone do not establish a profitable edge."]
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the causal fractal research campaign.")
    parser.add_argument("--symbols", nargs="+", default=list(REQUIRED_SYMBOLS))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--report-path", type=Path, default=Path("research/FRACTAL_RESEARCH_REPORT.md"))
    parser.add_argument("--results-path", type=Path, default=Path("research/results/fractal_research.json"))
    parser.add_argument("--minimum-years", type=float, default=MINIMUM_YEARS)
    parser.add_argument("--no-smoke", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    if args.minimum_years <= 0:
        LOG.error("--minimum-years must be positive")
        return 2
    try:
        results = run_campaign(
            cache_dir=args.cache_dir, report_path=args.report_path,
            results_path=args.results_path, symbols=args.symbols,
            min_years=args.minimum_years, run_smoke=not args.no_smoke,
        )
    except Exception:
        LOG.exception("Fractal research campaign failed")
        return 1
    print(f"Verdict: {results['overall_verdict']}")
    print(f"Eligible symbols: {', '.join(results['eligible_symbols']) or 'none'}")
    print(f"Report: {results['report_path']}")
    print(f"Results: {results['results_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
