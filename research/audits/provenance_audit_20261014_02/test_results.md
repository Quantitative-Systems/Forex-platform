# TEST RESULTS — Forensic Audit Reconciliation

**Audit ID:** `audit_20261014_02`  
**Framework:** pytest  
**Total tests executed:** 70  
**Passed:** 70  
**Failed:** 0  
**Skipped:** 0  
**Live capital:** $0.00 (Law 7 enforced)

---

## Test Suite Overview

| Test File | Passed | Failed | Skipped | Notes |
|---|---|---|---|---|
| `tests/unit/test_fractal_institutional.py` | 12 | 0 | 0 | Strategy on_bar, daily loss circuit, candidate rejection |
| `tests/unit/test_costs.py` | 12 | 0 | 0 | Commission model, swap/rollover modeling |
| `tests/unit/test_qualification_gates.py` | 25 | 0 | 0 | G1-G7 gates, walk-forward partitioning |
| `tests/unit/test_causal_data.py` | 9 | 0 | 0 | Causal invariant zero-lookahead, violation raises |
| `tests/unit/test_fractal_engine.py` | 12 | 0 | 0 | Hypothesis-based, FDR-corrected reports |
| **TOTAL** | **70** | **0** | **0** | — |

---

## Key Test Results and Relevance

### 1. `test_fractal_institutional.py` — 12/12 PASS
**Relevance:** Strategy engine behavior, order lifecycle, risk firewall.

| Test Name | Result |
|---|---|
| test_converts_usd_base_quote_pnl_and_fails_closed_on_cross | ✅ PASS |
| test_discovery_rejects_synthetic_fractal_candidate_before_backtesting | ✅ PASS |
| test_discovery_rejects_fractal_candidate_without_m1_resolution | ✅ PASS |
| test_daily_loss_circuit_stops_new_strategy_intents | ✅ PASS |

**Verdict:** Strategy correctly enforces daily loss circuits, rejects synthetic candidates, and respects M1 data resolution requirements.

### 2. `test_costs.py` — 12/12 PASS
**Relevance:** Transaction-cost modeling framework exists and is tested.

| Test Name | Result |
|---|---|
| test_fractional_lot_proportionality | ✅ PASS |
| test_custom_commission_rate | ✅ PASS |
| test_regular_daily_swap | ✅ PASS |
| test_wednesday_triple_swap | ✅ PASS |
| test_apply_rollover_to_position | ✅ PASS |

**Verdict:** Cost modeling framework is implemented and tested, but the style_matrix.py simulation does NOT use it. This confirms the "gross" nature of the original metrics.

### 3. `test_qualification_gates.py` — 25/25 PASS
**Relevance:** G1-G7 gating, walk-forward partitioning, split data handling.

| Key Gate | Result |
|---|---|
| G1–G7 automated discovery and qualification | ✅ PASS |
| Walk-forward partition creation | ✅ PASS |
| Data split by partition | ✅ PASS |

**Verdict:** Qualification gates are properly implemented. The style matrix gate (min_trades=30, min_pf=1.2, max_dd=0.20, min_expectancy=0.0) is a subset of the full G1-G7 pipeline.

### 4. `test_causal_data.py` — 9/9 PASS
**Relevance:** Causal integrity of data loading and alignment.

| Test Name | Result |
|---|---|
| test_causal_invariant_zero_lookahead | ✅ PASS |
| test_causal_invariant_violation_raises | ✅ PASS |
| test_csv_and_json_loading | ✅ PASS |
| test_downsample_m1_to_m5 | ✅ PASS |
| test_intraweek_drop | ✅ PASS |

**Verdict:** Causal data invariants hold — zero-lookahead enforcement is verified. Any violation raises an error, confirming the framework prevents look-ahead bias.

### 5. `test_fractal_engine.py` — 12/12 PASS
**Relevance:** Engine-level hypothesis testing, FDR correction, campaign safety.

| Test Name | Result |
|---|---|
| test_hypothesis_uses_only_prior_observations_and_never_executes | ✅ PASS |
| test_fdr_and_null_test_report_finite_resolution | ✅ PASS |
| test_campaign_fails_closed_for_synthetic_data_and_writes_smoke_report | ✅ PASS |

**Verdict:** Engine-level tests confirm that hypotheses use only prior observations and never execute live orders. FDR-corrected reports are finite and well-formed.

---

## Regression Suite Status

| Category | Status | Evidence |
|---|---|---|
| Strategy engine | ✅ ALL PASS (12/12) | on_bar, risk circuit, candidate rejection |
| Cost modeling | ✅ ALL PASS (12/12) | Commission, swap, rollover |
| Qualification gates | ✅ ALL PASS (25/25) | G1-G7, walk-forward |
| Causal data | ✅ ALL PASS (9/9) | Zero-lookahead, violation detection |
| Fractal engine | ✅ ALL PASS (12/12) | Hypothesis testing, FDR |
| **OVERALL** | ✅ **70/70 PASS** | **No failures, no skips** |

**Note:** All available regression tests pass. No test failures were encountered. The test suite confirms that the strategy engine, cost model, qualification gates, causal data invariants, and fractal engine all operate correctly per their specifications.

**No test modifications were made** to strategy logic or parameters. All tests run against the existing codebase unchanged.