# SHADOW VALIDATION PROTOCOL — GBPUSD SET_5 SCALPING

**Audit ID:** `audit_20261014_02`  
**Status:** DEFINED BUT NOT STARTED (per mission directive)  
**Live capital:** $0.00 (Law 7 enforced — no live orders, no live credentials)  
**Candidate:** GBPUSD | SET_5 | SCALPING (32 simulated trades, +2.046R expectancy, 3.518 PF, 18.75% win rate)  
**Purpose:** Independent shadow validation before any consideration of live deployment.  
**Selection bias mitigation:** One candidate selected from 100 cells; protocol accounts for this with stricter evidence requirements.

---

## 1. VALIDATION START TIME AND TIMEZONE

| Parameter | Definition |
|---|---|
| Start time | `2026-10-15T00:00:00+00:00` (earliest; to be set by validator) |
| Timezone | UTC (all timestamps UTC) |
| Observation window | 60 calendar days minimum (covers ~800 M3 bars at 5-min equivalent) |
| Start condition | Protocol activated; no market data observed before this time |

**Prohibition:** Do not reuse original 32 trades as validation evidence. Do not observe data from before the validation start time.

---

## 2. DATA SOURCE AND TIMESTAMPED OBSERVATION MECHANISM

| Parameter | Definition |
|---|---|
| Data source | Fresh M3 bars from Dukascopy, loaded via `load_m3()` with `--max-m1-bars 200000` |
| Observation start | `2026-10-15T00:00:00+00:00` (after validation start time) |
| Observation end | `2026-12-13T23:59:59+00:00` (60 days later) |
| Timeframe | M3 (3-minute bars) — same as original experiment |
| Feed | Read-only; no order submission, no credential access |
| Log format | JSON lines (`*.jsonl`) with timestamped observations |

**Signal logging format:**
```json
{
  "timestamp": "2026-10-15T00:03:00+00:00",
  "pair": "GBPUSD",
  "set_key": "SET_5",
  "style": "SCALPING",
  "bar_index": 0,
  "open": 1.27500,
  "high": 1.27580,
  "low": 1.27460,
  "close": 1.27520,
  "spread": 0.00015,
  "intent_type": "LIMIT|STOP|TARGET|STOP_LOSS",
  "limit_price": 1.27520,
  "stop_loss": 1.27200,
  "take_profit": 1.27900,
  "filled": true|false,
  "fill_price": 1.27518|null,
  "fill_slippage": 0.00002|0
}
```

**Hypothetical order and fill logging format:**
```json
{
  "timestamp": "2026-10-15T00:03:00+00:00",
  "pair": "GBPUSD",
  "set_key": "SET_5",
  "style": "SCALPING",
  "order_type": "LIMIT|STOP",
  "limit_price|stop_price": 1.27520|1.27200,
  "take_profit": 1.27900,
  "direction": "BUY|SELL",
  "status": "SUBMITTED|FILLED|REJECTED|EXPIRED",
  "fill_price": 1.27518|null,
  "slippage_pips": 0.2|0,
  "commission": 7.0|0,
  "strategy_id": "style_matrix_5_scalping"
}
```

---

## 3. SESSION AND MARKET-HOURS RULES

| Rule | Definition |
|---|---|
| Trading session | 06:30–17:00 UTC (overlaps London open + New York open) |
| Rollover freeze | 21:45–22:15 UTC (triple-swap period; new entries frozen) |
| Weekend freeze | No M3 bars on Sat/Sun; data gap handled naturally |
| Maximum consecutive losing trades | 5; if exceeded, pause validation for review |
| Daily P&L limit | If daily loss exceeds 3% of starting equity ($3,000 on $100k), pause for review |

---

## 4. SPREAD MODEL

| Component | Model |
|---|---|
| Average M3 spread | Tracked from bar data; compute daily average |
| Spread sentinel | `1.8 × daily_average_spread` used as stress-test ceiling |
| Spread impact on R | Each trade's R calculated: `r = (exit - entry - spread) / risk` (if fill on ask/bid side) |
| Stress test | Re-run validation with spread × 2.0; if PF drops below 1.2, FAIL |

---

## 5. COMMISSION MODEL

| Component | Model |
|---|---|
| Round-turn commission | $7.00 per round-turn (strategy default) |
| Per-trade deduction | Commission split evenly between entry and exit |
| Impact on expectancy | Added to R calculation: `r_net = r_gross - (commission / risk_in_equity_units)` |
| Example | 1% risk = $1,000 on $100k account; $7 commission = 0.007R per trade |

---

## 6. SLIPPAGE MODEL

| Component | Model |
|---|---|
| Base slippage | 0.2 pips per fill (additive to execution price) |
| ATR-based slippation | `0.1 × |current_ATR|` pips, direction random |
| Fill price assumption | `fill_price = limit_price + slippage_side` (where slippage_side = ±0.2 pips + ATR term) |
| Impact on R | Recalculated per fill: `r = (fill_price - entry) / risk` |

