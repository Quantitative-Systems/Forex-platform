"""Institutional Price Action Concepts — Premium/Discount, Order Blocks, FVG, Key Levels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class ZoneType(str, Enum):
    PREMIUM = "PREMIUM"
    DISCOUNT = "DISCOUNT"
    EQUILIBRIUM = "EQUILIBRIUM"


class OrderBlockType(str, Enum):
    BULLISH_OB = "BULLISH_OB"
    BEARISH_OB = "BEARISH_OB"


@dataclass
class PriceZone:
    zone_type: ZoneType
    low: Decimal
    high: Decimal
    equilibrium: Decimal
    strength: Decimal
    timestamp: datetime


@dataclass
class OrderBlock:
    block_type: OrderBlockType
    low: Decimal
    high: Decimal
    timestamp: datetime
    touched: bool = False
    mitigated: bool = False


@dataclass
class FairValueGap:
    gap_type: str
    top: Decimal
    bottom: Decimal
    timestamp: datetime
    filled: bool = False


@dataclass
class KeyLevel:
    price: Decimal
    level_type: str
    touches: int
    last_touch: Optional[datetime]


class InstitutionalConcepts:
    def __init__(
        self,
        lookback_bars: int = 100,
        premium_threshold: Decimal = Decimal("0.75"),
        discount_threshold: Decimal = Decimal("0.25"),
        ob_lookback: int = 20,
    ):
        self.lookback_bars = lookback_bars
        self.premium_threshold = premium_threshold
        self.discount_threshold = discount_threshold
        self.ob_lookback = ob_lookback
        self._highs: list[Decimal] = []
        self._lows: list[Decimal] = []
        self._closes: list[Decimal] = []
        self._timestamps: list[datetime] = []
        self._order_blocks: list[OrderBlock] = []
        self._fvgs: list[FairValueGap] = []
        self._key_levels: list[KeyLevel] = []

    def update(
        self,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        timestamp: datetime,
    ) -> None:
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)
        self._timestamps.append(timestamp)
        if len(self._highs) > self.lookback_bars:
            self._highs.pop(0)
            self._lows.pop(0)
            self._closes.pop(0)
            self._timestamps.pop(0)
        self._detect_order_blocks()
        self._detect_fvgs()
        self._detect_key_levels()

    def get_zone(self, current_price: Decimal) -> Optional[PriceZone]:
        if len(self._highs) < 20:
            return None
        period_high = max(self._highs)
        period_low = min(self._lows)
        range_size = period_high - period_low
        if range_size <= Decimal("0"):
            return None
        equilibrium = (period_high + period_low) / Decimal("2")
        position = (current_price - period_low) / range_size
        if position >= self.premium_threshold:
            zone_type = ZoneType.PREMIUM
            strength = (position - self.premium_threshold) / (Decimal("1") - self.premium_threshold)
        elif position <= self.discount_threshold:
            zone_type = ZoneType.DISCOUNT
            strength = (self.discount_threshold - position) / self.discount_threshold
        else:
            zone_type = ZoneType.EQUILIBRIUM
            strength = Decimal("0")
        return PriceZone(
            zone_type=zone_type,
            low=period_low,
            high=period_high,
            equilibrium=equilibrium,
            strength=min(strength, Decimal("1")),
            timestamp=self._timestamps[-1],
        )

    def is_in_discount(self, current_price: Decimal) -> bool:
        zone = self.get_zone(current_price)
        return zone is not None and zone.zone_type == ZoneType.DISCOUNT

    def is_in_premium(self, current_price: Decimal) -> bool:
        zone = self.get_zone(current_price)
        return zone is not None and zone.zone_type == ZoneType.PREMIUM
    def _detect_order_blocks(self) -> None:
        if len(self._highs) < self.ob_lookback + 3:
            return
        recent = list(zip(self._highs[-self.ob_lookback:], self._lows[-self.ob_lookback:],
                          self._closes[-self.ob_lookback:]))
        swing_high = max(h for h, _, _ in recent)
        swing_low = min(l for _, l, _ in recent)
        current_close = self._closes[-1]
        if current_close > swing_high:
            for i in range(len(recent) - 1, -1, -1):
                h, l, c = recent[i]
                if h == swing_high:
                    for j in range(i - 1, max(-1, i - 6), -1):
                        oh, ol, oc = recent[j]
                        op = self._closes[-self.ob_lookback + j - 1] if j > 0 else ol
                        if oc < op:
                            ob = OrderBlock(
                                block_type=OrderBlockType.BULLISH_OB,
                                low=ol,
                                high=oh,
                                timestamp=self._timestamps[-self.ob_lookback + j],
                            )
                            if not any(
                                ob.block_type == b.block_type
                                and abs(ob.low - b.low) < self._pip(Decimal("2"))
                                and abs(ob.high - b.high) < self._pip(Decimal("2"))
                                for b in self._order_blocks
                            ):
                                self._order_blocks.append(ob)
                            return
        if current_close < swing_low:
            for i in range(len(recent) - 1, -1, -1):
                h, l, c = recent[i]
                if l == swing_low:
                    for j in range(i - 1, max(-1, i - 6), -1):
                        oh, ol, oc = recent[j]
                        op = self._closes[-self.ob_lookback + j - 1] if j > 0 else oh
                        if oc > op:
                            ob = OrderBlock(
                                block_type=OrderBlockType.BEARISH_OB,
                                low=ol,
                                high=oh,
                                timestamp=self._timestamps[-self.ob_lookback + j],
                            )
                            if not any(
                                ob.block_type == b.block_type
                                and abs(ob.low - b.low) < self._pip(Decimal("2"))
                                and abs(ob.high - b.high) < self._pip(Decimal("2"))
                                for b in self._order_blocks
                            ):
                                self._order_blocks.append(ob)
                            return

    def _detect_fvgs(self) -> None:
        if len(self._highs) < 3:
            return
        h1, l1 = self._highs[-3], self._lows[-3]
        h2, l2 = self._highs[-2], self._lows[-2]
        h3, l3 = self._highs[-1], self._lows[-1]
        if l3 > h1:
            fvg = FairValueGap(
                gap_type="BULLISH_FVG", top=l3, bottom=h1, timestamp=self._timestamps[-1],
            )
            if not any(
                abs(fvg.top - f.top) < self._pip(Decimal("1"))
                and abs(fvg.bottom - f.bottom) < self._pip(Decimal("1"))
                for f in self._fvgs
            ):
                self._fvgs.append(fvg)
        if h3 < l1:
            fvg = FairValueGap(
                gap_type="BEARISH_FVG", top=l1, bottom=h3, timestamp=self._timestamps[-1],
            )
            if not any(
                abs(fvg.top - f.top) < self._pip(Decimal("1"))
                and abs(fvg.bottom - f.bottom) < self._pip(Decimal("1"))
                for f in self._fvgs
            ):
                self._fvgs.append(fvg)


    def _detect_key_levels(self) -> None:
        """Incremental check only for the NEW bar — O(1) instead of O(n)."""
        if len(self._highs) < 4:
            return
        # Only check the NEW bar against the previous 5 bars
        i = len(self._highs) - 1
        hi = self._highs[i]
        lo = self._lows[i]
        # Check if new high/low is a swing (compare with previous 5 bars)
        is_swing_high = all(hi >= self._highs[j] for j in range(max(0, i - 4), i))
        is_swing_low = all(lo <= self._lows[j] for j in range(max(0, i - 4), i))
        if is_swing_high:
            self._register_key_level(hi, "SWING_HIGH", self._timestamps[i])
        if is_swing_low:
            self._register_key_level(lo, "SWING_LOW", self._timestamps[i])

    def _register_key_level(self, price: Decimal, level_type: str, timestamp: datetime) -> None:
        pip = self._pip(Decimal("3"))
        for level in self._key_levels:
            if level.level_type == level_type and abs(level.price - price) < pip:
                level.touches += 1
                level.last_touch = timestamp
                return
    def get_nearest_order_block(
        self, price: Decimal, block_type: OrderBlockType, direction: int,
    ) -> Optional[OrderBlock]:
        candidates = [ob for ob in self._order_blocks if ob.block_type == block_type and not ob.mitigated]
        if direction > 0:
            below = [ob for ob in candidates if ob.high < price]
            if not below:
                return None
            return max(below, key=lambda ob: ob.high)
        else:
            above = [ob for ob in candidates if ob.low > price]
            if not above:
                return None
            return min(above, key=lambda ob: ob.low)

    def get_nearest_fvg(self, price: Decimal, direction: int) -> Optional[FairValueGap]:
        candidates = [f for f in self._fvgs if not f.filled]
        if direction > 0:
            above = [f for f in candidates if f.bottom > price]
            if not above:
                return None
            return min(above, key=lambda f: f.bottom)
        else:
            below = [f for f in candidates if f.top < price]
            if not below:
                return None
            return max(below, key=lambda f: f.top)

    @staticmethod
    def _pip(value: Decimal) -> Decimal:
        return value * Decimal("0.0001")

    @property
    def order_blocks(self) -> list[OrderBlock]:
        return self._order_blocks

    @property
    def fvgs(self) -> list[FairValueGap]:
        return self._fvgs

    @property
    def key_levels(self) -> list[KeyLevel]:
        return self._key_levels

        self._key_levels.append(KeyLevel(price=price, level_type=level_type, touches=1, last_touch=timestamp))
