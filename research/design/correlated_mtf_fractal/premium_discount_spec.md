# PREMIUM/DISCOUNT ZONE SPECIFICATION — CORRELATED MULTI-TIMEFRAME FRACTAL STRATEGY

**Document:** T3  
**Reference:** Concept Foundation (T1), Correlation Engine (T2)  
**Purpose:** Mathematical and computational specification for premium/discount zone detection  

---

## 1. THE PREMIUM-DISCOUNT CONCEPT

Premium and discount are not simply "overbought" and "oversold." They are **dynamic bands around the 50-period VWMA** that adapt to ATR:

- **Premium:** Price is significantly above VWMA relative to typical average true range
- **Discount:** Price is significantly below VWMA relative to typical average true range

Both are time-dependent — a 1-day premium zone is different from a 1-hour premium zone, and their boundaries scale with ATR.

### Relationship to phases per timeframe

| Phase | Premium/Discount Relationship |
|---|---|
| PH_T (Trending) | Price typically resides in mid/neutral zone; PZ → 0 |
| PH_PB (Pullback) | Price moves toward opposite zone; PZ approaches ±0.7 boundary |
| PH_CN (Continuation) | Price reverses direction toward PZ ≤ ±0.3 |
| PH_CH (Chop) | PZ remains near zero; range is ±0.5×ATR |

---

## 2. MATHEMATICAL FOUNDATIONS

### Volume-Weighted Moving Average (VWMA)

The 50-period VWMA approximates the institutional fair-value anchor used by large allocators:

```
VWMA[T][n=50] = sum( P[T][i] × V[T][i] ) over i = 1..50
               ─────────────────────────────
              sum( V[T][i] ) over i = 1..50
```

Where:
- `P` is the reference price (typically close; trade-offs between [close] vs [(open+high+low+close)/4] are minimal)
- `V` is the tick volume for candle i
- `T` is the timeframe (e.g., 1M means 50 one-month bars)

**Constraint:** VWMA is computed only after 50+ bars of data exist.

### Average True Range (ATR)

ATR measures volatility independent of direction:

```
True Range[t] = max(high[t] − low[t], |high[t] − close[t-1]|, |low[t] − close[t-1]|)

ATR[T][n=14] = (1/n) × Σ True Range T_bars back to T.bars_current_index
```

Where `n_bars_back` is from the immediately preceding T-bar interval.

**Timeframe scaling:** ATR must be computed over T_bars of the PRIMARY timeframe of each set (i.e., ATR must be fetched for 1M bar, 1W bar, etc., not from M1 subintervals).

---

## 2A. THE PREMIUM/DISCOUNT CALCULATION

Define the **PZ** (Premium/Discount Zone position) for each timeframe bar:

```
price_norm = close[i] / VWMA.T[i]     # dimensionless
atr_norm   = atr[i] / VWMA.T[i]       # dimensionless

PZ[i] = price_norm / atr_norm          # scaled: price location relative to VWMA/ATR

Equivalent formulation:
PZ[i] = (close[i] − VWMA.T[i]) / atr[i]
```

**Interpretation boundaries:**

| PZ Value (absolute) | Condition | Zone |
|---|---|---|
| PZ < -0.7 | Extreme overbought | Premium extreme |
| -0.7 ≤ PZ < -0.3 | Moderate overbought | Premium zone |
| -0.3 ≤ PZ ≤ +0.3 | Neutral | Fair-value zone |
| +0.3 ≤ PZ ≤ +0.7 | Moderate oversold | Discount zone |
| PZ > +0.7 | Extreme oversold | Discount extreme |

### Time-scale considerations

**Long timeframes (SET_1/SET_2):**
- Zone width proportional to 2.0×ATR (wider zones)
- Monthly bias requires larger adjustments to account for trend maturity

**Intermediate timeframes (SET_3):**
- Zone width proportional to 1.5×ATR (default)
- Daily bias uses moderate zones — responds to microstructure without excessive noise

