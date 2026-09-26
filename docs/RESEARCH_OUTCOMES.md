# Research Outcomes

## Executive result

The current repository does **not** have evidence of a production-ready profitable strategy.

The correct current status is:

```text
Live capital: LOCKED
Paper promotion: NO QUALIFIED CANDIDATE
Profitability: NOT PROVEN
```

## Validation performed

A read-only walk-forward check was run on the available cache:

```text
Dataset: EURUSD M15
Bars: 4,880
Date range: 2026-01-05 through 2026-03-18
Provenance: UNKNOWN (no metadata sidecar)
```

The current cache is therefore unsuitable for strict qualification.

### Observed OOS results

| Strategy | OOS trades | OOS expectancy | OOS Sharpe | OOS P&L |
|---|---:|---:|---:|---:|
| Asian scalper | 26 | -11.63 | -8.21 | -$302.50 |
| London breakout | 6 | -3.17 | -0.10 | -$19.00 |
| Trend continuation | 21 | -36.10 | -8.42 | -$758.00 |
| Macro carry | 0 | 0.00 | 0.00 | No trades |
| Triangular arbitrage | 0 | 0.00 | 0.00 | No trades |

These results are not evidence of future performance. They are a rejection signal for the current implementations and data set.

## Strict synthetic campaign

An explicit synthetic campaign was run to verify the safety pipeline:

```text
Configurations evaluated: 30
Models promoted: 0
G1 sample-size rejections: 21
G2 alpha-consistency rejections: 9
Live capital: $0.00 LOCKED
Paper daemon: SAFE_HALT
```

The synthetic campaign is not a profitability test. Its purpose was to confirm that weak candidates are rejected instead of promoted.

## Strict real-data run

A real-data run against the current cache halted with:

```text
Research pipeline halted safely:
Real historical data is required for EURUSD;
refusing to qualify dataset classified as UNKNOWN.
```

A network attempt to retrieve Dukascopy data timed out in the development environment. No real dataset was available to promote.

## What the results mean

The platform has demonstrated:

- Backtest execution mechanics.
- Cost and swap accounting.
- Walk-forward partitioning.
- Rolling robustness gating.
- Provenance enforcement.
- Fail-closed promotion behavior.
- Operational test coverage.

It has **not** demonstrated:

- Positive net expectancy on real multi-year data.
- Robust performance across all 28 pairs.
- Scalping profitability under realistic queue/fill models.
- Market-making profitability under order-book microstructure.
- Live broker execution quality.
- Forward paper profitability.
- Safe limited-live profitability.

## Required next experiment

1. Acquire real, licensed/provenance-labeled data for the 28-pair universe.
2. Generate a full manifest with row counts, date ranges, and quality reports.
3. Run fixed and rolling walk-forward campaigns across all relevant timeframes.
4. Apply G1–G8 without lowering thresholds to force promotions.
5. Paper trade every promoted candidate for an extended period.
6. Measure live-like slippage, latency, rejects, partial fills, and reconciliation.
7. Use a human-approved limited-live stage with minimal capital.

Until those steps produce positive, repeatable, cost-adjusted OOS results, the honest status remains **not profitable**.
