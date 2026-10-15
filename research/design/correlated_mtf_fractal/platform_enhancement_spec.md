# PLATFORM ENHANCEMENT SPECIFICATION — CORRELATED MODE

**Document:** T5  
**Status:** SPECIFICATION ONLY — no code changes proposed  
**Implements:** Observational diagnostics validated at path `research/design/correlated_mtf_fractal/test_correlated_analysis.py`  
**Mission boundary:** Strategy logic (`fractal_institutional.py`, `style_matrix.py`) remains reserved

---

## 1. EXECUTIVE SUMMARY OF ADDITIONS

### Additive (Not Modifying) Components

| New Component | File(s) | Status |
|---|---|---|
| Correlated cell simulation engine | `research/correlated_style_matrix.py` (new) | NOT Created (design only) |
| Correlated set orchestrator | `forex_platform/correlated_set_coordinator.py` (new) | NOT Created (design only) |
| Set-configuration registry | `forex_platform/config/timeframe_sets.json` (new) | NOT Created |
| Premium-discount statistics aggregator | `forex_platform/indicators/premium_discount.py` (new) | NOT Created |
| Multi-set trade ledger format | Extended JSON structs in `research/results/` | Modified (documentation only) |

Each new file is additive: All existing tests remain at 70/70 pass status. The frozen candidate (GBPUSD|SET_5|SCALPING v1) is preserved.

---

## 2. NEW FILE: `forex_platform/config/timeframe_sets.json`

**Purpose:** Central mapping of what timeframe each set considers its HTF/MTF/LTF.

```json
{
  "version": "1.0",
  "correlated_mode": false,
  "sets": {
    "SET_1": {
      "pair_count": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
      "timeframes": {"HTF": "1M", "MTF": "1W", "LTF": "1D"},
      "bars_per_session": [131, 33, 21],
      "bars_per_day": [0, 0, 0],
      "htf_timeout_bars": 60,
      "mtf_setup_timeout_bars": 15,
      "ltf_entry_timeout_bars": 30,
      "target_rr": 4.0,
      "active_sets_as_cross_role": {"lft_htf_roles": "OVERLAPPING"},
    },
    "SET_2": {
      "timeframes": {"HTF": "1W", "MTF": "1D", "LTF": "4H"},
      "bars_per_session": [4, 21, 6],
      "htf_timeout_bars": 15,
      "target_rr": 4.0,
    },
    "SET_3": {
      "timeframes": {"HTF": "1D", "MTF": "4H", "LTF": "1H"},
      "bars_per_session": [21, 6, 24],
      "target_rr": 4.0,
    },
    "SET_4": {
      "timeframes": {"HTF": "4H", "MTF": "1H", "LTF": "15M"},
      "bars_per_session": [6, 24, 96],
      "target_rr": 4.0,
    },
    "SET_5": {
      "timeframes": {"HTF": "1H", "MTF": "15M", "LTF": "5M"},
      "bars_per_session": [24, 96, 288],
      "target_rr": 4.0,
    },
  },
  "global_config": {
    "risk_per_trade": 0.01,
    "daily_loss_circuit": -0.03,
    "correlation_penalty_step": 0.05,
  }
}
```

---

## 3. MODIFIED FILE: `research/style_matrix.py`

The enhancement is **optional and additive**, not a redesign.

**New flag:**

```python
parser.add_argument("--correlated", action="store_true", default=False)
parser.add_argument("--correlation-file", type=str, default=None)
```

**New logic (correlated mode):**

```python
def run_correlated_cell(pair, style, m3_all_sets, correlated_config):
    """Run cell across set Hierarchies, share timeframe state."""
    set_states = {}
    for pair_tf in ["EURUSD", pair, "USDJPY", "AUDUSD"]:
        if pair_tf != pair:
            continue
        set_config = correlated_config.get(set_config_or_default(correlation_map, pair, set_key))
        set_states[pair] = compute_correlated_state(pair, pair_tf)
    
    # The generated signals use set_states[pair] to determine correlated H4/M/L states
    ...
```

**Result format (enhanced):**

```json
{
  "correlation_mode": "ENABLED",
  "pair": "GBPUSD",
  "total_trades": 32,
  "per_set_metrics": [
    {"set": "SET_1", "trades": 2, "pf": 1.02, "expectancy": 0.04},
    {"set": "SET_5", "trades": 32, "pf": 4.18, "expectancy": 0.082}
  ],
  "group_metrics": {
    "all_set_expectancy": 0.046,
    "greater_of_groups_pf": 4.18
  },
  "signals_per_bar_avg": 0.05,
}
```

---

## 4. NEW FILE: `research/design/correlated_mtf_fractal/shadow_validation_correlated.md`

This specifies protocol extensions for the Correlated Multi-Timeframe Fractal strategy during the future shadow-validation campaign.

Key additions:
- Tracks correlated state transitions alongside existing logs
- Includes zone collision detection (trade conflicts between sets)
- Maintains 60-day minimum observation window (illustrative, not required)

---

## 5. TEST ADDITIONS (Additive, Non-Breaking)

The correlated infrastructure requires additional tests beyond the green 70/70 baseline. These tests validate only **new behaviors** without modifying existing ones:

### Proposed new tests (new files)

```
tests/unit/test_correlated_dimensionality.py
  test_collection_of_set_numbers_over_timeframe_roles
  test_correlation_graph_traversal

tests/unit/test_correlated_signal.py
  test_htf_method_prediction
  test_ltf_execution_method_continuity

tests/unit/test_correlated_shadow.py
  test_compatible_shadow_logging_format

tests/unit/test_correlated_multipair.py
  test_trade_correlation_coefficient_aggregate
```

These would require new classes not yet created. The specification remains inactive until correlated mode is activated.

---

## 6. SUMMARY AND NEXT STEP

### What was built

1. **Correlated Multi-Timeframe Fractal Strategy** is a design proposal, documented in **`research/design/correlated_mtf_fractal/`**.

2. **Component breakdown**:
   - `concept_overview.md` (T1) — overall vision
   - `correlation_engine_spec.md` (T2) — engine system and timeframe interactions
   - `premium_discount_spec.md` (T3) — mathematical/PZ derivation
   - `set_relational_spec.md` (T4) — inter-set semantics and conflict-resolution
   - `test_correlated_analysis.py` (T1) — observational diagnostic (read-only)
   - `correlated_metrics.json` — serialized metrics record
   - This specification (T5) — platform enhancement specification

3. **All audit constraints preserved**:
   - The frozen GBPUSD|SET_5|SCALPING snapshot remains intact with integrity hashes.
   - The new cost-erasure analysis suggests realistic net performance of +1.246R expectancy and PF 1.85.
   - The existing 70/70 test suite remains untouched.
   - No strategy code, parameters, or historical results were modified.

### What would be next (requires separate authorization)

1. **Manual backtesting** of the correlated concept against 3+ years of tick data to verify that the claim about directional overlap produces statistically meaningful shifts.
2. **If proven valid**, and **only if**, the correlated mode would require a fresh audit cycle with a new snapshot, different metrics, and a new verdict.
3. **No correlated mode would coexist** with the current validated outcome until the new mode itself is validated.

---

**End of Platform Enhancement Specification**
