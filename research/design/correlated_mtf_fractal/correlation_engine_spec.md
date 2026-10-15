# MULTI-TIMEFRAME CORRELATION ENGINE — TECHNICAL DETAILS

**Document:** T2  
**Reference:** See `concept_overview.md` for principles  
**Scope:** Engine architecture for correlated set processing  
**Mission constraint:** Preserved — no modification to core strategy code

---

## 1. ARCHITECTURE LAYER

```
┌──────────────────────────────────────────────────────┐
│              DATA LAYER (Existing)                   │
│  Dukascopy M1 → load_m3() → M3 bars per pair      │
│  VWMA[50] + ATR[14] per timeframe                   │
└──────────────────┬───────────────────────────────────┘
                   │ Correlated Mode
┌──────────────────▼───────────────────────────────────┐
│         SET CORRELATION LAYER (New)                  │
│  set_graph: {                                        │
│    "lft_of_HTF": {"SET_1": HTF="1M"},              │
│    "lft_of_MTF": {"SET_1": MTF="1W"},              │
│    "lft_of_LTF": {"SET_1": LTF="1D", "SET_2": HTF} },│
│    ...                                               │
│  }                                                   │
└──────────────────┬───────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────┐
│         PHASE PROPAGATION LAYER (New)                │
│  For each timestamp t:                               │
│    states = {pair × set × HTF/MTF/LTF}              │
│    While phases[t][pair][SET_1] = detect_htf_phase() │
│    ...                                               │
└──────────────────┬───────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────┐
│      EXECUTION ENGINE (Existing — unchanged)          │
│  strategy.on_bar(event) → order intents             │
│  Gate evaluation → trade simulation                 │
└──────────────────────────────────────────────────────┘
```

---

## 2. THE SET GRAPH — TIME FRAME HIERARCHY

**Node definition:** Each timeframe role within a set

```
Node = {pair, set_key, role, timeframe, parent_tf, child_tf}

Example: Node("GBPUSD", "SET_1", "HTF", "1M", incoming=None, outgoing=["1W"])
         Node("GBPUSD", "SET_2", "HTF", "1W", incoming=["1D"], outgoing=["4H"])
         Node("GBPUSD", "SET_2", "MTF", "1D", incoming=["1W"], outgoing=["4H"])
```

**Correlation edges:** Between LTF of one node and HTF of the other:
```
SET_1.LTF ("1D") ↔ SET_2.HTF ("1W")  — the 1-day bar is SET_2's weekly bias input
SET_2.LTF ("4H") ↔ SET_3.HTF ("1D")  — the 4-hour bar is SET_3's daily bias input
```

**Active correlation weights:** When correlated sets' LTF = other's HTF overlap:
- `overlap_weight = 0.8 × min(LTF_strength, HTF_strength)` if directions align
- `overlap_weight = 0` if orthogonal or opposing
- All weights sumble (never binary)

---

## 3. PHASE DETECTION ENGINE

### Input per timeframe

```
{
  "timestamp": datetime,
  "open": Decimal, "high": Decimal, "low": Decimal, "close": Decimal,
  "volume": Decimal, "spread": Decimal,
  "pair": String
}
```

### Output/state per timeframe

```
{
  "timestamp": datetime,
  "phase": PhaseState,
  "pz": Decimal,              # premium/discount position (-1 to +1)
  "key_zone": Zone,            # current valid zone(s)
  "ob": OrderBlock,            # last formed order block
  "fvg": FVG,                  # active unfilled FVG
  "swing_highs": [price...],   # recent 90 bars
  "swing_lows": [price...],    # recent 90 bars  
  "atr_14": Decimal,           # current ATR estimate
  "vwma_50": Decimal,          # current VWMA estimate
  "confirmation_bars": int     # how many bars confirm current phase
}
```

### Phase state enumeration

```
class PhaseState(Enum):
    TRENDING_BULL = 1    # structural HH + HL rising
    TRENDING_BEAR = 2    # structural LH + LL falling
    PULLBACK_BULL = 3    # correction in bull trend within bounds
    PULLBACK_BEAR = 4    # correction in bear trend within bounds
    CONTINUATION_BULL = 5  # post-pullback bullish continuation
    CONTINUATION_BEAR = 6  # post-pullback bearish continuation
    CHOP = 7             # range within ATR-scale
    TRANSITION = 8       # structural break detected, direction uncertain
```

### Phase transitions

