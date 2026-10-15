# MASTER INDEX — CORRELATED MULTI-TIMEFRAME FRACTAL STRATEGY

**Design series:** T1–T6  
**Audit reference:** AUDIT_20261014_02  
**Status:** DRAFT COMPLETE  
**Live capital:** $0.00  
**Mission constraint:** Strategy code, parameters, and existing results preserved

---

## 1. MISSION OBJECTIVE

Document a **correlated multi-timeframe fractal trading strategy** using the 5 timeframe sets you described:

```
SET_1: 1M (HTF) / 1W (MTF) / 1D (LTF)
SET_2: 1W (HTF) / 1D (MTF) / 4H (LTF)
SET_3: 1D (HTF) / 4H (MTF) / 1H (LTF)
SET_4: 4H (HTF) / 1H (MTF) / 15m (LTF)
SET_5: 1H (HTF) / 15m (MTF) / 5m (LTF)
```

**Critical constraint (design):** Each set has its own statistical environment and dynamically evolving premium/discount levels. Sets correlate through overlapping timeframes (LTF[SET_1] = HTF[SET_2]), not through directional agreement.

---

## 2. DOCUMENT STRUCTURE

| File | Size | Purpose |
|---|---|---|
| `concept_overview.md` | 7.1 KB | Strategy foundation, phase progression, |
| `correlation_engine_spec.md` | 10.4 KB | Architecture, signaling logic, weight computation |
| `premium_discount_spec.md` | 6.9 KB | Mathematical derivation of PZ (premium/discount) metric |
| `set_relational_spec.md` | 13.0 KB | How sets relate/cascade, why directions need not agree |
| `platform_enhancement_spec.md` | 7.3 KB | Code additions required — but NOT made |
| `shadow_validation_correlated.md` | 7.6 KB | Extended validation protocol document |
| `test_correlated_analysis.py` | 12.0 KB | Read-only observational diagnostic |
| `correlated_metrics.json` | 4.2 KB | Output of observational diagnostics |
| **`INDEX.md`** | 1.8 KB | Master index (this document) |

---

## 3. KEY EVIDENCE FROM EXISTING AUDIT

The current audit (AUID: audit_20261014_02, at `research/audits/provenance_audit_20261014_02/`) shows:

### Current Platform Capabilities (Working)

✅ 70/70 unit tests pass (fractal engine, costs, qualification gates, causal data, fractal engine)
✅ Audit package with integrity hashes preserved
✅ Style matrix simulation successfully achieve 32 simulated trades in winning cell
✅ Promotion gate pipeline (research→validation→shadow→human confirmation→live)

### Observable Limitations

⚠️ **Cost modeling gap:** The 32-trade simulation is gross-of-cost. When realistic costs subtract (~0.8R per trade), expectancy drops from +2.046R to *~+1.25R* and PF drops from 3.52 to *~1.85*.

⚠️ **Macro fragmentation:** Only 1 out of 36 macro deep-history cells generates trades (14 trades, EURUSD SET_3 SWING, PF 0.73, FAIL).

⚠️ **No set correlation:** Each cell simulated independently — correlated set viewing isn't part of the existing pipeline.

### What No-Probe Data Establish Empirically

| Claim | Status |
|---|---|
| SET_5.SCALPING shows profit in independent simulation | ✅ observed |
| Correlated sets produce independent directional signals | 🧪 unverified |
| Cross-correlation improves performance empirically | 🧪 unverified |
| Any set documents that the correlated sets generate ≥100 completed trades across label sets | 🧪 unverified |

---

## 4. WHAT THE DESIGN STIPULATES

### Actionable Specifications (Design Level)

1. **Phase detection language** (T1 §2, T2 §3) — five phase states per timeframe measurable independently at present tick.

2. **Phase propagation** (T1 §3, T4 §3) — how HTF bias of SET_1 would calculate expected phase of SET_2's MTF and vice versa.

3. **Premium/discount metric** (T3) — PZ = (price − VWMA[50])/ATR[14], interpretable into cross-timeframe thresholds.

4. **Set graph** (T2 §2, T4 §5) — map of timeframes across sets with dual-role relationships.

5. **Risk allocation** (T4 §8) — 1% per trade, independent across sets, separate limits across long/short simultaneously.

6. **Cost addition policy** (S2 §4) — per-correlated-set penalty 0.05 R; spread/commission/slippage modeled separately.

### NOT ADDRESSED

1. A working correlated simulation that mutate trades automatically
2. Validated evidence of correlated-set profitability
3. Any platform modification to production code
4. Any platform modification to candidate strategy logic

---

## 5. PLATFORM ASSETS: WHAT YOU HAVE

### Existing Functionality (That the Correlated Design Would Extend)

| Feature | Location | Status |
|---|---|---|
| M1-based tick data | `data/cache/dukascopy/EURUSD_m1.parquet` (+ GBPUSD, USDJPY, AUDUSD) | ✅ Present (~200K bars per pair) |
| Timeframe aggregation | `forex_platform/market_data/loader.py::load_m3()` | ✅ Present |
| Strategy engine | `forex_platform/strategy_engine/fractal_institutional.py` | ✅ Present (70/70 tests pass) |
| Gates (min_trades, PF$', RR 'expectancy') | `research/style_matrix.py::GATES` | ✅ Present and enforced |
| Setup persistence | `STYLES[SCALPING]['setup_persistence_bars'] = 4` | ✅ Present |
| Correlated timezone | (new) | 🟡 Design-only |
| Premium/discount zones | (new indicator) | 🟡 Design-only |

---

## 6. DEPLOYMENT ROADMAP

### Stage 1 (Current — COMPLETED)

- ✅ Era audit reconciles *first validated* set (GBPUSD|SET_5|SCALPING)
- ✅ Freeze moment with integrity hashes
- ✅ Average cost impact estimated: expectancy drops 36%, PF drops 47%

### Stage 2 (Proposed But NOT Initiated — Requires separate authorized mission)

- **Manual backtesting of correlated concept** on 3+ years of tick data (≥2 pairs)
- **Validation observational diagnostics** — currently available in `test_correlated_analysis.py`

### Stage 3 (Separate Platform Enhancement Required)

- **New correlated simulation mode** in `style_matrix.py` (documented in `platform_enhancement_spec.md`)
- **Correlated-Set Gate validation**
- If metrics validate, fresh shadow validation from a new unvalidated audit snapshot

---

### A note on practicality

Your concept is ambitious. Considering:
- Only 1/100 independent cells currently passes gate rules → with correlated mode, expect proportionally fewer statistical results unless correlated behavior increases trade volume
- The macro experiment shows 35/36 sets produce insufficient trade count for statistical analysis

These are realistic outlooks — not reasons to decline, but reasons to thresholds high.

---

## 7. INTEGRITY SUMMARY FOR CURRENT WORK

| Component | Checkmark |
|---|---|
| Audit package (audit_20261014_02) | ✅ Frozen snapshot with hashes intact |
| Test suite status (70/70 passing) | ✅ No attempts to modify running framework |
| Strategy code (`fractal_institutional.py`) | ✅ No changes made |
| Stock background experiment | ✅ Left running (as-is macro specimen) |
| Live trading | ❌ Prohibited at $0.00 per Law 7 |
| Shadow validation start | ❌ Deferred per requester's initial instruction |

---

## END

This index catalogues design documentation for a **correlated multi-timeframe fractal strategy construction for future evaluation** near the existing institutional forex platform's framework and audited baseline.
