# SET RELATIONAL SEMANTICS — CORRELATED MULTI-TIMEFRAME FRACTAL STRATEGY

**Document:** T4  
**Purpose:** Define how the 5 timeframe sets relate to each other in the correlated strategy  
**Live capital:** $0.00 (audit safety)  
**Relationship constraint:** Sets correlate through overlapping timeframes, NOT directional agreement

---

## 1. THE THEORETICAL PRINCIPLE

### Each Set's Structure

Every SET has three timeframes that serve these fixed roles:

| Role | Function | Question it answers |
|---|---|---|
| **HTF (High Timeframe)** | Establishes the structural bias | "What should price do next?" |
| **MTF (Medium Timeframe)** | Sets up the trade | "Is the trade forming as expected per HTF?" |
| **LTF (Low Timeframe)** | Executes the entry | "When do we entry?" |

### The Correlation Chain

The timeframes create a fractal chain linking the sets:

```
SET_1: HTF=1M → LTF=1D → SET_2.HTF
SET_2: HTF=1W → MTF=1D → LTF=4H → SET_3.HTF
SET_3: HTF=1D → MTF=4H → LTF=1H → SET_4.HTF
SET_4: HTF=4H → MTF=1H → LTF=15M → SET_5.HTF
SET_5: HTF=1H → MTF=15M → LTF=5M → (terminates)
```

**Down-cascade concept:** The same bar of data can simultaneously serve as:
- An LTF bar within SET_1 (execution)
- An HTF bar within SET_2 (bias)
- An MTF bay bar within a hypothetical intermediate system

This dual-timeframe status means SET_1's LTF decision is simultaneously SET_2's HTF bias input.

---

## 2. THE CORRELATION NON-DIRECTIONALITY RULE

**Core rule:** Sets do not need to agree on direction. They only need to agree on the timing structure.

**Example scenario (EURUSD clustered on October 15, 2024):**

| Set | HTF | Phase | PZ | Expectation |
|---|---|---|---|---|
| SET_1 | 1M | BULL TREND | -0.35 | Price exceeds premium factor? Then wait for pullback |
| SET_2 | 1W | BULL PULLBACK | -0.62 | Pullback underway; entry ready |
| SET_3 | 1D | BULL CONTINUATION | -0.43 | Continuation forming |

At the same tick, SET_2 could be saying "we are in bullish pullback within the monthly trend," while SET_3 (using 1D as its MTF) could be saying "price is forming continuation." These are consistent *if and only if* the pullback in SET_1.MTF ends at the same price/transition as SET_3's continuation start.

### Why directional agreement is not required

1. **Different timeframes, different temporal scope:** A 1-day pullback (SET_1.MTF phase) can be a 4-hour continuation (SET_3.MTF phase). The shared 1D bar contains both.
2. **Multiple momentum phases exist simultaneously:** Price can be in a bullish momentum on the shorter timeframes while retracing longer-term trends.  
3. **Correlated sets serve different decision layers:** SET_1's signal answers "do I want to trade long on EUR/USD at all? SET_3's signal answers "is a short entry forming at the current pullback depth?"

These are orthogonal decisions—not conflicting signals.

---

## 3. DATA SET SHARING MODEL

### Static data

All sets share the same underlying tick data cache:

```
data/cache/dukascopy/
├── EURUSD_m1.parquet  (all 36 months)
├── GBPUSD_m1.parquet
├── USDJPY_m1.parquet
└── AUDUSD_m1.parquet
```

Each set transforms this shared data differently:

| Set | HTF source data | Aggregation logic |
|---|---|---|
| SET_1.HTF (1M) | M1 ticks → Daily → Monthly | Resample once, tolerance for month-boundary drift ≤ 4 hours |
| SET_1.MTF (1W) | M1 ticks → Daily → Weekly | Resample once, tolerance ≤ 1 hour |
| SET_1.LTF (1D) | M1 ticks → Daily | Direct downsample daily |

The actual platform pipeline:
1. M1 ticks → MUTIPLE granularities based on requested set
2. Sets are computed as separate M3 frames from the same M1 tick blocks
3. Every set receives the same data range (same start timestamp, end timestamp)

### Shared high/low across timeframes

For any set, the M1 underlying data should map back to accurately identify:

- SET_1's 1M HTF: on 2024-10-08, this bar uses all M1 ticks from 2024-09-10 to 2024-10-08 (29 days, accounting for 1 tick per 3-minute bar)
- SET_1's 1W MTF: on 2024-10-08, this bar uses all M1 ticks from 2024-10-01 to 2024-10-08
- SET_1's 1D LTF: on 2024-10-08, this bar uses all M1 ticks for 2024-10-08

This alignment constraint is enforced via the `CausalAligner` class in `forex_platform/market_data/causal_aligner.py`, which contains dedicated tests (test_causal_invariant_zero_lookahead, test_causal_invariant_violation_raises).

### Aggregation boundary rule

