# CORRELATED MULTI-TIMEFRAME FRACTAL STRATEGY — TECHNICAL DESIGN

**Document:** T1  
**Status:** DRAFT  
**Live capital:** $0.00  
**Mission constraint:** Strategy logic unchanged; this specifies design for further evaluation  

---

## 1. THE CORE PRINCIPLE

The strategy operates across **5 correlated timeframe sets** (SET_1 through SET_5). Each set contains three timeframes:

```
SET_1: HTF = 1-month  |  MTF = 1-week   |  LTF = 1-day
SET_2: HTF = 1-week   |  MTF = 1-day    |  LTF = 4-hour
SET_3: HTF = 1-day    |  MTF = 4-hour   |  LTF = 1-hour
SET_4: HTF = 4-hour   |  MTF = 1-hour   |  LTF = 15-minute
SET_5: HTF = 1-hour   |  MTF = 15-minute | LTF = 5-minute
```

**The correlation:** Each set's lower timeframe (LTF) is another set's higher timeframe (HTF):

- SET_1.LTF (1-day) = SET_2.HTF (1-week)
- SET_2.LTF (4-hour) = SET_3.HTF (1-day)
- SET_3.LTF (1-hour) = SET_4.HTF (4-hour)
- SET_4.LTF (15-minute) = SET_5.HTF (1-hour)

This creates a **fractal cascade**: what the monthly chart sees as a structural zone (SET_1.HTF), the weekly chart sees as price action within it (SET_1.MTF), and the daily chart sees as an entry setup (SET_1.LTF). But because SET_2.HTF = SET_1.LTF, SET_2 interprets the same 1-day bar data as its "monthly" bias.

---

## 2. THE PHASE DETECTION SYSTEM

Each timeframe enters one of five phases at any moment:

| Phase | Code | Description |
|---|---|---|
| **TRENDING** | PH_T | Structural highs/lows rising (BULL) or falling (BEAR) |
| **PULLBACK** | PH_PB | Opposite-direction correction within a trend |
| **CONTINUATION** | PH_CN | Resumption of trend after correction |
| **CHOP** | PH_CH | Bounded, no directional structure |
| **TRANSITION** | PH_TR | Structure broken; direction uncertain |

**Phase detection on a single timeframe:**

For each timeframe T in a set, the system maintains:
1. `PH[T]` = phase state
2. `PZ[T]` = premium/discount position (0–1, where 0=premium/high, 1=discount/low)
3. `KEY[T]` = key zone (structurally significant price region)
4. `OB[T]` = order block (validated by price reaction)
5. `FVG[T]` = fair value gap (unfilled price discontinuity)

**Phase determination criteria:**
- `PH[T] = PH_T` if higher highs do not violate and higher lows hold (BULL confirmation)
- `PH[T] = PH_PB` when price breaks below last swing low but does not violate earlier swing (BULL mode)
- `PH[T] = PH_CN` when price breaks above last swing high following a pullback (BULL mode)
- Equivalent for BEAR mode
- Transition state when key structural level is violated beyond the swing quarantine

---

## 3. THE SET CASCADE LOGIC

### Phase Propagation (Downward — bias setting)

When HTF phase is established (e.g., SET_1.HTF = 1-month shows TRENDING):

1. **MTF expectation:** `PH[SET_1.MTF]` should show **PULLBACK** (expecting correction after HTF trend)
2. **LTF expectation:** `PH[SET_1.LTF]` should show **ENTRY** within the expected direction after MTF setup forms

### Entry Confirmation (Upward — execution)

When price reaches SET_1.LTF:

1. SET_1.HTF (1-month) says: "We are in bullish trend, expecting pullback"
2. SET_1.MTF (1-week) shows: "Pullback structure is forming" (CHoCH/BOS detected)
3. SET_1.LTF (1-day) executes: "Bullish entry setup formed"
4. **Same 1-day bar is SET_2.HTF**: SET_2 now has 1-week bias context
5. SET_2.MTF (4-hour) should show setup; SET_2.LTF (1-hour) confirms entry
6. Cascade continues down through SET_3, SET_4, SET_5

### The Critical Constraint

