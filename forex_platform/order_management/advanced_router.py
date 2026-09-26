"""
Advanced Smart Order Router with Latency and Cost Optimization.
Extends the base SmartOrderRouter to consider real-time latency and transaction costs
when selecting execution venues.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, ConfigDict

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    LotSize,
    OrderIntent,
    OrderStatus,
)
from forex_platform.order_management.router import LiveCapitalLockedError, SlicedOrderBatch, SmartOrderRouter


class VenueMetrics(BaseModel):
    """
    Metrics for an execution venue.
    """
    model_config = ConfigDict(frozen=True)

    venue: str
    latency_ms: Decimal  # Average latency in milliseconds
    cost_per_unit: Decimal  # Cost per unit traded (in quote currency, e.g., USD per EUR)
    reliability_score: Decimal = Decimal("1.0")  # 0.0 to 1.0, higher is better


class AdvancedSmartOrderRouter(SmartOrderRouter):
    """
    Smart Order Router that selects venues based on latency and cost.
    """

    def __init__(self, allow_live_routing: bool = False):
        super().__init__(allow_live_routing)
        # Venue metrics cache: venue -> VenueMetrics
        self._venue_metrics: Dict[str, VenueMetrics] = {}
        # Weights for latency and cost in the scoring function
        self.latency_weight = Decimal("0.5")
        self.cost_weight = Decimal("0.5")

    def update_venue_metrics(self, venue: str, latency_ms: Decimal, cost_per_unit: Decimal, reliability_score: Decimal = Decimal("1.0")) -> None:
        """
        Update the metrics for a given venue.
        """
        self._venue_metrics[venue] = VenueMetrics(
            venue=venue,
            latency_ms=latency_ms,
            cost_per_unit=cost_per_unit,
            reliability_score=reliability_score,
        )

    def _venue_score(self, venue: str) -> Decimal:
        """
        Compute a score for a venue (lower is better).
        Score = latency_weight * normalized_latency + cost_weight * normalized_cost
        We normalize by the current min/max across venues.
        If no metrics, return a high score.
        """
        if venue not in self._venue_metrics:
            # If we have no metrics, return a high score to discourage use
            return Decimal("999999")

        metrics = self._venue_metrics[venue]
        # If we have no other venues, we cannot normalize; return a default score based on raw values
        if len(self._venue_metrics) == 1:
            return metrics.latency_ms + metrics.cost_per_unit

        # Normalize latency and cost across venues
        latencies = [m.latency_ms for m in self._venue_metrics.values()]
        costs = [m.cost_per_unit for m in self._venue_metrics.values()]
        min_lat, max_lat = min(latencies), max(latencies)
        min_cost, max_cost = min(costs), max(costs)

        # Avoid division by zero
        lat_range = max_lat - min_lat if max_lat != min_lat else Decimal("1")
        cost_range = max_cost - min_cost if max_cost != min_cost else Decimal("1")

        norm_lat = (metrics.latency_ms - min_lat) / lat_range
        norm_cost = (metrics.cost_per_unit - min_cost) / cost_range

        # Weighted sum (lower is better)
        score = self.latency_weight * norm_lat + self.cost_weight * norm_cost
        # Adjust by reliability (higher reliability reduces score)
        score = score * (Decimal("2.0") - metrics.reliability_score)  # reliability 1.0 -> multiplier 1.0, 0.5 -> multiplier 1.5
        return score

    def select_venue(self, intent: OrderIntent) -> str:
        """
        Select the best venue for the given intent based on latency and cost.
        If no venues are known, default to PAPER_ENGINE.
        """
        if not self._venue_metrics:
            return "PAPER_ENGINE"

        # Compute scores for all known venues
        scores: List[Tuple[str, Decimal]] = []
        for venue in self._venue_metrics:
            score = self._venue_score(venue)
            scores.append((venue, score))

        # Sort by score ascending (best first)
        scores.sort(key=lambda x: x[1])
        best_venue = scores[0][0]
        return best_venue

    def route_intent(
        self,
        intent: OrderIntent,
        destination: Optional[str] = None,
    ) -> List[ExecutionOrder]:
        """
        Route an OrderIntent to execution destination.
        If destination is None, select the best venue based on latency and cost.
        Otherwise, use the provided destination (with the same safety checks as base class).
        """
        if destination is None:
            destination = self.select_venue(intent)

        # Use the base class routing logic (which includes slicing, idempotency, and live capital checks)
        return super().route_intent(intent, destination=destination)