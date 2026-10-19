# Institutional Verdict — Forex Platform

**Date:** 17 October 2026  
**Status:** RESEARCH ONLY — Live capital $0  
**Verdict:** NOT YET DETERMINED — Awaiting validation campaign results

---

## 1. Platform State Summary

| Component | Status |
|-----------|--------|
| Causal state engine (7-timeframe ladder) | ✅ Implemented |
| 6 correlated timeframe sets (SET_1–SET_6) | ✅ Implemented |
| Causal indicators (Supertrend ATR=6/Factor=5, Stochastic 25/5/3) | ✅ Implemented + unit tested (9/9 passing) |
| SuperTrend-Stochastic MTF strategy | ✅ Implemented |
| Event-driven backtester with realistic costs | ✅ Existing |
| Dukascopy M1 data acquisition | ⚠️ Scripted, download interrupted |
| Validation campaign runner | ✅ Scripted, awaiting data |
| Live broker connection (MT5/cTrader/paper) | ✅ Existing adapters |
| Risk controls (1% per trade, drawdown limits) | ✅ Existing |

---

## 2. What the Platform IS

- A **research-grade quantitative framework** for building and validating forex trading strategies
- Causal (zero-lookahead) by design — every indicator and state update uses only closed-bar data
- Multi-timeframe fractal architecture with 6 correlated sets (1M/1W/1D down to 15M/5M/1M)
- Realistic cost modeling (spreads, commissions, swaps, slippage)
- Pre-trade risk gates (1% max risk per trade, daily loss limits)
- Paper trading and broker adapter infrastructure (MT5, cTrader)

---

## 3. What the Platform is NOT

- **Not a profitable trading system.** The platform does not generate profits by itself. Profitability depends entirely on the strategies developed and validated on it.
- **Not live-trading.** Live capital is $0. No strategy is authorized for live execution until it clears all predeclared gates on unseen data.
- **Not a guarantee of income.** Even strategies that pass backtesting may fail in live markets due to regime changes, execution slippage, or overfitting.

---

## 4. The User's Strategy Concept (SuperTrend-Stochastic MTF)

The strategy has been implemented as specified:

- **HTF bias:** Supertrend(ATR=6, Factor=5) determines trend direction
- **Setup:** Stochastic(25,5,3) pullback to 25 (oversold for bulls, 75 for bears)
- **Entry:** LTF Stochastic K/D crossover after candle close
- **Targets:** 3-tier TP (LTF swing high, MTF swing high, HTF swing high)
- **Stop:** LTF Supertrend level
- **Minimum RR:** 1:6 on TP3 (never less)
- **Trailing:** LTF ST → MTF ST → HTF ST as price advances through TP levels
- **Risk:** 1% max risk per trade per account
- **Sets:** 6 correlated timeframe sets (SET_1 through SET_6)
- **Symbols:** EURUSD, GBPUSD, USDJPY, AUDUSD

**Status:** Code implemented and loads correctly. **Not yet backtested** — awaiting Dukascopy M1 data download to complete.

---

## 5. Institutional Gates (Pre-Declared)

For the platform to be considered "institutionally profitable," a strategy must pass ALL of the following:

| Gate | Criterion | Status |
|------|-----------|--------|
| Minimum trades | 100+ per set across various pairs | ⏳ Pending data |
| Win rate | > 40% (with 1:6+ RR, lower win rate can still be profitable) | ⏳ Pending data |
| Profit factor | > 1.5 | ⏳ Pending data |
| Expectancy | > 0.5R per trade | ⏳ Pending data |
| Max drawdown | < 15% | ⏳ Pending data |
| Sharpe ratio | > 1.0 | ⏳ Pending data |
| Out-of-sample | IS/OOS walk-forward with WFE > 0.6 | ⏳ Pending data |
| Cost stress | Profitable at 2× spread/commission | ⏳ Pending data |

---

## 6. Honest Assessment

**Can this platform be profitable like top institutions?**

**Only if:** A strategy built on this platform survives the full gate sequence on unseen data with the metrics above. The platform itself provides institutional-grade *tools*, but profitability is not guaranteed.

**Key risks:**
1. **No live data yet** — The Dukascopy M1 download was interrupted. Without 2+ years of clean M1 data, no validation is possible.
2. **Strategy not yet backtested** — The SuperTrend-Stochastic strategy is coded but untested. It may or may not have an edge.
3. **Overfitting risk** — The strategy has multiple parameters (ATR period, Stochastic levels, RR targets) that could be curve-fit to historical data.
4. **Execution risk** — Even a profitable backtest may not translate to live profits due to slippage, spread widening, and broker execution quality.

**Recommendation:**
1. Complete the Dukascopy M1 download for all 4 pairs
2. Run the validation campaign (`research/supertrend_stochastic_campaign.py`)
3. Review results against the institutional gates above
4. If any cell passes all gates, promote to shadow trading (paper) for 2-4 weeks
5. Only after shadow validation passes, consider live deployment with small capital

---

## 7. Bottom Line

| Question | Answer |
|----------|--------|
| Is the platform profitable? | **Unknown** — no live trading, no backtest results yet |
| Is it institution-grade infrastructure? | **Yes** — causal, realistic costs, risk controls, multi-timeframe |
| Will the user's strategy make money? | **Unknown** — must be validated on real data first |
| Can it make money like top institutions? | **Possibly** — if a strategy passes all gates, but not guaranteed |

**This is research software. No strategy is profitable until it survives the full gate sequence on unseen data. Live capital is $0.**
