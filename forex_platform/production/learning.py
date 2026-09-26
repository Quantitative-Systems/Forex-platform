"""
Governed self-improvement pipeline.

The platform improves by running offline research, registering candidates with
data fingerprints, qualifying them against explicit gates, and promoting them
through fixed stages. Promotion into a real-money stage always requires a
human operator; the pipeline cannot move a strategy to live capital by itself.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import to_decimal
from forex_platform.production.observability import MetricsRegistry, get_logger
from forex_platform.production.settings import Settings
from forex_platform.production.store import AuditLog, ResearchRepository, StateStore, utc_now

logger = get_logger(__name__)


class CandidateStage(str, Enum):
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    LIMITED_LIVE = "LIMITED_LIVE"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


TERMINAL_STAGES = {CandidateStage.REJECTED, CandidateStage.RETIRED}

_ALLOWED_TRANSITIONS: Dict[CandidateStage, set] = {
    CandidateStage.CANDIDATE: {CandidateStage.VALIDATED, CandidateStage.REJECTED},
    CandidateStage.VALIDATED: {CandidateStage.PAPER, CandidateStage.REJECTED},
    CandidateStage.PAPER: {CandidateStage.LIMITED_LIVE, CandidateStage.RETIRED},
    CandidateStage.LIMITED_LIVE: {CandidateStage.RETIRED, CandidateStage.PAPER},
    CandidateStage.REJECTED: set(),
    CandidateStage.RETIRED: set(),
}


class CandidateMetrics(BaseModel):
    """Out-of-sample evidence attached to a candidate."""

    model_config = ConfigDict(frozen=True)

    oos_trades: int = 0
    oos_sharpe: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("100")
    net_return_pct: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    win_rate_pct: Decimal = Decimal("0")
    data_fingerprint: str = ""
    notes: Dict[str, Any] = Field(default_factory=dict)


class LearningPipeline:
    """Candidate qualification, promotion, champion tracking, and drift control."""

    CHAMPION_KEY = "learning.champions"
    DRIFT_KEY = "learning.drift"

    def __init__(
        self,
        settings: Settings,
        repo: ResearchRepository,
        state: StateStore,
        audit: Optional[AuditLog] = None,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.state = state
        self.audit = audit
        self.metrics = metrics or MetricsRegistry()

    # ------------------------------------------------------------------
    # Qualification
    # ------------------------------------------------------------------
    def evaluate_gates(self, metrics: CandidateMetrics) -> Dict[str, bool]:
        return {
            "min_oos_trades": metrics.oos_trades >= self.settings.learning_min_oos_trades,
            "min_oos_sharpe": to_decimal(metrics.oos_sharpe) >= self.settings.learning_min_oos_sharpe,
            "max_drawdown": to_decimal(metrics.max_drawdown_pct)
            <= self.settings.learning_max_drawdown_pct,
            "positive_return": to_decimal(metrics.net_return_pct) > Decimal("0"),
            "data_fingerprint_present": bool(metrics.data_fingerprint),
        }

    def register_candidate(
        self,
        strategy_id: str,
        metrics: CandidateMetrics,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        gates = self.evaluate_gates(metrics)
        qualified = all(gates.values())
        stage = CandidateStage.VALIDATED if qualified else CandidateStage.CANDIDATE
        candidate_id = f"CAND-{strategy_id}-{uuid.uuid4().hex[:10]}"
        record = {
            "candidate_id": candidate_id,
            "strategy_id": strategy_id,
            "stage": stage.value,
            "metrics": metrics.model_dump(mode="json"),
            "gates": gates,
            "parameters": parameters or {},
            "data_fingerprint": metrics.data_fingerprint,
            "created_at": utc_now(),
        }
        self.repo.upsert_candidate(record)
        self.metrics.increment(
            "forex_learning_candidates_total", labels={"qualified": str(qualified)}
        )
        if self.audit:
            self.audit.record(
                actor="system",
                role="system",
                action="learning.candidate_registered",
                target=candidate_id,
                details={"strategy_id": strategy_id, "gates": gates, "stage": stage.value},
            )
        if qualified and self.settings.learning_auto_promote_to_paper:
            self.promote(
                candidate_id,
                CandidateStage.PAPER,
                approved_by="system:auto-paper",
                notes="Qualified gates; automatically promoted to paper.",
            )
            record = self.repo.get_candidate(candidate_id) or record
        return record

    def promote(
        self,
        candidate_id: str,
        to_stage: CandidateStage | str,
        *,
        approved_by: Optional[str] = None,
        notes: str = "",
    ) -> Dict[str, Any]:
        candidate = self.repo.get_candidate(candidate_id)
        if not candidate:
            raise KeyError(f"Unknown candidate {candidate_id!r}")
        current = CandidateStage(candidate["stage"])
        target = CandidateStage(to_stage)
        if target not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"Illegal promotion {current.value} -> {target.value}")
        if target == CandidateStage.LIMITED_LIVE:
            if self.settings.learning_require_human_for_live and not approved_by:
                raise PermissionError(
                    "LIMITED_LIVE promotion requires an explicit human approver."
                )
            if not self.settings.is_live and self.settings.environment.value != "demo":
                raise PermissionError(
                    "LIMITED_LIVE promotion requires a demo or live environment configuration."
                )
        updated = dict(candidate)
        updated["stage"] = target.value
        self.repo.upsert_candidate(updated)
        self.repo.record_promotion(
            {
                "promotion_id": f"PROM-{uuid.uuid4().hex[:12]}",
                "candidate_id": candidate_id,
                "strategy_id": candidate["strategy_id"],
                "from_stage": current.value,
                "to_stage": target.value,
                "status": "APPLIED",
                "approved_by": approved_by,
                "notes": notes,
                "created_at": utc_now(),
            }
        )
        self.metrics.increment(
            "forex_learning_promotions_total",
            labels={"from": current.value, "to": target.value},
        )
        if self.audit:
            self.audit.record(
                actor=approved_by or "system",
                role="approver" if approved_by else "system",
                action="learning.promotion",
                target=candidate_id,
                details={"from": current.value, "to": target.value, "notes": notes},
            )
        if target == CandidateStage.PAPER:
            self._set_champion(candidate["strategy_id"], candidate_id, target)
        return self.repo.get_candidate(candidate_id) or updated

    # ------------------------------------------------------------------
    # Champion / rollback
    # ------------------------------------------------------------------
    def _set_champion(
        self, strategy_id: str, candidate_id: str, stage: CandidateStage
    ) -> None:
        champions = self.state.get(self.CHAMPION_KEY, {}) or {}
        previous = champions.get(strategy_id)
        champions[strategy_id] = {
            "candidate_id": candidate_id,
            "stage": stage.value,
            "since": utc_now(),
        }
        history = self.state.get(f"learning.history.{strategy_id}", []) or []
        if previous:
            history.append({**previous, "replaced_at": utc_now()})
        self.state.set(f"learning.history.{strategy_id}", history[-20:])
        self.state.set(self.CHAMPION_KEY, champions)

    def champion(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        champions = self.state.get(self.CHAMPION_KEY, {}) or {}
        return champions.get(strategy_id)

    def rollback(self, strategy_id: str, approved_by: str, notes: str = "") -> Optional[Dict[str, Any]]:
        """Restore the previous champion for a strategy."""
        history = self.state.get(f"learning.history.{strategy_id}", []) or []
        if not history:
            return None
        previous = history.pop()
        champions = self.state.get(self.CHAMPION_KEY, {}) or {}
        champions[strategy_id] = previous
        self.state.set(self.CHAMPION_KEY, champions)
        self.state.set(f"learning.history.{strategy_id}", history)
        if self.audit:
            self.audit.record(
                actor=approved_by,
                role="approver",
                action="learning.rollback",
                target=strategy_id,
                details={"restored": previous, "notes": notes},
            )
        return previous

    # ------------------------------------------------------------------
    # Drift monitoring / research cycles
    # ------------------------------------------------------------------
    def evaluate_drift(
        self,
        strategy_id: str,
        live_metrics: CandidateMetrics,
        *,
        baseline_candidate_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        champion = self.champion(strategy_id) or {}
        baseline_id = baseline_candidate_id or champion.get("candidate_id")
        baseline = self.repo.get_candidate(baseline_id) if baseline_id else None
        baseline_metrics = (baseline or {}).get("metrics", {}) if baseline else {}
        baseline_sharpe = to_decimal(baseline_metrics.get("oos_sharpe", self.settings.learning_min_oos_sharpe))
        checks = {
            "sharpe_within_half_of_baseline": to_decimal(live_metrics.oos_sharpe)
            >= baseline_sharpe / Decimal("2"),
            "drawdown_within_limit": to_decimal(live_metrics.max_drawdown_pct)
            <= self.settings.learning_max_drawdown_pct,
            "sufficient_activity": live_metrics.oos_trades >= max(
                10, self.settings.learning_min_oos_trades // 2
            ),
        }
        degraded = not all(checks.values())
        record = {
            "strategy_id": strategy_id,
            "baseline_candidate_id": baseline_id,
            "live_metrics": live_metrics.model_dump(mode="json"),
            "checks": checks,
            "degraded": degraded,
            "evaluated_at": utc_now(),
        }
        drift_state = self.state.get(self.DRIFT_KEY, {}) or {}
        drift_state[strategy_id] = record
        self.state.set(self.DRIFT_KEY, drift_state)
        self.metrics.set_gauge(
            "forex_learning_drift_degraded", 1.0 if degraded else 0.0, {"strategy": strategy_id}
        )
        if degraded:
            logger.warning("Strategy drift detected: %s", record)
            if self.audit:
                self.audit.record(
                    actor="system",
                    role="system",
                    action="learning.drift_detected",
                    target=strategy_id,
                    details=record,
                )
        return record

    def run_cycle(
        self,
        strategy_id: str,
        runner: Callable[[], CandidateMetrics],
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run one offline research cycle and register its candidate."""
        logger.info("Starting research cycle for %s", strategy_id)
        metrics = runner()
        candidate = self.register_candidate(strategy_id, metrics, parameters)
        logger.info(
            "Research cycle finished for %s candidate=%s stage=%s",
            strategy_id,
            candidate["candidate_id"],
            candidate["stage"],
        )
        return candidate

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        candidates = self.repo.list_candidates(limit=500)
        by_stage: Dict[str, int] = {}
        for candidate in candidates:
            by_stage[candidate["stage"]] = by_stage.get(candidate["stage"], 0) + 1
        return {
            "enabled": self.settings.learning_enabled,
            "candidates_by_stage": by_stage,
            "champions": self.state.get(self.CHAMPION_KEY, {}) or {},
            "recent_promotions": self.repo.list_promotions(limit=20),
            "recent_drift": self.state.get(self.DRIFT_KEY, {}) or {},
            "gates": {
                "min_oos_trades": self.settings.learning_min_oos_trades,
                "min_oos_sharpe": str(self.settings.learning_min_oos_sharpe),
                "max_drawdown_pct": str(self.settings.learning_max_drawdown_pct),
                "human_required_for_live": self.settings.learning_require_human_for_live,
            },
        }
