# Forex Platform

Forex Platform is a Python research and risk-control toolkit for spot foreign exchange. It combines market-data acquisition and quality checks, causal multi-timeframe market structure, strategy research, event-driven backtesting, portfolio controls, paper-trading services, and broker adapters.

> **Research status — 8 October 2026:** no strategy has demonstrated a profitable edge. The latest fractal campaign generated two trades across 20 asset/set cells; no cell passed all four qualification gates. The rider is rejected under its predeclared rules, while the broader fractal hypothesis remains unproven. Live capital is $0.

## Project status

| Area | Current status |
|---|---|
| Spot FX registry | 28 conventional currency pairs |
| Fractal structural rider | Implemented for research; not qualified or connected to broker execution |
| Assets in latest campaign | EURUSD, GBPUSD, USDJPY, AUDUSD |
| Latest campaign | 20 cells evaluated; 2 trades total; 0 cells passed all gates |
| Data | Real Dukascopy BID/ASK M1 candles, 2024-01-01 to 2026-10-08; roughly 2.77 years per pair |
| Live capital | $0 |

## What the platform does

- Publishes causal market states, confirmed swings, structural breaks, protected levels, phases, ranges, and key zones.
- Reads one shared seven-timeframe ladder through five overlapping HTF/MTF/LTF views.
- Tracks parent-to-child structural requirements and evaluates a research-only fractal strategy.
- Runs chronological backtests, walk-forward evaluation, transaction-cost stress, and qualification gates.
- Audits data provenance, quote-side OHLC integrity, spreads, and missing intervals.
- Provides portfolio allocation, currency exposure controls, pre-trade risk checks, paper trading, order management, reconciliation, and broker adapters.

Strategies elsewhere in the repository are research implementations. Their presence does not imply that they are profitable or ready for live use.

## Fractal timeframe design

The strategy builds its states from completed M1 vendor bars and aggregates the canonical ladder:

**Monthly (1M) → 1W → 1D → 4H → 1H → 15M → 3M**

Each closed timeframe state is shared across these views:

| Set | Higher timeframe (bias) | Middle timeframe (setup) | Lower timeframe (entry) |
|---|---:|---:|---:|
| SET 1 | 1M | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

The candidate maps HTF range location and trend to a pullback or continuation hypothesis, requires a subsequent MTF structural shift, and triggers on an LTF micro break or confirmed liquidity sweep-and-reclaim. It uses the next source-bar open, an LTF swing invalidation with a two-pip buffer, a structural target offering at least 4R after estimated costs, and no more than 1% account risk per trade. After a position reaches 2R, confirmed MTF protected swings can trail the stop. Conflicting signals are dropped; one movement is allocated to the set with the strongest structural reward-to-risk estimate. These are rules under evaluation, not evidence of an edge.

## Latest research result

The 20-cell run covered four pairs and all five overlapping sets. Dukascopy BID/ASK M1 histories contained approximately 1.03 million vendor bars per pair. The campaign found two accepted trades, both allocated to SET 5: EURUSD returned -1.106R and USDJPY returned +1.891R. GBPUSD and AUDUSD produced no trades. With only two trades, the results cannot establish statistical performance or asset generalization.

| Gate | Criterion | Campaign result |
|---|---|---|
| A | At least 80 full-window trades | Failed in all 20 cells |
| B | IS average winner ≥4R and expectancy ≥+0.35R | Failed in both traded cells |
| C | OOS/IS annualized return ≥0.50 | No qualifying cell |
| D | Net expectancy ≥0R under cost stress | Passed only for the single USDJPY trade; this does not qualify the cell |

All histories passed the campaign’s provenance and quote-anomaly checks and were marked eligible with gap warnings. The runner did not fabricate missing bars. A roughly 107-minute synchronized interruption on GBPUSD, USDJPY, and AUDUSD remains a data caveat. A historical Tier-1 news calendar was unavailable, so the requested news blackout was not applied. Swap values were static assumptions, not broker-specific history.

Read the [full campaign report](research/FRACTAL_STRUCTURAL_RIDER_REPORT.md), [machine-readable results](research/results/fractal_structural_rider.json), and [research outcomes](docs/RESEARCH_OUTCOMES.md). The earlier continuity-rejected HistData study is retained as a separate historical report at [FRACTAL_RESEARCH_REPORT.md](research/FRACTAL_RESEARCH_REPORT.md).

## Quick start

Python 3.12 or later is required.

~~~bash
python -m venv .venv
~~~

Activate the environment:

~~~powershell
.venv\Scripts\Activate.ps1
~~~

Or on Linux/macOS:

~~~bash
source .venv/bin/activate
~~~

Install the application and development dependencies:

~~~bash
python -m pip install -e ".[dev]"
python -m pytest
~~~

Install the optional Dukascopy downloader dependencies for the fractal campaign:

~~~bash
python -m pip install -e ".[research]"
~~~

Useful commands:

~~~bash
python -m forex_platform.cli status
python -m forex_platform.cli inspect-pair EURUSD
python -m forex_platform.cli fractal-research --help
~~~

## Acquire data and run the fractal campaign

Download real paired BID/ASK M1 bars for the four research pairs. The downloader defaults to 2024-01-01 through the current minute and writes under the local cache directory:

~~~bash
python forex_platform/market_data/download_dukascopy_m1.py \
  --symbols EURUSD GBPUSD USDJPY AUDUSD \
  --start 2024-01-01T00:00:00+00:00
~~~

Run the unified five-set campaign and regenerate the report and JSON results:

~~~bash
python research/fractal_structural_rider_batch.py
~~~

The runner reads data/cache/dukascopy and writes research/FRACTAL_STRUCTURAL_RIDER_REPORT.md and research/results/fractal_structural_rider.json. Raw downloads and cached market data are local and excluded from Git. Keep the campaign’s gap, news-calendar, spread, commission, and financing assumptions visible when interpreting results.

## Qualification and operating safeguards

The fractal campaign uses these predeclared gates:

1. At least 80 full-window trades.
2. In-sample average winner of at least 4R and expectancy of at least +0.35R.
3. OOS-to-IS annualized return ratio of at least 0.50.
4. Non-negative expectancy under doubled spread and stressed slippage.

The wider research framework includes additional sample-size, bootstrap, drawdown, Sharpe, and rolling-window checks. Passing research gates would still require independent review and forward paper trading. Live routing remains separately controlled; the current fractal candidate is not authorized for live capital.

Synthetic data can exercise software paths, but it is labelled and cannot qualify a strategy. A 4R target filter and a 1% risk cap do not guarantee positive expectancy or limit slippage losses to exactly 1%.

## Repository guide

- forex_platform/fractal_engine/ — canonical states, shared set views, movement identity, and parent-child requirements.
- forex_platform/strategy_engine/ — strategy candidates, including the fractal rider.
- forex_platform/market_data/ — data downloads, provenance, normalization, and quality audits.
- forex_platform/research_engine/ — backtesting and evaluation.
- forex_platform/risk_engine/ and forex_platform/portfolio_engine/ — risk controls and allocation.
- docs/ARCHITECTURE.md — system components and data flow.
- docs/STRATEGY_CATALOG.md — strategy descriptions and evidence requirements.
- docs/PRODUCT_ROADMAP.md — validation milestones.
- docs/RESEARCH_OUTCOMES.md — latest results and interpretation.

## License

MIT. Check data-source terms and broker requirements before using external data or connecting an account.
