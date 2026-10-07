"""
Automated Research Sweep & Robustness Filter.

Systematically evaluates baseline Market Model strategy variants across the
five canonical timeframe sets, partitions history into a strict
60% In-Sample / 20% Validation / 20% blind Out-of-Sample timeline, and
auto-prunes every configuration that fails any of the four hard gates:

  (a) Statistical Significance   : >= 80 trades over the tested window.
  (b) Asymmetry & Expectancy     : average winner >= 4.0R and net
                                   expectancy >= +0.35R (evaluated on IS).
  (c) Walk-Forward Efficiency    : OOS annualized return / IS annualized
                                   return >= 0.50.
  (d) Cost Stress                : re-run at 2x spread + 0.3 pips slippage
                                   must keep net expectancy >= 0.

Discipline invariants enforced by this module:

* Parameters are NEVER tuned on OOS. The In-Sample slice is always the first
  backtest executed for an experiment; the OOS slice is a blind holdout used
  only for measurement (gates b/c/d), never for selection feedback.
* Every permutation is logged under a unique experiment ID in
  ``research/results/experiments.json``.
* Only configurations surviving all four gates are rendered into
  ``research/SURVIVING_EDGES.md``.

Usage::

    python research/sweeper.py                       # full sweep
    python research/sweeper.py --sets SET_3 --symbols EURUSD
    python research/sweeper.py --require-real-data   # fail closed on provenance
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Allow direct script execution (`python research/sweeper.py`) from the repo
# root without requiring an editable install.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

from forex_platform.core.domain import CurrencyPair
from forex_platform.market_data.causal_aligner import CausalAligner, Timeframe
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.provenance import DataProvenance
from forex_platform.market_model.contracts import SwingType
from forex_platform.market_model.structure.breaks import detect_structure_breaks
from forex_platform.market_model.structure.swings import detect_swings
from forex_platform.research_engine.backtester import (
    BacktestResult,
    EventDrivenBacktester,
)
from forex_platform.strategy_engine.base import BaseStrategy
from forex_platform.strategy_engine.scalping import AsianRangeFadeScalper
from forex_platform.strategy_engine.session_breakout import LondonSessionBreakout
from forex_platform.strategy_engine.trend_continuation import TrendContinuationStrategy

logger = logging.getLogger("research.sweeper")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_PATH = REPO_ROOT / "research" / "results" / "experiments.json"
DEFAULT_EDGES_PATH = REPO_ROOT / "research" / "SURVIVING_EDGES.md"
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"

# ---------------------------------------------------------------------------
# Automated In-Sample / Validation / Out-of-Sample split (fractions of time)
# ---------------------------------------------------------------------------
SPLIT_RATIOS: Dict[str, float] = {"is": 0.60, "val": 0.20, "oos": 0.20}

# ---------------------------------------------------------------------------
# Hard elimination gates (auto-pruning thresholds)
# ---------------------------------------------------------------------------
GATE_A_STATISTICAL_SIGNIFICANCE = "GATE_A_STATISTICAL_SIGNIFICANCE"
GATE_B_ASYMMETRY_EXPECTANCY = "GATE_B_ASYMMETRY_EXPECTANCY"
GATE_C_WALK_FORWARD_EFFICIENCY = "GATE_C_WALK_FORWARD_EFFICIENCY"
GATE_D_COST_STRESS = "GATE_D_COST_STRESS"

GATE_ORDER: Tuple[str, ...] = (
    GATE_A_STATISTICAL_SIGNIFICANCE,
    GATE_B_ASYMMETRY_EXPECTANCY,
    GATE_C_WALK_FORWARD_EFFICIENCY,
    GATE_D_COST_STRESS,
)

GATE_A_MIN_TRADES = 80
GATE_B_MIN_AVG_WIN_R = 4.0
GATE_B_MIN_NET_EXPECTANCY_R = 0.35
GATE_C_MIN_WFE = 0.50
COST_STRESS_SPREAD_MULTIPLIER = 2.0
COST_STRESS_SLIPPAGE_PIPS = Decimal("0.3")

GATE_B_EVALUATED_ON = "IS"
GATE_D_EVALUATED_ON = "FULL_WINDOW"

VERDICT_SURVIVED = "SURVIVED"
VERDICT_REJECTED = "REJECTED"
VERDICT_ERROR = "ERROR"

MIN_BARS_FOR_EXPERIMENT = 30

TREND_LABELS: Dict[int, str] = {1: "UP", -1: "DOWN", 0: "NEUTRAL"}

# Exact window durations for close_time computation. "1mo" is handled
# separately via dt.offset_by (calendar months have no fixed duration).
PERIOD_DURATIONS: Dict[str, timedelta] = {
    "1w": timedelta(days=7),
    "1d": timedelta(days=1),
    "4h": timedelta(hours=4),
    "1h": timedelta(hours=1),
    "15m": timedelta(minutes=15),
    "5m": timedelta(minutes=5),
}

def gates_config() -> Dict[str, Any]:
    """Machine-readable gate configuration stored with every sweep."""
    return {
        GATE_A_STATISTICAL_SIGNIFICANCE: {
            "min_trades": GATE_A_MIN_TRADES,
            "window": "full tested window (IS+VAL+OOS)",
        },
        GATE_B_ASYMMETRY_EXPECTANCY: {
            "min_avg_win_r": GATE_B_MIN_AVG_WIN_R,
            "min_net_expectancy_r": GATE_B_MIN_NET_EXPECTANCY_R,
            "evaluated_on": GATE_B_EVALUATED_ON,
        },
        GATE_C_WALK_FORWARD_EFFICIENCY: {
            "min_oos_over_is_ann_return": GATE_C_MIN_WFE,
        },
        GATE_D_COST_STRESS: {
            "spread_multiplier": COST_STRESS_SPREAD_MULTIPLIER,
            "slippage_pips": float(COST_STRESS_SLIPPAGE_PIPS),
            "min_net_expectancy_r": 0.0,
            "evaluated_on": GATE_D_EVALUATED_ON,
        },
    }


# ---------------------------------------------------------------------------
# Canonical timeframe sets (HTF -> MTF -> LTF execution frame)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TimeframeSet:
    """One canonical multi-timeframe hypothesis frame.

    ``gen_*`` describes how base history is acquired (cache/synthesis at the
    generation timeframe); ``ltf_*`` describes the execution timeframe the
    backtester runs on (downsampled from the generation frame when different).
    """

    key: str
    htf_label: str
    mtf_label: str
    ltf_label: str
    ltf_period: str
    mtf_period: str
    htf_period: str
    ltf_timeframe: Timeframe
    gen_timeframe: Timeframe
    gen_bars: int

    @property
    def frame_labels(self) -> Dict[str, str]:
        return {"htf": self.htf_label, "mtf": self.mtf_label, "ltf": self.ltf_label}

    @property
    def needs_downsample(self) -> bool:
        return self.gen_timeframe != self.ltf_timeframe


TIMEFRAME_SETS: Dict[str, TimeframeSet] = {
    "SET_1": TimeframeSet(
        key="SET_1", htf_label="1Mo", mtf_label="1W", ltf_label="1D",
        ltf_period="1d", mtf_period="1w", htf_period="1mo",
        ltf_timeframe=Timeframe.D1, gen_timeframe=Timeframe.H1, gen_bars=36_000,
    ),
    "SET_2": TimeframeSet(
        key="SET_2", htf_label="1W", mtf_label="1D", ltf_label="4H",
        ltf_period="4h", mtf_period="1d", htf_period="1w",
        ltf_timeframe=Timeframe.H4, gen_timeframe=Timeframe.H1, gen_bars=15_000,
    ),
    "SET_3": TimeframeSet(
        key="SET_3", htf_label="1D", mtf_label="4H", ltf_label="1H",
        ltf_period="1h", mtf_period="4h", htf_period="1d",
        ltf_timeframe=Timeframe.H1, gen_timeframe=Timeframe.H1, gen_bars=3_500,
    ),
    "SET_4": TimeframeSet(
        key="SET_4", htf_label="4H", mtf_label="1H", ltf_label="15M",
        ltf_period="15m", mtf_period="1h", htf_period="4h",
        ltf_timeframe=Timeframe.M15, gen_timeframe=Timeframe.M15, gen_bars=4_000,
    ),
    "SET_5": TimeframeSet(
        key="SET_5", htf_label="1H", mtf_label="15M", ltf_label="5M",
        ltf_period="5m", mtf_period="15m", htf_period="1h",
        ltf_timeframe=Timeframe.M5, gen_timeframe=Timeframe.M5, gen_bars=12_000,
    ),
}


# ---------------------------------------------------------------------------
# Baseline model variants (baseline hypotheses = library-default parameters)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelVariant:
    """A named strategy hypothesis under test."""

    key: str
    strategy_cls: type
    params: Dict[str, Any] = field(default_factory=dict)

    def build(self, strategy_id: str, symbol: str) -> BaseStrategy:
        return self.strategy_cls(
            strategy_id=strategy_id, symbols=[symbol], **self.params
        )


VARIANTS: Dict[str, ModelVariant] = {
    "TREND_CONTINUATION_MTF": ModelVariant(
        "TREND_CONTINUATION_MTF", TrendContinuationStrategy, {}
    ),
    "LONDON_ORB_BREAKOUT": ModelVariant(
        "LONDON_ORB_BREAKOUT", LondonSessionBreakout, {}
    ),
    "ASIAN_RANGE_FADE": ModelVariant(
        "ASIAN_RANGE_FADE", AsianRangeFadeScalper, {}
    ),
}


@dataclass
class Dataset:
    """LTF bars for one (timeframe set, symbol) with provenance metadata."""

    df: pl.DataFrame
    provenance: str
    source: str


# ---------------------------------------------------------------------------
# R-multiple and performance metrics
# ---------------------------------------------------------------------------
def _round(value: Optional[float], places: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), places)


def trade_risk_units(trade: Any) -> Optional[Decimal]:
    """Initial protective risk of a trade in account-currency units.

    Risk is measured entry-to-stop-loss: |entry - stop_loss| * units.
    Returns None when the trade carries no usable stop level.
    """
    stop_loss = getattr(trade, "stop_loss", None)
    if stop_loss is None:
        return None
    price_risk = abs(Decimal(trade.entry_price) - Decimal(stop_loss))
    if price_risk <= 0:
        return None
    risk = price_risk * Decimal(int(trade.units))
    return risk if risk > 0 else None


def trade_r(trade: Any) -> Optional[float]:
    """Realized PnL of one trade expressed in R (initial risk) units."""
    risk = trade_risk_units(trade)
    if risk is None:
        return None
    return float(Decimal(trade.net_pnl) / risk)


def r_multiples(result: BacktestResult) -> List[float]:
    """All R-multiples computable from a backtest result (order preserved)."""
    out: List[float] = []
    for trade in result.trades:
        value = trade_r(trade)
        if value is not None:
            out.append(value)
    return out


def mean_expectancy_r(result: BacktestResult) -> Optional[float]:
    """Net expectancy: mean R over every trade with a computable risk unit."""
    values = r_multiples(result)
    if not values:
        return None
    return sum(values) / len(values)


def average_winning_r(result: BacktestResult) -> Optional[float]:
    """Average R of *winning* trades only (net_pnl > 0)."""
    values = [
        trade_r(t) for t in result.trades if Decimal(t.net_pnl) > Decimal("0")
    ]
    wins = [v for v in values if v is not None]
    if not wins:
        return None
    return sum(wins) / len(wins)


def annualized_return(result: BacktestResult) -> Optional[float]:
    """Compound annualized return from the timestamped equity curve."""
    equity = result.equity_curve
    if len(equity) >= 2:
        start_value = float(equity[0][1])
        end_value = float(equity[-1][1])
        days = (equity[-1][0] - equity[0][0]).total_seconds() / 86_400.0
        if start_value <= 0 or days <= 0:
            return None
        total_return = (end_value / start_value) - 1.0
    elif result.trades:
        first_ts = min(t.entry_time for t in result.trades)
        last_ts = max(t.exit_time for t in result.trades)
        days = (last_ts - first_ts).total_seconds() / 86_400.0
        start_value = float(result.initial_balance)
        if start_value <= 0 or days <= 0:
            return None
        total_return = float(result.total_net_pnl) / start_value
    else:
        return None

    if total_return <= -1.0:
        return -1.0
    # Clamp sub-day windows to one day: annualizing intraday spans explodes.
    years = max(days / 365.25, 1.0 / 365.25)
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def walk_forward_efficiency(
    is_annualized: Optional[float], oos_annualized: Optional[float]
) -> Optional[float]:
    """OOS annualized return / IS annualized return (None if IS <= 0)."""
    if is_annualized is None or oos_annualized is None:
        return None
    if is_annualized <= 0.0:
        return None
    return oos_annualized / is_annualized


def cost_stress_expectancy_r(
    result: BacktestResult,
    pair: CurrencyPair,
    slippage_pips: Decimal = COST_STRESS_SLIPPAGE_PIPS,
) -> Optional[float]:
    """Net expectancy of a cost-stressed run, in R units.

    ``result`` must already embed the 2x-spread cost multiplier (applied by the
    backtester to spreads and commissions). Slippage is then debited on every
    fill: entry + exit = 2 fills per trade, priced via the pair's pip size.
    """
    slippage_per_fill = pair.to_price(Decimal(slippage_pips))
    values: List[float] = []
    for trade in result.trades:
        risk = trade_risk_units(trade)
        if risk is None:
            continue
        stressed_net = (
            Decimal(trade.net_pnl)
            - slippage_per_fill * Decimal(2) * Decimal(int(trade.units))
        )
        values.append(float(stressed_net / risk))
    if not values:
        return None
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# Automated IS / VAL / OOS timeline partition (60 / 20 / 20)
# ---------------------------------------------------------------------------
def partition_is_val_oos(
    df: pl.DataFrame,
    is_ratio: float = SPLIT_RATIOS["is"],
    val_ratio: float = SPLIT_RATIOS["val"],
    oos_ratio: float = SPLIT_RATIOS["oos"],
) -> Tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Chronological 60/20/20 split of the timeline (never shuffled).

    * In-Sample (IS)   : first 60% - the only slice parameters may adapt to.
    * Validation (VAL) : middle 20% - sanity window, logged but never tuned on.
    * Blind OOS        : final 20% - holdout, measured only after IS passes.
    """
    if abs((is_ratio + val_ratio + oos_ratio) - 1.0) > 1e-9:
        raise ValueError(
            f"Split ratios must sum to 1.0, got {is_ratio + val_ratio + oos_ratio}"
        )
    if df.height < MIN_BARS_FOR_EXPERIMENT:
        raise ValueError(
            f"Need at least {MIN_BARS_FOR_EXPERIMENT} bars for a 60/20/20 split, "
            f"got {df.height}"
        )

    is_bars = max(int(df.height * is_ratio), 1)
    val_bars = max(int(df.height * val_ratio), 1)
    if is_bars + val_bars >= df.height:
        is_bars = max(1, df.height - 2)
        val_bars = 1

    is_df = df.slice(0, is_bars)
    val_df = df.slice(is_bars, val_bars)
    oos_df = df.slice(is_bars + val_bars)
    return is_df, val_df, oos_df


