# Strategy Catalog

The repository contains strategy families for comparative research. A strategy's label or implementation does not imply profitability. Every candidate must use eligible data and pass the shared validation gates before it can be promoted to forward paper trading.

## Fractal HTF/MTF/LTF candidate

**Component:** `forex_platform/strategy_engine/fractal_institutional.py`

**Status:** research-only; not qualified, not forward paper-traded, and not connected to broker execution.

The candidate uses completed M1 bars to build one canonical history across this ladder: 1M, 1W, 1D, 4H, 1H, 15M, and 3M. The five independent evaluation views are:

| Set | HTF bias | MTF setup | LTF entry |
|---|---:|---:|---:|
| SET 1 | 1M | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

The setup requires higher-timeframe continuation and suitable structural range location, middle-timeframe pullback and FVG/order-block context, then a confirmed lower-timeframe break followed by a later zone retest. A shared movement identifier deduplicates a structural move represented in overlapping sets.

Candidate controls include 0.25% default equity risk per trade, a one-lot maximum, a 4R minimum net reward-to-risk screen, spread and slippage reserves, session and rollover filtering, and a daily realized-loss limit. These are configuration guardrails, not evidence of positive expectancy or a loss guarantee.

**Latest evidence:** the four research histories failed the weekday continuity gate. All 20 asset/set cells remain untested; each cell requires at least 100 trades. See [Research Outcomes](RESEARCH_OUTCOMES.md) and the [detailed campaign report](../research/FRACTAL_RESEARCH_REPORT.md).

## Scalping

**Component:** `AsianRangeFadeScalper`

- Intended style: M1-M5 short-horizon trading.
- Current logic: Asian-session mean reversion using volatility-normalized price displacement and resting limit orders.
- Primary risks: spread widening, queue position, partial fills, latency, adverse selection, and regime drift.
- Required before production: tick/bid/ask data, a credible fill model, queue/latency simulation, and long forward tests.

## Intraday breakout

**Component:** `LondonSessionBreakout`

- Intended style: M5-M15 intraday.
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

## Candidate qualification

A candidate is not selected because its name matches a trading style. Before promotion, it must:

1. Use real, provenance-labeled, quality-passed data.
2. Meet the minimum sample size in each evaluated cell.
3. Pass the G1-G8 gates and show positive DEV, VAL, OOS, cost-shocked, and rolling-window results.
4. Pass multiple-testing and winner-concentration checks.
5. Remain paper-only until it passes forward observation.
6. Receive separate explicit review before any live capital is enabled.

The current research result is **no qualified candidate**.
