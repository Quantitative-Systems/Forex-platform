# Multi-Timeframe Forex Platform Architecture

This document summarizes the implemented Python research and risk-control platform. It is not a separate web application. Detailed component responsibilities live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Research flow

~~~text
Vendor or broker data
        |
Provenance, quote, and coverage checks
        |
Canonical causal market states
        |
Shared seven-timeframe ladder and five overlapping views
        |
HTF hypothesis -> MTF structural setup -> LTF trigger
        |
Event-driven backtest, allocation, risk, and cost evaluation
        |
Research qualification -> paper evaluation -> separately approved operations
~~~

The stages separate market description, conditional expectations, candidate entries, and capital authorization. An observed structural relationship is not itself a profitable strategy.

## Shared timeframe ladder

The canonical order is Monthly (1M), 1W, 1D, 4H, 1H, 15M, and 3M. One closed state per timeframe is reused by each set:

| Set | HTF bias | MTF setup | LTF entry |
|---|---:|---:|---:|
| SET 1 | 1M | 1W | 1D |
| SET 2 | 1W | 1D | 4H |
| SET 3 | 1D | 4H | 1H |
| SET 4 | 4H | 1H | 15M |
| SET 5 | 1H | 15M | 3M |

Consequently, a timeframe can be the entry scale for one set and the bias scale for another. Those views share state identity; they are not independent observations or separate portfolios.

## Structural rider candidate

The market-state layer exposes causal confirmed swings and breaks, trend, protected/weak levels, phase, dealing range location, and active structural zones. Parent-to-child requirements preserve when the expectation was created and whether the child state confirmed or invalidated it.

The research rider maps HTF trend and premium/discount location to a pullback or continuation expectation. It requires an MTF shift after the HTF state, followed by a closed LTF micro-BOS or liquidity sweep-reclaim. Execution is modelled at the next source-bar open. The initial stop uses a confirmed opposing LTF swing plus a two-pip buffer; the structural HTF target must permit at least 4R after estimated costs. Position risk is capped at 1% of equity. After +2R, the stop may trail confirmed MTF protected swings. Conflicting set signals are dropped and a shared movement is allocated once.

## Current evidence

The latest run used real Dukascopy BID/ASK M1 history for EURUSD, GBPUSD, USDJPY, and AUDUSD over 2024-01-01 to 2026-10-08. It evaluated all 20 pair/set cells, produced two trades, and had no all-gate survivors. EURUSD returned -1.106R on one trade; USDJPY returned +1.891R on one trade; the other pairs had no trades. The rule set is falsified under the declared gates. The broader claim that parent states add predictive and monetizable information remains unproven.

The campaign did not apply a historical Tier-1 news blackout because no event calendar was available. It used static financing assumptions and disclosed short unexplained data gaps. See [the full report](research/FRACTAL_STRUCTURAL_RIDER_REPORT.md) and [research interpretation](docs/RESEARCH_OUTCOMES.md).

## Running the campaign

Install the optional research dependencies, acquire paired BID/ASK data, and run the batch:

~~~bash
python -m pip install -e ".[research]"
python forex_platform/market_data/download_dukascopy_m1.py \
  --symbols EURUSD GBPUSD USDJPY AUDUSD \
  --start 2024-01-01T00:00:00+00:00
python research/fractal_structural_rider_batch.py
~~~

The output is research-only. A candidate must clear sample-size, expectancy, walk-forward, and cost-stress gates, then pass forward paper and independent review before any live consideration.
