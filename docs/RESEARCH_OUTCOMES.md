# Research Outcomes

## Current conclusion — 8 October 2026

The unified fractal structural rider is **falsified under its predeclared gates**. The broader claim that higher-timeframe state predicts monetizable lower-timeframe transitions remains **unproven**. The campaign executed all 20 pair/set cells but recorded only two trades; no cell met the 80-trade minimum or passed all four gates. Live capital remains $0.

The result is a rejection of this tested rule set under the stated sample and assumptions. Two trades are not enough to infer a stable win rate, expected return, or generalization to other assets and regimes.

## Campaign design

One causal seven-timeframe state history feeds five overlapping views:

| Set | HTF | MTF | LTF |
|---|---:|---:|---:|
| SET 1 | 1M (monthly) | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

The strategy uses HTF structural trend and range location to define a pullback or continuation hypothesis. It waits for an MTF shift after the HTF state, then triggers on a closed LTF micro-BOS or confirmed liquidity sweep-and-reclaim. Orders are modelled at the next source-bar open. Stops use the latest confirmed opposing LTF swing plus a two-pip buffer. The HTF structural target must offer at least 4R after the estimated costs; the strategy does not manufacture a 4R target. Account risk is capped at 1% per trade. After +2R, a confirmed MTF protected swing may ratchet the stop. Signals sharing a movement are arbitrated once across sets; conflicting directions are dropped.

The 20 matrix rows are allocations from a single all-set run for each pair. They are not 20 independent strategy portfolios.

## Data audit

The campaign used paired historical BID/ASK M1 candles from Dukascopy, from 2024-01-01 through 2026-10-08. No synthetic spread or fabricated candles were used.

| Pair | Vendor bars | Coverage | Unexplained gaps over 5 min | Largest such gap | Quote anomalies | Status |
|---|---:|---:|---:|---:|---:|---|
| EURUSD | 1,030,511 | 2.766 years | 9 | 10 min | 0 | Eligible with gap warnings |
| GBPUSD | 1,028,962 | 2.766 years | 19 | 107 min | 0 | Eligible with gap warnings |
| USDJPY | 1,030,671 | 2.766 years | 32 | 107 min | 0 | Eligible with gap warnings |
| AUDUSD | 1,028,212 | 2.766 years | 29 | 108 min | 0 | Eligible with gap warnings |

Dukascopy M1 bars are tick-built, so minutes without a vendor quote are absent. The campaign preserved those gaps rather than forward-filling them. It required REAL_VENDOR provenance, at least two years of history, valid BID/ASK OHLC, and no unexplained nonholiday gap of 120 minutes or more. Shorter unexplained intervals remain a caveat. GBPUSD, USDJPY, and AUDUSD share an approximately 107-minute interruption on 2024-12-17.

## Asset and set results

| Pair | Set | Trades | Net full-window result | Cost-stress result | Gate status |
|---|---|---:|---:|---:|---|
| EURUSD | SET 5 | 1 | -1.106R | -1.116R | A fail, B fail, C fail, D fail |
| USDJPY | SET 5 | 1 | +1.891R | +1.760R | A fail, B fail, C fail, D pass |
| GBPUSD | All sets | 0 | No trades | No trades | All gates fail |
| AUDUSD | All sets | 0 | No trades | No trades | All gates fail |

The other 18 individual pair/set cells each had zero trades and failed all four gates.

Gate A requires at least 80 full-window trades. Gate B requires an in-sample average winner of at least 4R and expectancy of at least +0.35R. Gate C requires annualized OOS/IS return of at least 0.50. Gate D requires non-negative full-window expectancy with doubled observed spread and stressed slippage. The one-trade USDJPY cost-stress pass cannot compensate for its sample-size and performance-gate failures.

Campaign totals: two raw qualifying signals, two unique movement IDs, no multi-set overlaps, and no conflicting signals dropped. Both events were assigned to SET 5. No pair had a positive OOS cell, and no candidate survived.

## Direct findings

- The timeframe sets are correlated views of one ladder **by construction** because each closed timeframe state is shared across set roles.
- The strategy campaign does not establish that HTF state predicts LTF transitions. That requires a dedicated transition-information analysis, with adequate independent observations.
- The frequency and monetizability of nested pullbacks at 4R or more are not established by this two-trade sample.
- SET 5 produced one loss and one gain before full qualification; there is no evidence that it holds an economic edge after friction.
- No asset generalization is established. Only EURUSD and USDJPY traded, once each.

## Execution assumptions and limits

- Chronological partitions are 60% in-sample, 20% validation, and 20% out-of-sample.
- Commission is $7 per standard lot round turn. Slippage starts at 0.2 pips with an ATR expansion reserve. Cost stress doubles observed bid/ask spread and uses 0.3-pip base slippage.
- Swap is a static model assumption (-0.6 pips long, +0.2 pips short, Wednesday 3x), not historical broker financing.
- A historical Tier-1 macro-news calendar was unavailable; the requested 30-minute news blackout was not applied.
- The small sample makes win rate, profit factor, annualized return, and drawdown unsuitable as evidence of future behavior.
- Nothing in this campaign authorizes live trading or guarantees returns.

The complete 20-cell report, data audit, deduplication counters, and premium/discount attribution are in [FRACTAL_STRUCTURAL_RIDER_REPORT.md](../research/FRACTAL_STRUCTURAL_RIDER_REPORT.md) and [fractal_structural_rider.json](../research/results/fractal_structural_rider.json).

## Earlier exploratory study

The separate HistData campaign in [FRACTAL_RESEARCH_REPORT.md](../research/FRACTAL_RESEARCH_REPORT.md) rejected its four histories under a stricter continuity policy and ran no performance tests. It is retained as an earlier data-quality study; it is not the latest rider campaign summarized above.

## Validation focus

Keep these rules frozen when evaluating a new held-out sample. Any strategy revision should be versioned and evaluated on development data before a new untouched out-of-sample period. Separately test whether parent states add predictive information to child-state transitions, measure signal frequency and overlap, and obtain a historical news calendar and broker-relevant financing assumptions. Do not lower the gates to force a promotion. Any qualifying candidate still requires forward paper evaluation and independent review before live consideration.
