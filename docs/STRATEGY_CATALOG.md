# Strategy Catalog

The project contains multiple research families so that styles can be compared under the same risk and research framework. A strategy family is not a profitability claim.

## Scalping

**Component:** `AsianRangeFadeScalper`

- Intended style: M1–M5 short-horizon trading.
- Current logic: Asian-session mean reversion using volatility-normalized price displacement and resting limit orders.
- Primary risks: spread widening, queue position, partial fills, latency, adverse selection, and regime drift.
- Required before production: tick/bid-ask data, order-book or fill model, queue/latency simulation, and long forward tests.

## Intraday breakout

**Component:** `LondonSessionBreakout`

- Intended style: M5–M15 intraday.
- Current logic: pre-London range, breakout buffer, ATR filter, session timing, and rollover exit rules.
- Primary risks: false breakouts, news spikes, spread expansion, and clustered entries.
- Required before production: multi-year real data, realistic fills, and regime-conditional validation.

## Trend continuation

**Component:** `TrendContinuationStrategy`

- Intended style: intraday and swing.
- Current logic: fast/slow EMA regime, Kaufman efficiency filter, and pullback continuation.
- Primary risks: sideways markets, whipsaw, late entries, and currency concentration.
- Required before production: rolling trend/range analysis and portfolio-level correlation limits.

## Carry / positional

**Component:** `MacroCarryStrategy`

- Intended style: multi-day and positional.
- Current logic: swap profile selection and volatility-shock suppression.
- Primary risks: carry unwind, rollover timing, changing broker swaps, policy surprises, and liquidity gaps.
- Required before production: broker-specific historical swap data, policy calendars, and carry-unwind stress tests.

## Pairs and statistical arbitrage

**Components:** `TriangularStatisticalArbitrage`, `PairsTradingStrategy`

- Intended style: relative value and hedging.
- Current logic: log-parity residual or spread z-score reversion.
- Primary risks: broken correlation, leg risk, partial fills, transaction costs, and asynchronous markets.
- Required before production: robust hedge ratios, cointegration stability, multi-leg recovery, and capacity analysis.

## Market making

**Component:** `MarketMakingStrategy`

- Intended style: passive liquidity provision.
- Current status: research scaffold only.
- Missing for production: level-2 data, queue position, cancel/replace latency, adverse-selection detection, inventory simulation, and realistic maker/taker economics.

## How a strategy is selected

A strategy is not selected because its name matches a trading style. It must:

1. Use real provenance-labeled data.
2. Pass G1–G8.
3. Show positive expectancy in DEV, VAL, OOS, cost shock, and rolling windows.
4. Be paper-only promoted.
5. Pass forward paper observation.
6. Be explicitly approved before any limited-live capital.

## Current conclusion

The catalog is broad enough to support research across styles, but the current development cache does not demonstrate a profitable combination. The correct behavior is to reject weak candidates and remain flat.
