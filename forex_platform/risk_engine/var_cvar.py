"""
Value-at-Risk (VaR) and Conditional Value-at-Risk (CVaR) Calculations.
Supports parametric, historical, and Monte Carlo methods.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
import math
import statistics
import random

from forex_platform.core.domain import Position


class VaRCalculator:
    """
    Value-at-Risk (VaR) and Conditional Value-at-Risk (CVaR) calculator.
    """

    def __init__(self, confidence_level: Decimal = Decimal("0.95")):
        """
        :param confidence_level: Confidence level for VaR (e.g., 0.95 for 95% VaR).
        """
        if confidence_level <= Decimal("0") or confidence_level >= Decimal("1"):
            raise ValueError("Confidence level must be between 0 and 1")
        self.confidence_level = confidence_level

    def historical_var(
        self,
        returns: List[Decimal],
        portfolio_value: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """
        Calculate VaR and CVaR using historical simulation.
        :param returns: List of historical returns (as decimals, e.g., 0.01 for 1%).
        :param portfolio_value: Current portfolio value.
        :return: (VaR, CVaR) as positive numbers representing loss.
        """
        if not returns:
            return Decimal("0"), Decimal("0")

        # Sort returns from worst to best (most negative to most positive)
        sorted_returns = sorted(returns)
        n = len(sorted_returns)
        # Index for VaR: ceil((1 - confidence) * n) - 1 (0-indexed)
        var_index = int(math.ceil((Decimal("1") - self.confidence_level) * Decimal(str(n)))) - 1
        if var_index < 0:
            var_index = 0
        if var_index >= n:
            var_index = n - 1

        var_return = sorted_returns[var_index]  # This is negative (loss)
        var_loss = -var_return * portfolio_value  # Positive loss amount

        # CVaR: average of returns worse than VaR threshold
        cvar_returns = sorted_returns[:var_index + 1]  # All returns at or worse than VaR
        if cvar_returns:
            cvar_return = sum(cvar_returns, Decimal("0")) / Decimal(str(len(cvar_returns)))
            cvar_loss = -cvar_return * portfolio_value
        else:
            cvar_loss = var_loss

        return var_loss, cvar_loss

    def parametric_var(
        self,
        mean_return: Decimal,
        std_return: Decimal,
        portfolio_value: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """
        Calculate VaR and CVaR assuming normal distribution.
        :param mean_return: Mean of returns (decimal).
        :param std_return: Standard deviation of returns (decimal).
        :param portfolio_value: Current portfolio value.
        :return: (VaR, CVaR) as positive numbers representing loss.
        """
        if std_return < Decimal("0"):
            raise ValueError("Standard deviation must be non-negative")

        # For normal distribution, VaR = -(mean - z * std) * portfolio_value
        # where z is the z-score for the confidence level (one-tailed)
        # We'll approximate z-score for common confidence levels
        z_scores = {
            Decimal("0.90"): Decimal("1.282"),
            Decimal("0.95"): Decimal("1.645"),
            Decimal("0.99"): Decimal("2.326"),
        }
        # Find closest confidence level or interpolate (simplified: use closest)
        conf_float = float(self.confidence_level)
        if conf_float in [0.90, 0.95, 0.99]:
            z = z_scores[self.confidence_level]
        else:
            # Default to 95% if not matched
            z = z_scores[Decimal("0.95")]

        var_return = mean_return - z * std_return
        var_loss = -var_return * portfolio_value  # Positive if var_return is negative

        # CVaR for normal distribution: CVaR = -(mean - std * phi(z) / (1 - confidence)) * portfolio_value
        # where phi(z) is the PDF of standard normal at z
        # We'll approximate phi(z) using a simple formula or lookup
        # For simplicity, we'll use an approximation: CVaR ≈ VaR + (std * z) / (1 - confidence) * something
        # Actually, CVaR = -(mean + std * (pdf(z) / (1 - confidence))) * portfolio_value
        # Let's compute pdf(z) = (1/sqrt(2*pi)) * exp(-z^2/2)
        pi = Decimal("3.14159265358979323846")
        sqrt_2pi = (Decimal("2") * pi).sqrt()
        pdf_z = (Decimal("-z") * z / Decimal("2")).exp() / sqrt_2pi  # This is approximate, we'll compute properly below

        # Let's compute step by step
        z_decimal = z
        pdf_z = (Decimal("-1") * z_decimal * z_decimal / Decimal("2")).exp() / (Decimal("2") * pi).sqrt()
        cvar_return = mean_return - std_return * pdf_z / (Decimal("1") - self.confidence_level)
        cvar_loss = -cvar_return * portfolio_value

        return var_loss, cvar_loss

    def monte_carlo_var(
        self,
        mean_return: Decimal,
        std_return: Decimal,
        portfolio_value: Decimal,
        num_simulations: int = 10000,
    ) -> Tuple[Decimal, Decimal]:
        """
        Calculate VaR and CVaR using Monte Carlo simulation.
        :param mean_return: Mean of returns.
        :param std_return: Standard deviation of returns.
        :param portfolio_value: Current portfolio value.
        :param num_simulations: Number of simulation paths.
        :return: (VaR, CVaR) as positive numbers representing loss.
        """
        if num_simulations <= 0:
            raise ValueError("Number of simulations must be positive")

        # Generate random returns from normal distribution
        # We'll use Box-Muller transform for simplicity
        random_returns: List[Decimal] = []
        for _ in range(num_simulations):
            # Generate two independent uniform(0,1) random numbers
            u1 = random.random()
            u2 = random.random()
            # Box-Muller transform to get two independent standard normal variables
            z0 = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
            z1 = math.sqrt(-2 * math.log(u1)) * math.sin(2 * math.pi * u2)
            # Use z0, convert to decimal and scale
            ret = Decimal(str(mean_return + std_return * Decimal(str(z0))))
            random_returns.append(ret)

        # Now calculate historical VaR on the simulated returns
        return self.historical_var(random_returns, portfolio_value)


class StressTester:
    """
    Stress testing engine for portfolio scenarios.
    """

    def __init__(self):
        self.scenarios: Dict[str, Dict[str, Decimal]] = {}
        self._load_default_scenarios()

    def _load_default_scenarios(self) -> None:
        """Load some default stress scenarios (e.g., market crashes, volatility spikes)."""
        # Example scenarios: symbol -> price change (as decimal, e.g., -0.1 for -10%)
        self.scenarios["Market Crash 2008"] = {
            "EUR/USD": Decimal("-0.20"),
            "GBP/USD": Decimal("-0.25"),
            "USD/JPY": Decimal("0.15"),  # JPY strengthens
            "USD/CHF": Decimal("0.10"),
            "AUD/USD": Decimal("-0.30"),
            "NZD/USD": Decimal("-0.30"),
            "USD/CAD": Decimal("0.05"),
        }
        self.scenarios["Volatility Spike"] = {
            # Assume all pairs move 2 standard deviations in random direction
            # We'll just set a fixed move for demo
            "EUR/USD": Decimal("-0.05"),
            "GBP/USD": Decimal("0.05"),
            "USD/JPY": Decimal("-0.03"),
            "USD/CHF": Decimal("0.03"),
            "AUD/USD": Decimal("-0.04"),
            "NZD/USD": Decimal("0.04"),
            "USD/CAD": Decimal("-0.02"),
        }
        self.scenarios["Flash Crash"] = {
            "EUR/USD": Decimal("-0.10"),
            "GBP/USD": Decimal("-0.15"),
            "USD/JPY": Decimal("0.20"),
            "USD/CHF": Decimal("0.15"),
            "AUD/USD": Decimal("-0.25"),
            "NZD/USD": Decimal("-0.25"),
            "USD/CAD": Decimal("0.10"),
        }

    def add_scenario(self, name: str, shocks: Dict[str, Decimal]) -> None:
        """Add a custom stress scenario."""
        self.scenarios[name] = shocks

    def run_stress_test(
        self,
        positions: List[Position],
        scenario_name: str,
        current_prices: Dict[str, Decimal],
    ) -> Dict[str, Any]:
        """
        Run a stress test on the portfolio.
        :param positions: List of current positions.
        :param scenario_name: Name of the scenario to apply.
        :param current_prices: Current prices for each symbol (symbol -> price).
        :return: Dictionary with scenario name, P&L impact, and position-level impacts.
        """
        if scenario_name not in self.scenarios:
            raise ValueError(f"Scenario '{scenario_name}' not found")

        shocks = self.scenarios[scenario_name]
        total_pnl_impact = Decimal("0")
        position_impacts: List[Dict[str, Any]] = []

        for pos in positions:
            symbol = pos.symbol  # Position.symbol is a canonical string (e.g. 'EURUSD')
            if symbol not in current_prices:
                # Skip if we don't have current price
                continue
            current_price = current_prices[symbol]
            shock = shocks.get(symbol, Decimal("0"))  # Default to no shock if not specified
            stressed_price = current_price + shock
            # P&L impact: (stressed_price - current_price) * position size * sign
            # For simplicity, assume position size is in units of base currency and we are long
            # In reality, you'd need to consider long/short and contract size
            # We'll assume pos has a 'size' attribute in base currency units and a 'side' attribute
            # For demo, we'll use a simplified calculation
            price_change = stressed_price - current_price
            # Assume 1 unit of base currency per position unit (simplified)
            pnl_impact = price_change * Decimal(str(pos.size))  # pos.size is number of base currency units
            if pos.side == "SELL":  # or however you represent short
                pnl_impact = -pnl_impact

            total_pnl_impact += pnl_impact
            position_impacts.append({
                "symbol": symbol,
                "position_size": str(pos.size),
                "side": pos.side,
                "current_price": str(current_price),
                "shock": str(shock),
                "stressed_price": str(stressed_price),
                "pnl_impact": str(pnl_impact),
            })

        return {
            "scenario": scenario_name,
            "total_pnl_impact": str(total_pnl_impact),
            "position_impacts": position_impacts,
        }


# Example usage (for demonstration)
if __name__ == "__main__":
    # Example VaR calculation
    var_calc = VaRCalculator(confidence_level=Decimal("0.95"))
    # Simulate some returns
    returns = [Decimal(str(random.uniform(-0.02, 0.02))) for _ in range(252)]  # Daily returns for a year
    portfolio_value = Decimal("1000000")  # $1M portfolio
    var, cvar = var_calc.historical_var(returns, portfolio_value)
    print(f"Historical VaR (95%): ${var:.2f}")
    print(f"Historical CVaR (95%): ${cvar:.2f}")

    # Example stress test
    # This would require Position objects; we'll skip for brevity
    pass