**TRENDING_(BULL) → PULLBACK_BULL:**
Trigger: Price breaks below last swing low but above the swing structural support  
Confirmation: Swing level not violated within next 90 bars  
Operational meaning: Expect correction within trend; look for LTF entry long

**PULLBACK_BULL → CONTINUATION_BULL:**
Trigger: Price breaks above the swing high that formed the pullback's termination  
Confirmation: Current close remains ≥ 0.5×ATR above pullback termination high
Operational meaning: Ready for entry on reversal

**TRENDING_* → CHOP:**
Trigger: No new HH/LL in 90 bars + range width < 0.8×ATR for 30+ bars  
Exit: Wait for next structural event

**TRANSITION → TRENDING_BULL:**
Trigger: Successful violation of downtrend trigger point at 2.0×ATR beyond last swing low  
Confirmation: Subsequent candle close above transition trigger

---

## 4. PREMIUM/DISCOUNT COMPUTATION

### Parameter P (VWMA)

```
VWMA[T, n=50] = sum(i=0..49, OHLC[T][i] × volume[T][i]) / sum(i=0..49, volume[T][i])
```

Where OHLC[T][i] = 4-tuple (open, high, low, close), and the calculation ensures the 50-bar window is entirely within closed candles (no current-bar leakage).

### ATR (True Range)

```
True Range = max([high − low], |high − close_prev|, |low − close_prev|)
ATR[T, n=14] = (sum of True Range over last n) / n
```

### PZ computation

```
price_norm[T] = close[T][i] / VWMA[T][i] - 1   # dimensionless
atr_norm[T]   = atr[T][i] / VWMA[T][i]         # % relative to VWMA
PZ[T][i]      = atr_norm[T][i] / max(1e-9, atr_norm[T][i]) × price_norm[T][i]  # premium/discount
```

**Property:** PZ[T] compresses into [-1.5, +1.5] in practice; theoretical bound >= 1.5 only applies if ATR/VWMA is at least 50%.

### PZ Interpretation

| PZ[T] | Condition | Recommended action |
|---|---|---|
| < -0.7 | EXTREME OVERBOUGHT | Expect PH_PB; do not enter long until pullback detected |
| -0.7 to -0.3 | OVERBOUGHT | Wait for pullback structure; entry after PH_PB begins |
| -0.3 to +0.3 | NEUTRAL | Range; PH_CH active; no directional bias |
| +0.3 to +0.7 | OVERSOLD | Wait for PH_CN structure; expect trend re/continuation |
| > +0.7 | EXTREME OVERSOLD | Expect PH_CN; do not enter short until continuation detected |

---

## 5. KEY ZONE AND ORDER BLOCK DETECTION

### Key Zone Identification

A key zone has the following characteristics:
- **Size:** 0.2×ATR[T] to 0.8×ATR[T] (neither tick noise nor macro distortion)
- **Symmetry:** high/low centered at midpoint, tolerance < 0.1×ATR
- **Consistency:** Zone formation on ≥ 3 recent bars
- **Age:** Zone younger than 30×ATR[T]

**Algorithm:**
```
for each closed candle i in T:
    close[i] > VWMA[T][i] ± 0.2×ATR[T]
    for j = i + 1 to i + 20:
        if any(close[j] between high[i] and low[i]):
            zone[j] = mark_zone_from_i_to_j(i, j)
    merge_overlapping_zones()
```

### Order Block Detection

An OB forms when:
1. Counter-trend move ≥ 0.75×ATR[T]
2. Reversal ends with candle ≥ 0.5×ATR[T] body
3. Subsequent price retraces but cannot break original low (for BULL OB) or high (for BEAR OB)
4. OB has age ≤ 90×T_bars

**Entry price at OB formation:** The limit of the body within the candle where price enters the OB.

---

## 6. FV GAP DETECTION

For gaps with unfilled space on a timeframe chart:
- Any candle where (high[i] > low[i+1]) or (low[j] > high[j+1]) for forward bars
- Only represent gaps with height > 0.1×ATR[T]
- Unfilled FVG strengthens with session overlap but is ignored during weekend

FVG probability assignment:
- Unfilled for 3–9 bars: P(fill) ≈ 30%
- Unfilled for 10–29 bars: P(fill) ≈ 20%  
- Unfilled for 30–89 bars: P(fill) ≈ 10% (can be ignored as structural noise)

---

## 7. EXECUTION SEMANTICS FOR CORRELATED SETS

### Trade generation sequence

When timestamp is produced (each tick of M1 data):