**Short timeframes (SET_4/SET_5):**
- Zone width proportional to 1.0×ATR (tighter zones)
- Ensures shorter-duration indicators don't lag longer-timeframe structures

---

## 3. PHASE-DEPENDENT PZ EFFECT

The actual premium/discount zone a price lands in depends on the current phase. A price close to PZ = -0.7 during PH_PB vs. PH_CN has fundamentally different meanings:

| Phase | PZ = -0.7 | PZ = +0.7 |
|---|---|---|
| PH_T BULL | Expect pullback soon | — |
| PH_T BEAR | — | Expect pullback soon |
| PH_PB BULL | Already deep correction | — |
| PH_PB BEAR | — | Already deep correction |
| PH_CN BULL | (Bound fails) — | Continue impulse |
| PH_CH | Neutral | RG_B / CHOP |

**Premium := high probability of reversal down.** **Discount := high probability of reversal up.** The "high probability" is derived from the count of wins vs. losses per zone in the last 90 bars — either manual or through an aggregator.

---

## 4. KEYZONE FUSION WITH PZ

The concept of key zones (from T1 §4) coexists with premium/discount zones. A key zone is **structural** (established by price reactions at sampled swings), while PZ is **dynamic** (computed from current price ÷ VWMA).

### Priority rules

1. **Key zone overrides structural premium/discount:** If price is inside an active key zone (e.g., SET_2 key zone [1.2785, 1.2820]), the set should behave as if a zone was created on that key zone (regardless of PZ state).

2. **PZ completes premium/discount depth measurement:** Even inside a key zone, price can approach either end of the zone. PZ quantifies how deep within the zone price is.

3. **Zone cancellation:** When both PZ and zone conditions agree:
   - BULL: PZ < -0.3 (pullback) AND key_zone_low within tolerance of price
   - BEAR: PZ > +0.3 (pullback) AND key_zone_high within tolerance of price

4. **Reject conflict:** When PZ says reversal imminent but key zone says continuation, resolve by higher timeframe authority (e.g., SET_1.HTF key zone overrides SET_2 key zone).

---

## 5. COMPUTATIONAL ALGORITHM

For timestep t at timeframe T in set S:

```
ATM[T] ← load_atr(T)
VWMA[T] ← load_vwma(T)

PZ[T] ← (close[t] - VWMA[T][t]) / max(1e-9, ATR[T][t])   # handle zero-ATR

zone_id = candidate_zone(S, T, close[t])
zone_a, zone_b = zone[T][zone_id]

if phase[HTF][t] == PH_T_BULL and PZ[T] < -0.3:
    waiting_for_pullback = True
    setup_init = (close[t] − zone_b) / (zone_b − zone_a)

    if setup is forming:
        ready_for_ltf = True
```

---

## 6. INTERACTION WITH THE CASCadingFractalCoordinator

The platform already has a `CascadingFractalCoordinator` that computes canonical state for each timeframe. For premium/discount, the coordinator would extend its tuple (phase, confirmation_bars, structural_levels, key_zone) with:

```
coordinator.tuple[state] = (
    phase,
    confirmation_bars,
    structural_levels,
    key_zone,
    zone_width,         # ATR-scaled
    zone_center,        # VWMA_estimated
    zone_labels: List[str],     # HTF/MTF/LTF roles sharing this zone
)
```

The canonical state becomes an **timeframe-aware parametric zone descriptor**. Each set can ask the coordinator:

- What is the VWMA for this timeframe right now?
- What is the ATR for this timeframe?  
- Am I in premium or discount relative to this timeframe's VWMA?
- Do I have a valid key zone with overlapping HTF zone?

This does not change coordinator computation logic — it extends the output fields.

---

### CHAPTER END

The premium/discount system is a **timeframe-specific measure** that quantifies how far price is from fair value, scaled by volatility. It differs from traditional Bollinger Bands in using VWMA (an institutional anchor) and ATR (volatility normalization). This design yields dynamic, phase-aware zones that complement the static structural key zone logic already in place.
