# Final Terminal Report — Fractal Structural Rider

Generated (UTC): 2026-10-08T08:53:30.609974+00:00
Overall verdict: **FALSIFIED**

## A. Executive verdict

This is a causal, research-only result. Passing the gates would identify a candidate for further independent validation; it cannot establish guaranteed or future profitability.

## B. Twenty-cell performance matrix

Signals were arbitrated across the shared seven-timeframe ladder, then assigned to the set with the highest cost-adjusted structural R:R. Set cells are not independent portfolios.

| Pair | Set | Trades | Win rate | Avg win R (IS) | Exp. R (IS) | PF (full) | Max DD % | WFE | Cost stress Exp. R | A-D | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| USDJPY | SET_1 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| USDJPY | SET_2 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| USDJPY | SET_3 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| USDJPY | SET_4 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| USDJPY | SET_5 | 1 | 100.00% | 1.891 | 1.891 | 999.000 | 0.000% | 0.000 | 1.760 | FAIL/FAIL/FAIL/PASS | REJECTED |

Gate order: A full-window trades ≥80; B IS average winner ≥4.0R and expectancy ≥+0.35R; C annualized OOS/IS return ≥0.50; D full-window stress expectancy ≥0R.

### Data quality and coverage

| Pair | Vendor bars | Coverage years | Strict midweek gaps | Sparse gaps (≤5 missing minutes) | Holiday gaps | Other gaps >5 minutes | Largest other gap (minutes) | Price/quote anomalies | Eligibility |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| USDJPY | 1,030,671 | 2.766 | 2,778 | 2,733 | 13 | 32 | 107 | 0 | ELIGIBLE_WITH_GAP_WARNINGS |

The generic strict audit treats any absent minute as a gap. Dukascopy M1 is tick-built, so no-tick minutes are not emitted; the strategy uses raw vendor bars and does not fabricate or forward-fill candles. Eligibility here requires real-vendor provenance, ≥2 years, no bid/ask OHLC anomalies, and no unexplained nonholiday gap of 120 minutes or more. Shorter unexplained gaps are reported as warnings. A synchronized roughly 107-minute gap occurs on GBPUSD, USDJPY, and AUDUSD on 2024-12-17; results on those pairs have that feed interruption caveat.

## C. Cross-set deduplication audit

- Raw qualifying signals: 1
- Unique physical movement IDs with a signal: 1
- Extra set representations of same-time movements: 0
- Conflicting opposing signals dropped: 0
- Each accepted movement is allocated once to the set with highest net expected R:R.

## D. Dealing-range value-add

Entry outcomes below are grouped by each trade's causal HTF, MTF, and LTF range location at signal time.

| Pair | Set role | Location | Trades | Mean R |
|---|---|---|---:|---:|
| USDJPY | SET_5:HTF | DISCOUNT | 1 | 1.891 |
| USDJPY | SET_5:MTF | DISCOUNT | 1 | 1.891 |
| USDJPY | SET_5:LTF | PREMIUM | 1 | 1.891 |

## E. Scale tradability

- SET_1: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/1 cells passed all gates; 0/1 had positive OOS expectancy.
- SET_2: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/1 cells passed all gates; 0/1 had positive OOS expectancy.
- SET_3: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/1 cells passed all gates; 0/1 had positive OOS expectancy.
- SET_4: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/1 cells passed all gates; 0/1 had positive OOS expectancy.
- SET_5: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/1 cells passed all gates; 0/1 had positive OOS expectancy.

## F. Asset generalization

- USDJPY: all-gate sets=none; positive OOS expectancy sets=none.

## G. Surviving edge candidates

None. No asset/set allocation cleared all four hard gates.

## H. Direct scientific answers

1. Are the five sets correlated views of one continuous ladder? **Yes by construction**; the same closed-timeframe states feed every role.
2. Does higher-timeframe state predict lower-timeframe transitions? **Not established by trade expectancy alone**; a separate transition-information test is needed.
3. Are nested pullbacks more frequent and monetizable at ≥4R? **Not established unless the state-transition frequency and all-gate trade results support it.**
4. Does Set 5 retain information after micro-spread friction? **See Set 5 OOS and 2× spread stress metrics above; no pass means no economic edge is established.**
5. Final fractal-hypothesis verdict: **FALSIFIED**.

## Data and model limits

- Source is real Dukascopy historical BID/ASK M1 candles; spread is measured from the aligned open/close quote sides. No synthetic candles or fixed synthetic spread were used.
- Base commission is $7 per standard lot round turn. Static swap assumptions are shown in the JSON and are not broker-specific historical rates.
- A historical Tier-1 macro-news calendar was not available, so the requested 30-minute news blackout was not applied. The reported campaign therefore does not satisfy that requested filter; no events were guessed or fabricated.
- Equal-high/low pools are clustered from the latest 24 confirmed swings on each timeframe to keep liquidity levels recent and scale-relative.
- The research results do not authorize live trading and do not guarantee profits.
