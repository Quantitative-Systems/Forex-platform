"""Research-only SuperTrend/Stochastic candidate with structural HTF/MTF gates."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, Optional

from forex_platform.core.domain import (
    CurrencyPair, LotSize, OrderSide, OrderType, UrgencyLevel,
)
from forex_platform.market_data.causal_aligner import Timeframe
from forex_platform.strategy_engine.base import BarEvent, BaseStrategy
from forex_platform.fractal_engine.state_engine import UniversalTimeframeStateEngine


class SuperTrendStochasticStructuralStrategy(BaseStrategy):
    """HTF structure -> fresh MTF zone/retest -> LTF SuperTrend/Stochastic trigger.

    Required causal BarEvent.htf_data keys:
      htf_trend, mtf_trend: "UP" or "DOWN"
      mtf_zone_low, mtf_zone_high: boundaries of a fresh, unmitigated MTF zone
      ltf_swing_low / ltf_swing_high: latest confirmed opposing LTF swing
      htf_target: structural objective in the HTF trend direction
      structure_fresh: true only while the MTF setup remains fresh

    The causal aligner/state engine must supply these fields from closed bars.
    Missing or inconsistent context always fails closed. The class only emits
    order intents; broker execution remains subject to risk and paper gates.
    """

    def __init__(
        self,
        strategy_id: str = "supertrend_stochastic_structural",
        symbols: Optional[list[str]] = None,
        *,
        timeframe: Timeframe = Timeframe.M5,
        supertrend_atr_period: int = 6,
        supertrend_multiplier: Decimal | str | float = Decimal("5"),
        stochastic_period: int = 25,
        stochastic_k_smoothing: int = 5,
        stochastic_d_smoothing: int = 3,
        oversold: Decimal | str | float = Decimal("25"),
        overbought: Decimal | str | float = Decimal("75"),
        minimum_net_reward_risk: Decimal | str | float = Decimal("4"),
        lot_size: Decimal | str | float = Decimal("0.01"),
        slippage_buffer_pips: Decimal | str | float = Decimal("0.2"),
        commission_pips_per_lot: Decimal | str | float = Decimal("0.7"),
        max_spread_pips: Decimal | str | float = Decimal("3"),
        max_history_bars: int = 1000,
    ) -> None:
        if supertrend_atr_period < 2:
            raise ValueError("supertrend_atr_period must be >= 2")
        if stochastic_period < 2 or stochastic_k_smoothing < 1 or stochastic_d_smoothing < 1:
            raise ValueError("stochastic periods must be positive and stochastic_period >= 2")
        if not Decimal("0") < Decimal(str(oversold)) < Decimal(str(overbought)) < Decimal("100"):
            raise ValueError("thresholds must satisfy 0 < oversold < overbought < 100")
        if Decimal(str(minimum_net_reward_risk)) < Decimal("1"):
            raise ValueError("minimum_net_reward_risk must be >= 1")
        if Decimal(str(lot_size)) <= 0 or max_history_bars < 100:
            raise ValueError("lot_size must be positive and max_history_bars >= 100")
        if Decimal(str(supertrend_multiplier)) <= 0 or Decimal(str(max_spread_pips)) < 0:
            raise ValueError("supertrend_multiplier must be positive and max_spread_pips non-negative")

        super().__init__(
            strategy_id=strategy_id,
            name="SuperTrend/Stochastic Structural Continuation (research-only)",
            symbols=symbols or ["GBPUSD"],
            timeframes=[timeframe],
            parameters={
                "timeframe": timeframe.value,
                "supertrend_atr_period": supertrend_atr_period,
                "supertrend_multiplier": str(supertrend_multiplier),
                "stochastic_period": stochastic_period,
                "stochastic_k_smoothing": stochastic_k_smoothing,
                "stochastic_d_smoothing": stochastic_d_smoothing,
                "minimum_net_reward_risk": str(minimum_net_reward_risk),
                "lot_size": str(lot_size),
                "max_history_bars": max_history_bars,
            },
        )
        self.timeframe = timeframe
        self.supertrend_atr_period = supertrend_atr_period
        self.supertrend_multiplier = Decimal(str(supertrend_multiplier))
        self.stochastic_period = stochastic_period
        self.stochastic_k_smoothing = stochastic_k_smoothing
        self.stochastic_d_smoothing = stochastic_d_smoothing
        self.oversold = Decimal(str(oversold))
        self.overbought = Decimal(str(overbought))
        self.minimum_net_reward_risk = Decimal(str(minimum_net_reward_risk))
        self.default_lot = LotSize.from_lots(lot_size)
        self.slippage_buffer_pips = Decimal(str(slippage_buffer_pips))
        self.commission_pips_per_lot = Decimal(str(commission_pips_per_lot))
        self.max_spread_pips = Decimal(str(max_spread_pips))
        self._max_history = max_history_bars
        self._last_intent_bar: dict[str, tuple[object, Decimal]] = {}
        self._state_engines: dict[str, UniversalTimeframeStateEngine] = {}

    @staticmethod
    def _true_ranges(bars: list[BarEvent]) -> list[Optional[Decimal]]:
        result: list[Optional[Decimal]] = [None]
        for i in range(1, len(bars)):
            current, previous = bars[i], bars[i - 1]
            result.append(max(current.high - current.low,
                              abs(current.high - previous.close),
                              abs(current.low - previous.close)))
        return result

    @classmethod
    def supertrend_series(
        cls, bars: list[BarEvent], period: int = 6,
        multiplier: Decimal | str | float = Decimal("5"),
    ) -> list[tuple[Optional[int], Optional[Decimal]]]:
        """Return causal (direction, supertrend-line) values aligned to bars."""
        if period < 2:
            raise ValueError("period must be >= 2")
        out: list[tuple[Optional[int], Optional[Decimal]]] = [(None, None) for _ in bars]
        if len(bars) <= period:
            return out
        mult = Decimal(str(multiplier))
        tr = cls._true_ranges(bars)
        upper: list[Optional[Decimal]] = [None] * len(bars)
        lower: list[Optional[Decimal]] = [None] * len(bars)
        atr = sum((v for v in tr[1:period + 1] if v is not None), Decimal("0")) / Decimal(period)
        mid = (bars[period].high + bars[period].low) / Decimal("2")
        upper[period], lower[period] = mid + mult * atr, mid - mult * atr
        direction = 1 if bars[period].close >= mid else -1
        out[period] = (direction, lower[period] if direction > 0 else upper[period])
        for i in range(period + 1, len(bars)):
            current, previous = bars[i], bars[i - 1]
            tr_window = [v for v in tr[i - period + 1:i + 1] if v is not None]
            if len(tr_window) != period:
                continue
            atr = sum(tr_window, Decimal("0")) / Decimal(period)
            mid = (current.high + current.low) / Decimal("2")
            basic_upper, basic_lower = mid + mult * atr, mid - mult * atr
            prev_upper, prev_lower = upper[i - 1], lower[i - 1]
            if prev_upper is None or prev_lower is None:
                continue
            upper[i] = basic_upper if basic_upper < prev_upper or previous.close > prev_upper else prev_upper
            lower[i] = basic_lower if basic_lower > prev_lower or previous.close < prev_lower else prev_lower
            prev_direction = out[i - 1][0]
            if prev_direction == -1 and current.close > upper[i]:
                direction = 1
            elif prev_direction == 1 and current.close < lower[i]:
                direction = -1
            else:
                direction = prev_direction or 1
            out[i] = (direction, lower[i] if direction > 0 else upper[i])
        return out

    def stochastic_series(self, bars: list[BarEvent]) -> tuple[list[Optional[Decimal]], list[Optional[Decimal]]]:
        raw: list[Optional[Decimal]] = []
        for i in range(len(bars)):
            if i + 1 < self.stochastic_period:
                raw.append(None)
                continue
            window = bars[i + 1 - self.stochastic_period:i + 1]
            high, low = max(b.high for b in window), min(b.low for b in window)
            raw.append(Decimal("50") if high == low else
                       (bars[i].close - low) * Decimal("100") / (high - low))
        smooth_k: list[Optional[Decimal]] = []
        for i in range(len(raw)):
            w = raw[max(0, i + 1 - self.stochastic_k_smoothing):i + 1]
            smooth_k.append(None if len(w) < self.stochastic_k_smoothing or any(v is None for v in w)
                            else sum((v for v in w if v is not None), Decimal("0")) / Decimal(len(w)))
        smooth_d: list[Optional[Decimal]] = []
        for i in range(len(smooth_k)):
            w = smooth_k[max(0, i + 1 - self.stochastic_d_smoothing):i + 1]
            smooth_d.append(None if len(w) < self.stochastic_d_smoothing or any(v is None for v in w)
                            else sum((v for v in w if v is not None), Decimal("0")) / Decimal(len(w)))
        return smooth_k, smooth_d

    @staticmethod
    def _decimal(context: dict[str, Any], key: str) -> Optional[Decimal]:
        try:
            value = context.get(key)
            number = Decimal(str(value))
            return number if value is not None and number.is_finite() else None
        except Exception:
            return None

    @staticmethod
    def _latest_confirmed_ltf_swing(history: list[BarEvent], swing_type: str, lookback: int = 2) -> Optional[Decimal]:
        """Find the newest uniquely confirmed pivot using only completed bars."""
        if len(history) < lookback * 2 + 1:
            return None
        for index in range(len(history) - lookback - 1, lookback - 1, -1):
            pivot = history[index]
            window = history[index - lookback:index + lookback + 1]
            value = pivot.low if swing_type == "low" else pivot.high
            peers = [bar.low if swing_type == "low" else bar.high
                     for offset, bar in enumerate(window) if offset != lookback]
            if all(value < peer for peer in peers):
                return value
        return None

    def _build_context(
        self, event: BarEvent, history: list[BarEvent], engine: UniversalTimeframeStateEngine
    ) -> dict[str, Any]:
        """Build an entry context from already-closed H1/M15 states and confirmed M5 pivots.

        A caller may supply an already causally-aligned context in htf_data.
        Otherwise this method derives the context from the shared state engine.
        """
        supplied = event.htf_data
        required = {
            "htf_trend", "mtf_trend", "structure_fresh", "mtf_zone_low",
            "mtf_zone_high", "htf_target",
        }
        if required.issubset(supplied.keys()):
            return dict(supplied)

        htf, mtf = engine.get_state("1H"), engine.get_state("15M")
        if htf is None or mtf is None or not htf.state_valid or not mtf.state_valid:
            return {}
        decision_time = event.timestamp + self.timeframe.duration
        # Fail closed on stale structural snapshots after feed gaps or restarts.
        if decision_time - htf.timestamp > timedelta(hours=2):
            return {}
        if decision_time - mtf.timestamp > timedelta(minutes=30):
            return {}
        if htf.structural_trend not in (-1, 1) or mtf.structural_trend != htf.structural_trend:
            return {}
        trend = "UP" if htf.structural_trend > 0 else "DOWN"

        # Only active MTF zones created by closed bars, still fresh, and touched
        # by the current completed entry candle are eligible.
        eligible_zones = []
        for zone in mtf.zones:
            age = decision_time - zone.timestamp_end
            if timedelta(0) <= age <= timedelta(hours=4) and zone.price_low <= event.high and zone.price_high >= event.low:
                eligible_zones.append(zone)
        if not eligible_zones:
            return {}
        zone = max(eligible_zones, key=lambda item: item.timestamp_end)

        if trend == "UP":
            stops = self._latest_confirmed_ltf_swing(history, "low")
            targets = [level.price for level in htf.key_levels
                       if level.weak and str(level.swing_type).lower() in ("high", "swingtype.high")
                       and level.price > event.close]
            if stops is None or not targets:
                return {}
            stop, target = stops, min(targets)
        else:
            stops = self._latest_confirmed_ltf_swing(history, "high")
            targets = [level.price for level in htf.key_levels
                       if level.weak and str(level.swing_type).lower() in ("low", "swingtype.low")
                       and level.price < event.close]
            if stops is None or not targets:
                return {}
            stop, target = stops, max(targets)

        return {
            "htf_trend": trend,
            "mtf_trend": trend,
            "structure_fresh": True,
            "mtf_zone_low": str(zone.price_low),
            "mtf_zone_high": str(zone.price_high),
            "ltf_swing_low" if trend == "UP" else "ltf_swing_high": str(stop),
            "htf_target": str(target),
        }

    def on_bar(self, event: BarEvent) -> list:
        if event.timeframe != self.timeframe:
            raise ValueError(f"strategy requires completed {self.timeframe.value} bars")
        self.update_history(event)
        history = self.get_history(event.symbol)
        symbol = event.symbol.upper().replace("/", "").replace("_", "").replace("-", "")
        engine = self._state_engines.setdefault(
            symbol,
            UniversalTimeframeStateEngine(symbol, base_timeframe=self.timeframe, max_history_bars=100_000),
        )
        engine.update({
            "timestamp": event.timestamp,
            "open": float(event.open), "high": float(event.high),
            "low": float(event.low), "close": float(event.close),
            "volume": float(event.volume), "spread": float(event.spread),
        })
        warmup = max(self.supertrend_atr_period + 2,
                     self.stochastic_period + self.stochastic_k_smoothing + self.stochastic_d_smoothing)
        if len(history) < warmup:
            return []
        pair = CurrencyPair.from_symbol(event.symbol)
        if event.spread < 0 or event.spread > self.max_spread_pips:
            return []
        context = self._build_context(event, history, engine)
        if not context:
            return []
        trend = context.get("htf_trend")
        if trend not in ("UP", "DOWN") or context.get("mtf_trend") != trend:
            return []
        if context.get("structure_fresh") is not True:
            return []
        zone_low, zone_high = self._decimal(context, "mtf_zone_low"), self._decimal(context, "mtf_zone_high")
        target = self._decimal(context, "htf_target")
        if zone_low is None or zone_high is None or target is None or zone_low > zone_high:
            return []
        if event.high < zone_low or event.low > zone_high:
            return []

        st = self.supertrend_series(history, self.supertrend_atr_period, self.supertrend_multiplier)
        k, d = self.stochastic_series(history)
        if len(st) < 2 or k[-1] is None or k[-2] is None or d[-1] is None:
            return []
        direction, previous_direction = st[-1][0], st[-2][0]
        if direction is None or previous_direction is None:
            return []
        long_signal = (trend == "UP" and direction == 1 and
                       k[-2] <= self.oversold < k[-1] and k[-1] > d[-1])
        short_signal = (trend == "DOWN" and direction == -1 and
                        k[-2] >= self.overbought > k[-1] and k[-1] < d[-1])
        if not (long_signal or short_signal):
            return []

        stop = self._decimal(context, "ltf_swing_low" if long_signal else "ltf_swing_high")
        entry = event.close
        if stop is None or (long_signal and not stop < entry) or (short_signal and not stop > entry):
            return []
        if (long_signal and not target > entry) or (short_signal and not target < entry):
            return []
        risk, reward = abs(entry - stop), abs(target - entry)
        costs = pair.to_price(event.spread + self.slippage_buffer_pips + self.commission_pips_per_lot)
        if reward <= costs or (reward - costs) / (risk + costs) < self.minimum_net_reward_risk:
            return []

        symbol = pair.symbol
        bar_key = (event.timestamp, event.close)
        if self._last_intent_bar.get(symbol) == bar_key:
            return []
        self._last_intent_bar[symbol] = bar_key
        return [self.create_intent(
            symbol=symbol,
            side=OrderSide.BUY if long_signal else OrderSide.SELL,
            order_type=OrderType.MARKET,
            lot_size=self.default_lot,
            timestamp=event.timestamp,
            stop_loss=pair.round_price(stop),
            take_profit=pair.round_price(target),
            urgency=UrgencyLevel.MEDIUM,
            client_tag="SUPERTREND_STOCH_STRUCTURAL_LONG" if long_signal else "SUPERTREND_STOCH_STRUCTURAL_SHORT",
        )]
