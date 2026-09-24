"""
Currency Exposure Governor and Portfolio Allocator.
Enforces hard limits on individual currency net-delta concentration (default 25% of equity).
Rejects or downsizes unbalanced order intents to prevent accidental multi-pair correlated bets.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.domain import (
    CurrencyPair,
    LotSize,
    OrderIntent,
    OrderSide,
    Position,
    to_decimal,
)
from forex_platform.portfolio_engine.exposure_matrix import (
    CurrencyExposureMatrix,
    PortfolioExposureSnapshot,
)


class GovernorDecision(BaseModel):
    """
    Verdict on whether an order intent satisfies portfolio currency exposure constraints.
    """
    model_config = ConfigDict(frozen=True)

    approved: bool
    downsized: bool = False
    reason: str
    breached_currency: Optional[str] = None
    current_exposure_pct: Optional[Decimal] = None
    projected_exposure_pct: Optional[Decimal] = None
    original_lots: Optional[LotSize] = None
    allowed_lots: Optional[LotSize] = None
    suggested_order: Optional[OrderIntent] = None
    snapshot: Optional[PortfolioExposureSnapshot] = None


class CurrencyExposureGovernor:
    """
    Institutional Currency Exposure Governor.
    Prevents account-destroying correlation risk by hard-capping single-currency
    net exposure to <= 25% of account equity.
    """

    DEFAULT_MAX_EXPOSURE_PCT = Decimal("0.25")  # 25% of account equity
    MINIMUM_TRADE_UNITS = 1_000                  # 0.01 micro lot minimum

    def __init__(
        self,
        max_single_currency_exposure_pct: Decimal = DEFAULT_MAX_EXPOSURE_PCT,
        allow_downsizing: bool = True,
        exposure_matrix: Optional[CurrencyExposureMatrix] = None,
    ):
        self.max_exposure_pct = to_decimal(max_single_currency_exposure_pct)
        self.allow_downsizing = allow_downsizing
        self.matrix = exposure_matrix or CurrencyExposureMatrix()

    def evaluate_intent(
        self,
        intent: OrderIntent,
        open_positions: List[Position],
        account_equity: Decimal | float | str,
        current_prices: Optional[Dict[str, Decimal]] = None,
    ) -> GovernorDecision:
        """
        Evaluate a proposed OrderIntent against current portfolio exposures.
        If the intent would push any currency's net delta beyond max_exposure_pct,
        either downsize it (if allow_downsizing=True and headroom exists) or reject it.
        """
        equity = to_decimal(account_equity)
        if equity <= Decimal("0"):
            return GovernorDecision(
                approved=False,
                reason="Governor Failure: Account equity must be positive.",
            )

        # Baseline exposure before the proposed order
        base_snapshot = self.matrix.calculate_portfolio_exposure(
            positions=open_positions,
            account_equity=equity,
            current_prices=current_prices,
            max_exposure_cap_pct=self.max_exposure_pct,
        )

        # Projected exposure if full order is executed
        projected_snapshot = self.matrix.simulate_order_exposure(
            positions=open_positions,
            intent=intent,
            account_equity=equity,
            current_prices=current_prices,
            max_exposure_cap_pct=self.max_exposure_pct,
        )

        # Identify any currencies exceeding the exposure cap
        breached_currencies: List[str] = []
        for curr, delta in projected_snapshot.deltas.items():
            if delta.abs_exposure_pct > self.max_exposure_pct:
                # Check if this order is actually risk-reducing for this currency
                base_delta = base_snapshot.deltas.get(curr)
                base_abs_exp = base_delta.abs_exposure_pct if base_delta else Decimal("0.0")
                if delta.abs_exposure_pct < base_abs_exp:
                    # Risk-reducing trade (closing or hedging an existing breach) -> allowed
                    continue
                breached_currencies.append(curr)

        if not breached_currencies:
            return GovernorDecision(
                approved=True,
                downsized=False,
                reason="Approved: Currency net delta within institutional risk limits.",
                original_lots=intent.lot_size,
                allowed_lots=intent.lot_size,
                snapshot=projected_snapshot,
            )

        # An exposure breach was detected!
        primary_breach = breached_currencies[0]
        curr_delta_base = base_snapshot.deltas.get(primary_breach)
        curr_delta_proj = projected_snapshot.deltas.get(primary_breach)

        curr_exp = curr_delta_base.abs_exposure_pct if curr_delta_base else Decimal("0.0")
        proj_exp = curr_delta_proj.abs_exposure_pct if curr_delta_proj else Decimal("0.0")

        # Check if downsizing is requested and possible
        if self.allow_downsizing:
            downsized_intent, max_lots = self._attempt_downsize(
                intent=intent,
                open_positions=open_positions,
                account_equity=equity,
                current_prices=current_prices,
            )
            if downsized_intent is not None and max_lots is not None:
                # Verify downsized snapshot
                downsized_snapshot = self.matrix.simulate_order_exposure(
                    positions=open_positions,
                    intent=downsized_intent,
                    account_equity=equity,
                    current_prices=current_prices,
                    max_exposure_cap_pct=self.max_exposure_pct,
                )
                return GovernorDecision(
                    approved=True,
                    downsized=True,
                    reason=(
                        f"Downsized: Currency '{primary_breach}' projected exposure ({proj_exp * 100:.1f}%) "
                        f"exceeded cap ({self.max_exposure_pct * 100:.1f}%). Sizing reduced to {max_lots.standard_lots} lots."
                    ),
                    breached_currency=primary_breach,
                    current_exposure_pct=curr_exp,
                    projected_exposure_pct=proj_exp,
                    original_lots=intent.lot_size,
                    allowed_lots=max_lots,
                    suggested_order=downsized_intent,
                    snapshot=downsized_snapshot,
                )

        # Reject order completely
        return GovernorDecision(
            approved=False,
            downsized=False,
            reason=(
                f"Governor Rejection: Net exposure on currency '{primary_breach}' would reach "
                f"{proj_exp * 100:.1f}%, exceeding hard cap of {self.max_exposure_pct * 100:.1f}% of equity."
            ),
            breached_currency=primary_breach,
            current_exposure_pct=curr_exp,
            projected_exposure_pct=proj_exp,
            original_lots=intent.lot_size,
            allowed_lots=None,
            suggested_order=None,
            snapshot=projected_snapshot,
        )

    def _attempt_downsize(
        self,
        intent: OrderIntent,
        open_positions: List[Position],
        account_equity: Decimal,
        current_prices: Optional[Dict[str, Decimal]],
    ) -> tuple[Optional[OrderIntent], Optional[LotSize]]:
        """
        Binary search / analytical calculation to find the maximum lot size <= original lot size
        such that all currency exposures remain <= max_exposure_pct.
        """
        original_units = intent.lot_size.units
        if original_units <= self.MINIMUM_TRADE_UNITS:
            return None, None

        # Binary search for max allowable units between MINIMUM_TRADE_UNITS and original_units
        low = self.MINIMUM_TRADE_UNITS
        high = original_units
        best_units = 0

        # Step in micro-lot increments (1,000 units)
        step = 1_000

        while low <= high:
            mid = ((low + high) // (2 * step)) * step
            if mid < self.MINIMUM_TRADE_UNITS:
                break

            test_lot = LotSize.from_units(mid)
            test_intent = OrderIntent(
                intent_id=f"{intent.intent_id}_downsized",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                lot_size=test_lot,
                limit_price=intent.limit_price,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                urgency=intent.urgency,
                timestamp=intent.timestamp,
                client_tag=f"{intent.client_tag or ''}_DOWNSIZED".strip("_"),
            )

            test_snapshot = self.matrix.simulate_order_exposure(
                positions=open_positions,
                intent=test_intent,
                account_equity=account_equity,
                current_prices=current_prices,
                max_exposure_cap_pct=self.max_exposure_pct,
            )

            if test_snapshot.is_balanced:
                best_units = mid
                low = mid + step  # Try larger size
            else:
                high = mid - step  # Too large, step down

        if best_units >= self.MINIMUM_TRADE_UNITS and best_units < original_units:
            final_lot = LotSize.from_units(best_units)
            final_intent = OrderIntent(
                intent_id=f"{intent.intent_id}_adj",
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                lot_size=final_lot,
                limit_price=intent.limit_price,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                urgency=intent.urgency,
                timestamp=intent.timestamp,
                client_tag=f"{intent.client_tag or ''}_DOWNSIZED".strip("_"),
            )
            return final_intent, final_lot

        return None, None
