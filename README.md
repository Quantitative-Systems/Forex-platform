# Forex Platform — Institutional Quantitative Trading Research Framework

A production-grade, research-only platform for causal multi-timeframe structural analysis and strategy validation in spot foreign exchange. Built for rigorous statistical elimination, not backtest overfitting.

> **Research Status — 11 October 2026:** The fractal structural rider campaign is in active validation. All institutional upgrades (two-stage OTE entry, ATR stop buffering, +2.0R break-even, +3.5R structural trailing, session liquidity filter, higher-timeframe correlation filter) are implemented and verified. The full 3-year, 4-pair, 5-set batch validation is processing. Live capital remains $0. No strategy is authorized for live execution until it clears all predeclared gates on unseen data.

---

## Executive Architecture

### Unified Fractal State Engine — 7-Timeframe Ladder

The platform constructs a single, causally consistent state ladder from raw M1 vendor bars:

**Monthly (1M) → Weekly (1W) → Daily (1D) → 4-Hour (4H) → 1-Hour (1H) → 15-Minute (15M) → 3-Minute (3M)**

Each closed bar updates exactly one state per timeframe. No forward-looking information. All higher timeframes are fully closed before lower timeframes evaluate them.

### Overlapping Hierarchical Sets (5 Causal Views)

| Set | Higher Timeframe (Bias) | Middle Timeframe (Setup) | Lower Timeframe (Entry) | Role |
|-----|------------------------|-------------------------|------------------------|------|
| **SET 1** | 1M | 1W | 1D | Macro Context |
| **SET 2** | 1W | 1D | 4H | Inter-Week Swing |
| **SET 3** | 1D | 4H | 1H | Intra-Week Swing |
| **SET 4** | 4H | 1H | 15M | **Primary Alpha Engine** |
| **SET 5** | 1H | 15M | 3M | Session Execution |

Lower timeframe of one set equals higher timeframe of the next (e.g., SET 5 LTF = 3M = SET 4 MTF). This fractal correlation is enforced by the engine.

### Cross-Set Arbitration

Signals sharing a physical 3M movement compete by net structural R:R. The winning set receives the allocation; conflicting signals are dropped. This preserves a single causal portfolio rather than 5 independent strategies.

---

## Market Model Specification

### Structure (External Bounds → Internal Swings → Breaks)

- **External Bounds**: The two most recent confirmed swing highs and lows that define the structural range at each timeframe.
- **Protected Swings**: Swings that have not been invalidated by a subsequent close. Marked `protected=True`.
- **Weak Swings**: The weaker of the two external bounds (closest to equilibrium). Target for CONTINUATION trades.
- **Break of Structure (BOS)**: A confirmed close beyond a protected swing in the trend direction.
- **Change of Character (CHoCH)**: A liquidity sweep of a swing extreme followed by a reclaim and structural shift.
- **Swing Detection**: Scale-aware pivot lookbacks (1M=2, 1W=3, 1D=3, 4H=5, 1H=5, 15M=3, 3M=3) calibrated for bar density.

### Dealing Range (Dynamic Premium/Discount)

Each timeframe maintains a `StructuralRange` from its external bounds:
- **Equilibrium** = (High + Low) / 2
- **Premium** = Price > 50% of range (upper half)
- **Discount** = Price < 50% of range (lower half)
- **Ambiguous** = No valid external bounds (insufficient structure)

Range location is a continuous [0,1] ratio for precise zone logic, not just categorical labels.

### Phase Engine (Binary Classification)

Every structural movement is classified at completion:
- **CONTINUATION**: HTF trend aligned with HTF corrective zone (Long in Discount, Short in Premium)
- **PULLBACK**: HTF trend opposing HTF corrective zone (eliminated in current implementation — CONTINUATION only)

The MTF must show structural alignment (price in HTF corrective zone, HTF structure not invalidated, MTF test of HTF key zone).

---

## Strategy Execution Harness — `InstitutionalFractalStrategy`

The single active research candidate. All parameters frozen unless explicitly noted.

### 1. Two-Stage OTE Mitigation Limit Entry

**Eliminates rigid 50% equilibrium chasing.** Uses the Optimal Trade Entry (OTE) zone:

- **Impulse Leg**: The structural move that created the trigger (MICRO_BOS, CHoCH, or SWEEP_RECLAIM)
- **OTE Zone**: 38.2% → 61.8% Fibonacci retracement of the impulse leg
- **Limit Price**: Midpoint of OTE zone
- **Expiry**: 12 completed LTF candles (180 minutes on 15M) — extended from 4 candles (60 min)
- **Market Fallback**: If no impulse data, converts to market order at current price