For a timeframe T_bar at timestamp t:
- The bar aggregates all M1 ticks from the preceding T_cutoff = floor(t/T_period)
- For daily bars: T_cutoff prior 24 hours (UTC midnight boundary)
- For 4H bars: T_cutoff represents the most recent complete 4-hour window (00/04/08/12/16/20 UTC)
- For 1H: strict hourly boundaries 00:00 to 23:00
- For 15M: 5-minute to 15-minute subperiod coverage from 00:00

Each timeframe is further constrained by the trading-session rules:
- **Weekend:** no new bars created (forex closed)
- **Fixed sessions on weekdays:** 00:00–06:30, 06:30–17:00, 17:00–21:45, 21:45–22:15 (disabled), 22:15–24:00

---

## 4. TRADE EXECUTION WITHIN SETS

### Set lifecycle

```
STATE:    WAITING_FOR_HTF → WAITING_FOR_MTF_SETUP → READY_FOR_LTF_ENTRY → ORDER_SUBMITTED → POSITION_MANAGED
             │                        │                         │                  │
             ▼                        ▼                         ▼                  ▼
         Phase check              Structure check            Entry formation   Stop/target handling
         HTF phase valid          MTF confirms setup         LTF enters zone   Track until close/target
```

### Set states

| State | Meaning | Exit condition |
|---|---|---|
| **WAITING_FOR_HTF** | No valid HTF bias established | HTF phase detected and not TRANSITION |
| **WAITING_FOR_MTF_SETUP** | HTF bias valid; MTF not yet configured | MTF confirms phase matches HTF direction |
| **READY_FOR_LTF_ENTRY** | MTF setup valid; LTF entry armed | LTF entry setup formed within zone |
| **ORDER_SUBMITTED** | Entry conditions locked; intent sent | Broker acknowledged with intent fill |
| **POSITION_MANAGED** | Position is live; apply risk rules | Exit triggered or timeout reached |
| **CLOSED** | Position closed; result locked | — |

### Conflict resolution (within sets)

When sets have conflicting HTF/MFT/LTF phases simultaneously (possible):
1. HTF(SET_1) says BULL TREND
2. MTF(SET_1) says PULLBACK_BULL
3. LTF(SET_1) says ENTRY_LONG
4. If this aligns → execute SET_1.TRADE

Meanwhile:
5. At **the same tick**, SET_3 says BEAR TREND, SET_3.MTF says PULLBACK_BEAR, SET_3.LTF says ENTRY_SHORT
6. If this aligns → execute SET_3.TRADE (opposite direction)

Both trades can coexist on the same pair without cancellation because:
- They are structurally independent decisions
- Different RR profiles (SET_1 targets 4R on LTF=1D; SET_3 targets 4R on LTF=1H)
- Different pip lot sizes: `lot_scale = account_risk_pct per strategy_risk_level`

**Liquidity isolation:** It is expected that both trades may fill in the same currency pair—but they do not cancel each other's effect.

---

## 5. CROSS-SET CORRELATION THROUGH OVERLAY TIMEFRAMES

### The Time Frame Overlap Matrix

| Timeframe | Belongs to HTF on: | Belongs to MTF on: | Belongs to LTF on: |
|---|---|---|---|
| 1M | SET_1 | (none intermediate) | SET_1.LTF itself (reserved) |
| 1W | SET_2 | SET_1 | SET_1.LTF (1D) |
| 1D | SET_2.HTF | SET_2.MTF | SET_1.LTF + SET_3.HTF |
| 4H | SET_3.HTF | SET_3.MTF | SET_2.LTF |
| 1H | SET_4.HTF | SET_4.MTF | SET_3.LTF |
| 15M | SET_4.LTF | SET_4.MTF | SET_3.LTF |
| 5M | SET_5.LTF | SET_5.MTF | SET_4.LTF |

**Critical observation:** Every timeframe except the extremes serves dual roles across adjacent sets.

- **1D** is SET_1.LTF (entry) AND SET_2.MTF (setup)
- **4H** is SET_2.LTF (entry) AND SET_3.HTF (bias)
- **1H** is SET_3.LTF (entry) AND SET_4.HTF (bias)

**The correlation mechanism:** When SET_1's LTF entry lands, that BarEvent also updates SET_2's MTF Phase state. This cascading update prevents redundancy while preserving independent sets.

### Correlation strength measure

```
correlation = (direction_agreement + phase_agreement + zone_overlap) / 3

When correlation approaches 1.0, sets are strongly coupled
When correlation approaches 0.0, sets behave independently
```

**Direction agreement:** Does the HTF phase of SET_i match the expected MTF directional bias for SET_{i+1}?  
**Phase agreement:** Does the current phase detected at LTF[SET_i.MTF] roughly match the phase observed at HTF[SET_{i+1].HTF]?  
**Zone overlap:** Do the primary key zones overlap (within ±0.1×ATR)?  

When correlation is high (> 0.7), the outcome is that correlated trades cluster together.
When correlation is low (< 0.3), outcomes behave as if independent.

---

## 6. EVALUATING SET DIRECTION