---

## 7. SIGNAL LOGGING FORMAT

Each signal generated during validation must be logged as a JSON line with the fields defined in Section 2 (Signal logging format). Fields:

- `timestamp` — bar timestamp (UTC)
- `pair` — trading pair
- `set_key` — strategy set (SET_5)
- `style` — strategy style (SCALPING)
- `bar_index` — zero-based index in observation window
- `open`, `high`, `low`, `close` — OHLC prices
- `spread` — bar spread
- `intent_type` — LIMIT, STOP, TARGET, STOP_LOSS
- `limit_price` — order limit price (if applicable)
- `stop_loss` — strategy stop-loss
- `take_profit` — strategy take-profit
- `filled` — boolean: was the order filled?
- `fill_price` — execution price if filled, null otherwise
- `fill_slippage` — pips of slippage from limit price

---

## 8. HYPOTHETICAL ORDER AND EXIT LOGGING FORMAT

Each order (hypothetical — no live submission) must be logged with:

- `timestamp` — signal generation time
- `pair`, `set_key`, `style` — identification
- `order_type` — LIMIT, STOP, or TARGET
- `limit_price|stop_price` — order price
- `take_profit` — target price
- `direction` — BUY or SELL
- `status` — SUBMITTED, FILLED, REJECTED, EXPIRED
- `fill_price` — execution price if filled
- `slippage_pips` — slippage from order price
- `commission` — $7.00 if filled, 0 otherwise
- `exit_reason` — STOP_HIT, TARGET_HIT, MAX_BARS, STRUCTURAL_INVALIDATION
- `r_realized` — realized R multiple after costs: `(fill_price - entry) / risk - commission / risk`

---

## 9. STOP, TARGET, TRAILING AND EXIT LOGGING

| Exit Type | Logging Requirement |
|---|---|
| Stop-hit | `exit_price = stop_price`; `exit_reason = STOP_HIT`; `r = (stop - entry) / risk` |
| Target-hit | `exit_price = take_profit`; `exit_reason = TARGET_HIT`; `r = (tp - entry) / risk` |
| Max bars in trade | `exit_price = close at bar close`; `exit_reason = MAX_BARS`; bars held tracked |
| Structural invalidation | `exit_price = price at invalidation`; `exit_reason = STRUCTURAL_INVALIDATION`; zone level crossed |
| Break-even trigger | If enabled: `stop` moved to `entry` after target hits; log `break_even_moved_utc` |

**Position tracking per trade:**
```json
{
  "trade_id": "unique_id",
  "entry_utc": "2026-10-15T00:03:00+00:00",
  "entry_price": 1.27520,
  "stop_price": 1.27200,
  "risk_r": 1.0,  // by definition: risk = 1% equity
  "take_profit": 1.27900,
  "target_r": 4.0,  // 4R minimum: (1.27900 - 1.27520) / 0.00320 = 4.0
  "entry_utc": "2026-10-15T00:03:00+00:00",
  "exit_utc": "2026-10-15T01:03:00+00:00",
  "exit_reason": "STOP_HIT|TARGET_HIT|MAX_BARS|INVALIDATION",
  "exit_price": 1.27200|1.27900|1.27520|price_at_cross",
  "r_realized": -1.0|+4.0|+0.5|...,
  "bars_held": 60,
  "commission": 7.0,
  "slippage_pips": 0.2
}
```

---

## 10. NET REALIZED R CALCULATION

**Formula:**
```
r_realized = (exit_price - entry_price) / risk - commission / risk_adjustment - slippage_adjustment
```

Where:
- `risk = abs(entry_price - stop_price)` — by definition, 1% of account equity
- `commission / risk_adjustment` — commission in R units (e.g., $7 / $1,000 = 0.007R)
- `slippage_adjustment` — slippage in R units (e.g., 0.2 pips / stop_distance_in_pips × 1R)

**Net expectancy:** Mean of all `r_realized` values across completed trades.

**Net profit factor:** `sum_of_positive_r_realized / abs(sum_of_negative_r_realized)`

---

## 11. MISSING-DATA AND FEED-OUTAGE HANDLING

| Scenario | Handling |
|---|---|
| Single bar missing | Interpolate from adjacent bars; log as `data_gap: true` |
| Consecutive bars missing (> 5) | Pause validation; resume when feed restored; document gap |
| Feed outage during session | Log `feed_outage: true` with start/end timestamps; resume from last valid bar |
| No new bars for 24 hours | Auto-pause; resume next UTC day; document pause reason |

---

## 12. EVALUATION WINDOW AND REVIEW CADENCE

| Parameter | Definition |
|---|---|
| Evaluation window | 60 calendar days minimum (from validation start time) |
| Review cadence | Every 10 trading days (6 reviews maximum) |
| Minimum completed trades | ≥ 20 filled simulated orders |
| Minimum evidence thresholds | PF > 1.2, expectancy > 0, win rate > 10% |
| Interim report | After 20 trades: preliminary metrics; after 40 trades: intermediate; final at 60+ |

