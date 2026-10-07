# Product Roadmap

The priority is evidence quality and execution validation. Adding strategies or optimizing settings cannot substitute for reliable data and out-of-sample results.

## Current position

The fractal research candidate and five-set campaign are implemented. The latest public dataset contains about 2.08 years of bid/ask M1 history for EURUSD, GBPUSD, USDJPY, and AUDUSD, but each pair failed the weekday continuity audit. As a result, 0 of the 20 asset/set cells have been backtested. The platform has no qualified candidate and no live capital is enabled.

## Phase 0 - Evidence integrity

- Acquire complete, licensed or otherwise authorized, provenance-labeled bid/ask data.
- Prefer the same broker feed intended for execution.
- Record source, license or permitted use, time zone, checksums, row counts, date ranges, and quality reports.
- Preserve weekday gaps; reject histories that could conceal intrabar exits.
- Build a full manifest for the four-pair fractal campaign before expanding to the broader 28-pair registry.

**Exit criteria:** all qualifying histories pass provenance, continuity, spread, timestamp, and quote-quality checks for the minimum coverage period.

## Phase 1 - Fractal candidate evaluation

- Run all five timeframe sets independently for EURUSD, GBPUSD, USDJPY, and AUDUSD.
- Collect at least 100 completed trades in each of the 20 asset/set cells.
- Keep the HTF/MTF/LTF rules and risk assumptions fixed before the final OOS evaluation.
- Run chronological DEV/VAL/OOS splits, rolling windows, bootstrap confidence checks, FDR correction, doubled-cost stress, and top-winner removal.
- Report every cell, including rejected and under-sampled cells.

**Exit criteria:** a candidate passes the predefined G1-G8 gates with positive out-of-sample and cost-stressed results across multiple assets and windows. Reaching 100 trades alone is not a pass.

## Phase 2 - Forward paper trading

- Run only research-qualified candidates through the paper execution path.
- Log decisions, order rejects, partial fills, latency, spreads, slippage, and reconciliation events.
- Compare observed paper execution with research assumptions.
- Monitor changing performance without tuning on the forward evaluation period.

**Exit criteria:** forward results remain positive and within predeclared tolerances over a sufficiently long, regime-diverse sample.

## Phase 3 - Portfolio validation

- Combine only forward paper-qualified strategies.
- Enforce gross, symbol, currency, correlation, and drawdown limits.
- Stress correlated exposures and liquidity events.
- Re-run portfolio-level OOS and forward evaluation.

**Exit criteria:** net portfolio performance remains positive after costs and risk limits under OOS and forward observation.

## Phase 4 - Broker certification

- Connect a broker demo account through the authenticated gateway.
- Verify symbol mapping, order types, fills, cancels, positions, account data, and reconciliation.
- Test disconnects, restarts, duplicate requests, rejects, and partial fills.

**Exit criteria:** a broker-specific certification report passes and the paper path matches the intended live order route.

## Phase 5 - Limited live operation

- Require explicit human approval.
- Start with minimal capital and a strict account-level ceiling.
- Keep continuous monitoring and automatic kill switches enabled.
- Prohibit automatic capital increases.

**Exit criteria:** risk and operational stability remain within the agreed limits over a reviewed observation period.

## Phase 6 - Additional asset classes

Only after spot-FX data, execution, and risk controls are validated should the platform add indices, metals, commodities, equities, crypto, futures, or options. Each asset class needs its own contract specifications, hours, margin, financing, settlement, and risk model.

## Research principles

- Do not promise guaranteed returns.
- Do not treat synthetic or unknown-provenance data as performance evidence.
- Do not lower gates to force a strategy promotion.
- Do not treat a minimum trade count as proof of an edge.
- Do not enable live capital automatically.
- Do not treat strategy count as a measure of research quality.
