# Forex Platform

Forex Platform is a Python research and risk-control toolkit for spot foreign exchange. It brings market data checks, strategy research, backtesting, portfolio controls, paper trading, and broker adapters into one auditable codebase.

> **Research status (7 October 2026): profitability is not proven.** The latest fractal campaign found no history that passed its continuity checks, so none of the requested asset/timeframe-set backtests ran. Live capital remains $0.

## Project status

| Area | Current status |
|---|---|
| Spot FX registry | 28 conventional currency pairs |
| Fractal candidate | Implemented for research; not qualified or connected to broker execution |
| Research assets in the latest campaign | EURUSD, GBPUSD, USDJPY, AUDUSD |
| Requested asset/set tests | 0 of 20 run; each cell requires at least 100 trades |
| Latest data audit | 2.08 years of M1 history per asset, rejected for weekday quote gaps |
| Promotion | No candidate qualified |
| Live capital | $0 |
| Latest full test run | 236 passed, 1 skipped |

## What the platform does

- Builds causal market structure states, confirmed swings, breaks, phases, and key zones.
- Evaluates one shared timeframe history through five overlapping HTF/MTF/LTF views.
- Provides strategy research, chronological and rolling walk-forward evaluation, cost stress, and promotion gates.
- Audits data provenance, OHLC consistency, spreads, weekends, and missing weekday bars.
- Provides portfolio allocation, currency exposure controls, risk checks, paper trading, order management, reconciliation, and broker adapters.
- Keeps research results separate from paper promotion and live-trading authorization.

The project includes strategy families for scalping, session breakouts, trend continuation, carry, relative value, pairs trading, and a market-making research scaffold. A strategy's presence in the repository is not evidence that it is profitable.

## Fractal timeframe design

The candidate consumes completed M1 bars and builds the canonical ladder:

`1M -> 1W -> 1D -> 4H -> 1H -> 15M -> 3M`

Each closed state is shared across the set views:

| Set | Higher timeframe (bias) | Middle timeframe (setup) | Lower timeframe (entry) |
|---|---:|---:|---:|
| SET 1 | 1M | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

The research candidate looks for higher-timeframe continuation and range location, a middle-timeframe pullback with zone context, then a confirmed lower-timeframe break followed by a later retest. It deduplicates overlapping views of the same structural move and applies risk, spread, session, daily-loss, and minimum net reward-to-risk filters. These are testable rules, not a proven edge.

## Current research result

The latest campaign downloaded 100 monthly archives covering 25 months for the four research pairs and built bid/ask M1 caches. Each pair had more than two years of data, but the audit found missing weekday quotes:

| Pair | Missing weekday intervals flagged | Result |
|---|---:|---|
| EURUSD | 52 | Rejected |
| GBPUSD | 57 | Rejected |
| USDJPY | 45 | Rejected |
| AUDUSD | 68 | Rejected |

A missing interval can hide a stop or target touch. The research gate therefore rejected all four histories instead of filling long gaps and treating the result as complete. Consequently, all 20 asset/set cells are marked `NOT_RUN_INSUFFICIENT_REAL_DATA`. No strategy returns, win rate, expectancy, or drawdown can be inferred from this campaign.

Read the [full research report](research/FRACTAL_RESEARCH_REPORT.md), the [machine-readable results](research/results/fractal_research.json), and the [research outcomes summary](docs/RESEARCH_OUTCOMES.md).

## Quick start

Python 3.12 or later is required.

```bash
python -m venv .venv
```

Activate the environment:

```powershell
.venv\Scripts\Activate.ps1
```

Or on Linux/macOS:

```bash
source .venv/bin/activate
```

Install the project and development dependencies, then run the tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

Useful commands:

```bash
python -m forex_platform.cli status
python -m forex_platform.cli inspect-pair EURUSD
python -m forex_platform.cli fractal-research --help
```

## Run the fractal research campaign

Run against the local data cache and regenerate the report:

```bash
python -m forex_platform.cli fractal-research --no-smoke
```

Download the public HistData bid/ask tick archives for the research pair set, build M1 caches, and run the strict campaign:

```bash
python -m forex_platform.cli fractal-research \
  --download-histdata \
  --data-start-month 2024-09 \
  --data-end-month 2026-09 \
  --no-smoke
```

By default, reports are written to `research/FRACTAL_RESEARCH_REPORT.md` and `research/results/fractal_research.json`. Raw downloads and data caches remain local under `data/raw/` and `data/cache/`; these directories are excluded from Git. The current public dataset fails the continuity gate, as described above.

To qualify a candidate, provide complete, provenance-documented bid/ask history for all four research pairs. The campaign requires at least two years of continuous usable history and at least 100 trades in each asset/set cell before it evaluates performance gates. Passing those checks would still be a research result, not authorization for live trading.

## Research qualification gates

The standard strategy qualification process applies eight checks:

1. **G1 - Sample size:** at least 100 development, 30 validation, and 30 out-of-sample trades (or stricter campaign-specific limits).
2. **G2 - Consistency:** positive expectancy in development, validation, and out-of-sample periods.
3. **G3 - Statistical support:** bootstrap evidence that positive profit probability exceeds 95%.
4. **G4 - Walk-forward stability:** out-of-sample expectancy is at least 50% of development expectancy.
5. **G5 - Cost stress:** expectancy remains positive when spread and commissions are doubled.
6. **G6 - Drawdown:** maximum drawdown does not exceed 15%.
7. **G7 - Risk-adjusted return:** Sharpe ratio is at least 0.50.
8. **G8 - Rolling robustness:** at least 60% of rolling out-of-sample windows are positive, and the worst window has positive expectancy.

The fractal campaign adds a minimum of 100 trades in every asset/set cell, multiple-testing correction, and top-winner concentration checks. Passing any gate is not a guarantee of future returns.

## Validation and operating safeguards

The research framework uses chronological development, validation, and out-of-sample partitions; rolling windows; transaction-cost stress; bootstrap checks; multiple-testing correction; and G1-G8 qualification gates. Candidates that pass research gates remain paper-only until separately reviewed and forward-tested.

The execution and risk components include position and currency exposure limits, spread and session filters, loss controls, kill switches, and order reconciliation. Broker support requires broker-specific configuration and certification. Do not connect credentials or allocate live capital based on the current fractal candidate.

Synthetic data can be used to exercise software paths. It is explicitly labeled and cannot qualify a strategy.

## Repository guide

- `forex_platform/fractal_engine/` - canonical states, timeframe sets, shared views, movement identity, and research.
- `forex_platform/strategy_engine/` - strategy candidates.
- `forex_platform/market_data/` - downloads, provenance, normalization, and quality audits.
- `forex_platform/research_engine/` - backtesting and evaluation.
- `forex_platform/risk_engine/` and `forex_platform/portfolio_engine/` - risk and allocation.
- `docs/ARCHITECTURE.md` - system components and data flow.
- `docs/STRATEGY_CATALOG.md` - strategy descriptions and evidence requirements.
- `docs/PRODUCT_ROADMAP.md` - next validation milestones.
- `research/FRACTAL_RESEARCH_REPORT.md` - latest detailed fractal campaign report.

## License

MIT. Check data-source terms and broker requirements before using external data or connecting an account.
