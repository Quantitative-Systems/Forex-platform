# Final Terminal Report — Fractal Structural Rider

Generated (UTC): 2026-10-14T03:00:01.195304+00:00
Overall verdict: **FALSIFIED**

## A. Executive verdict

This is a causal, research-only result. Passing the gates would identify a candidate for further independent validation; it cannot establish guaranteed or future profitability.

## B. Twenty-cell performance matrix

Signals were arbitrated across the shared seven-timeframe ladder, then assigned to the set with the highest cost-adjusted structural R:R. Set cells are not independent portfolios.

| Pair | Set | Trades | Win rate | Avg win R (IS) | Exp. R (IS) | PF (full) | Max DD % | WFE | Cost stress Exp. R | A-D | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| EURUSD | SET_1 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| EURUSD | SET_2 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| EURUSD | SET_3 | 14 | 14.29% | 1.071 | -0.882 | 0.138 | 10.189% | — | -0.628 | FAIL/FAIL/FAIL/FAIL | REJECTED |
| EURUSD | SET_4 | 67 | 5.97% | 5.550 | -0.577 | 0.459 | 32.179% | — | -0.570 | FAIL/FAIL/FAIL/FAIL | REJECTED |
| EURUSD | SET_5 | 177 | 6.78% | 5.709 | -0.645 | 0.325 | 72.082% | — | -0.727 | PASS/FAIL/FAIL/FAIL | REJECTED |
| GBPUSD | SET_1 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| GBPUSD | SET_2 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| GBPUSD | SET_3 | 15 | 6.67% | 0.010 | -0.817 | 0.001 | 12.781% | — | -1.063 | FAIL/FAIL/FAIL/FAIL | REJECTED |
| GBPUSD | SET_4 | 41 | 4.88% | 0.595 | -1.127 | 0.171 | 34.165% | — | -1.139 | FAIL/FAIL/FAIL/FAIL | REJECTED |
| GBPUSD | SET_5 | 168 | 6.55% | 8.214 | -0.394 | 0.564 | 69.062% | — | -1.001 | PASS/FAIL/FAIL/FAIL | REJECTED |
| USDJPY | SET_1 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| USDJPY | SET_2 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| USDJPY | SET_3 | 5 | 40.00% | 25.607 | 5.579 | 11.896 | 3.255% | 0.005 | 4.305 | FAIL/PASS/FAIL/PASS | REJECTED |
| USDJPY | SET_4 | 26 | 15.38% | 4.535 | -0.074 | 0.618 | 20.283% | — | -0.491 | FAIL/FAIL/FAIL/FAIL | REJECTED |
| USDJPY | SET_5 | 118 | 9.32% | 7.022 | -0.113 | 0.563 | 49.854% | — | -0.985 | PASS/FAIL/FAIL/FAIL | REJECTED |
| AUDUSD | SET_1 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| AUDUSD | SET_2 | 0 | — | — | — | — | — | — | — | — | REJECTED |
| AUDUSD | SET_3 | 6 | 16.67% | 0.053 | -0.556 | 0.015 | 5.501% | — | -1.074 | FAIL/FAIL/FAIL/FAIL | REJECTED |
| AUDUSD | SET_4 | 46 | 2.17% | 0.991 | -1.024 | 0.028 | 38.838% | — | -1.262 | FAIL/FAIL/FAIL/FAIL | REJECTED |
| AUDUSD | SET_5 | 101 | 6.93% | 2.963 | -0.892 | 0.249 | 57.474% | — | -1.136 | PASS/FAIL/FAIL/FAIL | REJECTED |

Gate order: A full-window trades ≥80; B IS average winner ≥4.0R and expectancy ≥+0.35R; C annualized OOS/IS return ≥0.50; D full-window stress expectancy ≥0R.
Profit factor is shown as ∞ when a sample has wins but no losses; this is not informative with the very small trade counts in this campaign.

### Data quality and coverage

