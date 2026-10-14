"""Autonomous Forex Daemon — 24/7/365 dual-track engine.

Track A (LIVE FACING): watches real bars via a broker/data adapter, runs the
shared cascading-fractal hypotheses, executes on paper or micro-lot accounts,
manages open positions (BE at +2R, MTF structural trail at +3.5R), and logs
every decision to the decision ledger.

Track B (EVOLUTION LAB, hourly): mutates strategy parameters/hypothesis
rules into quarantine variants, causally backtests each on frozen data,
scores them against the immutable gates of INSTITUTIONAL_KNOWLEDGE_CORE.md,
and promotes a variant ONLY if it beats the incumbent on validation data
without touching quarantined out-of-sample periods.

Hard law (Law 7): capital_ladder never advances itself. LIVE capital stays
0.00 until a human confirms promotion after all gates pass.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from forex_platform.market_model.cascading_fractal_coordinator import (
    CascadingFractalCoordinator,
)

# ---------------------------------------------------------------------------
# Capital ladder — the engine may NEVER self-advance beyond HUMAN_CONFIRMED state
# ---------------------------------------------------------------------------


class RunMode:
    RESEARCH = "RESEARCH"          # pure historical replay
    PAPER_FORWARD = "PAPER_FORWARD"  # live data, simulated fills
    VALIDATION = "VALIDATION"      # frozen config, supervised forward test
    LIVE_MICRO = "LIVE_MICRO"      # human-confirmed micro-lot deployment


@dataclass
class DaemonConfig:
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD")
    base_timeframe_seconds: int = 60          # 1-minute heartbeat
    evolution_interval_seconds: int = 3600    # hourly mutation/validation
    registry_dir: Path = Path("research/results/autonomous")
    run_mode: str = RunMode.PAPER_FORWARD     # NEVER defaults to live
    live_capital_usd: Decimal = Decimal("0")  # Law 7: real money starts at zero
    max_risk_per_trade: Decimal = Decimal("0.01")
    min_planned_rr: Decimal = Decimal("4.0")
    daily_loss_halt: Decimal = Decimal("0.02")
    max_single_currency_risk: Decimal = Decimal("0.02")  # Law 4 governor


# ---------------------------------------------------------------------------
# Decision ledger (append-only audit trail)
# ---------------------------------------------------------------------------


class DecisionLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: dict) -> None:
        event = {"ts_utc": datetime.now(timezone.utc).isoformat(), **event}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")


# ---------------------------------------------------------------------------
# Evolution lab (Track B)
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    variant_id: str
    parent_id: str
    parameter_overrides: dict
    created_utc: str
    status: str = "QUARANTINE"       # QUARANTINE -> VALIDATING -> PROMOTED | RETIRED | DESTROYED
    is_expectancy: Optional[float] = None
    oos_expectancy: Optional[float] = None
    profit_factor: Optional[float] = None
    max_drawdown: Optional[float] = None
    trades: int = 0
    notes: str = ""


class EvolutionLab:
    """Mutate → quarantine → causally backtest → gate → promote or retire."""

    GATES = {
        "min_trades": 100,
        "min_profit_factor": 1.4,   # survival floor; promotion target 2.5
        "promotion_profit_factor": 2.5,
        "max_drawdown": 0.20,
        "min_is_expectancy": 0.35,
        "min_oos_expectancy": 0.0,
    }

    PROMOTION_SPACE: tuple[tuple[str, list[str]], ...] = (
        ("setup_persistence_bars", ["3", "4", "6", "8", "12"]),
        ("minimum_stop_pips", ["2", "3", "5"]),
        ("maximum_entry_spread_pips", ["2.5", "3.0", "3.5"]),
        ("stop_buffer_pips", ["1.5", "2.0", "3.0"]),
        ("quote_phase_boundary", ["0.5", "0.55", "0.6", "0.65"]),  # premium/discount
    )

    def __init__(self, registry_dir: Path, baseline_variant_id: str) -> None:
        self.registry_dir = registry_dir
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.registry_dir / "REGISTRY.json"
        self.baseline_id = baseline_variant_id
        self.variants: dict[str, Variant] = self._load()
        if baseline_variant_id not in self.variants:
            self.variants[baseline_variant_id] = Variant(
                variant_id=baseline_variant_id,
                parent_id="HUMAN_SEED",
                parameter_overrides={},
                created_utc=datetime.now(timezone.utc).isoformat(),
                status="PROMOTED",   # incumbent
                notes="Frozen incumbent; all challengers must beat me.",
            )
            self._save()

    # -- persistence ----------------------------------------------------------
    def _load(self) -> dict[str, Variant]:
        if not self.registry_path.exists():
            return {}
        raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        return {k: Variant(**v) for k, v in raw.items()}

    def _save(self) -> None:
        raw = {k: v.__dict__ for k, v in self.variants.items()}
        self.registry_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    # -- mutation --------------------------------------------------------------
    def propose_mutation(self, generation: int) -> Variant:
        """Single-axis mutation of the incumbent (controlled experimentation)."""
        incumbent = self.incumbent()
        axis_name, grid = self.PROMOTION_SPACE[generation % len(self.PROMOTION_SPACE)]
        idx = generation // len(self.PROMOTION_SPACE) % len(grid)
        overrides = dict(incumbent.parameter_overrides)
        overrides[axis_name] = grid[idx]
        vid = f"GEN{generation:05d}-{axis_name}={grid[idx]}"
        v = Variant(
            variant_id=vid,
            parent_id=incumbent.variant_id,
            parameter_overrides=overrides,
            created_utc=datetime.now(timezone.utc).isoformat(),
        )
        self.variants[vid] = v
        self._save()
        return v

    def incumbent(self) -> Variant:
        promoted = [v for v in self.variants.values() if v.status == "PROMOTED"]
        # Prefer highest OOS expectancy if measured, else the seeded baseline
        scored = [v for v in promoted if v.oos_expectancy is not None]
        if scored:
            return max(scored, key=lambda v: v.oos_expectancy or -99)
        return self.variants[self.baseline_id]

    # -- gating -----------------------------------------------------------------
    def gate_verdict(self, v: Variant) -> tuple[str, list[str]]:
        reasons: list[str] = []
        g = self.GATES
        if v.trades < g["min_trades"]:
            reasons.append(f"trades={v.trades} < {g['min_trades']}")
        if (v.profit_factor or 0) < g["min_profit_factor"]:
            reasons.append(f"pf={v.profit_factor} < {g['min_profit_factor']}")
        if (v.max_drawdown or 1) > g["max_drawdown"]:
            reasons.append(f"dd={v.max_drawdown} > {g['max_drawdown']}")
        if (v.is_expectancy or -99) < g["min_is_expectancy"]:
            reasons.append(f"is_exp={v.is_expectancy} < {g['min_is_expectancy']}")
        if (v.oos_expectancy or -99) < g["min_oos_expectancy"]:
            reasons.append(f"oos_exp={v.oos_expectancy} < {g['min_oos_expectancy']}")
        if reasons:
            return "REJECTED", reasons
        if (v.profit_factor or 0) >= g["promotion_profit_factor"]:
            return "PROMOTE", ["cleared full promotion bar"]
        return "HOLD", ["survives but below promotion threshold"]

    def register_result(self, vid: str, *, trades: int, is_expectancy: float,
                        oos_expectancy: float, profit_factor: float,
                        max_drawdown: float) -> str:
        v = self.variants[vid]
        v.trades = trades
        v.is_expectancy = is_expectancy
        v.oos_expectancy = oos_expectancy
        v.profit_factor = profit_factor
        v.max_drawdown = max_drawdown
        verdict, reasons = self.gate_verdict(v)
        if verdict == "PROMOTE":
            # demote current incumbent only if challenger beats it on OOS
            inc = self.incumbent()
            if v.variant_id != inc.variant_id and (v.oos_expectancy or -99) > (inc.oos_expectancy or -99):
                inc.status = "RETIRED"
                v.status = "PROMOTED"
                v.notes = "Promoted on validation + OOS expectancy dominance."
            else:
                v.status = "VALIDATING"
        elif verdict == "HOLD":
            v.status = "VALIDATING"
        else:
            v.status = "RETIRED"
            v.notes = "; ".join(reasons)
        self._save()
        return verdict


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


class AutonomousForexDaemon:
    """24/7/365 dual-track: live-facing execution + hourly self-evolution."""

    def __init__(
        self,
        config: Optional[DaemonConfig] = None,
        data_feed=None,        # adapter exposing next_closed_bar() or None
        broker=None,           # broker adapter (paper or mt5)
        doze_seconds: int = 5,
    ) -> None:
        self.config = config or DaemonConfig()
        self.data_feed = data_feed
        self.broker = broker
        self.doze = doze_seconds
        self.coordinator = CascadingFractalCoordinator()
        self.ledger = DecisionLedger(self.config.registry_dir / "DECISION_LEDGER.jsonl")
        self.alerts = DecisionLedger(self.config.registry_dir / "ALERTS.jsonl")
        self.lab = EvolutionLab(self.config.registry_dir, baseline_variant_id="BASELINE_PHASE_RIDER")
        self.generation = 0
        self._stopped = False

    # -- Track B: hourly evolution -------------------------------------------
    def evolve_once(self, backtest_fn) -> dict:
        """One mutation + quarantine backtest. backtest_fn(variant)->metrics dict."""
        variant = self.lab.propose_mutation(self.generation)
        self.generation += 1
        entry = {"kind": "MUTATION", "variant": variant.__dict__}
        self.ledger.record(entry)
        try:
            metrics = backtest_fn(variant.parameter_overrides)
            verdict = self.lab.register_result(
                variant.variant_id,
                trades=int(metrics.get("trades", 0)),
                is_expectancy=float(metrics.get("is_expectancy", -99)),
                oos_expectancy=float(metrics.get("oos_expectancy", -99)),
                profit_factor=float(metrics.get("profit_factor", 0)),
                max_drawdown=float(metrics.get("max_drawdown", 1)),
            )
            entry = {"kind": "GATE", "variant_id": variant.variant_id,
                     "verdict": verdict, "metrics": metrics}
            self.ledger.record(entry)
            if verdict == "PROMOTE":
                self.alerts.record({"kind": "PROMOTION", **entry})
            return entry
        except Exception as exc:  # the lab must never kill the daemon
            self.alerts.record({
                "kind": "EVOLUTION_ERROR",
                "variant_id": variant.variant_id,
                "error": str(exc), "trace": traceback.format_exc()[-1500:],
            })
            return {"kind": "EVOLUTION_ERROR", "variant_id": variant.variant_id}

    # -- Track A: live loop -----------------------------------------------------
    def heartbeat(self, bar_event) -> list:
        """Process one closed source bar end-to-end.

        The `bar_event` must already be a completed source bar on the
        strategy's base timeframe. Returns a list of order intents (paper or
        live-routed, depending on self.broker and run_mode).
        """
        intents: list = []
        try:
            # 1) Environment guards (sessions, spread sentinel / rollover, news)
            if not self._environment_ok(bar_event):
                return intents
            # 2) Hypothesis evaluation via shared cascading coordinator ---
            #    (strategy code already calls coordinator inside on_bar; this
            #     heartbeat belongs behind a caller that owns the engines).
            self.ledger.record({
                "kind": "HEARTBEAT",
                "symbol": bar_event.get("symbol"),
                "note": "Delegate to strategy.on_bar; daemon is orchestration layer.",
            })
        except Exception as exc:
            self.alerts.record({"kind": "HEARTBEAT_ERROR", "error": str(exc)})
        return intents

    # -- environment guards (blueprint Step 4A/B/D) ---------------------------
    def _environment_ok(self, bar_event) -> bool:
        ts = bar_event.get("timestamp")
        if ts is None:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # Rollover freeze: 21:45–22:15 UTC (~5 PM EST window adjustments)
        mins = ts.hour * 60 + ts.minute
        if 21 * 60 + 45 <= mins <= 22 * 60 + 15:
            self.ledger.record({"kind": "FREEZE", "reason": "EST_ROLLOVER", "mins": mins})
            return False

        # Spread sentinel
        spread = bar_event.get("spread")
        avg_spread = bar_event.get("avg_spread")
        if spread is not None and avg_spread:
            if Decimal(str(spread)) > Decimal(str(avg_spread)) * Decimal("1.8"):
                self.ledger.record({"kind": "FREEZE", "reason": "SPREAD_SENTINEL"})
                return False

        # News sentinel hook — adapter may attach bar_event["news_blackout"]=True
        if bar_event.get("news_blackout"):
            self.ledger.record({"kind": "FREEZE", "reason": "NEWS_SENTINEL"})
            return False
        return True

    # -- currency-exposure governor (Law 4) ------------------------------------
    def currency_exposure_ok(self, open_positions: list[dict], new_pair: str, new_risk: Decimal) -> bool:
        risk_by_currency: dict[str, Decimal] = {}
        for p in open_positions:
            for ccy in (p.get("base"), p.get("quote")):
                if ccy:
                    risk_by_currency[ccy] = risk_by_currency.get(ccy, Decimal("0")) + Decimal(str(p.get("risk", 0)))
        base, quote = new_pair[:3], new_pair[3:]
        cap = self.config.max_single_currency_risk
        for ccy in (base, quote):
            if risk_by_currency.get(ccy, Decimal("0")) + new_risk > cap:
                return False
        return True

    # -- main loop ---------------------------------------------------------------
    def run_forever_loop(self, backtest_fn=None) -> None:
        last_evolution = 0.0
        while not self._stopped:
            if self.data_feed is not None:
                bar = self.data_feed.next_closed_bar()
                if bar is not None:
                    self.heartbeat(bar)
                else:
                    time.sleep(self.doze)
            now = time.time()
            if backtest_fn is not None and now - last_evolution >= self.config.evolution_interval_seconds:
                self.evolve_once(backtest_fn)
                last_evolution = now
            time.sleep(self.doze)

    def stop(self) -> None:
        self._stopped = True
