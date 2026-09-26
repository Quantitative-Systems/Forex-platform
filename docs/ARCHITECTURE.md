# Architecture

## Purpose

Forex Platform separates research, allocation, risk, and execution so that a strategy cannot bypass controls by placing an order directly.

```text
Data acquisition
    ↓
Provenance + quality audit
    ↓
Strategy plugins
    ↓
Backtest / walk-forward / cost stress
    ↓
G1–G8 qualification
    ↓
Paper-only promotion
    ↓
Regime-aware target allocation
    ↓
Pre-trade firewall
    ↓
OMS and order state machine
    ↓
Broker registry
    ↓
Persistence, reconciliation, audit, metrics
```

## Research layer

### `forex_platform/research_engine/backtester.py`

The event-driven backtester:

- Sorts bars chronologically.
- Fills intents on the next bar open.
- Applies session-aware spread.
- Applies commission and swap.
- Resolves ambiguous stop/target collisions adversely.
- Records immutable trade records.
- Produces an equity curve and drawdown statistics.
- Calculates timeframe-aware Sharpe from equity returns.

### `forex_platform/research_engine/walkforward.py`

The engine supports:

- Fixed DEV/VAL/OOS chronological partitions.
- Rolling windows with a configurable step.
- Deep-copied strategy instances to prevent state leakage.
- No random shuffling of time series.

### `forex_platform/research_engine/evaluate.py`

The evaluator applies G1–G8. Failed candidates are archived under `research/failed/`; passing candidates are written as `PROMOTABLE_PAPER_ONLY` artifacts. Promotion never grants live authority.

## Data layer

### Registry

`forex_platform/core/instruments.py` defines 28 conventional spot pairs formed from:

```text
USD EUR GBP JPY CHF AUD NZD CAD
```

The registry is the explicit tradable-universe boundary. The generic domain parser remains permissive for legacy compatibility, but allocation and download paths use the registry.

### Provenance

`forex_platform/market_data/provenance.py` distinguishes:

- `REAL_VENDOR`
- `BROKER_EXPORT`
- `SYNTHETIC`
- `UNKNOWN`

Only real vendor or broker-exported data qualifies in strict mode. Parquet caches should have a `.parquet.meta.json` sidecar.

### Quality

`DataQualityAuditor` checks:

- OHLC integrity.
- Zero/negative prices.
- Weekend gaps versus midweek feed drops.
- Session-specific spreads.
- Rolling spread behavior.

### Acquisition

`forex-platform download-history` supports:

```bash
--all-fx
--timeframes M15
--years 5
```

The batch downloader fails closed by default. Synthetic fallback requires an explicit flag and is never qualifying evidence.

## Strategy layer

Plugins implement `BaseStrategy` and emit intents. Existing families cover scalping, intraday breakout, trend continuation, carry, triangular/statistical arbitrage, pairs trading, and a market-making research scaffold.

The strategy layer is deliberately not trusted with final position sizing. It produces candidate orders; the portfolio and risk layers decide what may actually be routed.

## Portfolio layer

`forex_platform/portfolio_engine/` provides:

- Currency net-delta exposure matrix.
- Currency exposure governor.
- Causal market-regime engine.
- Target-position allocator.
- Defensive currency hedge-intent generation.
- Gross, symbol, and currency-delta caps.

The production execution service exposes `submit_targets()`, which converts targets to deltas and then reuses the same `submit_intent()` path used for normal orders.

## Risk layer

The risk path is fail-closed. It includes:

- Six-tier pre-trade firewall.
- Telemetry freshness and clock-drift checks.
- Weekend and rollover gates.
- Spread gates.
- Daily-loss circuit breaker.
- High-water-mark protection.
- Hierarchical kill switches.
- Margin and credit checks.
- VaR/CVaR research utilities.
- Global exposure and reconciliation controls.

## Production layer

`forex_platform/production/` contains:

- Validated environment settings.
- Broker registry and routing.
- MT5 remote adapter.
- Execution service.
- OMS persistence and audit log.
- News blackout and fundamental-rate services.
- Governed learning/champion pipeline.
- HTTP API and operator dashboard.

The Windows gateway is `tools/mt5_agent.py`. It uses bearer authentication, HMAC signing, timestamps, nonce replay protection, account binding, and TLS. MetaTrader5 credentials stay on the Windows host.

## Persistence

SQLite WAL mode is used for the single-node control plane. The design is crash-recoverable and auditable, but it is not a distributed database. A multi-node production deployment would require an external database, leader election, and distributed idempotency design.