# ---------------------------------------------------------------------------
# Frame aggregation and causal Market Model context
# ---------------------------------------------------------------------------
def downsample_period(df: pl.DataFrame, period: str) -> pl.DataFrame:
    """Aggregate OHLCV bars into left-labeled ``[t, t + period)`` windows.

    Mirrors :meth:`CausalAligner.downsample` but supports calendar periods the
    ``Timeframe`` enum does not cover (``1w`` / ``1mo``). ``close_time`` is the
    exact window open time plus the window length (calendar-exact for months),
    which is what causal as-of joins key on.
    """
    if period == "1mo":
        close_expr = pl.col("timestamp").dt.offset_by("1mo").alias("close_time")
    elif period in PERIOD_DURATIONS:
        close_expr = (
            pl.col("timestamp") + PERIOD_DURATIONS[period]
        ).alias("close_time")
    else:
        raise ValueError(f"Unsupported aggregation period: {period!r}")

    return (
        df.sort("timestamp")
        .group_by_dynamic(
            "timestamp",
            every=period,
            period=period,
            closed="left",
            label="left",
        )
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
            pl.col("spread").mean(),
        )
        .with_columns(close_expr)
        .sort("close_time")
    )


def causal_trend_series(bars: pl.DataFrame, lookback: int = 2) -> List[str]:
    """Per-bar structural trend using only data available at that bar.

    Built from the Market Model primitives (:func:`detect_swings`,
    :func:`detect_structure_breaks`): a swing at index ``i`` is only admitted
    once bar ``i + lookback`` has closed, and structure breaks are only
    actionable after their swing is confirmed (``confirmation_bars=lookback``).
    The verdict mirrors :func:`get_trend_direction`: the most recent confirmed
    BOS/CHOCH direction wins; otherwise HH/HL progression decides.
    """
    n = bars.height
    if n == 0:
        return []

    swings = detect_swings(bars, lookback=lookback)
    breaks = detect_structure_breaks(bars, swings, confirmation_bars=lookback)

    swing_events: Dict[int, List[Any]] = {}
    for swing in swings:
        swing_events.setdefault(swing.index + lookback, []).append(swing)
    break_events: Dict[int, List[Any]] = {}
    for brk in breaks:
        break_events.setdefault(brk.index, []).append(brk)

    highs: List[Decimal] = []
    lows: List[Decimal] = []
    last_break_dir: Optional[int] = None
    trend = 0
    out: List[str] = []

    for i in range(n):
        for swing in swing_events.get(i, ()):
            if swing.swing_type == SwingType.HIGH:
                highs.append(swing.price)
            else:
                lows.append(swing.price)
        for brk in break_events.get(i, ()):
            last_break_dir = brk.direction

        if last_break_dir is not None:
            trend = last_break_dir
        elif len(highs) >= 2 and len(lows) >= 2:
            if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
                trend = 1
            elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
                trend = -1
            else:
                trend = 0
        out.append(TREND_LABELS[trend])
    return out