| Pair | First bar UTC | Last bar UTC | Vendor bars | Coverage years | Strict midweek gaps | Sparse gaps (≤5 missing minutes) | Holiday gaps | Other gaps >5 minutes | Largest other gap (minutes) | Price/quote anomalies | Eligibility |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| EURUSD | 2024-01-01 | 2026-10-08 | 1,030,511 | 2.766 | 3,409 | 3,357 | 43 | 9 | 10 | 0 | ELIGIBLE_WITH_GAP_WARNINGS |
| GBPUSD | 2024-01-01 | 2026-10-08 | 1,028,962 | 2.766 | 4,026 | 3,972 | 35 | 19 | 107 | 0 | ELIGIBLE_WITH_GAP_WARNINGS |
| USDJPY | 2024-01-01 | 2026-10-08 | 1,030,671 | 2.766 | 2,778 | 2,733 | 13 | 32 | 107 | 0 | ELIGIBLE_WITH_GAP_WARNINGS |
| AUDUSD | 2024-01-01 | 2026-10-08 | 1,028,212 | 2.766 | 4,537 | 4,463 | 45 | 29 | 108 | 0 | ELIGIBLE_WITH_GAP_WARNINGS |

The generic strict audit treats any absent minute as a gap. Dukascopy M1 is tick-built, so no-tick minutes are not emitted; the strategy uses raw vendor bars and does not fabricate or forward-fill candles. Eligibility here requires real-vendor provenance, ≥2 years, no bid/ask OHLC anomalies, and no unexplained nonholiday gap of 120 minutes or more. Shorter unexplained gaps are reported as warnings. A synchronized roughly 107-minute gap occurs on GBPUSD, USDJPY, and AUDUSD on 2024-12-17; results on those pairs have that feed interruption caveat.

## C. Cross-set deduplication audit

- Raw qualifying signals: 1146
- Unique physical movement IDs with a signal: 1146
- Extra set representations of same-time movements: 0
- Conflicting opposing signals dropped: 0
- Each accepted movement is allocated once to the set with highest net expected R:R.

## D. Dealing-range value-add

Entry outcomes below are grouped by each trade's causal HTF, MTF, and LTF range location at signal time.

