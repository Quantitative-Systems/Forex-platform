# CRYPTO ENGINE — Promotion Pipeline

Everything in this folder is **validated production code only**.
Research, experiments, and scratch work live in `research/` — never here.

## The Pipeline (automatic)

```
research/ (scratch: new strategy, new style, new idea)
    |
    v  1. CAUSAL BACKTEST (development window)
    |
   PASS gates? (min trades, expectancy > 0, PF >= 1.4, DD <= 20%)
    |                            \
   YES                            NO
    |                              v
    v                       research/failed/ (archived, reason logged)
 2. VALIDATION WINDOW (unseen data)
    |
   PASS?
    |                            \
   YES                            NO -> research/failed/
    v
 3. engines/crypto_engine/PRODUCED here (frozen config + code ref)
    |
    v  4. PAPER FORWARD 2-4 weeks vs live expectations
    |
   PASS?
    |                            \
   YES                            NO -> research/failed/
    v
 HUMAN CONFIRMS -> 5. LIVE micro-lot (capital ladder; Law 7)
```

Nothing enters this directory without:
- A `research/results/*.json` evidence artifact passing gates
- A frozen config captured in `engines/crypto_engine/MANIFEST.json`
- A Git commit SHA the result was produced on

`promotion.py` enforces this automatically. Failed cells stay in
`research/` and `research/failed/` — the engine folder only ever grows
with things proven to work.
