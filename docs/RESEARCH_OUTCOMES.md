# Research Outcomes

## Current conclusion

The latest fractal research campaign does **not** establish a profitable strategy. No candidate has qualified, and live capital remains **$0**.

The campaign checked whether the available history could support the requested four-asset, five-timeframe-set evaluation. It did not proceed to performance testing because all four bid/ask M1 histories failed the weekday continuity gate.

## Data audit

The public HistData downloader retrieved 100 monthly archives: 25 months for each of EURUSD, GBPUSD, USDJPY, and AUDUSD. The resulting histories span approximately 2.08 years per pair and are tagged with vendor provenance.

| Pair | M1 rows | Weekday gaps flagged | Price anomalies | Quality result |
|---|---:|---:|---:|---|
| EURUSD | 775,273 | 52 | 0 | Rejected |
| GBPUSD | 774,699 | 57 | 0 | Rejected |
| USDJPY | 774,569 | 45 | 0 | Rejected |
| AUDUSD | 774,083 | 68 | 0 | Rejected |

Longer gaps were left visible rather than filled. An absent quote interval can hide an intrabar stop or target, so these histories cannot be treated as continuous execution evidence. Details, hashes, and source notes are in `research/results/fractal_research.json`.

## Asset and timeframe-set matrix

The campaign defines 20 independent research cells: four currency pairs multiplied by five timeframe sets. Each cell requires at least 100 completed trades before performance gates can be evaluated.

| Measure | Result |
|---|---:|
| Cells required | 20 |
| Cells with quality-passed data | 0 |
| Cells backtested | 0 |
| Minimum trades required per cell | 100 |
| Qualified candidates | 0 |

Every cell is `NOT_RUN_INSUFFICIENT_REAL_DATA`. Trade counts, expectancy, win rate, profit factor, drawdown, Sharpe, walk-forward performance, and cost-shock performance are unavailable. A skipped test is not a losing result, but it is also not evidence of profitability.

## Candidate design

The research candidate consumes completed M1 bars and uses one causal state history for five overlapping views:

| Set | HTF | MTF | LTF |
|---|---:|---:|---:|
| SET 1 | 1M | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

The candidate combines HTF continuation and range location, MTF pullback and zone context, and a confirmed LTF break followed by a later retest. Shared movement identity prevents overlapping set views from counting one structural move repeatedly. The candidate has risk sizing, session and daily-loss controls, a spread screen, a slippage reserve, and a minimum net 4R reward-to-risk filter. Those rules remain hypotheses until validated on eligible data.

The backtester now models buys at ask and sells at bid, evaluates short exits using ask prices, handles gap-through stops with adverse slippage, and closes open positions at the end of a test window. Those accounting changes improve simulation fidelity; they do not create or prove an edge.

The candidate is registered for research only. It has not qualified, has not been forward paper-traded, and is not connected to the broker execution path.

## Earlier exploratory result

A prior short EURUSD M15 test used 4,880 bars from 5 January through 18 March 2026 with unknown provenance. Its OOS results were:

| Strategy | OOS trades | Expectancy | OOS Sharpe | OOS P&L |
|---|---:|---:|---:|---:|
| Asian scalper | 26 | -11.63 | -8.21 | -$302.50 |
| London breakout | 6 | -3.17 | -0.10 | -$19.00 |
| Trend continuation | 21 | -36.10 | -8.42 | -$758.00 |
| Macro carry | 0 | 0.00 | 0.00 | No trades |
| Triangular arbitrage | 0 | 0.00 | 0.00 | No trades |

This small, unknown-source test is not eligible qualification evidence. It is retained as a rejection signal for those specific configurations on that sample.

## What is established

- Timeframe sets are defined as overlapping views of one canonical ladder.
- Completed states are shared by identity across views, and confirmed structure is published causally.
- The research campaign enforces real-data provenance, coverage, quality, sample-size, walk-forward, cost-stress, and multiple-testing checks.
- The backtester includes explicit bid/ask-side execution and end-of-window liquidation behavior.
- Current data failed the quality gate and weak candidates remain unpromoted.

## What is not established

- Positive net expectancy on quality-passed, multi-year market data.
- 100 trades in each asset/set cell.
- Robustness across market regimes, brokers, or the full 28-pair registry.
- Forward paper performance, live execution quality, or safe live profitability.
- That a 4R target filter or any other rule has a positive expectancy.

## Next validation milestone

Obtain complete, provenance-documented bid/ask history for EURUSD, GBPUSD, USDJPY, and AUDUSD. Prefer the broker feed intended for execution; a different venue's spread and fill behavior may not match it. Keep missing intervals visible, record time zone and source, and rerun the quality audit.

Only after the data passes should the campaign attempt at least 100 trades in each of the 20 cells, apply the existing out-of-sample and cost-stress gates, and report every result including failures. Any survivors must then be forward paper-tested before a separate human-reviewed live stage.
