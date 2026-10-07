# Surviving Edges

Only Market Model variants that passed **all four hard elimination gates**
are listed here. Anything rejected by any gate is auto-pruned.

- Generated: 2026-10-06T15:07:45.233375+00:00
- Sweep run: `SWEEP-20261006T150742Z`
- Experiments evaluated: 3
- Survivors: 0

## Gates

| # | Gate | Criterion | Evaluated on |
|---|------|-----------|--------------|
| a | Statistical Significance | Trade count >= 80 trades | Full tested window (IS+VAL+OOS) |
| b | Asymmetry & Expectancy | Avg winner >= 4.0R and Net Expectancy >= +0.35R | In-Sample (first 60%) |
| c | Walk-Forward Efficiency | OOS annualized / IS annualized >= 0.50 | IS vs blind OOS |
| d | Cost Stress | 2x spread + 0.3 pips slippage keeps Net Expectancy >= 0 | Full window re-run (top candidates only) |

## Surviving Models

| Timeframe Set | Pair | Model Variant | IS Expectancy | OOS Expectancy | Max DD | Net Surviving R |
|---------------|------|---------------|---------------|----------------|--------|-----------------|
| _(no configuration survived all four gates)_ | | | | | | |

## Notes

- Timeline split: 60% In-Sample / 20% Validation / 20% blind Out-of-Sample (chronological, never shuffled).
- Parameters are evaluated on In-Sample first and are **never tuned on OOS**.
- `IS Expectancy` / `OOS Expectancy` = mean R per trade over each slice.
- `Max DD` = maximum drawdown (equity %) over the full tested window.
- `Net Surviving R` = full-window net expectancy (mean R) after all gates.
- Full per-experiment logs: `research/results/experiments.json`.
