"""Liquidity pools: equal highs/lows (EQH/EQL) and wick sweeps.

An EQH/EQL pool forms when two or more swing points of the same type sit
within ``tolerance`` of each other — resting stop orders cluster there.
A sweep wicks beyond the pool extreme but closes back inside; a body close
through the pool is a structural break, not a sweep.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Sequence

import polars as pl
from pydantic import BaseModel, ConfigDict

from forex_platform.market_model.contracts import (
    MarketZone,
    SwingPoint,
    SwingType,
    ZoneScope,
    ZoneType,
)


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class LiquidityKind(str, Enum):
    """Kind of liquidity pool."""

    EQH = "EQH"
    EQL = "EQL"


class LiquidityPool(BaseModel):
    """A cluster of equal highs or equal lows."""

    model_config = ConfigDict(frozen=True)

    kind: LiquidityKind
    price: Decimal
    tolerance: Decimal
    swing_indexes: List[int]
    timestamp: datetime
    scope: ZoneScope = ZoneScope.EXTERNAL


class LiquiditySweep(BaseModel):
    """A wick beyond a pool extreme that closes back inside."""

    model_config = ConfigDict(frozen=True)

    index: int
    timestamp: datetime
    direction: int
    swept_price: Decimal
    close_price: Decimal
    extreme_price: Decimal


def detect_liquidity_pools(
    swings: Sequence[SwingPoint],
    *,
    tolerance: Decimal = Decimal("0.0005"),
    min_touches: int = 2,
    scope: ZoneScope = ZoneScope.EXTERNAL,
) -> List[LiquidityPool]:
    """Cluster same-type swing points within ``tolerance`` into pools.

    Equal highs (EQH) group swing highs; equal lows (EQL) group swing
    lows. Only swings matching ``scope`` are pooled. A pool requires at
    least ``min_touches`` members and is priced at the cluster mean.
    """
    pools: List[LiquidityPool] = []
    for swing_type, kind in ((SwingType.HIGH, LiquidityKind.EQH), (SwingType.LOW, LiquidityKind.EQL)):
        candidates = sorted(
            (s for s in swings if s.swing_type == swing_type and s.scope == scope),
            key=lambda s: (s.price, s.index),
        )
        cluster: List[SwingPoint] = []
        for swing in candidates:
            if cluster and abs(swing.price - cluster[-1].price) > tolerance:
                if len(cluster) >= min_touches:
                    pools.append(
                        LiquidityPool(
                            kind=kind,
                            price=sum((s.price for s in cluster), Decimal("0")) / Decimal(len(cluster)),
                            tolerance=tolerance,
                            swing_indexes=[s.index for s in cluster],
                            timestamp=cluster[-1].timestamp,
                            scope=scope,
                        )
                    )
                cluster = []
            cluster.append(swing)
        if len(cluster) >= min_touches:
            pools.append(
                LiquidityPool(
                    kind=kind,
                    price=sum((s.price for s in cluster), Decimal("0")) / Decimal(len(cluster)),
                    tolerance=tolerance,
                    swing_indexes=[s.index for s in cluster],
                    timestamp=cluster[-1].timestamp,
                    scope=scope,
                )
            )

    return pools

def detect_liquidity_sweeps(
    bars: pl.DataFrame,
    pools: Sequence[LiquidityPool],
    *,
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    timestamp_col: str = "timestamp",
) -> List[LiquiditySweep]:
    """Detect wick sweeps of liquidity pools that fail to close through.

    EQH pool at price P: bar wicks above P but closes at or below P -> sweep.
    EQL pool at price P: bar wicks below P but closes at or above P -> sweep.
    A body close through P is a structural break and is not reported here.
    """
    if not pools or bars.is_empty():
        return []

    highs = bars[high_col].to_list()
    lows = bars[low_col].to_list()
    closes = bars[close_col].to_list()
    stamps = bars[timestamp_col].to_list()

    sweeps: List[LiquiditySweep] = []
    for i in range(bars.height):
        for pool in pools:
            pool_ts = pool.timestamp
            if stamps[i] is not None and pool_ts is not None and stamps[i] < pool_ts:
                continue  # pool did not exist yet
            price = _dec(pool.price)
            if pool.kind == LiquidityKind.EQH:
                extreme = _dec(highs[i])
                close = _dec(closes[i])
                if extreme > price and close <= price:
                    sweeps.append(
                        LiquiditySweep(
                            index=i,
                            timestamp=stamps[i],
                            direction=-1,
                            swept_price=price,
                            close_price=close,
                            extreme_price=extreme,
                        )
                    )
            else:  # EQL
                extreme = _dec(lows[i])
                close = _dec(closes[i])
                if extreme < price and close >= price:
                    sweeps.append(
                        LiquiditySweep(
                            index=i,
                            timestamp=stamps[i],
                            direction=1,
                            swept_price=price,
                            close_price=close,
                            extreme_price=extreme,
                        )
                    )
    return sweeps


def liquidity_pools_to_zones(pools: Sequence[LiquidityPool]) -> List[MarketZone]:
    """Convert liquidity pools to generic :class:`MarketZone` records."""
    return [
        MarketZone(
            zone_type=ZoneType.EQUAL_HIGH_LOW,
            scope=pool.scope,
            index_start=min(pool.swing_indexes),
            index_end=max(pool.swing_indexes),
            timestamp_start=pool.timestamp,
            timestamp_end=pool.timestamp,
            price_low=pool.price,
            price_high=pool.price,
        )
        for pool in pools
    ]
