# Surviving Edges

> This file records the earlier six-variant Market Model sweep. The later unified fractal rider campaign also produced no survivor; its complete 20-cell results are in [FRACTAL_STRUCTURAL_RIDER_REPORT.md](FRACTAL_STRUCTURAL_RIDER_REPORT.md).

Only Market Model variants that passed **all four hard elimination gates**
are listed here. Anything rejected by any gate is auto-pruned.

- Generated: 2026-10-07T18:16:05.034025+00:00
- Sweep run: `SWEEP-20261007T181532Z`
- Experiments evaluated: 6
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