def _trend_context_frame(df: pl.DataFrame, period: str) -> pl.DataFrame:
    """Build a ``[close_time, trend]`` context frame from LTF bars."""
    ctx_bars = downsample_period(df, period)
    if ctx_bars.height == 0:
        return ctx_bars
    trends = causal_trend_series(ctx_bars)
    return ctx_bars.select("close_time").with_columns(
        pl.Series("trend", trends, dtype=pl.Utf8)
    )


def build_context(df: pl.DataFrame, tfset: TimeframeSet) -> pl.DataFrame:
    """Attach causally aligned MTF/HTF structural-trend context.

    Adds a struct column ``htf_data`` with keys ``htf_trend`` and
    ``mtf_trend`` (UP/DOWN/NEUTRAL, or null before enough context exists).
    Alignment is a backward as-of join on the context bar's ``close_time`` and
    is verified by :class:`CausalAligner` (it asserts
    ``context_close_time <= bar timestamp``), so no lookahead can leak in.
    """
    if "htf_data" in df.columns:
        return df

    out = df
    for prefix, period in (
        ("mtf_", tfset.mtf_period),
        ("htf_", tfset.htf_period),
    ):
        ctx = _trend_context_frame(df, period)
        if ctx.height > 0:
            out = CausalAligner.align_higher_timeframe(out, ctx, prefix=prefix)

    for col in ("htf_trend", "mtf_trend"):
        if col not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.Utf8).alias(col))

    return out.with_columns(pl.struct(["htf_trend", "mtf_trend"]).alias("htf_data"))