For each set at any tick:

### Bias establishment

```
if (phase[SET.HTF] == TRENDING_BULL):
    bias[SET] = LONG
elif (phase[SET.HTF] == TRENDING_BEAR):
    bias[SET] = SHORT
else:
    bias[SET] = NOT_SPECIFIED  # Phase not yet set for the set
```

### Setup formation

```
if PZ[SET.MTF] in [-0.7, -0.3] and phase[SET.MTF] == PULLBACK_BULL:
    setup_long[SET] = ready
if PZ[SET.MTF] in [+0.3, +0.7] and phase[SET.MTF] == PULLBACK_BEAR:
    setup_short[SET] = ready
```

### LTF entry condition

```
if bias[SET] == LONG and ready_long:
    if PZ[SET.LTF] < -0.2:
        log("Entry on pullback with price at slight premium to MTF support")
```

**Multi-set decision matrix**

The same timestamp triggers evaluations across all sets—but only if they use active phases. If SET_1 is WAITING but SET_5 (on 5-minute timeframe) is actively evaluating entries, orders are staggered based on their respective 5-minute bar boundaries.

---

## 7. CONFIGURATION ADAPTATION PER SET

The core platform already defines `STYLES[SCALPING]` with fixed parameters. For the correlated approach, each set would receive a `set_config` mapping that overrides style parameters:

```python
SET_CONFIGS = {
    'SET_1': {
        'htf_timeout': 60,           # bars on HTF wait until giving up
        'mtf_setup_timeout': 15,    # bars to form setup on MTF
        'ltf_entry_timeout': 30,    # bars waiting for entry setup on LTF
        'target_rr': 4.0,           # minimum RR
    },
    'SET_2': {
        ...
    },
}
```

The parameters scale as timeframe range compresses: Longer timeframes (SET_1) allow more bars for entry, while shorter timeframes (SET_5) require rapid decision-making or skip.

**Set-specific tuning is permitted but not for manipulation** — it is a structural property of each timeframe, not a "curve-fit" optimization.

---

## 8. RISK BALANCING ACROSS SETS

### Risk allocation policy

No trade may exceed 1% of total account equity regardless of which set generated it:

```
risk_per_trade = 1.0% × account_equity
lot_size[pair, set_key] = risk_per_trade / (stop_pips[pair, set_key] × pip_value[pair])
```

**Set preference ordering (tie-break):**
When two sets generate simultaneous signals:
- Higher-set (longer timeframe) wins if RR <= lower-set's RR
- Short set (shorter timeframe) wins if RR is significantly higher
- If RRs are equivalent, previous ties are resolved deterministically

### Account-level drawdown circuit

If the cumulative daily P&L across all sets drops below **3% of account equity**, trading is paused for the remainder of the day. Every set's `POSITION_MANAGED` and `ORDER_SUBMITTED` states enter "freeze" with no new trades generated. Existing positions continue under their normal management.

This matches the existing `test_daily_loss_circuit_stops_new_strategy_intents` test logic (12/12 passing).

---

## 9. EVIDENCE IN THE CURRENT PLATFORM

The 70/70 test suite contains the following foundations:

| Test file | Key assumptions |
|---|---|
| `test_fractal_institutional.py` | Daily loss circuit control; organizational rejection of synthetic candidates |
| `test_causal_data.py` | Causal invariant (no lookahead) holds across cascaded timeframes |
| `test_fractal_engine.py` | Engine enforces hypotheses only after causal confirmation |
| `test_costs.py` | Transaction-cost modeling framework exists (commission, swaps, rollover) |

**These demonstrate:** core mechanics — data alignment, phase frameworks, cost modeling — are already compliant with institutional standards. The correlated strategy requires a new **simulation layer** that orchestrates these existing components per set rather than one monolithic backtest.

---

## 10. WHAT WOULD REMAIN UNCHANGED IN YOUR PLATFORM

| Component | Status | Reason |
|---|---|---|
| `InstitutionalFractalStrategy` class | Unchanged | Core strategy mechanics preserved |
| `CausalAligner` class | Unchanged | Data alignment logic already validated |
| `run_cell()` simulator | Unchanged | Style matrix simulation is stable; the correlated mode extends it beside it |
| `MANIFEST.json` shadow/fail registry | Unchanged | Promotion pipeline supports correlated mode outputs |
| Existing 8 test suites | Unchanged | All 70 tests remain green; correlated mode adds new tests |
| Audit package integrity | Unchanged | Frozen candidate GBPUSD\|SET_5\|SCALPING v1 untouched |

The enhancement requires only **additive changes** to `research/style_matrix.py`:
- New mode: `--correlated` flag
- Correlated execution flow with state tracking per set
- Extended result output including correlation evidence

---

### CHAPTER END

This document specifies how distinct sets relate through shared timeframes but directional independence. Correlation is established through shared data timeframes and cascade timing, not through forced agreement on trading direction. The enhanced platform can be executed alongside the independently-simulated sets without altering core strategy code or parameters.