| Pair | Set role | Location | Trades | Mean R |
|---|---|---|---:|---:|
| EURUSD | SET_3:HTF | PREMIUM | 8 | -0.354 |
| EURUSD | SET_3:HTF | DISCOUNT | 6 | -0.951 |
| EURUSD | SET_3:MTF | PREMIUM | 8 | -0.624 |
| EURUSD | SET_3:MTF | DISCOUNT | 6 | -0.591 |
| EURUSD | SET_3:LTF | PREMIUM | 6 | -0.634 |
| EURUSD | SET_3:LTF | DISCOUNT | 8 | -0.591 |
| EURUSD | SET_4:HTF | DISCOUNT | 37 | -1.057 |
| EURUSD | SET_4:HTF | PREMIUM | 30 | 0.299 |
| EURUSD | SET_4:MTF | PREMIUM | 31 | -0.921 |
| EURUSD | SET_4:MTF | DISCOUNT | 36 | -0.044 |
| EURUSD | SET_4:LTF | PREMIUM | 33 | -0.520 |
| EURUSD | SET_4:LTF | DISCOUNT | 34 | -0.382 |
| EURUSD | SET_5:HTF | PREMIUM | 90 | -0.415 |
| EURUSD | SET_5:HTF | DISCOUNT | 87 | -0.919 |
| EURUSD | SET_5:MTF | PREMIUM | 87 | -0.434 |
| EURUSD | SET_5:MTF | DISCOUNT | 88 | -0.872 |
| EURUSD | SET_5:MTF | EQUILIBRIUM | 2 | -1.372 |
| EURUSD | SET_5:LTF | PREMIUM | 83 | -0.630 |
| EURUSD | SET_5:LTF | DISCOUNT | 93 | -0.684 |
| EURUSD | SET_5:LTF | EQUILIBRIUM | 1 | -1.321 |
| GBPUSD | SET_3:HTF | PREMIUM | 14 | -0.893 |
| GBPUSD | SET_3:HTF | DISCOUNT | 1 | -1.106 |
| GBPUSD | SET_3:MTF | DISCOUNT | 7 | -0.904 |
| GBPUSD | SET_3:MTF | PREMIUM | 8 | -0.909 |
| GBPUSD | SET_3:LTF | PREMIUM | 11 | -0.869 |
| GBPUSD | SET_3:LTF | DISCOUNT | 4 | -1.012 |
| GBPUSD | SET_4:HTF | PREMIUM | 23 | -1.249 |
| GBPUSD | SET_4:HTF | DISCOUNT | 18 | -0.453 |
| GBPUSD | SET_4:MTF | PREMIUM | 21 | -1.212 |
| GBPUSD | SET_4:MTF | DISCOUNT | 20 | -0.571 |
| GBPUSD | SET_4:LTF | DISCOUNT | 25 | -0.777 |
| GBPUSD | SET_4:LTF | PREMIUM | 16 | -1.091 |
| GBPUSD | SET_5:HTF | PREMIUM | 82 | 0.164 |
| GBPUSD | SET_5:HTF | DISCOUNT | 86 | -0.994 |
| GBPUSD | SET_5:MTF | PREMIUM | 88 | -0.015 |
| GBPUSD | SET_5:MTF | DISCOUNT | 78 | -0.867 |
| GBPUSD | SET_5:MTF | EQUILIBRIUM | 2 | -1.525 |
| GBPUSD | SET_5:LTF | PREMIUM | 82 | 0.157 |
| GBPUSD | SET_5:LTF | DISCOUNT | 85 | -0.994 |
| GBPUSD | SET_5:LTF | EQUILIBRIUM | 1 | -0.425 |
| USDJPY | SET_3:HTF | DISCOUNT | 5 | 4.470 |
| USDJPY | SET_3:MTF | PREMIUM | 2 | -1.080 |
| USDJPY | SET_3:MTF | DISCOUNT | 3 | 8.170 |
| USDJPY | SET_3:LTF | DISCOUNT | 4 | 5.871 |
| USDJPY | SET_3:LTF | PREMIUM | 1 | -1.132 |
| USDJPY | SET_4:HTF | DISCOUNT | 8 | 0.529 |
| USDJPY | SET_4:HTF | PREMIUM | 18 | -1.032 |
| USDJPY | SET_4:MTF | PREMIUM | 14 | -0.270 |
| USDJPY | SET_4:MTF | DISCOUNT | 12 | -0.881 |
| USDJPY | SET_4:LTF | DISCOUNT | 10 | 0.578 |
| USDJPY | SET_4:LTF | PREMIUM | 16 | -1.258 |
| USDJPY | SET_5:HTF | PREMIUM | 92 | -0.758 |
| USDJPY | SET_5:HTF | DISCOUNT | 26 | 0.189 |
| USDJPY | SET_5:MTF | PREMIUM | 79 | -0.602 |
| USDJPY | SET_5:MTF | DISCOUNT | 38 | -0.411 |
| USDJPY | SET_5:MTF | EQUILIBRIUM | 1 | -1.638 |
| USDJPY | SET_5:LTF | PREMIUM | 76 | -0.614 |
| USDJPY | SET_5:LTF | DISCOUNT | 42 | -0.432 |
| AUDUSD | SET_3:HTF | DISCOUNT | 4 | -1.121 |
| AUDUSD | SET_3:HTF | PREMIUM | 2 | -0.565 |
| AUDUSD | SET_3:MTF | DISCOUNT | 3 | -0.943 |
| AUDUSD | SET_3:MTF | PREMIUM | 3 | -0.928 |
| AUDUSD | SET_3:LTF | PREMIUM | 3 | -0.346 |
| AUDUSD | SET_3:LTF | DISCOUNT | 3 | -1.526 |
| AUDUSD | SET_4:HTF | PREMIUM | 33 | -1.033 |
| AUDUSD | SET_4:HTF | DISCOUNT | 13 | -1.134 |
| AUDUSD | SET_4:MTF | PREMIUM | 29 | -0.999 |
| AUDUSD | SET_4:MTF | DISCOUNT | 17 | -1.169 |
| AUDUSD | SET_4:LTF | PREMIUM | 34 | -0.969 |
| AUDUSD | SET_4:LTF | DISCOUNT | 12 | -1.323 |
| AUDUSD | SET_5:HTF | PREMIUM | 56 | -0.599 |
| AUDUSD | SET_5:HTF | DISCOUNT | 45 | -1.073 |
| AUDUSD | SET_5:MTF | PREMIUM | 63 | -0.686 |
| AUDUSD | SET_5:MTF | DISCOUNT | 38 | -1.016 |
| AUDUSD | SET_5:LTF | PREMIUM | 57 | -0.625 |
| AUDUSD | SET_5:LTF | DISCOUNT | 42 | -1.037 |
| AUDUSD | SET_5:LTF | EQUILIBRIUM | 2 | -1.328 |