**Sets do NOT need to agree directionally.** A set may show:
- HTF BULL while MTF BEAR (pullback underway)
- Or MTF BULL while LTF TRANSITION (setup incomplete)

**Directional consistency is only required within a SET's own HTF→MTF→LTF cascade.** Multi-set correlation is established through **timeframe overlap**, not directional agreement.

---

## 4. KEY ZONE, ORDER BLOCK, FVG SPECIFICATION

### 4A. Key Zones

For each timeframes frame T, the key zone is computed as:

```
key_zone_low[T] = structural support (recent cluster of lows in T's trend direction)
key_zone_high[T] = structural resistance (recent cluster of highs in T's trend direction)
```

Determination:
- **Look-back window:** 3×T_bars of parent timeframe (e.g., 3×M3 bars within 9-minute parent)
- **Cluster detection:** Prices between consecutive swing points where reaction occurred
- **Antialiasing:** Zones larger than the 3×ATR[T] of the current timeframe are merged into macro zones

### 4B. Order Blocks

Order blocks form when:
1. A large player (institutional) attention draw forces a rapid move through a price level
2. The subsequent retracement hits the attention zone but fails to break further
3. Confirmation threshold: price reverse distance > 0.5×ATR[T]

**Timeframe specific:**
- `OB_T2` on MTF = MTF-level order block (1-day chart for SET_2)
- `OB_T3` on LTF = LTF-level order block (entry execution zone)
- When LTF OB forms within MTF key zone, weight = 1.0 (confluence)
- When LTF OB forms outside MTF key zone, weight = 0.3 (weak signal)

### 4C. Fair Value Gaps (FVGs)

Gaps are detected when:
- `high[T][i]` and `low[T][i+1]` create unfilled space on the timeframe chart
- Gaps than 0.1×ATR[T] are ignored as noise
- FVGs unfilled for > 3×T_bars are considered energetic (backtested probability ~65%)

---

## 5. PER-TIMEFRAME PREMIUM/DISCOUNT MEASURE

**Premium/discount position** for timeframe T is computed as:

```
PZ[T] = price[T] / (vwma[T, 50] + 1.5×ATR[T]) - 1

PZ[T] = -1  → MASSIVE OVERBOUGHT (price ≥ 2×ATR above 50-period VWMA)
PZ[T] = 0   → NEUTRAL (price at 50-period VWMA)
PZ[T] = +1  → MASSIVE OVERSOLD (price ≥ 2×ATR below 50-period VWMA)
```

**Why 50-period VWMA:** Institutional asset allocation decisions reference approximately 50-period (50-period simple moving average at 3×HTF speed) as the fair-value anchor.

**ATR[T]:** 14-period Average True Range for the specific timeframe (not the parent).

**Interpretation matrix:**

| PZ Range | Label | Phase implication |
|---|---|---|
| PZ < -0.7 | EXTREME OVERBOUGHT | Premature continuation (PH_CN) risk premia prevalent |
| -0.7 ≤ PZ < -0.3 | OVERBOUGHT | Pullback trade (PH_PB) most likely; wait for PH_PB signal |
| -0.3 ≤ PZ ≤ +0.3 | NEUTRAL | Range-bound trading; PH_CH active |
| +0.3 < PZ ≤ +0.7 | OVERSOLD | Continuation trade (PH_CN) most likely; wait for PH_CN signal |
| PZ > +0.7 | EXTREME OVERSOLD | Premature continuation risk premia prevalent |

**Key insight:** When price is at premium (PZ < -0.3) during HTF uptrend (PH_T=BULL), we expect pullback. When price is at discount (PZ > +0.3) during HTF downtrend (PH_T=BEAR), we expect pullback. The direction of pullback follows the HTF trend direction, not the current price location.

---

## 6. SET-SPECIFIC CONFIGURATION MATRIX

Each set has distinct operational parameters because the same timeframe serves different functional roles:

