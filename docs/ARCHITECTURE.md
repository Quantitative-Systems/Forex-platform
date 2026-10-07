# Architecture

## Purpose

Forex Platform separates research, allocation, risk, and execution so that a strategy cannot bypass controls by placing an order directly.

```text
Data acquisition
    |
Provenance and quality audit
    |
Strategy plugins and shared market state
    |
Backtest, walk-forward evaluation, and cost stress
    |
G1-G8 qualification
    |
Paper-only promotion
    |
Regime-aware target allocation
    |
Pre-trade firewall
    |
OMS and order state machine
    |
Broker registry
    |
Persistence, reconciliation, audit, and metrics
```

## Research layer

### `forex_platform/research_engine/backtester.py`

The event-driven backtester:

- Sorts bars chronologically.
- Fills intents on the next bar open.
- Applies spread, commission, and swap.
- Resolves ambiguous stop/target collisions adversely.
- Models buys at ask and sells at bid where quote-side data is available.
- Evaluates short exits on ask prices, models adverse gap-through stop slippage, and closes open positions at the test-window boundary.
- Records trade records, an equity curve, and drawdown statistics.

Backtest assumptions remain venue-specific. A bar-level simulation does not reproduce order-book queue position or every tick-level path.

### `forex_platform/research_engine/walkforward.py`

The engine supports:

- Fixed chronological DEV/VAL/OOS partitions.
- Rolling windows with a configurable step.
- Deep-copied strategy instances to prevent state leakage.
- No random shuffling of time series.

### `forex_platform/research_engine/evaluate.py`

The evaluator applies G1-G8. Failed candidates are archived under `research/failed/`; passing candidates are written as `PROMOTABLE_PAPER_ONLY` artifacts. Promotion never grants live authority.

## Fractal state and strategy research

### Canonical state engine

`forex_platform/fractal_engine/state_engine.py` publishes causal state for completed candles, including confirmed swings, structural breaks, phase, trend, structural range, and active zones. A pivot is not published until its right-side confirmation bars have closed.

### Shared set views

`forex_platform/fractal_engine/state_graph.py` provides shared role views over the ladder 1M -> 1W -> 1D -> 4H -> 1H -> 15M -> 3M. The same immutable timeframe state is referenced by identity across overlapping views.

| Set | HTF | MTF | LTF |
|---|---:|---:|---:|
| SET 1 | 1M | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

`forex_platform/fractal_engine/hypothesis_engine.py` records conditional state paths. It describes transitions; it does not by itself establish returns or a tradable edge.

### Fractal candidate

`forex_platform/strategy_engine/fractal_institutional.py` consumes completed M1 input. The research candidate combines HTF continuation and range location, MTF pullback and zone context, and a confirmed LTF break followed by a later retest. Movement identifiers prevent the same structural leg from being counted repeatedly through overlapping sets. The candidate remains research-only until it passes eligible-data, sample-size, out-of-sample, cost-stress, and forward-paper checks.

The latest report is `research/FRACTAL_RESEARCH_REPORT.md`. The current bid/ask campaign failed its weekday continuity checks, so no asset/set performance metrics are available.

## Data layer

### Registry

`forex_platform/core/instruments.py` defines 28 conventional spot pairs formed from USD, EUR, GBP, JPY, CHF, AUD, NZD, and CAD. The registry is the explicit tradable-universe boundary. The generic domain parser remains permissive for legacy compatibility, but allocation and download paths use the registry.

### Provenance

`forex_platform/market_data/provenance.py` distinguishes `REAL_VENDOR`, `BROKER_EXPORT`, `SYNTHETIC`, and `UNKNOWN`. Only real vendor or broker-exported data can qualify in strict mode. Parquet caches should have a metadata sidecar.

### Quality

`DataQualityAuditor` checks OHLC integrity, non-positive prices, weekend gaps versus midweek feed drops, session-specific spreads, and rolling spread behavior.

### Acquisition

`forex-platform download-history` supports multi-symbol/timeframe downloads and fails closed by default. Synthetic fallback requires an explicit flag and is never qualifying evidence.

`forex_platform/market_data/histdata_ticks.py` downloads monthly raw bid/ask tick archives, converts the fixed EST-without-DST timestamps to UTC, aggregates separate bid/ask M1 OHLC, and records provenance. It only carries forward very short no-quote gaps; longer gaps remain visible to quality gates. Downloaded archives and caches stay local and are excluded from Git.

## Strategy layer

Plugins implement `BaseStrategy` and emit intents. Families cover scalping, intraday breakout, trend continuation, carry, triangular/statistical arbitrage, pairs trading, market making research, and fractal institutional structure.

The strategy layer does not control final position sizing. It produces candidate orders; the portfolio and risk layers decide what may be routed.

## Portfolio layer

`forex_platform/portfolio_engine/` provides:

- Currency net-delta exposure matrix.
- Currency exposure governor.
- Causal market-regime engine.
- Target-position allocator.
- Defensive currency hedge-intent generation.
- Gross, symbol, and currency-delta caps.

The production execution service exposes `submit_targets()`, which converts targets to deltas and reuses the `submit_intent()` path used for normal orders.

## Risk layer

The risk path is fail-closed. It includes a six-tier pre-trade firewall, telemetry freshness and clock-drift checks, weekend and rollover gates, spread gates, daily-loss circuit breakers, high-water-mark protection, hierarchical kill switches, margin and credit checks, VaR/CVaR research utilities, global exposure controls, and reconciliation.

## Production layer

`forex_platform/production/` contains validated environment settings, broker registry and routing, an MT5 remote adapter, execution service, OMS persistence and audit log, news blackout and fundamental-rate services, a governed learning/champion pipeline, HTTP API, and operator dashboard.

The Windows gateway is `tools/mt5_agent.py`. It uses bearer authentication, HMAC signing, timestamps, nonce replay protection, account binding, and TLS. MetaTrader5 credentials stay on the Windows host.

## Persistence

SQLite WAL mode is used for the single-node control plane. The design is crash-recoverable and auditable, but it is not a distributed database. Multi-node deployment would require an external database, leader election, and distributed idempotency design.
