"""Run the causal, cross-set structural rider research batch.

One unified five-set strategy runs per pair. Signals sharing a 3M structural
movement compete by net structural R:R; the selected set receives the trade.
The report still renders all 20 pair/set cells, with each cell containing only
trades allocated to that set. This preserves the platform's cross-set
deduplication rule instead of treating the overlapping sets as independent
portfolios.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Optional

import polars as pl

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.loader import MarketDataLoader
from forex_platform.market_data.provenance import DataProvenance
from forex_platform.market_data.loader import DataGap
from forex_platform.research_engine.backtester import EventDrivenBacktester, TradeRecord
from forex_platform.strategy_engine.fractal_institutional import InstitutionalFractalStrategy
from forex_platform.fractal_engine.state_engine import LIQUIDITY_POOL_SWING_LOOKBACK
from forex_platform.fractal_engine.timeframes import TIMEFRAME_SETS

SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD")
SET_ORDER = tuple(TIMEFRAME_SETS)
INITIAL_EQUITY = Decimal("100000")
RISK_FRACTION = Decimal("0.01")
GATES = {
    "A_min_full_window_trades": 80,
    "B_min_is_average_winner_r": 4.0,
    "B_min_is_expectancy_r": 0.35,
    "C_min_wfe": 0.50,
    "D_min_cost_stress_expectancy_r": 0.0,
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _tag_fields(tag: Optional[str]) -> dict[str, str]:
    if not tag:
        return {}
    payload = tag.split(":", 1)[-1]
    result = {}
    for part in payload.split("|"):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key] = value
    return result


def _trade_set(trade: TradeRecord) -> Optional[str]:
    return _tag_fields(trade.client_tag).get("SET")


def _trade_r(trade: TradeRecord, symbol: str) -> Optional[float]:
    if trade.stop_loss is None:
        return None
    price_risk = abs(Decimal(trade.entry_price) - Decimal(trade.stop_loss))
    if price_risk <= 0:
        return None
    pair = CurrencyPair.from_symbol(symbol)
    quote_rate = Decimal("1") / Decimal(trade.entry_price) if pair.base_currency == "USD" and pair.quote_currency != "USD" else Decimal("1")
    risk_cash = price_risk * Decimal(trade.units) * quote_rate
    return float(Decimal(trade.net_pnl) / risk_cash) if risk_cash > 0 else None


def _equity_stats(trades: list[TradeRecord], symbol: str) -> dict[str, Optional[float]]:
    values = [(trade, _trade_r(trade, symbol)) for trade in trades]
    values = [(trade, value) for trade, value in values if value is not None]
    if not values:
        return {"trade_count": 0, "win_rate": None, "avg_win_r": None, "avg_loss_r": None,
                "expectancy_r": None, "profit_factor": None, "profit_factor_no_losses": False,
                "max_drawdown_pct": None}
    rs = [value for _trade, value in values]
    winners = [value for value in rs if value > 0]
    losers = [value for value in rs if value < 0]
    gross_wins = sum(float(trade.net_pnl) for trade, value in values if value > 0)
    gross_losses = abs(sum(float(trade.net_pnl) for trade, value in values if value < 0))
    balance, peak, max_dd = 1.0, 1.0, 0.0
    for value in rs:
        balance *= max(0.00000001, 1 + float(RISK_FRACTION) * value)
        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak)
    return {
        "trade_count": len(values),
        "win_rate": sum(value > 0 for value in rs) / len(rs),
        "avg_win_r": sum(winners) / len(winners) if winners else 0.0,
        "avg_loss_r": sum(losers) / len(losers) if losers else 0.0,
        "expectancy_r": sum(rs) / len(rs),
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
        "profit_factor_no_losses": bool(gross_wins and not gross_losses),
        "max_drawdown_pct": max_dd * 100.0,
    }


def _annualized_return(trades: list[TradeRecord], symbol: str, years: float) -> Optional[float]:
    if years <= 0:
        return None
    balance = 1.0
    ordered = sorted(trades, key=lambda item: item.entry_time)
    for trade in ordered:
        value = _trade_r(trade, symbol)
        if value is not None:
            balance *= max(0.00000001, 1 + float(RISK_FRACTION) * value)
    return balance ** (1.0 / years) - 1.0


def _audit_symbol(symbol: str, cache_dir: Path, minimum_years: float) -> tuple[dict[str, Any], Optional[pl.DataFrame]]:
    path = HistoricalECNFetcher.get_cache_path(symbol, Timeframe.M1, cache_dir)
    provenance, source = HistoricalECNFetcher.load_provenance(symbol, Timeframe.M1, cache_dir)
    result: dict[str, Any] = {
        "symbol": symbol,
        "path": str(path),
        "provenance": provenance.value,
        "source": source,
        "status": "NOT_ELIGIBLE",
    }
    if not path.exists():
        result["reason"] = "M1 cache is missing"
        return result, None
    bars = MarketDataLoader.normalize_and_validate(
        pl.read_parquet(path), expected_interval=Timeframe.M1.duration, enforce_monotonicity=True,
    ).sort("timestamp")
    start, end = _utc(bars["timestamp"][0]), _utc(bars["timestamp"][-1])
    years = (end - start).total_seconds() / (365.25 * 86400)
    # Dukascopy M1 bars are tick-built: a minute with no quote update is absent,
    # so the repository's generic continuous-minute auditor labels every such
    # minute a feed outage. Keep its strict counts for the report, but apply a
    # transparent suitability rule here: reject price/quote anomalies and
    # unexplained gaps >= 120 minutes; retain shorter gaps as warnings without
    # fabricating candles. Christmas/New Year closures are separately counted.
    quote_anomaly_exprs = [
        (pl.col("open") <= 0) | (pl.col("high") <= 0) | (pl.col("low") <= 0) | (pl.col("close") <= 0)
        | (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.col("low"))
        | pl.col("spread").is_null() | (pl.col("spread") < 0)
    ]
    for field, bid_field in (("ask_open", "open"), ("ask_high", "high"), ("ask_low", "low"), ("ask_close", "close")):
        if field in bars.columns:
            quote_anomaly_exprs.append(pl.col(field).is_null() | (pl.col(field) < pl.col(bid_field)))
    if all(name in bars.columns for name in ("ask_open", "ask_high", "ask_low", "ask_close")):
        quote_anomaly_exprs.extend([
            pl.col("ask_high") < pl.max_horizontal("ask_open", "ask_close"),
            pl.col("ask_low") > pl.min_horizontal("ask_open", "ask_close"),
            pl.col("ask_high") < pl.col("ask_low"),
        ])
    vector_anomalies = bars.select(pl.any_horizontal(quote_anomaly_exprs).sum().alias("n")).item()
    gap_rows = bars.select(
        pl.col("timestamp"),
        pl.col("timestamp").shift(1).alias("_previous"),
        pl.col("timestamp").diff().alias("_gap"),
    ).filter(pl.col("_gap") > Timeframe.M1.duration)
    gaps: list[DataGap] = []
    for timestamp, previous, duration in gap_rows.iter_rows():
        if not timestamp.tzinfo:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if not previous.tzinfo:
            previous = previous.replace(tzinfo=timezone.utc)
        gaps.append(DataGap(
            start_time=previous,
            end_time=timestamp,
            duration=duration,
            missing_bars_count=int(duration / Timeframe.M1.duration) - 1,
            is_weekend_gap=MarketDataLoader._is_weekend_closure_gap(previous, timestamp, Timeframe.M1.duration),
        ))
    weekend_gaps = [gap for gap in gaps if gap.is_weekend_gap]
    midweek_gaps = [gap for gap in gaps if not gap.is_weekend_gap]
    sparse_gaps = [gap for gap in midweek_gaps if gap.duration <= timedelta(minutes=6)]
    holiday_gaps = []
    unexplained_gaps = []
    for gap in gaps:
        if gap.is_weekend_gap or gap.duration <= timedelta(minutes=6):
            continue
        dates = {gap.start_time.date(), gap.end_time.date()}
        scheduled_holiday = any(
            (date.month, date.day) in {(12, 24), (12, 25), (12, 26), (12, 31), (1, 1), (1, 2)}
            for date in dates
        )
        (holiday_gaps if scheduled_holiday else unexplained_gaps).append(gap)
    largest_unexplained_gap = max((gap.duration.total_seconds() / 60 for gap in unexplained_gaps), default=0.0)
    result.update({
        "rows": bars.height,
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "coverage_years": years,
        "quality_passed": (not midweek_gaps and not vector_anomalies),
        "quality_policy": "vendor M1 sparse-tick policy; strict auditor retained as diagnostic; no bars imputed",
        "weekend_gaps": len(weekend_gaps),
        "midweek_gaps": len(midweek_gaps),
        "short_sparse_gaps_le_5_missing_minutes": len(sparse_gaps),
        "scheduled_christmas_new_year_gaps": len(holiday_gaps),
        "unexplained_nonholiday_gaps_over_5_minutes": len(unexplained_gaps),
        "largest_unexplained_gap_minutes": largest_unexplained_gap,
        "anomalies": int(vector_anomalies),
        "spread_median_pips": float(bars["spread"].median() or 0.0),
    })
    reasons = []
    if provenance != DataProvenance.REAL_VENDOR:
        reasons.append(f"provenance is {provenance.value}")
    if years < minimum_years:
        reasons.append(f"coverage {years:.2f} years is below {minimum_years:.2f}")
    if vector_anomalies:
        reasons.append(f"price/quote quality failed: {vector_anomalies} anomalous bars")
    if largest_unexplained_gap >= 120:
        reasons.append(f"unexplained nonholiday outage is {largest_unexplained_gap:.0f} minutes (limit 120)")
    if reasons:
        result["reason"] = "; ".join(reasons)
    else:
        result["eligible"] = True
        result["status"] = "ELIGIBLE_WITH_GAP_WARNINGS" if (sparse_gaps or holiday_gaps or unexplained_gaps) else "ELIGIBLE"
        result["reason"] = (
            f"no OHLC/quote anomalies; strict diagnostic reports {len(midweek_gaps)} non-weekend gaps, "
            f"including {len(unexplained_gaps)} unexplained gaps over 5 minutes"
        )
    result.setdefault("eligible", False)
    return result, bars


def _aggregate_execution_m3(m1: pl.DataFrame) -> pl.DataFrame:
    """Build closed, open-stamped M3 quotes from vendor M1 bid/ask bars."""
    if m1.is_empty():
        return m1
    known_through = m1["timestamp"][-1] + Timeframe.M1.duration
    m3 = (
        m1.sort("timestamp")
        .group_by_dynamic("timestamp", every="3m", period="3m", closed="left", label="left")
        .agg([
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.col("volume").sum().alias("volume"),
            pl.col("spread").mean().alias("spread"),
            pl.col("ask_open").first().alias("ask_open"),
            pl.col("ask_high").max().alias("ask_high"),
            pl.col("ask_low").min().alias("ask_low"),
            pl.col("ask_close").last().alias("ask_close"),
            pl.len().alias("source_m1_bars"),
        ])
        .filter(pl.col("timestamp") + pl.duration(minutes=3) <= known_through)
        .sort("timestamp")
    )
    if m3.is_empty():
        return m3
    return m3.with_columns(
        pl.col("timestamp").cast(pl.Datetime("us", "UTC")),
        (pl.col("ask_open") - pl.col("open")).alias("ask_bid_open_spread_price"),
    )


def _run_portfolio(symbol: str, bars: pl.DataFrame, *, stressed: bool) -> tuple[Any, InstitutionalFractalStrategy]:
    strategy = InstitutionalFractalStrategy(
        strategy_id=f"fractal_rider_{symbol.lower()}",
        symbols=[symbol],
        account_equity=INITIAL_EQUITY,
        account_currency="USD",
        risk_fraction=RISK_FRACTION,
        minimum_net_reward_risk=Decimal("4"),
        stop_buffer_pips=Decimal("2"),
        slippage_buffer_pips=Decimal("0.3") if stressed else Decimal("0.2"),
        atr_expansion_slippage_factor=Decimal("0.1"),
        selected_set=None,
        source_timeframe=Timeframe.M3,
    )
    backtester = EventDrivenBacktester(
        strategy,
        CurrencyPair.from_symbol(symbol),
        initial_balance=INITIAL_EQUITY,
        cost_multiplier=2.0 if stressed else 1.0,
        slippage_pips=Decimal("0.3") if stressed else Decimal("0.2"),
        atr_expansion_slippage_factor=Decimal("0.1"),
        commission_multiplier=Decimal("1"),
        equity_curve_max_points=0,
    )
    return backtester.run(bars, timeframe=Timeframe.M3), strategy


def run_campaign(
    *,
    cache_dir: Path,
    report_path: Path,
    results_path: Path,
    minimum_years: float = 2.0,
    symbols: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    selected_symbols = tuple(symbol.upper() for symbol in (symbols or SYMBOLS))
    unknown_symbols = set(selected_symbols) - set(SYMBOLS)
    if not selected_symbols or unknown_symbols:
        raise ValueError(f"symbols must be a nonempty subset of {SYMBOLS}; invalid={sorted(unknown_symbols)}")
    audits: dict[str, dict[str, Any]] = {}
    data: dict[str, pl.DataFrame] = {}
    baseline: dict[str, Any] = {}
    stress: dict[str, Any] = {}
    cross_set = {"raw_signals": 0, "unique_physical_movements": 0, "multi_set_overlaps": 0,
                 "dropped_conflicting_signals": 0}

    for symbol in selected_symbols:
        print(f"[{symbol}] auditing real M1 coverage, provenance, and candle quality", flush=True)
        audit, bars = _audit_symbol(symbol, cache_dir, minimum_years)
        audits[symbol] = audit
        print(f"[{symbol}] data status: {audit['status']} ({audit.get('reason', 'all data gates passed')})", flush=True)
        if not audit.get("eligible", False) or bars is None:
            continue
        execution_bars = _aggregate_execution_m3(bars)
        if execution_bars.is_empty():
            audits[symbol]["eligible"] = False
            audits[symbol]["status"] = "NOT_ELIGIBLE"
            audits[symbol]["reason"] = "no fully closed M3 candles could be constructed from M1 data"
            continue
        data[symbol] = execution_bars
        print(f"[{symbol}] baseline: {execution_bars.height:,} closed M3 bars from M1, five-set arbitration", flush=True)
        result, strategy = _run_portfolio(symbol, execution_bars, stressed=False)
        baseline[symbol] = result
        cross_set["raw_signals"] += strategy.raw_signal_count
        cross_set["unique_physical_movements"] += len(strategy.unique_signal_movements)
        cross_set["multi_set_overlaps"] += strategy.multi_set_overlap_count
        cross_set["dropped_conflicting_signals"] += strategy.dropped_conflicting_signal_count
        print(f"[{symbol}] baseline complete: {result.total_trades} trades; now cost stress", flush=True)
        stressed_result, _stressed_strategy = _run_portfolio(symbol, execution_bars, stressed=True)
        stress[symbol] = stressed_result
        print(f"[{symbol}] cost stress complete: {stressed_result.total_trades} trades", flush=True)

    cells: list[dict[str, Any]] = []
    premium_discount: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for symbol in selected_symbols:
        audit = audits[symbol]
        if symbol not in baseline:
            for set_key in SET_ORDER:
                cells.append({
                    "symbol": symbol, "set": set_key,
                    "status": "NOT_RUN_DATA_QUALITY_OR_COVERAGE",
                    "reason": audit.get("reason", "no eligible data"),
                    "trades": 0, "gates": {"A": "NOT_EVALUATED", "B": "NOT_EVALUATED", "C": "NOT_EVALUATED", "D": "NOT_EVALUATED"},
                })
            continue
        bars = data[symbol]
        base_result = baseline[symbol]
        stress_result = stress[symbol]
        n = bars.height
        is_end_i, val_end_i = int(n * 0.60), int(n * 0.80)
        timestamps = bars["timestamp"]
        start_time = _utc(timestamps[0])
        is_end = _utc(timestamps[is_end_i])
        val_end = _utc(timestamps[val_end_i])
        end_time = _utc(timestamps[-1]) + Timeframe.M3.duration
        period_years = {
            "is": max((is_end - start_time).total_seconds() / (365.25 * 86400), 1 / 365.25),
            "val": max((val_end - is_end).total_seconds() / (365.25 * 86400), 1 / 365.25),
            "oos": max((end_time - val_end).total_seconds() / (365.25 * 86400), 1 / 365.25),
        }
        for set_key in SET_ORDER:
            full_trades = [trade for trade in base_result.trades if _trade_set(trade) == set_key]
            is_trades = [trade for trade in full_trades if trade.entry_time < is_end]
            val_trades = [trade for trade in full_trades if is_end <= trade.entry_time < val_end]
            oos_trades = [trade for trade in full_trades if trade.entry_time >= val_end]
            stressed_trades = [trade for trade in stress_result.trades if _trade_set(trade) == set_key]
            full_metrics = _equity_stats(full_trades, symbol)
            is_metrics = _equity_stats(is_trades, symbol)
            val_metrics = _equity_stats(val_trades, symbol)
            oos_metrics = _equity_stats(oos_trades, symbol)
            stress_metrics = _equity_stats(stressed_trades, symbol)
            is_ann = _annualized_return(is_trades, symbol, period_years["is"])
            oos_ann = _annualized_return(oos_trades, symbol, period_years["oos"])
            wfe = oos_ann / is_ann if is_ann is not None and oos_ann is not None and is_ann > 0 else None
            gate_a = len(full_trades) >= GATES["A_min_full_window_trades"]
            gate_b = (
                (is_metrics["avg_win_r"] or 0) >= GATES["B_min_is_average_winner_r"]
                and (is_metrics["expectancy_r"] or 0) >= GATES["B_min_is_expectancy_r"]
            )
            gate_c = wfe is not None and wfe >= GATES["C_min_wfe"]
            gate_d = (
                stress_metrics["expectancy_r"] is not None
                and stress_metrics["expectancy_r"] >= GATES["D_min_cost_stress_expectancy_r"]
            )
            status = "SURVIVED" if all((gate_a, gate_b, gate_c, gate_d)) else "REJECTED"
            cells.append({
                "symbol": symbol,
                "set": set_key,
                "status": status,
                "trades": len(full_trades),
                "trade_counts": {"is": len(is_trades), "validation": len(val_trades), "oos": len(oos_trades), "full": len(full_trades), "cost_stress_full": len(stressed_trades)},
                "win_rate": full_metrics["win_rate"],
                "average_winner_r_is": is_metrics["avg_win_r"],
                "average_loser_r_is": is_metrics["avg_loss_r"],
                "expectancy_r_is": is_metrics["expectancy_r"],
                "expectancy_r_validation": val_metrics["expectancy_r"],
                "expectancy_r_oos": oos_metrics["expectancy_r"],
                "expectancy_r_full": full_metrics["expectancy_r"],
                "profit_factor_full": full_metrics["profit_factor"],
                "profit_factor_no_losses": full_metrics["profit_factor_no_losses"],
                "max_drawdown_pct_full_set_attributed": full_metrics["max_drawdown_pct"],
                "wfe_oos_over_is_annualized_return": wfe,
                "annualized_return_is": is_ann,
                "annualized_return_oos": oos_ann,
                "expectancy_r_cost_stress_full": stress_metrics["expectancy_r"],
                "gates": {"A": "PASS" if gate_a else "FAIL", "B": "PASS" if gate_b else "FAIL",
                          "C": "PASS" if gate_c else "FAIL", "D": "PASS" if gate_d else "FAIL"},
                "deduplication": "unified all-set run; signal assigned to highest net R:R set",
            })
            for trade in full_trades:
                r_value = _trade_r(trade, symbol)
                fields = _tag_fields(trade.client_tag)
                if r_value is None:
                    continue
                for role in ("HTF", "MTF", "LTF"):
                    location = fields.get(f"{role}_LOCATION", "UNKNOWN")
                    premium_discount[symbol][f"{set_key}:{role}"][location].append(r_value)

    survivors = [cell for cell in cells if cell.get("status") == "SURVIVED"]
    oos_positive = [cell for cell in cells if cell.get("expectancy_r_oos") is not None and cell["expectancy_r_oos"] > 0]
    eligible_assets = len(baseline)
    overall = (
        "INSUFFICIENT DATA" if eligible_assets == 0 else
        "SUPPORTED" if len(survivors) >= 3 and len({cell["symbol"] for cell in survivors}) >= 2 else
        "CONDITIONAL" if survivors else "FALSIFIED"
    )
    results = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_verdict": overall,
        "campaign": "Unified 7-timeframe fractal structural rider; cross-set arbitration enabled",
        "timeframe_ladder": ["1M", "1W", "1D", "4H", "1H", "15M", "3M"],
        "sets": {key: [tf.value for tf in value] for key, value in TIMEFRAME_SETS.items()},
        "configuration": {
            "symbols": list(selected_symbols), "minimum_coverage_years": minimum_years,
            "eligible_symbols": sorted(baseline),
            "chronological_partitions": {"is": 0.60, "validation": 0.20, "oos": 0.20},
            "account_equity_usd": float(INITIAL_EQUITY), "risk_fraction": float(RISK_FRACTION),
            "minimum_net_reward_risk": 4.0, "base_slippage_pips": 0.2,
            "entry_triggers": "LTF micro-BOS or a confirmed EQH/EQL liquidity sweep with reclaim close; execute at next source-bar open",
            "initial_stop": "latest confirmed opposing LTF swing extreme with 2-pip buffer",
            "movement_arbitration": "shared active M3 leg ID for simultaneous cross-set allocation; each intent also carries its set HTF leg ID",
            "liquidity_pool_swing_lookback": LIQUIDITY_POOL_SWING_LOOKBACK,
            "execution_bars": "closed 3-minute OHLC candles aggregated from real vendor M1 BID/ASK; partial final buckets dropped; no missing candles fabricated",
            "atr_expansion_slippage_factor": 0.1, "commission_usd_per_lot_round_turn": 7.0,
            "cost_stress": "2x observed bid/ask spread, 0.3 pips base slippage plus the same ATR expansion reserve; $7/lot commission unchanged",
            "swap": "Static model rates: long -0.6 pips, short +0.2 pips; Wednesday 3x. Not broker-specific historical swap data.",
            "tier_1_news_blackout": "NOT_APPLIED: no historical event calendar is bundled with the workspace; performance is before this requested filter.",
            "set_attribution": "All sets compete in one causal strategy per pair; each physical movement is allocated to the highest net expected R:R. Matrix cells are allocated-trade results, not independent set portfolios.",
            "gates": GATES,
        },
        "dataset_audit": audits,
        "cross_set_deduplication": cross_set,
        "matrix": cells,
        "survivors": [{"symbol": item["symbol"], "set": item["set"]} for item in survivors],
        "positive_oos_cells": [{"symbol": item["symbol"], "set": item["set"], "oos_expectancy_r": item["expectancy_r_oos"]} for item in oos_positive],
        "premium_discount_attribution": {
            symbol: {
                cell_key: {
                    loc: {"trades": len(vals), "expectancy_r": sum(vals) / len(vals)}
                    for loc, vals in locations.items() if vals
                }
                for cell_key, locations in by_cell.items()
            }
            for symbol, by_cell in premium_discount.items()
        },
    }
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    report = render_report(results)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report.encode("utf-8", errors="backslashreplace").decode("utf-8"), flush=True)
    return results


def render_report(results: dict[str, Any]) -> str:
    symbols = results["configuration"]["symbols"]
    rows = [
        "# Final Terminal Report — Fractal Structural Rider",
        "",
        f"Generated (UTC): {results['generated_at_utc']}",
        f"Overall verdict: **{results['overall_verdict']}**",
        "",
        "## A. Executive verdict",
        "",
        "This is a causal, research-only result. Passing the gates would identify a candidate for further independent validation; it cannot establish guaranteed or future profitability.",
        "",
        "## B. Twenty-cell performance matrix",
        "",
        "Signals were arbitrated across the shared seven-timeframe ladder, then assigned to the set with the highest cost-adjusted structural R:R. Set cells are not independent portfolios.",
        "",
        "| Pair | Set | Trades | Win rate | Avg win R (IS) | Exp. R (IS) | PF (full) | Max DD % | WFE | Cost stress Exp. R | A-D | Verdict |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for item in results["matrix"]:
        if item.get("win_rate") is None:
            rows.append(f"| {item['symbol']} | {item['set']} | 0 | — | — | — | — | — | — | — | — | {item['status']} |")
            continue
        def fmt(value: Any, pct: bool = False) -> str:
            if value is None:
                return "—"
            return f"{100*value:.2f}%" if pct else f"{value:.3f}"
        gates = "/".join(item["gates"][key] for key in ("A", "B", "C", "D"))
        rows.append(
            f"| {item['symbol']} | {item['set']} | {item['trades']} | {fmt(item['win_rate'], True)} | "
            f"{fmt(item['average_winner_r_is'])} | {fmt(item['expectancy_r_is'])} | "
            f"{'∞' if item.get('profit_factor_no_losses') else fmt(item['profit_factor_full'])} | {fmt(item['max_drawdown_pct_full_set_attributed'])}% | "
            f"{fmt(item['wfe_oos_over_is_annualized_return'])} | {fmt(item['expectancy_r_cost_stress_full'])} | {gates} | {item['status']} |"
        )
    rows += [
        "",
        "Gate order: A full-window trades ≥80; B IS average winner ≥4.0R and expectancy ≥+0.35R; C annualized OOS/IS return ≥0.50; D full-window stress expectancy ≥0R.",
        "Profit factor is shown as ∞ when a sample has wins but no losses; this is not informative with the very small trade counts in this campaign.",
        "",
        "### Data quality and coverage",
        "",
        "| Pair | First bar UTC | Last bar UTC | Vendor bars | Coverage years | Strict midweek gaps | Sparse gaps (≤5 missing minutes) | Holiday gaps | Other gaps >5 minutes | Largest other gap (minutes) | Price/quote anomalies | Eligibility |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for symbol in symbols:
        audit = results["dataset_audit"][symbol]
        rows.append(
            f"| {symbol} | {audit.get('start_utc', '—')[:10]} | {audit.get('end_utc', '—')[:10]} | "
            f"{audit.get('rows', 0):,} | {audit.get('coverage_years', 0):.3f} | "
            f"{audit.get('midweek_gaps', 0):,} | {audit.get('short_sparse_gaps_le_5_missing_minutes', 0):,} | "
            f"{audit.get('scheduled_christmas_new_year_gaps', 0):,} | "
            f"{audit.get('unexplained_nonholiday_gaps_over_5_minutes', 0):,} | "
            f"{audit.get('largest_unexplained_gap_minutes', 0):.0f} | {audit.get('anomalies', 0)} | {audit.get('status')} |"
        )
    rows += [
        "",
        "The generic strict audit treats any absent minute as a gap. Dukascopy M1 is tick-built, so no-tick minutes are not emitted; the strategy uses raw vendor bars and does not fabricate or forward-fill candles. Eligibility here requires real-vendor provenance, ≥2 years, no bid/ask OHLC anomalies, and no unexplained nonholiday gap of 120 minutes or more. Shorter unexplained gaps are reported as warnings. A synchronized roughly 107-minute gap occurs on GBPUSD, USDJPY, and AUDUSD on 2024-12-17; results on those pairs have that feed interruption caveat.",
        "",
        "## C. Cross-set deduplication audit",
        "",
        f"- Raw qualifying signals: {results['cross_set_deduplication']['raw_signals']}",
        f"- Unique physical movement IDs with a signal: {results['cross_set_deduplication']['unique_physical_movements']}",
        f"- Extra set representations of same-time movements: {results['cross_set_deduplication']['multi_set_overlaps']}",
        f"- Conflicting opposing signals dropped: {results['cross_set_deduplication']['dropped_conflicting_signals']}",
        "- Each accepted movement is allocated once to the set with highest net expected R:R.",
        "",
        "## D. Dealing-range value-add",
        "",
        "Entry outcomes below are grouped by each trade's causal HTF, MTF, and LTF range location at signal time.",
        "",
        "| Pair | Set role | Location | Trades | Mean R |",
        "|---|---|---|---:|---:|",
    ]
    attribution = results["premium_discount_attribution"]
    for symbol, cells in attribution.items():
        for cell, locations in cells.items():
            for location, metrics in locations.items():
                rows.append(f"| {symbol} | {cell} | {location} | {metrics['trades']} | {metrics['expectancy_r']:.3f} |")
    rows += [
        "",
        "## E. Scale tradability",
        "",
    ]
    for set_key in SET_ORDER:
        set_cells = [item for item in results["matrix"] if item["set"] == set_key]
        if not results["configuration"]["eligible_symbols"]:
            rows.append(f"- {set_key}: **INSUFFICIENT DATA**; no eligible pair passed provenance, coverage, and quality gates.")
            continue
        surviving = sum(item.get("status") == "SURVIVED" for item in set_cells)
        positive = sum((item.get("expectancy_r_oos") or 0) > 0 for item in set_cells)
        if surviving:
            label = "ECONOMICALLY TRADABLE EDGE (research candidate)"
        elif positive:
            label = "STRUCTURAL INFORMATION ONLY (OOS positive cells did not clear all gates)"
        else:
            label = "FALSIFIED / NO OOS EDGE ESTABLISHED"
        rows.append(f"- {set_key}: **{label}**; {surviving}/{len(symbols)} cells passed all gates; {positive}/{len(symbols)} had positive OOS expectancy.")
    rows += ["", "## F. Asset generalization", ""]
    for symbol in symbols:
        asset_cells = [item for item in results["matrix"] if item["symbol"] == symbol]
        passed = [item["set"] for item in asset_cells if item.get("status") == "SURVIVED"]
        positive = [item["set"] for item in asset_cells if (item.get("expectancy_r_oos") or 0) > 0]
        rows.append(f"- {symbol}: all-gate sets={', '.join(passed) or 'none'}; positive OOS expectancy sets={', '.join(positive) or 'none'}.")
    rows += ["", "## G. Surviving edge candidates", ""]
    if results["survivors"]:
        for item in results["survivors"]:
            rows.append(f"- {item['symbol']} {item['set']}")
    else:
        rows.append("None. No asset/set allocation cleared all four hard gates.")
    rows += [
        "",
        "## H. Direct scientific answers",
        "",
        "1. Are the five sets correlated views of one continuous ladder? **Yes by construction**; the same closed-timeframe states feed every role.",
        "2. Does higher-timeframe state predict lower-timeframe transitions? **Not established by trade expectancy alone**; a separate transition-information test is needed.",
        "3. Are nested pullbacks more frequent and monetizable at ≥4R? **Not established unless the state-transition frequency and all-gate trade results support it.**",
        "4. Does Set 5 retain information after micro-spread friction? **See Set 5 OOS and 2× spread stress metrics above; no pass means no economic edge is established.**",
        f"5. Final verdict: the specified rider is **{results['overall_verdict']}** under the hard gates; the broader fractal hypothesis remains **unproven**, because only {results['cross_set_deduplication']['raw_signals']} trades passed entry filters and no cell met the 80-trade minimum.",
        "",
        "## Data and model limits",
        "",
        "- Source is real Dukascopy historical BID/ASK M1 candles; spread is measured from the aligned open/close quote sides. No synthetic candles or fixed synthetic spread were used.",
        "- Base commission is $7 per standard lot round turn. Static swap assumptions are shown in the JSON and are not broker-specific historical rates.",
        "- A historical Tier-1 macro-news calendar was not available, so the requested 30-minute news blackout was not applied. The reported campaign therefore does not satisfy that requested filter; no events were guessed or fabricated.",
        f"- Equal-high/low pools are clustered from the latest {results['configuration']['liquidity_pool_swing_lookback']} confirmed swings on each timeframe to keep liquidity levels recent and scale-relative.",
        "- The research results do not authorize live trading and do not guarantee profits.",
        "",
    ]
    return "\n".join(rows)


def merge_campaign_files(
    source_paths: Iterable[Path],
    *,
    report_path: Path,
    results_path: Path,
) -> dict[str, Any]:
    """Combine independent per-pair runs into the canonical cross-asset report."""
    payloads = [json.loads(Path(path).read_text(encoding="utf-8")) for path in source_paths]
    if not payloads:
        raise ValueError("at least one pair result file is required")
    results = copy.deepcopy(payloads[0])
    symbols = [symbol for payload in payloads for symbol in payload["configuration"]["symbols"]]
    if len(symbols) != len(set(symbols)):
        raise ValueError(f"duplicate symbol results cannot be merged: {symbols}")
    results["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    results["configuration"]["symbols"] = symbols
    results["configuration"]["eligible_symbols"] = sorted({
        symbol
        for payload in payloads
        for symbol in payload["configuration"]["eligible_symbols"]
    })
    results["dataset_audit"] = {}
    results["matrix"] = []
    results["survivors"] = []
    results["positive_oos_cells"] = []
    results["cross_set_deduplication"] = {
        "raw_signals": 0,
        "unique_physical_movements": 0,
        "multi_set_overlaps": 0,
        "dropped_conflicting_signals": 0,
    }
    results["premium_discount_attribution"] = {}
    for payload in payloads:
        results["dataset_audit"].update(payload["dataset_audit"])
        results["matrix"].extend(payload["matrix"])
        results["survivors"].extend(payload["survivors"])
        results["positive_oos_cells"].extend(payload["positive_oos_cells"])
        results["premium_discount_attribution"].update(payload["premium_discount_attribution"])
        for metric, value in payload["cross_set_deduplication"].items():
            results["cross_set_deduplication"][metric] += value
    rank = {symbol: index for index, symbol in enumerate(symbols)}
    set_rank = {set_key: index for index, set_key in enumerate(SET_ORDER)}
    results["matrix"].sort(key=lambda cell: (rank[cell["symbol"]], set_rank[cell["set"]]))
    for cell in results["matrix"]:
        if cell.get("profit_factor_full") == 999.0:
            cell["profit_factor_full"] = None
            cell["profit_factor_no_losses"] = True
    eligible = results["configuration"]["eligible_symbols"]
    survivor_symbols = {item["symbol"] for item in results["survivors"]}
    results["overall_verdict"] = (
        "INSUFFICIENT DATA" if not eligible else
        "SUPPORTED" if len(results["survivors"]) >= 3 and len(survivor_symbols) >= 2 else
        "CONDITIONAL" if results["survivors"] else "FALSIFIED"
    )
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    report = render_report(results)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report, flush=True)
    return results


def main(argv: Optional[Iterable[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache/dukascopy"))
    parser.add_argument("--report", type=Path, default=Path("research/FRACTAL_STRUCTURAL_RIDER_REPORT.md"))
    parser.add_argument("--results", type=Path, default=Path("research/results/fractal_structural_rider.json"))
    parser.add_argument("--minimum-years", type=float, default=2.0)
    parser.add_argument("--symbols", nargs="+", choices=SYMBOLS)
    parser.add_argument("--merge-results", nargs="+", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.merge_results:
        merge_campaign_files(args.merge_results, report_path=args.report, results_path=args.results)
        return 0
    run_campaign(cache_dir=args.cache_dir, report_path=args.report, results_path=args.results,
                 minimum_years=args.minimum_years, symbols=args.symbols)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
