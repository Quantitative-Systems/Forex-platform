# Fractal Cross-Timeframe Research Report

> **Historical data-quality study (7 October 2026).** This report describes an earlier HistData run whose strict continuity policy rejected the available histories before performance testing. It has been superseded as the latest strategy result by [FRACTAL_STRUCTURAL_RIDER_REPORT.md](FRACTAL_STRUCTURAL_RIDER_REPORT.md), which evaluates the subsequent Dukascopy BID/ASK campaign. See [current research outcomes](../docs/RESEARCH_OUTCOMES.md).

- Generated: 2026-10-07T12:33:48.909467+00:00
- Verdict: **INSUFFICIENT DATA**
- Live capital: **$0.00**

## A. Executive verdict

No real-market fractal edge can be established from the available data. Shared architecture identity is testable; predictability and tradability are not established.
The architecture identity result does not establish market predictability or economic edge.

## B. Architecture audit

| Existing view | Fractal research view |
|---|---|
| Timeframe sets are run through independent strategy paths. | One canonical state history is reused by role views over the same ladder. |
| HTF context flows into MTF/LTF strategy logic. | Observed state, conditional path, execution eligibility and capital allocation are kept separate. |

Canonical ladder: **1M -> 1W -> 1D -> 4H -> 1H -> 15M -> 3M**.

## C. Existing build validation

- Structure uses strict symmetric pivots, body-close BOS/CHOCH and protected/weak swing helpers. The new engine publishes pivots only after right-side confirmation.
- Existing zone modules cover session levels, FVGs, order blocks, liquidity pools and sweeps. The new snapshots retain structural levels and causal live FVG/OB/liquidity zones.
- The frozen phase vocabulary remains PULLBACK and CONTINUATION; neutral structure conservatively defaults to PULLBACK in the existing classifier.
- The pre-existing institutional_phase_rider.py is not a runnable baseline: malformed indentation prevents import; its legacy timeframe enum also lacks the referenced monthly/weekly values, and the class calls an absent _get_history_df helper. It is excluded from this research. A separate institutional fractal candidate is registered in discovery, requires real M1 data, and remains research/paper-only pending full qualification.
- The research candidate combines HTF continuation and range location, MTF pullback/location with directional FVG or order-block context, and a confirmed LTF break followed by a later zone retest. It deduplicates entries by shared 3M movement identity and applies risk, spread, a cost-adjusted minimum 4R filter, session/rollover and daily realized loss controls.
- Existing contracts carry per-timeframe structure, swings, breaks, zones, phase and trend, but not canonical cross-set identity or independent range location.
- Backtest, qualification, walk-forward, risk, paper, instrument, session, provenance and quality modules remain the platform's canonical implementations.
- Execution accounting fix: bid-quoted bars now fill buys at ask and sells at bid, evaluate short exits on ask OHLC, model gap-through stop slippage, and liquidate open positions at test-window boundaries; regression coverage was added.
- Optional HistData acquisition: the research command can download raw bid/ask tick archives, convert fixed-EST timestamps to UTC, aggregate separate bid/ask M1 OHLC, and preserve vendor provenance. Only very short no-quote gaps and the bounded Sunday reopen gap are carried forward; longer feed gaps stay visible to quality gates.
- Defect fixed: liquidity-pool extraction did not return its populated list; a regression test now covers it.

## D. Fractal state graph and set views

| Set | HTF | MTF | LTF |
|---|---|---|---|
| SET 1 | 1M | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

Each closed candle state is immutable and reused by reference across every set role. Higher states align by a backward close-time lookup.

## E. Cross-timeframe transition results

Not estimable: no symbol passed provenance, quality and coverage gates.

Probabilities describe state transitions, not returns. CIs use Wilson and circular moving-block bootstrap; null p-values have finite resolution and are FDR adjusted.

## F. Nested pullback results

Not measurable on eligible real data. The synthetic smoke fixture is excluded.

## G. Premium/discount results

Each timeframe uses its own confirmed structural range. The exact midpoint is EQUILIBRIUM; lower/higher positions are DISCOUNT/PREMIUM. Competing ranges containing price are marked AMBIGUOUS. Incremental information is not established.

## H. Set correlation results

Sets overlap by definition. Canonical object identity is audited in the JSON; this does not measure statistical return correlation.

## I. One movement / multiple sets

Movement IDs identify confirmed structural legs on the finest available input. Representations are views of that ID. Trade opportunity and duplicated signal counts remain unmeasured because there is no eligible candidate signal stream.

## J. Baseline versus fractal

Existing sweep: 3 experiments, 0 survivors, provenance {'SYNTHETIC': 3}. This short/synthetic artifact is not a valid real-data comparison. No PnL expectancy, PF, win rate, drawdown, OOS or cost-adjusted baseline/fractal comparison is available.

## K. Asset results

- EURUSD: INSUFFICIENT_DATA — no real, quality-passed source with required continuous coverage.
- GBPUSD: INSUFFICIENT_DATA — no real, quality-passed source with required continuous coverage.
- USDJPY: INSUFFICIENT_DATA — no real, quality-passed source with required continuous coverage.
- AUDUSD: INSUFFICIENT_DATA — no real, quality-passed source with required continuous coverage.

## L. Scale results

All five sets are observation-only; no empirical scale has capital eligibility.

