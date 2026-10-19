# INSTITUTIONAL KNOWLEDGE CORE — The Engine's Immutable Doctrine

This file is the machine-legible constitution of the Autonomous Fractal Engine.
Every strategy the engine invents, every mutation it tests, every promotion
decision it makes must comply with these laws. They are not suggestions.

---

## LAW 1 — The Three Universal Primitives

Every market state, on every timeframe, in every asset class, is decomposed
into exactly three primitives. Nothing else exists.

1. **STRUCTURE / TREND** — confirmed swing highs/lows, BOS (continuation),
   CHoCH (reversal), internal vs. external structure, protected vs. weak
   extremes. A swing does not exist until its confirmation candles close.

2. **KEY ZONES / LIQUIDITY LEVELS** — Order Blocks, Fair Value Gaps,
   supply/demand, equal highs/lows pools, dealing-range equilibrium.
   A zone has: construction rule, creation timestamp, validity condition,
   invalidation rule. Zones are never identified retroactively from outcomes.

3. **PHASE** — what price is doing *within* the structure:
   - Continuation (expansion ride) — trend direction, from logical origin.
   - Pullback (correction ride) — counter-phase retrace toward zone/equilibrium.
   - Transition — structure shifting; engine may observe but not commit.
   - Unknown — contradictory evidence; engine MUST stand down.

---

## LAW 2 — The Correlated 5-Set Ladder (One Market, Five Lenses)

The market is ONE organism observed at seven resolutions:

`1M → 1W → 1D → 4H → 1H → 15M → 3M/1M`

Grouped into five operational sets (each is a HORIZON, not a strategy):

| Set | HTF | MTF | LTF | Horizon Role |
|-----|-----|-----|-----|--------------|
| SET_1 | 1M | 1W | 1D | Macro positioning / investing |
| SET_2 | 1W | 1D | 4H | Swing trading |
| SET_3 | 1D | 4H | 1H | Intraday swing / daily |
| SET_4 | 4H | 1H | 15M | Intraday session scalping |
| SET_5 | 1H | 15M | 3M/1M | Micro scalping / sniper |

**The correlation law:** One set's LTF = the next set's MTF = the set after's
HTF. A structural event is ONE canonical event, shared by every set that
consumes it. It is never recomputed differently per set, and never allowed
to count as multiple independent confirmations.

**The non-forcing law:** Sets may disagree. A bullish SET_3 recovery inside a
bearish SET_1 pullback is valid and recorded. We do not silence a valid
lower-set signal because a higher set leans the other way; we contextualize.

---

## LAW 3 — The Execution Sequence (Never Reordered)

For ANY horizon — scalp, intraday, swing, positional — the sequence is
identical and inviolable:

1. **HTF BIAS** — structure + key zones + current location
   (Premium > 0.65 / Equilibrium / Discount < 0.35 of the HTF dealing range)
   → formulate the phase hypothesis: what must price do, toward which
   destination, and what would invalidate this expectation.

2. **MTF SETUP** — is price behaving as the HTF hypothesis requires?
   Structural shift of trend/character on the MTF proves formation of the
   expected phase. Not formed = WAIT. Waiting is a valid position.

3. **LTF ENTRY** — only after valid MTF setup: precise trigger
   (micro-BOS body close, CHoCH reclaim, sweep-and-reclaim),
   preferably at/inside a KeyZone or OTE pocket (38.2–61.8%).

4. **RISK GATE** — check before any order exists.
5. **MANAGEMENT** — government of the open position (LAW 4).

---

## LAW 4 — Money Management (The Engine May Never Break These)

- **Max risk per trade: 1% of current account equity.** Computed from stop
  distance, pip value, instrument contract specs, lot steps. No exceptions.
- **Minimum planned reward:risk: 1:4** against a REAL structural target.
  Target too close = trade rejected. Targets are NEVER moved outward to
  satisfy the ratio.
- **Stops:** LTF structural invalidation + volatility buffer
  (≥ 2 pips or 0.5 × ATR(15M) equivalent).
- **Trailing:** MTF protected swings. Break-even at +2.0R. Structural trail
  engages at +3.5R. If price reverses, we lose little; if it runs to the HTF
  destination, we capture it.
- **Take profit:** HTF structural destination (weak external extreme for
  continuation; equilibrium/opposing boundary for pullback).
- **Currency-exposure governor:** aggregate risk on any single base/quote
  currency ≤ 2.0% total across all open positions.
- **Daily kill-switch:** daily realized PnL ≤ −2% of equity halts new entries
  until the next session.

---

## LAW 5 — Environment Survival (24/7/365 Discipline)

- **Session awareness (FX):** Asian range defines liquidity pools;
  London sweeps/ expands; New York confirms or reverses.
- **Spread sentinel:** no entries when spread > 1.8 × rolling average;
  hard freeze around 5 PM EST rollover.
- **News sentinel:** no entries ±15 min around Tier-1 macro events
  (NFP, CPI, FOMC, and asset equivalents).
- **Asset universality:** the doctrine applies to FX, gold, and indices.
  Asset-adapter layers translate mechanics (sessions, rollover,
  settlement); the primitives are identical.

---

## LAW 6 — Self-Evolution (How The Engine Improves Without Corruption)

The engine must improve over time — and must never mutate into an
overfit monster. The evolutionary contract:

1. **Mutations are hypotheses, not truths.** A self-generated variant
   (new trigger, zone weighting, phase rule) enters a QUARANTINE lab.
2. **Causal validation only.** Look-ahead contaminated variants are
   destroyed on detection and logged as process violations.
3. **Frozen baselines.** Every variant is compared against a preserved,
   reproducible baseline on identical data, costs, and splits.
4. **Gates before promotion:** net expectancy > 0 after realistic costs;
   profit factor ≥ 1.4 minimum (target ≥ 2.5); drawdown ≤ 20%; sufficient
   independent sample; robustness under spread/slippage stress; generalization
   across pairs and regimes; validation-set confirmation.
5. **Out-of-sample is sacred.** The quarantined final period is never used
   for tuning. Touching it re-opens it as development data — never as proof.
6. **Frequency ≠ edge.** Trade-count targets are research targets; filters
   are never loosened to manufacture volume.
7. **Failure taxonomy:** every failed variant is classified —
   implementation defect / hypothesis failure / insufficient evidence —
   and recorded in the failure registry so the engine never wastes
   compute repeating dead ends.

---

## LAW 7 — Capital Deployment Authority

The engine's capital ladder is absolute and sequential:

`RESEARCH replay → PAPER forward → FROZEN validation → LIVE micro-lot`

Real capital = $0.00 by default. Promotion up the ladder requires passing
all Law 6 gates at the current level, plus human confirmation for the final
step. The engine may never unilaterally move its own capital ladder state.

---

*This document is version-controlled. Any change is an event with an author,
a reason, and an audit entry. The engine reads it at boot and after every
self-evolution cycle.*