# ---------------------------------------------------------------------------
# Experiment identity and hard gate evaluation
# ---------------------------------------------------------------------------
def make_experiment_id(
    tfset: TimeframeSet,
    pair_symbol: str,
    variant: ModelVariant,
    parameters: Dict[str, Any],
) -> str:
    """Deterministic unique ID for one permutation (set x pair x variant x params)."""
    payload = json.dumps(
        {
            "set": tfset.key,
            "frames": [tfset.htf_label, tfset.mtf_label, tfset.ltf_label],
            "pair": pair_symbol,
            "variant": variant.key,
            "params": parameters,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10].upper()
    return f"EXP-{tfset.key}-{pair_symbol}-{variant.key}-{digest}"


def evaluate_gates(
    *,
    pair: CurrencyPair,
    is_result: BacktestResult,
    val_result: BacktestResult,
    oos_result: BacktestResult,
    full_result: BacktestResult,
    stress_result: Optional[BacktestResult] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """Apply the four hard elimination filters (auto-pruning).

    Returns ``(gate_report, rejected_by)`` where ``rejected_by`` is the FIRST
    failing gate in spec order (a -> b -> c -> d), or None when all gates pass.
    Gate D is only evaluated once A/B/C pass (cost-stress re-run of the top
    candidates); otherwise it is reported as ``NOT_EVALUATED``.
    """
    gates: Dict[str, Dict[str, Any]] = {}

    # (a) Statistical significance: >= 80 trades over the tested window.
    total_trades = full_result.total_trades
    gates[GATE_A_STATISTICAL_SIGNIFICANCE] = {
        "passed": total_trades >= GATE_A_MIN_TRADES,
        "min_trades": GATE_A_MIN_TRADES,
        "observed_trades": total_trades,
        "evaluated_on": "FULL_WINDOW",
    }

    # (b) Asymmetry & expectancy: IS-only. OOS never tunes or gates selection.
    avg_win_r = average_winning_r(is_result)
    net_exp_r = mean_expectancy_r(is_result)
    gate_b_passed = (
        avg_win_r is not None
        and avg_win_r >= GATE_B_MIN_AVG_WIN_R
        and net_exp_r is not None
        and net_exp_r >= GATE_B_MIN_NET_EXPECTANCY_R
    )
    gates[GATE_B_ASYMMETRY_EXPECTANCY] = {
        "passed": gate_b_passed,
        "min_avg_win_r": GATE_B_MIN_AVG_WIN_R,
        "min_net_expectancy_r": GATE_B_MIN_NET_EXPECTANCY_R,
        "observed_avg_win_r": _round(avg_win_r),
        "observed_net_expectancy_r": _round(net_exp_r),
        "evaluated_on": GATE_B_EVALUATED_ON,
    }

    # (c) Walk-forward efficiency: OOS annualized / IS annualized >= 0.50.
    is_ann = annualized_return(is_result)
    oos_ann = annualized_return(oos_result)
    wfe = walk_forward_efficiency(is_ann, oos_ann)
    gate_c_passed = wfe is not None and wfe >= GATE_C_MIN_WFE
    gates[GATE_C_WALK_FORWARD_EFFICIENCY] = {
        "passed": gate_c_passed,
        "min_efficiency": GATE_C_MIN_WFE,
        "observed_efficiency": _round(wfe),
        "is_annualized_return": _round(is_ann),
        "oos_annualized_return": _round(oos_ann),
        "evaluated_on": "IS_vs_OOS",
    }

    # (d) Cost stress: only reached for candidates that cleared A/B/C.
    abc_passed = all(gates[g]["passed"] for g in GATE_ORDER[:3])
    if abc_passed:
        stress_exp = (
            cost_stress_expectancy_r(stress_result, pair)
            if stress_result is not None
            else None
        )
        gate_d_passed = stress_exp is not None and stress_exp >= 0.0
        gate_d_status = "EVALUATED"
    else:
        stress_exp = None
        gate_d_passed = None
        gate_d_status = "NOT_EVALUATED"
    gates[GATE_D_COST_STRESS] = {
        "passed": gate_d_passed,
        "status": gate_d_status,
        "spread_multiplier": COST_STRESS_SPREAD_MULTIPLIER,
        "slippage_pips": float(COST_STRESS_SLIPPAGE_PIPS),
        "min_net_expectancy_r": 0.0,
        "observed_stress_expectancy_r": _round(stress_exp),
        "evaluated_on": GATE_D_EVALUATED_ON,
    }

    rejected_by: Optional[str] = None
    for gate_name in GATE_ORDER:
        if gates[gate_name]["passed"] is False:
            rejected_by = gate_name
            break
    return gates, rejected_by


# ---------------------------------------------------------------------------
# Experiment runner (IS is always evaluated first; OOS is measured blind)
# ---------------------------------------------------------------------------
def _summarize_result(result: BacktestResult) -> Dict[str, Any]:
    return {
        "trades": result.total_trades,
        "net_profit": _round(float(result.total_net_pnl), 2),
        "max_drawdown": _round(float(result.max_drawdown_pct), 4),
        "expectancy_r": _round(mean_expectancy_r(result)),
        "avg_win_r": _round(average_winning_r(result)),
        "annualized_return": _round(annualized_return(result)),
    }


def run_experiment(
    tfset: TimeframeSet,
    symbol: str,
    variant: ModelVariant,
    dataset: Dataset,
) -> Dict[str, Any]:
    """Execute one permutation (timeframe set x pair x model variant).

    Sequence is deliberate: partition -> IS backtest FIRST -> VAL -> blind OOS
    -> full-window -> gates. The cost-stress re-run (gate d) only executes for
    candidates that already cleared gates a/b/c. Nothing in this function feeds
    OOS observations back into parameter selection (baseline parameters are
    fixed library defaults for every slice).
    """
    pair = CurrencyPair.from_symbol(symbol)
    experiment_id = make_experiment_id(tfset, symbol, variant, variant.params)

    is_df, val_df, oos_df = partition_is_val_oos(dataset.df)
    boundaries = {
        "is": {
            "start": str(is_df["timestamp"][0]),
            "end": str(is_df["timestamp"][-1]),
            "bars": is_df.height,
        },
        "val": {
            "start": str(val_df["timestamp"][0]),
            "end": str(val_df["timestamp"][-1]),
            "bars": val_df.height,
        },
        "oos": {
            "start": str(oos_df["timestamp"][0]),
            "end": str(oos_df["timestamp"][-1]),
            "bars": oos_df.height,
        },
    }

    def _execute(frame: pl.DataFrame, cost_multiplier: float) -> BacktestResult:
        strategy = variant.build(
            strategy_id=f"{tfset.key}_{variant.key}_{symbol}".lower(), symbol=symbol
        )
        tester = EventDrivenBacktester(
            strategy=strategy,
            currency_pair=pair,
            cost_multiplier=cost_multiplier,
        )
        return tester.run(frame, timeframe=tfset.ltf_timeframe)

    record: Dict[str, Any] = {
        "experiment_id": experiment_id,
        "sweep_run_id": None,  # stamped by the sweep driver
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "timeframe_set": tfset.key,
        "timeframe_chain": f"{tfset.htf_label}->{tfset.mtf_label}->{tfset.ltf_label}",
        "pair": symbol,
        "model_variant": variant.key,
        "parameters": dict(variant.params),
        "data_provenance": dataset.provenance,
        "data_source": dataset.source,
        "bars_total": dataset.df.height,
        "splits": {"ratios": dict(SPLIT_RATIOS), "boundaries": boundaries},
        "phase_sequence": ["IS", "VAL", "OOS_BLIND", "FULL", "COST_STRESS_IF_TOP"],
    }

    try:
        # 1. IN-SAMPLE FIRST (the only slice a tuner may ever observe).
        is_result = _execute(is_df, cost_multiplier=1.0)
        record["is"] = _summarize_result(is_result)

        # 2. Validation sanity window (no tuning performed).
        val_result = _execute(val_df, cost_multiplier=1.0)
        record["val"] = _summarize_result(val_result)

        # 3. BLIND OOS holdout - measured, never tuned on.
        oos_result = _execute(oos_df, cost_multiplier=1.0)
        record["oos"] = _summarize_result(oos_result)

        # 4. Full-window result (basis for the trade-count gate and Max DD).
        full_result = _execute(dataset.df, cost_multiplier=1.0)
        record["full_window"] = _summarize_result(full_result)

        # 5. Hard elimination filters. Cheap gates first; the cost-stress
        #    re-run only happens for top candidates (gates a/b/c passed).
        stress_result: Optional[BacktestResult] = None
        abc_probe = evaluate_gates(
            pair=pair,
            is_result=is_result,
            val_result=val_result,
            oos_result=oos_result,
            full_result=full_result,
            stress_result=None,
        )
        if abc_probe[1] is None:
            stress_result = _execute(dataset.df, cost_multiplier=2.0)

        gates, rejected_by = evaluate_gates(
            pair=pair,
            is_result=is_result,
            val_result=val_result,
            oos_result=oos_result,
            full_result=full_result,
            stress_result=stress_result,
        )

        record["gates"] = gates
        record["rejected_by"] = rejected_by
        record["verdict"] = VERDICT_SURVIVED if rejected_by is None else VERDICT_REJECTED
        record["cost_stress"] = {
            "executed": stress_result is not None,
            "spread_multiplier": COST_STRESS_SPREAD_MULTIPLIER,
            "slippage_pips": float(COST_STRESS_SLIPPAGE_PIPS),
            "expectancy_r": _round(
                cost_stress_expectancy_r(stress_result, pair)
                if stress_result is not None
                else None
            ),
            "trades": stress_result.total_trades if stress_result else 0,
        }
        record["metrics"] = {
            "is_expectancy_r": _round(mean_expectancy_r(is_result)),
            "val_expectancy_r": _round(mean_expectancy_r(val_result)),
            "oos_expectancy_r": _round(mean_expectancy_r(oos_result)),
            "full_expectancy_r": _round(mean_expectancy_r(full_result)),
            "total_trades": full_result.total_trades,
            "max_drawdown_pct": _round(float(full_result.max_drawdown_pct), 4),
            "net_surviving_r": (
                _round(mean_expectancy_r(full_result))
                if record["verdict"] == VERDICT_SURVIVED
                else None
            ),
        }
    except Exception as exc:  # keep the sweep alive; record the failure
        logger.exception("Experiment %s failed", experiment_id)
        record["verdict"] = VERDICT_ERROR
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["rejected_by"] = f"ERROR:{type(exc).__name__}"

    return record


# ---------------------------------------------------------------------------
# Experiment store: research/results/experiments.json
# ---------------------------------------------------------------------------
def load_experiments(path: Path) -> Dict[str, Any]:
    """Load the experiment store, tolerating a missing or empty file."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt experiment store at %s; starting fresh", path)
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def upsert_experiments(
    path: Path,
    records: List[Dict[str, Any]],
    sweep_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist records under their unique experiment IDs (merge, never clobber)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    store = load_experiments(path)
    store.setdefault("schema_version", "1.0")
    store.setdefault("split_ratios", dict(SPLIT_RATIOS))
    store["gates"] = gates_config()
    experiments = store.get("experiments")
    if not isinstance(experiments, dict):
        experiments = {}
        store["experiments"] = experiments

    for record in records:
        key = str(record["experiment_id"])
        experiments[key] = record
    store["last_sweep"] = sweep_summary
    store["experiment_count"] = len(experiments)
    store["survivor_count"] = sum(
        1
        for exp in experiments.values()
        if isinstance(exp, dict) and exp.get("verdict") == VERDICT_SURVIVED
    )

    path.write_text(
        json.dumps(store, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return store


def surviving_records(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All experiment records that passed all four hard gates."""
    experiments = store.get("experiments") or {}
    survivors = [
        exp
        for exp in experiments.values()
        if isinstance(exp, dict) and exp.get("verdict") == VERDICT_SURVIVED
    ]
    survivors.sort(
        key=lambda r: (
            r.get("timeframe_set", ""),
            r.get("pair", ""),
            r.get("model_variant", ""),
        )
    )
    return survivors


def _fmt_expectancy(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.3f}R"


def _fmt_dd(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def render_surviving_edges_md(store: Dict[str, Any]) -> str:
    """Render ``research/SURVIVING_EDGES.md`` for models passing ALL gates."""
    survivors = surviving_records(store)
    last_sweep = store.get("last_sweep") or {}
    generated = last_sweep.get("completed_utc") or datetime.now(timezone.utc).isoformat()

    lines: List[str] = [
        "# Surviving Edges",
        "",
        "Only Market Model variants that passed **all four hard elimination gates**",
        "are listed here. Anything rejected by any gate is auto-pruned.",
        "",
        f"- Generated: {generated}",
        f"- Sweep run: `{last_sweep.get('sweep_run_id', 'n/a')}`",
        f"- Experiments evaluated: {store.get('experiment_count', 0)}",
        f"- Survivors: {len(survivors)}",
        "",
        "## Gates",
        "",
        "| # | Gate | Criterion | Evaluated on |",
        "|---|------|-----------|--------------|",
        f"| a | Statistical Significance | Trade count >= {GATE_A_MIN_TRADES} trades | Full tested window (IS+VAL+OOS) |",
        f"| b | Asymmetry & Expectancy | Avg winner >= {GATE_B_MIN_AVG_WIN_R:.1f}R and Net Expectancy >= {GATE_B_MIN_NET_EXPECTANCY_R:+.2f}R | In-Sample (first 60%) |",
        f"| c | Walk-Forward Efficiency | OOS annualized / IS annualized >= {GATE_C_MIN_WFE:.2f} | IS vs blind OOS |",
        f"| d | Cost Stress | 2x spread + {COST_STRESS_SLIPPAGE_PIPS} pips slippage keeps Net Expectancy >= 0 | Full window re-run (top candidates only) |",
        "",
        "## Surviving Models",
        "",
        "| Timeframe Set | Pair | Model Variant | IS Expectancy | OOS Expectancy | Max DD | Net Surviving R |",
        "|---------------|------|---------------|---------------|----------------|--------|-----------------|",
    ]

    for rec in survivors:
        metrics = rec.get("metrics") or {}
        lines.append(
            "| {set} | {pair} | {variant} | {is_exp} | {oos_exp} | {dd} | {net} |".format(
                set=f"{rec.get('timeframe_set', '?')} ({rec.get('timeframe_chain', '?')})",
                pair=rec.get("pair", "?"),
                variant=rec.get("model_variant", "?"),
                is_exp=_fmt_expectancy(metrics.get("is_expectancy_r")),
                oos_exp=_fmt_expectancy(metrics.get("oos_expectancy_r")),
                dd=_fmt_dd(metrics.get("max_drawdown_pct")),
                net=_fmt_expectancy(metrics.get("net_surviving_r")),
            )
        )

    if not survivors:
        lines.append("| _(no configuration survived all four gates)_ | | | | | | |")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Timeline split: "
            f"{SPLIT_RATIOS['is']:.0%} In-Sample / {SPLIT_RATIOS['val']:.0%} Validation / "
            f"{SPLIT_RATIOS['oos']:.0%} blind Out-of-Sample (chronological, never shuffled).",
            "- Parameters are evaluated on In-Sample first and are **never tuned on OOS**.",
            "- `IS Expectancy` / `OOS Expectancy` = mean R per trade over each slice.",
            "- `Max DD` = maximum drawdown (equity %) over the full tested window.",
            "- `Net Surviving R` = full-window net expectancy (mean R) after all gates.",
            "- Full per-experiment logs: `research/results/experiments.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def write_surviving_edges(path: Path, store: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_surviving_edges_md(store), encoding="utf-8")


# ---------------------------------------------------------------------------
# Data acquisition (cache-first, synthetic fallback, provenance-labelled)
# ---------------------------------------------------------------------------
def _clean_bars(df: pl.DataFrame, provenance_value: str) -> pl.DataFrame:
    """Sort, de-duplicate, and drop weekend rows from synthetic histories."""
    df = df.sort("timestamp").unique(subset=["timestamp"], maintain_order=True)
    if provenance_value == DataProvenance.SYNTHETIC.value:
        # Mirror the discovery pipeline: drop Sat/Sun before backtesting.
        df = df.filter(~pl.col("timestamp").dt.weekday().is_in([6, 7]))
    return df


class ResearchSweeper:
    """Automated sweep driver across timeframe sets x pairs x model variants."""

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        set_keys: Optional[List[str]] = None,
        variant_keys: Optional[List[str]] = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        results_path: Path = DEFAULT_RESULTS_PATH,
        edges_path: Path = DEFAULT_EDGES_PATH,
        allow_synthetic: bool = True,
        require_real_data: bool = False,
    ) -> None:
        self.symbols = [
            s.upper().replace("/", "").replace("_", "").replace("-", "")
            for s in (symbols or ["EURUSD", "GBPUSD", "USDJPY"])
        ]
        self.set_keys = list(set_keys or TIMEFRAME_SETS.keys())
        self.variant_keys = list(variant_keys or VARIANTS.keys())

        unknown_sets = [k for k in self.set_keys if k not in TIMEFRAME_SETS]
        if unknown_sets:
            raise ValueError(f"Unknown timeframe sets: {unknown_sets}")
        unknown_variants = [k for k in self.variant_keys if k not in VARIANTS]
        if unknown_variants:
            raise ValueError(f"Unknown model variants: {unknown_variants}")

        self.cache_dir = Path(cache_dir)
        self.results_path = Path(results_path)
        self.edges_path = Path(edges_path)
        self.allow_synthetic = allow_synthetic
        self.require_real_data = require_real_data
        self._base_cache: Dict[Tuple[str, str], Tuple[pl.DataFrame, str, str]] = {}

    def acquire_base(
        self, symbol: str, timeframe: Timeframe, min_bars: int
    ) -> Tuple[pl.DataFrame, str, str]:
        """Return (bars, provenance, source) at the generation timeframe."""
        cache_key = (symbol, timeframe.value)
        cached = self._base_cache.get(cache_key)
        if cached is not None:
            return cached

        df: Optional[pl.DataFrame] = None
        provenance = DataProvenance.UNKNOWN
        source = "unknown"

        cached_path = HistoricalECNFetcher.get_cache_path(
            symbol, timeframe, self.cache_dir
        )
        if cached_path.exists():
            try:
                loaded = pl.read_parquet(cached_path)
                prov, src = HistoricalECNFetcher.load_provenance(
                    symbol, timeframe, self.cache_dir
                )
                prov_value = (
                    prov.value if isinstance(prov, DataProvenance) else str(prov)
                )
                need = max(MIN_BARS_FOR_EXPERIMENT * 5, min_bars // 2)
                if loaded.height >= need:
                    df = loaded
                    provenance = DataProvenance(prov_value)
                    source = src
                elif prov_value == DataProvenance.SYNTHETIC.value:
                    logger.info(
                        "Cache for %s %s too short (%d < %d); regenerating",
                        symbol, timeframe.value, loaded.height, need,
                    )
                else:
                    # Never overwrite real data; work with what exists.
                    logger.warning(
                        "Real cache for %s %s is short (%d bars); using as-is",
                        symbol, timeframe.value, loaded.height,
                    )
                    df = loaded
                    provenance = DataProvenance(prov_value)
                    source = src
            except Exception as exc:
                logger.warning("Failed reading cached parquet %s: %s", cached_path, exc)
                df = None

        if df is None:
            if not self.allow_synthetic:
                raise RuntimeError(
                    f"No usable history for {symbol} {timeframe.value} in "
                    f"{self.cache_dir} and synthetic fallback is disabled."
                )
            logger.info(
                "Synthesizing deterministic ECN history for %s %s (%d bars)",
                symbol, timeframe.value, min_bars,
            )
            df = HistoricalECNFetcher.generate_synthetic_ecn_history(
                symbol=symbol, timeframe=timeframe, num_bars=min_bars
            )
            provenance = DataProvenance.SYNTHETIC
            source = "deterministic synthetic ECN generator"
            HistoricalECNFetcher.cache_to_parquet(
                df, symbol, timeframe, self.cache_dir,
                provenance=provenance, source=source,
            )

        df = _clean_bars(df, provenance.value)
        if self.require_real_data and provenance not in (
            DataProvenance.REAL_VENDOR,
            DataProvenance.BROKER_EXPORT,
        ):
            raise RuntimeError(
                f"Real historical data is required for {symbol}; refusing dataset "
                f"classified as {provenance.value}."
            )

        result = (df, provenance.value, source)
        self._base_cache[cache_key] = result
        return result

    def acquire_dataset(self, tfset: TimeframeSet, symbol: str) -> Dataset:
        """Bars for one (set, symbol) at the set's LTF execution timeframe."""
        df, provenance, source = self.acquire_base(
            symbol, tfset.gen_timeframe, tfset.gen_bars
        )
        if tfset.needs_downsample:
            ltf = downsample_period(df, tfset.ltf_period)
            # The final window is partial after downsampling; drop it.
            if ltf.height > 1:
                ltf = ltf.slice(0, ltf.height - 1)
            df = ltf.select(
                ["timestamp", "open", "high", "low", "close", "volume", "spread"]
            )
        if df.height < MIN_BARS_FOR_EXPERIMENT:
            raise RuntimeError(
                f"{symbol} {tfset.key}: only {df.height} LTF bars "
                f"(need >= {MIN_BARS_FOR_EXPERIMENT})."
            )
        return Dataset(df=df, provenance=provenance, source=source)

    def run(
        self,
        datasets: Optional[Dict[Tuple[str, str], Dataset]] = None,
    ) -> Dict[str, Any]:
        """Execute the sweep and persist both deliverables.

        ``datasets`` may inject pre-built ``(set_key, symbol) -> Dataset``
        frames (used by tests to run without touching the data cache). All
        injected frames must already contain the ``htf_data`` context column.
        """
        sweep_run_id = datetime.now(timezone.utc).strftime("SWEEP-%Y%m%dT%H%M%SZ")
        started = datetime.now(timezone.utc)
        records: List[Dict[str, Any]] = []
        provenance_by_pair: Dict[str, str] = {}

        for set_key in self.set_keys:
            tfset = TIMEFRAME_SETS[set_key]
            for symbol in self.symbols:
                if datasets is not None:
                    dataset = datasets[(set_key, symbol)]
                else:
                    raw = self.acquire_dataset(tfset, symbol)
                    dataset = Dataset(
                        df=build_context(raw.df, tfset),
                        provenance=raw.provenance,
                        source=raw.source,
                    )
                provenance_by_pair[f"{set_key}/{symbol}"] = dataset.provenance

                for variant_key in self.variant_keys:
                    variant = VARIANTS[variant_key]
                    record = run_experiment(tfset, symbol, variant, dataset)
                    record["sweep_run_id"] = sweep_run_id
                    records.append(record)
                    logger.info(
                        "%s %s %s -> %s%s",
                        set_key,
                        symbol,
                        variant_key,
                        record["verdict"],
                        f" (rejected by {record['rejected_by']})"
                        if record.get("rejected_by")
                        else "",
                    )

        summary = {
            "sweep_run_id": sweep_run_id,
            "started_utc": started.isoformat(),
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "symbols": self.symbols,
            "timeframe_sets": self.set_keys,
            "variants": self.variant_keys,
            "split_ratios": dict(SPLIT_RATIOS),
            "gates": gates_config(),
            "experiments_run": len(records),
            "survived": sum(1 for r in records if r["verdict"] == VERDICT_SURVIVED),
            "rejected": sum(1 for r in records if r["verdict"] == VERDICT_REJECTED),
            "errors": sum(1 for r in records if r["verdict"] == VERDICT_ERROR),
            "provenance_by_pair": provenance_by_pair,
        }

        store = upsert_experiments(self.results_path, records, summary)
        write_surviving_edges(self.edges_path, store)

        return {
            "sweep": summary,
            "records": records,
            "store": store,
            "results_path": str(self.results_path),
            "edges_path": str(self.edges_path),
        }

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Automated Market Model research sweep & robustness filter: "
            "runs baseline variants across the 5 canonical timeframe sets, "
            "applies the four hard elimination gates, and writes "
            "research/results/experiments.json + research/SURVIVING_EDGES.md."
        )
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Pairs to sweep (default: EURUSD GBPUSD USDJPY)",
    )
    parser.add_argument(
        "--sets", nargs="+", choices=sorted(TIMEFRAME_SETS), default=None,
        help="Timeframe sets to run (default: all five)",
    )
    parser.add_argument(
        "--variants", nargs="+", choices=sorted(VARIANTS), default=None,
        help="Model variants to run (default: all baselines)",
    )
    parser.add_argument(
        "--results-path", type=Path, default=DEFAULT_RESULTS_PATH,
        help="Experiment log destination (default: research/results/experiments.json)",
    )
    parser.add_argument(
        "--edges-path", type=Path, default=DEFAULT_EDGES_PATH,
        help="Survivor table destination (default: research/SURVIVING_EDGES.md)",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
        help="Parquet history cache directory (default: data/cache)",
    )
    parser.add_argument(
        "--no-synthetic", action="store_true",
        help="Fail instead of synthesizing history that is missing from the cache",
    )
    parser.add_argument(
        "--require-real-data", action="store_true",
        help="Refuse any dataset that is not REAL_VENDOR/BROKER_EXPORT provenance",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug-level logging",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    sweeper = ResearchSweeper(
        symbols=args.symbols,
        set_keys=args.sets,
        variant_keys=args.variants,
        cache_dir=args.cache_dir,
        results_path=args.results_path,
        edges_path=args.edges_path,
        allow_synthetic=not args.no_synthetic,
        require_real_data=args.require_real_data,
    )

    try:
        outcome = sweeper.run()
    except Exception as exc:
        logger.error("Sweep aborted: %s", exc)
        return 1

    summary = outcome["sweep"]
    print("")
    print("=" * 72)
    print(f"  RESEARCH SWEEP COMPLETE  ({summary['sweep_run_id']})")
    print("=" * 72)
    print(f"  Sets      : {', '.join(summary['timeframe_sets'])}")
    print(f"  Symbols   : {', '.join(summary['symbols'])}")
    print(f"  Variants  : {', '.join(summary['variants'])}")
    print(
        f"  Results   : {summary['experiments_run']} run | "
        f"{summary['survived']} survived | "
        f"{summary['rejected']} rejected | {summary['errors']} errors"
    )
    print(f"  Log       : {outcome['results_path']}")
    print(f"  Survivors : {outcome['edges_path']}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())