## M. Set 5 forensic result

Not determinable from the available cache. M15 cannot construct 3M and no eligible 2-year four-pair M1 history was found. Set 5 remains observation/confirmation only.

## N. Adversarial validation

- Lookahead: stream updates use completed candles; as-of joins use state timestamps.
- Swing leakage: right-side confirmation precedes publication and structural breaks.
- Range leakage: premium/discount uses only confirmed swings; ambiguous ranges suppress the label.
- Cross-set duplication: state snapshots are shared objects; movement IDs deduplicate representations by base structural leg.
- Gap, Sunday open, rollover, spread, commissions, slippage, swaps, news, intrabar collision, target geometry, drift, outliers and regime concentration: not re-simulated in this state-only report; existing execution and risk tests remain separate.
- The candidate is registered for discovery/backtesting only. Its entry cost screen uses bar spread, commission and a fixed slippage reserve; it is not wired to paper/broker execution and does not itself simulate calendar-news blackouts, volatility-based market impact, +2R structural trailing, bid/ask intrabar exits, portfolio heat/exposure or reconciliation.
- No candidate is promoted; selection and winner-concentration claims are therefore unavailable. The institutional fractal candidate is registered but has not been evaluated on eligible data.

## O. Statistical validation

Eligible symbols: none. Distinct source-signature N, chronological 60/20/20 splits, bootstrap, circular-shift null and BH FDR are reported when estimable. Trade expectancy, G1-G8 gates, walk-forward returns, cost shock and top-winner removal require candidate trades and are unavailable.

## P. New edge registry

No new candidate passed promotion; registry is empty.

## Q. Final capital eligibility

- SET 1: **OBSERVATION ONLY**.
- SET 2: **OBSERVATION ONLY**.
- SET 3: **OBSERVATION ONLY**.
- SET 4: **OBSERVATION ONLY**.
- SET 5: **OBSERVATION ONLY**.
- Live capital: **$0.00**.

## R. Test status

Unit tests cover timeframe definitions, canonical state identity, causal updates, premium/discount separation, movement deduplication, graph transitions, null-test resolution, real-data candidate gates, M1-only setup, five-set discovery registration, risk sizing, the frozen 4R floor, account-currency conversion, bid/ask fills, window-end liquidation and the liquidity-pool return regression.

## S. Files and modules

- forex_platform/fractal_engine/timeframes.py — ladder, sets and temporal boundaries.
- forex_platform/fractal_engine/state_engine.py — causal state, swings, structural ranges and zones.
- forex_platform/fractal_engine/state_graph.py — shared role views, graph and movement ledger.
- forex_platform/fractal_engine/hypothesis_engine.py — time-filtered conditional paths.
- forex_platform/fractal_engine/research.py — automated data gate, statistics and report.
- research/FRACTAL_RESEARCH_REPORT.md and research/results/fractal_research.json — generated outputs.

## T. Final scientific conclusion

1. Sets are overlapping views of the specified ladder: **yes by definition**.
2. One canonical state is consistent across set roles: **yes by architecture and identity audit**.
3. Higher timeframe state predicts lower transitions: **not established**.
4. Timeframe-relative premium/discount adds information: **not established**.
5. Nested pullbacks are useful/common in real FX: **not established**.
6. One base structural leg can be represented by several set views: **yes by movement identity**; duplicated trade signals remain unmeasured.
7. Fractal layer improves edge over baseline: **not established**.
8. Opportunity improves without expectancy degradation: **not established**.
9. Set 5 information versus execution economics: **not determinable**.
10. Economically tradable scales: **none established**.
11. Cross-asset generalization: **not established**.
12. Overall status: **INSUFFICIENT DATA**.
13. Freeze the ladder, three-domain Market Model, state identity and current risk/execution invariants; keep all sets observation-only until independent real-data evidence passes promotion gates.

## U. Per-asset / per-set trading qualification

Minimum requested sample: **100 trades per asset/set cell**. Cell tests use isolated set selection and, where eligible real M1 history exists, the platform G1-G8, walk-forward, rolling-window and doubled-cost gates. OOS moving-block tests receive Benjamini-Hochberg FDR correction across the valid cells; passing results remain research-only pending cross-asset review.

| Asset | Set | Trades Dev/Val/OOS/Total | OOS expectancy | 2x-cost expectancy | FDR q | Status |
|---|---|---:|---:|---:|---:|---|
| EURUSD | SET 1 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| EURUSD | SET 2 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| EURUSD | SET 3 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| EURUSD | SET 4 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| EURUSD | SET 5 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| GBPUSD | SET 1 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| GBPUSD | SET 2 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| GBPUSD | SET 3 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| GBPUSD | SET 4 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| GBPUSD | SET 5 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| USDJPY | SET 1 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| USDJPY | SET 2 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| USDJPY | SET 3 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| USDJPY | SET 4 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| USDJPY | SET 5 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| AUDUSD | SET 1 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| AUDUSD | SET 2 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| AUDUSD | SET 3 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| AUDUSD | SET 4 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |
| AUDUSD | SET 5 | — | — | — | — | NOT_RUN_INSUFFICIENT_REAL_DATA |

No synthetic run is counted toward these trade minimums. A cell below 100 trades is insufficient sample; positive backtest metrics alone do not establish a profitable edge.