> **Rationale**: Institutional flow completes corrective cycles within the 38.2%–61.8% band. The 50% line is a subset, not the target.

### 2. Volatility-Buffered Structural Stops

**Replaces fixed 2-pip buffer with ATR-scaled insulation:**

```
dynamic_buffer_pips = max(2.0 pips, 0.5 × ATR_15M(14))
stop_price = trigger_swing_extreme ± dynamic_buffer_pips
```

- **Long**: Stop below the swept/reclaimed swing low
- **Short**: Stop above the swept/reclaimed swing high
- **Invalidation**: A close beyond this level invalidates the entry pattern

> **Rationale**: 2 pips is insufficient during volatility expansion; 0.5×ATR adapts while the 2-pip floor prevents over-tightening in calm markets.

### 3. Dynamic De-Risking & Structural Trailing (3-Stage)

| Stage | Trigger | Action | Rationale |
|-------|---------|--------|-----------|
| **Break-Even** | +2.0R favorable excursion | Stop → Entry + 0.1R (covers commissions) | Locks in risk-free trade; prevents round-trip losers |
| **Structural Trailing** | +3.5R favorable excursion | Stop → Most recent MTF protected swing (higher low / lower high) | Trails behind confirmed structure, not arbitrary increments |
| **Hard Target** | HTF External Weak Extreme | Take profit at Weak High (long) / Weak Low (short) | Structural target, not fixed R-multiple |

> **Rationale**: +2.0R break-even is the institutional standard for de-risking. +3.5R structural trailing captures extended moves while respecting market structure. The HTF Weak Extreme is the natural exhaustion point for CONTINUATION trades.

### 4. Session Liquidity Filter (Unified Window)

**Replaces fragmented London/NY windows with a single high-liquidity block:**

- **Active Window**: 06:30 UTC → 17:00 UTC (continuous)
- **Blackout**: 20:30 UTC → 06:30 UTC (Asian session + rollover)
- **Logic**: Only trade when Tier-1 liquidity providers are active. No orders placed, no management actions during blackout.

> **Rationale**: Fragmented windows miss the continuous London-through-NY liquidity cycle. The unified block captures the full professional trading day.

### 5. Higher-Timeframe Correlation Filter (Top-Down Alignment)

**NEW: Mandatory bias confirmation from next higher timeframe:**

```python
NEXT_HIGHER_TIMEFRAME = {
    "3M": "15M", "15M": "1H", "1H": "4H", "4H": "1D",
    "1D": "1W", "1W": "1M", "1M": None
}
```

- **Long (direction=+1)**: Require `higher_state.structural_trend == +1`
- **Short (direction=-1)**: Require `higher_state.structural_trend == -1`
- **No alignment → Signal rejected** at trigger validation stage

> **Rationale**: A 15M trigger in a 4H bearish trend is counter-trend noise. The correlation filter ensures causal consistency across the fractal ladder.

### 6. Risk & Execution Parameters (Frozen)

| Parameter | Value | Constraint |
|-----------|-------|------------|
| Account Risk | 1.0% per trade | Hard cap (0, 0.01] |
| Minimum Net R:R | 4.0 | After estimated spread + commission + slippage |
| Stop Distance | 5–50 pips | Structural invalidation bounded |
| Max Entry Spread | 2.5 pips | Filters illiquid prints |
| Commission | $7/RT standard lot | Dukascopy ECN baseline |
| Slippage | 0.2 pips base + 0.1×ATR expansion | Causal reserve |
| Max Lots | 100 | Position cap |
| Lot Step | 0.01 | Precision |

---

## Data & Validation Framework

### Data Provenance — `REAL_VENDOR` Only

- **Source**: Dukascopy historical BID/ASK M1 candlesticks
- **Coverage**: 2024-01-01 → 2026-10-08 (~2.77 years, ~1.03M bars/pair)
- **Aggregation**: M1 → M3 (3-minute) for execution; causal aligner builds all 7 canonical timeframes
- **Quality Gates**: No OHLC/quote anomalies; midweek gaps reported as warnings; no unexplained gaps >5 min; no synthetic fabrication or forward-fill

### Chronological Partitioning (60/20/20)

| Partition | Purpose | Duration |
|-----------|---------|----------|
| **In-Sample (IS)** | Parameter calibration, edge discovery | First 60% |
| **Validation (VAL)** | Threshold tuning, overfit detection | Next 20% |
| **Out-of-Sample (OOS)** | Blind test, final qualification | Final 20% |