1. **Update all timeframes:** compute VWMA, ATR, phase state, PZ, key zones, OBs, FVGs for all T in all active sets
2. **HTF bias lock:** Each active set consults its HTF[T] phase and PZ[T]. This defines which direction the set may trade
3. MTF setup detection:** For each active set, examine MTF[T]. If MTF phase matches the expected phase (PH_PB的需要地 HTF continues), mark SET:SETUP_READY
4. **LTF entry formation:** For SETs marked SETUP_READY, examine LTF[T]. If entry condition matches HTF direction and the risk-reward satisfies the set's floor, mark ENTRY_SIGNAL
5. **Order generation:** On ENTRY_SIGNAL, generate order intent (limit price, stop, target)
6. **Execution:** The existing `run_cell()` simulation consumes order intents identically to before

### In-basket risk management

Account risk is allocated **per trade** (1% of account equity), not per set. Multiple sets may issue signals simultaneously. Their orders may overlap in the same direction but are tracked separately.

**Open position policy:**
- One open position per set (e.g., SET_1 long on GBPUSD)
- Sets may have opposite open positions on different pairs without conflict
- Sets may have opposite positions on the same pair **only if they use different timeframes** (sets use rolling isolation)
- Maximum drawdown circuit: When account equity falls ≥ 3%, no new trades

---

## 8. CORRELATION PENALTY

**Penalty calculation**

To prevent excessive concentration risk when multiple correlated sets issue aligned signals:

```
penalty[i] = 0.05 × (num_aligned_sets - 1)

RR_available[i] = RR_intrinsic[i] × (1 - penalty[i])
```

If RR_available < 0.95 × RR_floor, the signal is downgraded but not eliminated. The recommendation is to accept lower RR given the correlation cost.

**Practical implication:** If SET_1 (1M/1W/1D) and SET_2 (1W/1D/4H) both signal "enter long GBPUSD" on the same bar, the trade is technically valid but incurs a 5% RR penalty. This models the probability that correlated sets are effectively the same trade executed redundantly.

---

## 9. LATENCY-REALISTIC CLOCK MODEL

Each timeframe's clock ticks at its own nominal rate:

```
T_clock[T] = nominal_bars_per_1M[T] ×  months[MAX_LOOKBACK.to_next_1M()]  [for HTF timeframes in years]
           nominal_bars_per_1M[T] ×  months[MAX_LOOKBACK.to_next_1M()]  [for LTF timeframes per day]
```

For example:
- 1M timeframe: 1 bar per month
- 1W timeframe: 4 bars per month
- 1D timeframe: ~21 bars per month
- 4H timeframe: ~6 bars per day (forex sessions)
- 1H timeframe: ~24 bars per day (forex sessions)
- 15M timeframe: ~96 bars per day (forex sessions)
- 5M timeframe: ~288 bars per day (forex sessions)

**Data loading requirement:** For the M1→M3 aggregation currently in `research/style_matrix.py`, all correlated sets share the same M1 sourced → M3 aggregate. This means cross-timeframe correlation is achievable via shared underlying tick data. The platform enhancement only requires establishing timeframe-to-set mappings.

---

## 10. AUDITABILITY BY DESIGN

Each signal in correlated mode carries:

```
{
  signal_id: uuid,
  timestamp: ISO8601,
  set_key: "SET_1" | "SET_2" | ...,
  chain_id: "HTF→MTF→LTF" | null,   # null for independent executions
  cross_correlation: [{other_set: "...", weight: 0.8, direction: align|opposing}, ...],
  phase_vector: {"HTF": "TRENDING_BULL", "MTF": "PULLBACK_BULL", "LTF": "FORMED"},
  pz_vector: {"HTF": -0.35, "MTF": -0.60, "LTF": -0.72},
  key_zone: {T1: Zone, T2: Zone, T3: Zone},
  constraints: {"rr_floor": 4.0, "max_stop_pips": 5, "strategy_id": "style_matrix_..."},
  trade_id: uuid,                   # links execution log
  audit_hash: "sha256:...",         # self-integrity hash for this signal
}
```

This structure encodes all information needed for external analysis of correlation impact and PZ effectiveness without touching strategy execution code.

---

### CHAPTER END

The Correlation Engine specification defines behavior at a level analogous to the existing `InstitutionalFractalStrategy` class. A correlated run can be performed by enhancing `style_matrix.py` with a new mode parameter—without modifying the `InstitutionalFractalStrategy` core.