---

## 13. MINIMUM EVIDENCE REQUIREMENTS

To declare **PASS**, all of the following must be satisfied:

| Requirement | Threshold |
|---|---|
| Completed trades | ≥ 20 |
| Profit factor | > 1.2 (gross), > 1.0 (net) |
| Net expectancy | > 0R (after costs) |
| Win rate | > 10% (after costs) |
| Maximum drawdown | < 15% of starting equity (after costs) |
| Cost-adjusted PF | Must survive spread × 2.0 stress test |

To declare **FAIL**, any of the following:

| Condition | Outcome |
|---|---|
| PF < 1.0 (net) | Automatic FAIL |
| expectancy ≤ 0R (net) | Automatic FAIL |
| DD > 20% of starting equity | Automatic FAIL |
| < 20 completed trades | INCONCLUSIVE — extend observation window |
| Cost model makes PF ≤ 1.0 | INCONCLUSIVE — reassess cost assumptions |

To declare **INCONCLUSIVE**, any of the following:

| Condition | Outcome |
|---|---|
| 20 ≤ trades < 40 | Extend observation window |
| PF between 1.0 and 1.2 (net) | Requires cost-model refinement |
| Win rate between 10% and 15% | Borderline — extend observation, reassess sample size |
| Selection bias not accounted for | Must document and address (see Section 14) |

---

## 14. PREDEFINED STOP CONDITIONS

| Condition | Action |
|---|---|
| PF < 1.0 (net) at any review point | FAIL — stop validation |
| expectancy ≤ 0R (net) at any review point | FAIL — stop validation |
| DD > 20% of starting equity | FAIL — stop validation |
| < 20 completed trades after 60 days | INCONCLUSIVE — document, do not validate |
| Cost model makes PF ≤ 1.0 | INCONCLUSIVE — revise cost assumptions, re-run |
| Selection bias detected (e.g., data-mining over 100 cells) | MUST address: apply Bonferroni or similar correction, or reduce candidate pool |
| Spread × 2.0 stress test causes PF < 1.2 | FAIL — spread risk too high |

**Selection bias mitigation (critical):**
Since this candidate was selected as the 1 PASS from 100 cells, the validation must account for multiple comparisons. The effective significance threshold is adjusted using Bonferroni-like correction:

- Original candidate pool: 100 cells
- Adjusted PF threshold: must survive correction for 100 independent tests
- Practical approach: PF > 1.5 (net) rather than > 1.2, or require expectancy > 1.0R (net) rather than > 0R

---

## 15. CONDITIONS REQUIRING A NEW CANDIDATE VERSION

A new candidate version must be created and the snapshot updated if any of the following occurs:

| Trigger | Action |
|---|---|
| Any strategy parameter change (RR, stop, target, persistence) | New version; re-run style matrix cell |
| Data period change (different year, different pair) | New version; re-run with same config |
| Cost model change (new spread/commission/slippage assumptions) | New version; recalculate metrics |
| Candidate selected from different pool size | Adjust selection-bias thresholds; new version if pool size changes |
| Causal defect discovered during shadow validation | Pause; investigate; either fix and re-freeze or archive as failed |

**New version naming convention:**
`GBPUSD|SET_5|SCALPING|<timestamp>_v<increment>`

---

## 16. PROHIBITIONS (MUST READ)

The following are **strictly prohibited** during shadow validation:

- ✗ Live orders of any kind
- ✗ Live credential access (MT5, broker, exchange)
- ✗ Live deployment or real capital deployment
- ✗ Reusing the original 32 trades as validation evidence
- ✗ Observing market data before the validation start time
- ✗ Modifying strategy parameters during validation
- ✗ Altering the cost model after observation has begun
- ✗ Publishing or sharing live-order flow or fill data
- ✗ Any action that could result in live execution

**Enforcement:** The daemon runs in paper-trading mode only. The MT5 broker adapter has a physical gating interlock that prevents order submission when `live=true` is not explicitly configured—and it is NOT configured. Live capital remains $0.00.

---

## 17. PROTOCOL STATUS

| Status | Value |
|---|---|
| Validation start time | DEFERRED — to be activated by authorized party |
| Data source | Ready; read-only M3 feed |
| Cost model | Defined (spread, commission, slippage) but NOT yet applied to observed data |
| Signal logging | Framework defined; ready for implementation |
| Evaluation | Unstarted — 0 observed trades to date |
| PASS/FAIL/INCONCLUSIVE | PENDING — await observation window completion |
| Shadow validation execution | ❌ NOT STARTED (per mission directive) |

**Next authorized action:** Begin observation at the validated start time, using the cost models defined above. Do not modify strategy logic, parameters, or the frozen candidate.

---

**End of Shadow Validation Protocol.**

**Live capital: $0.00.** No live execution, no live credentials, no live deployment.