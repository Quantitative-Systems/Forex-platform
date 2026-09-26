# Product Roadmap

The order below is intentional. Do not add more strategies before closing the data and validation gaps.

## Phase 0 — Evidence integrity

- Acquire licensed/provenance-labeled data.
- Build a full 28-pair manifest.
- Reject synthetic/unknown data in strict qualification.
- Record source, license, checksum, and quality audit.

**Exit criteria:** every qualifying dataset has real provenance and a passing quality report.

## Phase 1 — Robust intraday research

- Validate London breakout, Asian mean reversion, and trend continuation across all pairs.
- Use M5/M15/M30 data with real spreads.
- Run fixed and rolling walk-forward windows.
- Apply cost shock, slippage, and latency assumptions.

**Exit criteria:** at least one strategy passes G1–G8 on multiple years and multiple pairs.

## Phase 2 — Forward paper trading

- Run the exact production execution path in paper mode.
- Record rejects, partial fills, latency, slippage, and reconciliation events.
- Compare paper results with research assumptions.

**Exit criteria:** forward results remain within documented tolerance of research results.

## Phase 3 — Portfolio construction

- Combine only paper-qualified strategies.
- Use regime-aware target allocation.
- Enforce gross, symbol, currency, correlation, and drawdown limits.
- Add defensive hedging during correlation and liquidity stress.

**Exit criteria:** portfolio-level OOS and forward results remain positive after portfolio costs.

## Phase 4 — Broker certification

- Connect a real MT5 demo account through the authenticated gateway.
- Verify symbols, fills, cancels, positions, account data, and reconciliation.
- Test disconnects, restarts, duplicate requests, and partial fills.

**Exit criteria:** a broker-specific certification report passes.

## Phase 5 — Limited live operation

- Human-approved live environment.
- Minimal capital and strict per-account ceiling.
- Continuous monitoring and automatic kill switches.
- No automatic capital increases.

**Exit criteria:** operational stability and risk compliance over an agreed observation period.

## Phase 6 — Additional asset classes

Only after spot-FX execution and risk controls are proven should the platform add:

- Indices
- Metals
- Commodities
- Equities/ETFs
- Crypto
- Futures
- Options

Each asset class requires its own contract specifications, hours, margin, financing, settlement, and risk model.

## What this roadmap deliberately avoids

- Promising guaranteed returns.
- Treating synthetic data as evidence.
- Automatically enabling live capital.
- Adding strategy count as a substitute for validation.
- Calling a research scaffold production-ready.
