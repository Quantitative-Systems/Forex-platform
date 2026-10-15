# SHADOW VALIDATION PROTOCOL : CORRELATED MULTI-TIMEFRAME FRACTAL MODE

**Document:** S2  
**Reference:** Existing shadow validation protocol at `research/audits/provenance_audit_20261014_02/shadow_validation_protocol.md`  
**Live capital:** $0.00 ( Law 7: Human confirmation required)  
**Status:** DEFINED BUT NOT STARTED — per mission directives  

---

## 1. WHY NEW VALIDATION RULES ARE NEEDED

The existing shadow validation protocol I documented in the audit is a *baseline* intended for any strategy. For the **correlated multi-timeframe strategy framework**, follow-on comment is needed because:

- **Sets Do Not Agree on Direction:** Multiple sets can issue opposite signals on the same bar as a feature (not defect)
- **Cross-Set Correlation:** The same bar detection overlaps (SET_1.LTF = SET_2.HTF), so correlation must be measured, not assumed
- **Premium/Discount Zones:** Dynamic zones rather than fixed-ATR with 4R RR are emphasized

---

## 2. VALIDATION STRUCTURE — SET BASELINE & CORRELATED COMPARISON

The shadow validation could operate in two independent tracks:

### Track A: Existing (Baseline) Validation [Already Defined]
**File:** `research/audits/provenance_audit_20261014_02/shadow_validation_protocol.md`

**Status:** Unstarted, in force for any single-set strategy  
**Duration:** 60 calendar days minimum

### Track B: Correlated Phase Validation [This Document]
**Status:** Unstarted, elected strategy-specific  
**Would coexist with Track A validation as a separate window**
**Duration (proposed):** 60 calendar days minimum (matched scaling)

---

## 3. ENHANCED SIGNAL LOGGING FOR CORRELATED MODE

When correlated mode is active, the following additional fields must be logged per tick-step:

```json
{
  "timestamp_utc": "2026-10-15T00:03:00+00:00",
  "tick_id": "uuid",
  "correlated_mode": true,
  "phase_matrix": {
    "SET_1_HTf_Bull?": {"mode": "ACTIVE", "mom_score": 0.62, "signal": "WAIT_PULLBACK_PH"},
    "SET_2_HTf_Bull?": {"mode": "ACTIVE", "mom_score": 0.52, "signal": "WAIT_MTF_SETUP"},
    "SET_3_HTf_Bull?": {"mode": "ACTIVE", "mom_score": 0.31, "signal": "WAIT_LTF_ENTRY"},
    ...
  },
  "conflict_map": {
    "GBPUSD": {
      "preferred_by": "SET_1",
      "conflicting_sets": ["SET_3"]
    }
  },
  "pz_vector": {
    "SET_1": -0.42,
    "SET_2": -0.13,
    "SET_3": +0.01,
    "SET_4": +0.05,
    "SET_5": +0.08
  },
  "key_zone_intensity": {
    "SET_1": { "active": true, "pct_age": 0.65, "test_count": 3 },
    "SET_2": { "active": true, "pct_age": 0.30, "test_count": 1 },
    "SET_3": { "active": false },
    ...
  },
  "set_execution": {
    "active_iters": 2,
    "signal_present": true,
    "signal_filtered_reason": "GOOD_ENTRY_BUT_2_SETS_AGREE"
  }
}
```

This schema extension ensures that:
1. The correlated state per set is visible in logs
2. Phase states across timeframes are identifiable without re-simulation
3. Shadow validation can later reconstruct whether an existing entry was correlated with other sets
4. Selection bias from observing correlated trades may be analytically estimated

---

## 4. COST MODEL — CORRELATED ADJUSTMENT

### Baseline per-Trade Costs (from solid validation's original protocol)

| Component | R Contribution* |
|---|---|
| Spread (via 1.8 pips average) | 0.6R |
| Commission (via $7/round-turn) | 0.07R |
| Slippage (via 0.2 pips + ATR Expansion) | 0.1R |
| **Total per filled trade** | **~0.8R** |

*R units assume risk fraction = 1% of account equity.

### Correlated Mode Penalty

When multiple sets are actively signaling on the same tick:

- If corr = 1.0 (all return same direction): penalized RR by 0.05R per additional aligned set
- If corr = 0.5 (partial agreement): penalized RR by 0.03R per recently aligned set
- If corr < 0.5: No penalty (independent trade rationale)

```
corr_penalty_trade = 0.05R × max(0, num_active_sets_for_direction(sign) - 1)
```

This forces stronger reward/risk ratios to be proposed when multiple sets align, so the expected cost of correlated over-concentration is ceded.

---

## 5. EVIDENCE STANDARD FOR CORRELATED MODE

For correlated mode challenging baseline shadow validation:

### Prometheus Toward Success Criteria

| Category | Baseline Existing | Correlated Mode Tracking |
|---|---|---|
| Minimum final completed trades | 20 | ≥20 correlated-base trades (set-sourced) |
| Win rate after costs | > 15% | > 15% |
| Profit factor after costs | > 1.2 | > 1.2 |
| Net expectancy (cost-adjusted) | > 0R | > 0R |
| Max drawdown after costs | < 15% | < 20% (because extra trades) |
| Bonferroni-adjusted PF | > 1.2 final test retestable with Bonferroni 1.0 | > 1.2 |

**Correlated mode advantages:**
- More signals expected per day → May reach 20 completed trades sooner than 60 days
- High philosophical coherence because directions across timeframes may be correlated
- Fewer spread entries during strongly-(correlated)-combination scenarios (due to penalty)

**Related to bias coefficients (c0):**
- The Bonferroni adjustment compensates for test selection bias (1 candidate out of 100 was selected).
- In the same way, a correlated strategy uses **two additional parameter families** (set correlations + penalty magnitudes) beyond the base style matrix.
- Therefore the correlation bias correction is at least equivalent (and potentially more conservative): **c0 ≥ 2.0** standard corrections are recommended.

---

## 6. INTERNATIONAL PAPER TRADING BRIDGE TIMELINE

Track A baseline current state remains *unstarted*. When it begins, correlated mode could open in parallel.

If during Track A, the strategy behaves as expected with baseline costs:
- **Continue** with Track A
- If Track A marginally underperforms but correlated phase tracking is still not competing with other pairs' microstructure → run Track B

If validated, **authorize transition to live simulation-first execution** (but still Law 7 guard).

---

## 7. HOSTILE SUNDOWN MEASURE (Force Correlated Mode Shutdown)

The following conditions allow ending shadow validation analysis earlier:

| Condition | Coverage | Regulation |
|---|---|---|
| Correlation shedding > 80% in one tick | Compare next bar without correlated logic | Resume baseline |
| Num correlated decisions < 10% over period (force separate) | Tabulate daily stats | Resume baseline |
| Estimated PF net of correlated costs drops below 1.0 | Re-evaluating continuously | Audit warntrend continue |
| Repeat patterned do-overs at typical hours | Time-of-day aggregate logs | Fail closed conditions, restart baseline |

---

### APP-end

This specification describes necessary extensions for shadow-validation of the correlated multi-timeframe fractal strategy. It is **unstarted** in compliance with mission directives. No strategy logic has been modified. The frozen candidate is preserved.