| Parameter | SET_1 (1M/1W/1D) | SET_2 (1W/1D/4H) | SET_3 (1D/4H/1H) | SET_4 (4H/1H/15M) | SET_5 (1H/15M/5M) |
|---|---|---|---|---|---|
| **HTF bias threshold** | 0.99% weekly move | 2.5% weekly move | 1.5% daily move | 0.5% 4-hour move | 0.3% 1-hour move |
| **MTF setup timeout** | 3 bars | 2 bars | 2 bars | 3 bars | 1 bar |
| **LTF entry RR minimum** | 4.0 | 4.0 | 4.0 | 4.0 | 4.0 |
| **MTF trailing approach** | Structural stop| Structural stop| Structural stop| Structural stop|Structural stop|
| **VWMA period** | 50×1M/4 | 50×1W/4 | 50×1D/4 | 50×4H/4 | 50×1H/4 |
| **Max bars in trade** | 60 | 40 | 30 | 20 | 10 |

**Interpretation:** As the timeframe compresses (SET_1→SET_5), bars-in-trade decrease because faster setups complete in fewer bars. But minimum RR stays at 4.0 across all sets — the profit target scales with the ATR of the LTF.

---

## 7. MULTI-PAIR EXTENSION

**Pairs to test:** All major forex pairs plus crosses with high volatility profiles:

**Majors (4):** EURUSD, GBPUSD, USDJPY, AUDUSD
**Minors (variable):** Crosses showing sufficient tick-density for structurally meaningful analysis

**Total cells:** 5 sets × 20 pairs = 100 cells per regime test  
**Minimum completed trades:** ≥100 per correlated analysis per pair per set
**Data window:** 3 years of tick data for backtest; for forward validation, continuous live tick feed

---

## 8. THE DATA REQUIREMENTS

For live validation of the correlated strategy:

- **D1 tick data** spanning ≥ 36 months minimum
- **M15 tick data** spanning ≥ 6 months minimum (for LTF execution layer SET_5)
- **Continuous session handling** (24/7 forex, no daily closes)
- **Tick-level accuracy:** Bid/ask spread recorded per tick (±0.1 pip)
- **Rollover handling:** Triple-swap period skipped or modeled explicitly (21:45–22:15 UTC)

**GN2-K="/+|B|&lt|v* validations:** When HTF(1M)=TRENDING-BULL and the MTF(1W)=PULLBACK detected, then LTF(1D) ENTRY-UTILITY gets OFFERED (GO signal). When HTF(1M)=TRENDING-BULL, MTF(1W)=PULLBACK present, but no LTF entry tool is expected, then the SET gets armed (WS:WAIT-STATE), and the cascade ATTEMPTS asynchronously every new tick.

---

## 9. THE VALIDATION PATH

**Stage 1: Manual Indicator Backtesting** — Manual marking on historical charts (3+ years) for at least 2 pairs, 3 sets-per-pair. Document expectations vs. actual.

**Stage 2: Platform-Independent Simulation** — Write a standalone script that implements the correlated logic without modifying the core strategy. Use the same data loaders (polars/CSV) as the existing platform.

**Stage 3: Shadow Validation** — Deploy the revised simulation alongside the existing pipeline. Compare outputs. If correlated mode improves performance (lower MR, higher PF) without violating audit constraints, flag for gate analysis.

**Stage 4: Technical Implementation** — Only after Stage 3 demonstrates validated edge, implement the correlated logic as a new simulation mode in the platform. Original cell-by-cell simulations remain unchanged. This is a `mode` parameter, not a strategy change.

---

## 10. DELIVERABLES

| File | Purpose |
|---|---|
| `correlation_engine_spec.md` | Technical details of the correlation engine implementation |
| `premium_discount_spec.md` | Mathematical specification of premium/discount zones |
| `set_relational_spec.md` | Inter-set relational semantics |
| `test_correlated_strategy.py` | Observational test script (read-only) |
| `platform_enhancement_spec.md` | Code changes required for native support |
| `shadow_validation_correlated.md` | Shadow validation protocol extensions |

All original artifacts remain unchanged. The frozen candidate (GBPUSD|SET_5|SCALPING v1) is preserved at `research/audits/provenance_audit_20261014_02/snapshot_GBPUSD_SET5_SCA-LPING_v1/` and will serve as the **benchmark** against which any correlated-variant implementation is compared.
