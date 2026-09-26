# Forex Platform

Forex Platform is a research-first quantitative trading platform for conventional spot FX. It provides reproducible backtesting, strategy qualification, portfolio allocation, risk controls, paper trading, and an auditable broker execution path.

> **Important:** this repository is infrastructure and research software. It does not guarantee profits, prevent all losses, or constitute financial advice. The current evidence does **not** show a production-ready profitable strategy. See [`docs/RESEARCH_OUTCOMES.md`](docs/RESEARCH_OUTCOMES.md).

## Current status

| Area | Status |
|---|---|
| Tests | 203 passed, 2 warnings |
| Spot-FX universe | 28 conventional pairs |
| Backtesting | Causal bar-level engine with costs, swaps, next-bar fills, adverse-first stops |
| Validation | DEV/VAL/OOS, rolling OOS, cost shock, G1–G8 gates |
| Paper trading | Implemented |
| MT5 execution | Authenticated remote gateway implemented; real broker setup required |
| Live trading | Fail-closed; explicit allowlist, TLS, capital, and demo-soak requirements |
| Profitability | Not proven; current cache is insufficient and unknown provenance |

## What it includes

- 28-pair registry across USD, EUR, GBP, JPY, CHF, AUD, NZD, and CAD.
- Research components for scalping, intraday, swing, carry, pairs/statistical arbitrage, and market making.
- Causal backtester and rolling walk-forward engine.
- Data provenance, quality audits, and `.meta.json` cache sidecars.
- Market-regime detection, target-position allocation, currency exposure controls, and hedge-intent generation.
- Six-tier pre-trade firewall, kill switches, circuit breakers, margin/VaR checks, OMS ledger, reconciliation, and audit logs.
- Linux control plane with health/readiness/metrics endpoints and operator dashboard.
- Separate Windows MT5 gateway that keeps broker credentials on the terminal host.

## Install

Python 3.12+ is required.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

## Run

```bash
forex-platform status
forex-platform inspect-pair EURUSD
forex-platform run-pipeline --symbols EURUSD --allow-synthetic-data
```

The synthetic command is a smoke test only. Synthetic data is explicitly labeled and cannot qualify for promotion.

Strict real-data workflow:

```bash
forex-platform download-history --all-fx --timeframes M15 --years 5
forex-platform run-pipeline --symbols EURUSD,GBPUSD,USDJPY --timeframe M15 --bars 10000 --allow-live-download
```

Only `REAL_VENDOR` and `BROKER_EXPORT` datasets can qualify in strict mode.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system design and data flow.
- [`docs/STRATEGY_CATALOG.md`](docs/STRATEGY_CATALOG.md) — strategy families and limitations.
- [`docs/RESEARCH_OUTCOMES.md`](docs/RESEARCH_OUTCOMES.md) — measured results and current blockers.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — deployment, MT5 setup, secrets, and runbooks.
- [`docs/PRODUCT_ROADMAP.md`](docs/PRODUCT_ROADMAP.md) — ordered path to a validated product.

## Safety model

The default execution mode is `PAPER`. Demo/live accounts must use the authenticated `mt5_remote` adapter over HTTPS with a bearer token, HMAC signing, replay protection, account binding, and TLS. Broker credentials never enter the Linux control plane.

Promotion requires G1–G8:

1. Minimum sample size.
2. Positive expectancy across DEV/VAL/OOS.
3. Positive bootstrap confidence.
4. Acceptable walk-forward ratio.
5. Positive expectancy under cost shock.
6. Drawdown within limit.
7. Positive OOS Sharpe.
8. Rolling OOS robustness: at least 60% positive windows and positive worst-window expectancy.

A promotion artifact is `PROMOTABLE_PAPER_ONLY`; it is not live authorization.

## Deployment

```bash
cp .env.example .env
forex-platform serve-production
```

Optional files:

- `Dockerfile`
- `docker-compose.yml`
- `deploy/forex-platform.service`

Health endpoints:

- `/healthz` — liveness
- `/readyz` — readiness including broker connectivity
- `/metrics` — metrics
- `/` — authenticated operator dashboard

## Current conclusion

This repository is a serious research and safety foundation, but it is **not currently a profitable live trading system**. The next required milestone is real, provenance-labeled multi-pair data, followed by a strict campaign, forward paper trading, and only then limited-live validation.

## License

MIT. Review data, broker, exchange, legal, and regulatory terms before operational use.