## E. Scale tradability

- SET_1: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/4 cells passed all gates; 0/4 had positive OOS expectancy.
- SET_2: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/4 cells passed all gates; 0/4 had positive OOS expectancy.
- SET_3: **STRUCTURAL INFORMATION ONLY (OOS positive cells did not clear all gates)**; 0/4 cells passed all gates; 2/4 had positive OOS expectancy.
- SET_4: **FALSIFIED / NO OOS EDGE ESTABLISHED**; 0/4 cells passed all gates; 0/4 had positive OOS expectancy.
- SET_5: **STRUCTURAL INFORMATION ONLY (OOS positive cells did not clear all gates)**; 0/4 cells passed all gates; 1/4 had positive OOS expectancy.

## F. Asset generalization

- EURUSD: all-gate sets=none; positive OOS expectancy sets=SET_3.
- GBPUSD: all-gate sets=none; positive OOS expectancy sets=SET_5.
- USDJPY: all-gate sets=none; positive OOS expectancy sets=SET_3.
- AUDUSD: all-gate sets=none; positive OOS expectancy sets=none.

## G. Surviving edge candidates

None. No asset/set allocation cleared all four hard gates.

## H. Direct scientific answers

1. Are the five sets correlated views of one continuous ladder? **Yes by construction**; the same closed-timeframe states feed every role.
2. Does higher-timeframe state predict lower-timeframe transitions? **Not established by trade expectancy alone**; a separate transition-information test is needed.
3. Are nested pullbacks more frequent and monetizable at ≥4R? **Not established unless the state-transition frequency and all-gate trade results support it.**
4. Does Set 5 retain information after micro-spread friction? **See Set 5 OOS and 2× spread stress metrics above; no pass means no economic edge is established.**
5. Final verdict: the specified rider is **FALSIFIED** under the hard gates; the broader fractal hypothesis remains **unproven**, because only 1146 trades passed entry filters and no cell met the 80-trade minimum.

## Data and model limits

- Source is real Dukascopy historical BID/ASK M1 candles; spread is measured from the aligned open/close quote sides. No synthetic candles or fixed synthetic spread were used.
- Base commission is $7 per standard lot round turn. Static swap assumptions are shown in the JSON and are not broker-specific historical rates.
- A historical Tier-1 macro-news calendar was not available, so the requested 30-minute news blackout was not applied. The reported campaign therefore does not satisfy that requested filter; no events were guessed or fabricated.
- Equal-high/low pools are clustered from the latest 24 confirmed swings on each timeframe to keep liquidity levels recent and scale-relative.
- The research results do not authorize live trading and do not guarantee profits.
