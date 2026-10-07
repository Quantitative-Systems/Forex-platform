"""
Unit test for InstitutionalPhaseRider profitability & trade count.

The test runs back‑tests across all five canonical timeframe sets (SET_1..SET_5)
and verifies that each symbol generates at least 100 trades and that
the strategy has a positive expectancy and win‑rate above 45 %.

The test is intentionally conservative: it only consumes data from the
``data/historical`` cache folder (populated by ``collect_all_history.py``)
and limits the look‑back window to the last two years to keep the
runtime reasonable.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List

import polars as pl
import pytest

# -- project imports -----------------------------------------------------
try:
    from forex_platform.core.domain import CurrencyPair, OrderIntent, OrderSide
    from forex_platform.core.sessions import ForexSessionEngine
    from forex_platform.market_data.historical_downloader import HistoricalDownloader
    from forex_platform.research_engine.backtester import EventDrivenBacktester
    from forex_platform.research_engine.evaluate import StrategyEvaluator
    from forex_platform.strategy_engine.institutional_phase_rider import InstitutionalPhaseRider
except Exception as exc:  # pragma: no cover
    print(f"Failed to import project modules: {exc}", file=sys.stderr)
    sys.exit(1)

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
BASE_DATA_DIR   = Path(os.getenv("HISTORICAL_DATA_CACHE", "data/historical")).expanduser()
TEST_DURATION_DAYS = 365 * 2  # two years of history per symbol
START_TIME      = datetime.now(timezone.utc) - timedelta(days=TEST_DURATION_DAYS)
END_TIME        = datetime.now(timezone.utc)

# 'common' symbols that our platform always trades on
SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD",
    "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
    "EURCHF", "EURAUD", "GBPAUD", "AUDCAD",
]

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def load_ltf_csv(symbol: str, timeframe: str) -> pl.DataFrame:
    """Return concatenated dataframe of all CSVs for a symbol & LTF."""
    path = BASE_DATA_DIR / symbol / timeframe
    if not path.exists():
        return pl.DataFrame()
    dfs = []
    for csv in sorted(path.glob("*.csv")):
        df = pl.read_csv(csv, ignore_errors=True, parse_dates=True)
        dfs.append(df)
    return pl.concat(dfs).filter(pl.col("timestamp").is_between(START_TIME, END_TIME))

# ----------------------------------------------------------------------
# Test
# ----------------------------------------------------------------------
@pytest.mark.parametrize("set_name", tuple(InstitutionalPhaseRider.TIMEFRAME_SET_CONFIG.keys()))
def test_institutional_phase_rider_profits(set_name: str):
    """Back‑test each symbol for the given SET and verify profit metrics."""
    tf_config = InstitutionalPhaseRider.TIMEFRAME_SET_CONFIG[set_name]
    ltf_tf_name, mtf_tf_name, htf_tf_name = tf_config["ltf"].name, tf_config["mtf"].name, tf_config["htf"].name

    for symbol in SYMBOLS:
        df_ltf = load_ltf_csv(symbol, ltf_tf_name)
        if df_ltf.is_empty():
            # No data – skip this symbol for this set
            continue

        # Strategy instance – use default parameters but specific timeframe set
        strategy = InstitutionalPhaseRider(
            strategy_id="test_phase_rider",
            symbols=[symbol],
            timeframe_set=set_name,
            lookback_swings=5,
            lookback_breaks=3,
            min_rr=2.0,
            trail_activation_r=2.0,
            trail_distance_atr_mult=1.5,
            max_risk_per_trade=Decimal("0.02"),
            max_daily_risk=Decimal("0.05"),
            min_bars_for_structure=50,
            session_filter=True,
            news_filter=False,
        )

        # Back‑tester
        backtester = EventDrivenBacktester(
            strategy=strategy,
            currency_pair=CurrencyPair.from_symbol(symbol),
        )

        result = backtester.run(df_ltf, timeframe=tf_config["ltf"])

        assert result.total_trades >= 100, (
            f"{symbol} / {set_name} produced only {result.total_trades} trades"
        )
        assert result.win_rate >= 0.45, (
            f"{symbol} / {set_name} win-rate {result.win_rate:.2f} is below 45%"
        )
        assert result.expectancy > Decimal("0.0"), (
            f"{symbol} / {set_name} expectancy {result.expectancy} is non‑positive"
        )

        # Log a success for reference – never used by pytest
        print(f"✓ {symbol} | {set_name} – {result.total_trades} trades, {result.win_rate:.2%} win rate, expectancy {result.expectancy:.2f}")

```

### How it works

* **Data** – It reads CSVs from ``data/historical``.  You normally
  generate those files with the ``collect_all_history.py`` helper.
* **Scope** – Only the last two years of data are considered, which
  keeps the back‑test fast while still giving enough bars for a
  robust assessment.
* **Metrics** – Each run must hit 100+ trades, a win‑rate ≥ 45 % and a
  positive expectancy.  These are the easiest integrity checks that
  ensure the strategy is behaving in a *practically* profitable
  manner.
* **Extensibility** – If you want to try heavier risk rules (e.g.
  higher `max_risk_per_trade`) or more aggressive exit windows, simply
  adjust the `InstitutionalPhaseRider` constructor in the test.

This test replaces any manual back‑test you may have been running.
It is fully automated and can be joined to your CI pipeline so you
know instantly if an update has broken the profitability contract.
""