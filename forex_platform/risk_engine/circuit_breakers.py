"""
Circuit breakers and dynamic risk dampening for Forex Platform.
- Daily Loss Dampener: -2% drawdown reduces order sizing by 50%.
- Daily Loss Circuit Breaker: -4% drawdown halts trading until 00:00 UTC.
- All-Time High-Water Mark Stop: -10% drawdown activates SAFE_MODE.
"""

from __future__ import annotations

from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, ConfigDict

from forex_platform.core.domain import to_decimal


class CircuitBreakerStatus(BaseModel):
    """Snapshot of current circuit breaker evaluation."""
    model_config = ConfigDict(frozen=True)

    current_equity: Decimal
    daily_open_equity: Decimal
    high_water_mark: Decimal
    daily_pnl_pct: Decimal
    hwm_drawdown_pct: Decimal
    sizing_multiplier: Decimal
    is_dampened: bool
    is_daily_halted: bool
    is_safe_mode: bool
    reason: Optional[str] = None


class CircuitBreakerEngine:
    """
    Evaluates equity drawdown thresholds and enforces capital preservation rules.
    """

    DAILY_DAMPENER_THRESHOLD = Decimal("-0.02")  # -2.0%
    DAILY_HALT_THRESHOLD = Decimal("-0.04")      # -4.0%
    HWM_SAFE_MODE_THRESHOLD = Decimal("-0.10")   # -10.0%

    def __init__(self, initial_equity: Decimal | float | str):
        self.initial_equity = to_decimal(initial_equity)
        self.current_equity = self.initial_equity
        self.daily_open_equity = self.initial_equity
        self.high_water_mark = self.initial_equity
        self.current_trading_date: date = datetime.now(timezone.utc).date()

        self._daily_halted: bool = False
        self._safe_mode: bool = False
        self._halt_reason: Optional[str] = None

    def update_equity(
        self,
        current_equity: Decimal | float | str,
        timestamp: datetime,
    ) -> CircuitBreakerStatus:
        """
        Update equity state, handle daily rollover boundary, and evaluate circuit breakers.
        """
        utc_dt = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        curr_eq = to_decimal(current_equity)
        curr_date = utc_dt.date()

        # Check for day boundary (00:00 UTC rollover)
        if curr_date > self.current_trading_date:
            self.current_trading_date = curr_date
            self.daily_open_equity = curr_eq
            self._daily_halted = False
            self._halt_reason = None

        self.current_equity = curr_eq

        # Update high-water mark
        if curr_eq > self.high_water_mark:
            self.high_water_mark = curr_eq

        # Compute drawdown percentages
        if self.daily_open_equity > Decimal("0"):
            daily_pnl_pct = (curr_eq - self.daily_open_equity) / self.daily_open_equity
        else:
            daily_pnl_pct = Decimal("0.0")

        if self.high_water_mark > Decimal("0"):
            hwm_drawdown_pct = (curr_eq - self.high_water_mark) / self.high_water_mark
        else:
            hwm_drawdown_pct = Decimal("0.0")

        # Evaluate rules
        # 1. HWM Drawdown Stop (-10% -> SAFE_MODE)
        if hwm_drawdown_pct <= self.HWM_SAFE_MODE_THRESHOLD:
            self._safe_mode = True
            self._halt_reason = (
                f"CRITICAL: High-water mark drawdown {hwm_drawdown_pct * 100:.2f}% "
                f"breached -10.0% threshold. SAFE_MODE activated."
            )

        # 2. Daily Loss Halt (-4% -> Trading Halted until 00:00 UTC)
        if daily_pnl_pct <= self.DAILY_HALT_THRESHOLD:
            self._daily_halted = True
            if not self._safe_mode:
                self._halt_reason = (
                    f"CIRCUIT BREAKER TRIPPED: Daily equity drawdown {daily_pnl_pct * 100:.2f}% "
                    f"breached -4.0% limit. Trading halted until 00:00 UTC."
                )

        # 3. Determine sizing multiplier
        if self._safe_mode or self._daily_halted:
            sizing = Decimal("0.0")
            is_dampened = False
        elif daily_pnl_pct <= self.DAILY_DAMPENER_THRESHOLD:
            # -2% to -4% drawdown dampens order sizing by 50%
            sizing = Decimal("0.50")
            is_dampened = True
        else:
            sizing = Decimal("1.00")
            is_dampened = False

        return CircuitBreakerStatus(
            current_equity=self.current_equity,
            daily_open_equity=self.daily_open_equity,
            high_water_mark=self.high_water_mark,
            daily_pnl_pct=daily_pnl_pct,
            hwm_drawdown_pct=hwm_drawdown_pct,
            sizing_multiplier=sizing,
            is_dampened=is_dampened,
            is_daily_halted=self._daily_halted,
            is_safe_mode=self._safe_mode,
            reason=self._halt_reason,
        )

    def reset_safe_mode(self, new_equity: Decimal | float | str) -> None:
        """Manual administrative intervention to reset SAFE_MODE."""
        eq = to_decimal(new_equity)
        self.current_equity = eq
        self.daily_open_equity = eq
        self.high_water_mark = eq
        self._safe_mode = False
        self._daily_halted = False
        self._halt_reason = None