No random splits. No look-ahead. Partition boundaries are fixed calendar dates.

### 4-Gate Elimination Funnel

| Gate | Criterion | Evaluated On | Pass Required |
|------|-----------|--------------|---------------|
| **A** | ≥ 80 full-window trades | Full Window | All cells |
| **B** | IS avg win ≥ 4.0R **AND** IS expectancy ≥ +0.35R | In-Sample | All cells |
| **C** | OOS/IS annualized return ratio ≥ 0.50 | IS vs OOS | All cells |
| **D** | Net expectancy ≥ 0R under 2× spread + stressed slippage | Full Window (stress) | All cells |

**Additional Diagnostics**: Trade funnel telemetry (bars → HTF bias → MTF setup → LTF trigger → RR eval → dispatch), RR histogram at entry, cost-stress delta, walk-forward efficiency.

---

## Quickstart & Reproduction

### Environment Setup

```bash
# Python 3.12+
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows
# source .venv/bin/activate         # Linux/macOS

python -m pip install -e ".[dev]"
python -m pip install -e ".[research]"  # Dukascopy downloader
python -m pytest                      # Verify unit tests
```

### Data Acquisition

```bash
# Download real paired BID/ASK M1 bars (default: 2024-01-01 to now)
python forex_platform/market_data/download_dukascopy_m1.py \
  --symbols EURUSD GBPUSD USDJPY AUDUSD \
  --start 2024-01-01T00:00:00+00:00
```

Output: `data/cache/dukascopy/{SYMBOL}_M1.parquet` + `.meta.json` provenance records.

### Run Full Campaign (5 Sets × 4 Pairs)

```bash
# Full 3-year validation with institutional targets
python research/fractal_structural_rider_batch.py \
  --symbols EURUSD GBPUSD USDJPY AUDUSD \
  --results research/results/fractal_structural_rider_all_sets.json \
  --minimum-years 2.0
```

Expected runtime: 2–3 hours (processes ~2.9M M3 bars across 4 pairs × 2 backtests).

Outputs:
- `research/FRACTAL_STRUCTURAL_RIDER_REPORT.md` — Human-readable terminal report
- `research/results/fractal_structural_rider_all_sets.json` — Machine-readable results

### Run Parameter Sweeper (All Variants × All Sets)

```bash
python research/sweeper.py \
  --symbols EURUSD GBPUSD USDJPY AUDUSD \
  --sets SET_1 SET_2 SET_3 SET_4 SET_5 \
  --variants FRACTAL_STRUCTURAL_RIDER
```

### Unit Tests

```bash
python -m pytest tests/unit/ -v
```

---

## Repository Structure

```
forex_platform/
├── core/                    # Domain math, lot sizing, session windows, instruments
├── market_data/             # ECN loaders, Dukascopy fetcher, causal aligner, provenance
├── market_model/            # Strategy-agnostic structure (swings, breaks), zones (OB, FVG, levels), phases
├── fractal_engine/          # 7-timeframe state ladder, state graph, cross-timeframe hypothesis mapping
├── strategy_engine/         # Active institutional strategy harnesses (fractal_institutional.py)
├── costs/                   # ECN friction, commission, dynamic spread, swap financing models
├── research_engine/         # Event-driven backtester, evaluation framework
├── risk_engine/             # Portfolio risk controls, drawdown limits, exposure management
├── portfolio_engine/        # Allocation, currency exposure, pre-trade checks
└── cli/                     # Command-line interface

research/
├── fractal_structural_rider_batch.py   # Full 5-set campaign runner
├── sweeper.py                          # Parameter sweep across variants/sets
├── FRACTAL_STRUCTURAL_RIDER_REPORT.md  # Terminal report template
├── SURVIVING_EDGES.md                  # Gate-passing edge registry
└── results/                            # JSON outputs (gitignored)

tests/
├── unit/                               # Unit tests (pytest)
└── integration/                        # Integration tests

docs/
├── ARCHITECTURE.md                     # System components & data flow
├── STRATEGY_CATALOG.md                 # Strategy descriptions & evidence requirements
├── PRODUCT_ROADMAP.md                  # Validation milestones
└── RESEARCH_OUTCOMES.md                # Latest results & interpretation
```

---

## License

MIT. Check Dukascopy data-source terms and broker requirements before using external data or connecting an account.

**This is research software. No strategy is profitable until it survives the full gate sequence on unseen data. Live capital is $0.**