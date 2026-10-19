# Next Steps — SuperTrend-Stochastic MTF Forex Platform

**Status (19 Oct 2026):** Campaign PID 16584 running on EURUSD SET_1. All 4 pairs' M1 data downloaded. Tests: 249 passed.

---

## Phase 1: Campaign Evaluation (immediate, when campaign finishes)

### 1.1 Run gate evaluation
```bash
python research/evaluate_campaign_results.py
```
Reads `research/results/supertrend_stochastic_campaign.json` and scores all 24 cells
(6 sets × 4 pairs) against institutional gates:
- 100+ trades per cell
- Profit factor > 1.5
- Expectancy > 0.5R
- Max drawdown < 15%
- Sharpe > 1.0
- Win rate > 40%

Writes `research/results/campaign_gate_evaluation.json`.

### 1.2 Decision matrix

| Outcome | Action |
|---------|--------|
| 0 cells pass all gates | Do NOT shadow trade. Analyze failing gates (likely PF or expectancy). Adjust strategy params (ATR period, stochastic levels, RR target). Re-run backtest. |
| 1+ cells pass all gates | Proceed to Phase 2 shadow trading for those cells only. |
| All cells fail | Strategy concept may not have an edge. Consider revising entry/exit logic before more testing. |

---

## Phase 2: Shadow (Paper) Trading — 60 days

### 2.1 Run shadow validation
```bash
python research/shadow_trading_runner.py \
    --symbols EURUSD GBPUSD USDJPY AUDUSD \
    --days 60 \
    --equity 100000
```
- Runs in PAPER mode only — live capital $0
- Uses AutoTradingController with pre-trade risk firewall (1% risk per trade)
- Logs all hypothetical orders/fills for review

### 2.2 Shadow pass/fail criteria (per shadow_validation_protocol.md)
| Condition | Verdict |
|-----------|---------|
| PF < 1.0 net at any review | FAIL — stop |
| Expectancy ≤ 0R net | FAIL — stop |
| DD > 20% of equity | FAIL — stop |
| < 20 trades after 60 days | INCONCLUSIVE |
| Spread × 2.0 stress → PF < 1.2 | FAIL |

---

## Phase 3: Infrastructure Gaps to Fix Before Live Consideration

### 3.1 News calendar (currently missing)
Your concept requires **30-minute news blackout before/after Tier-1 events**. The platform has `enable_news_filter=True` in the strategy but **no historical news calendar is integrated**. The prior rider campaign explicitly noted this as a gap.

**Action:** Add a news calendar source (e.g., ForexFactory, Investing.com, or Dukascopy economic events) and wire it into the strategy's `on_bar` to suppress entries 30 min before/after high-impact events (NFP, CPI, FOMC, rate decisions).

### 3.2 Expanded symbol universe (majors + minors)
Currently 4 pairs: EURUSD, GBPUSD, USDJPY, AUDUSD. `download_dukascopy_m1.py` `INSTRUMENTS` dict only supports these 4. You want majors + minors.

**Action:** Add to `INSTRUMENTS` in `forex_platform/market_data/download_dukascopy_m1.py`:
```python
"EURGBP": "EUR/GBP", "EURJPY": "EUR/JPY", "GBPJPY": "GBP/JPY",
"AUDJPY": "AUD/JPY", "NZDUSD": "NZD/USD", "USDCAD": "USD/CAD",
"GBPCHF": "GBP/CHF", "EURCHF": "EUR/CHF", "USDCHF": "USD/CHF",
```
Then re-run `tools/download_full_m1.py` for the expanded list.

### 3.3 Cost model realism
Current backtest uses static swap assumptions (-0.6 pips long, +0.2 pips short, Wednesday 3x) and $7/lot commission. Real broker spreads vary by session (wider in Asian/rollover, tighter in London/NY overlap).

**Action:** Make spread model session-aware using `ForexSessionEngine.regime_tag` (already implemented in `forex_platform/core/sessions.py`).

### 3.4 Multi-symbol concurrent execution
The strategy accepts `symbols` as a list but the backtester runs one symbol at a time in the campaign. For shadow/live, the `AutoTradingController` handles multiple symbols concurrently — verify the strategy's `_supertrends` and `_stochastics` dicts are keyed per symbol (they are, initialized in `__init__`).

---

## Phase 4: Live Deployment (only after Phases 1-3 pass)

**Not authorized until:**
1. ≥1 cell passes all institutional gates on backtest
2. Shadow trading passes 60 days with PF > 1.0, expectancy > 0R, DD < 20%
3. News calendar integrated and tested
4. Human approval (gate per `docs/OPERATIONS.md`)

Live capital remains $0 until all above are satisfied.

---

## Quick Reference Commands

```bash
# Check if campaign is still running
Get-Process -Id 16584

# Check campaign progress
Get-Content data/campaign_stdout.log | Select-Object -Last 10

# Run gate evaluation (after campaign completes)
python research/evaluate_campaign_results.py

# Run shadow trading (only if cells pass gates)
python research/shadow_trading_runner.py --symbols EURUSD GBPUSD USDJPY AUDUSD --days 60

# Full test suite
python -m pytest tests -q
```

**Live capital: $0.00.** No live orders, no live credentials, no live deployment until Phase 4 gates are cleared.
