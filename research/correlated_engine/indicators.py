#!/usr/bin/env python3
"""
Core Indicators for Correlated MTF Fractal Strategy
====================================================
Implements VWMA, ATR, Premium/Discount Zones, Swing Detection,
Key Zones, Order Blocks, and Fair Value Gaps.
"""

import polars as pl
import numpy as np
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from collections import deque


@dataclass
class IndicatorState:
    """Complete indicator state for a timeframe."""
    vwma_50: float = 0.0
    atr_14: float = 0.0
    pz: float = 0.0
    swing_highs: List[Tuple[int, float]] = None  # (index, price)
    swing_lows: List[Tuple[int, float]] = None
    key_zones: List[Dict] = None
    order_blocks: List[Dict] = None
    fvgs: List[Dict] = None
    
    def __post_init__(self):
        if self.swing_highs is None:
            self.swing_highs = []
        if self.swing_lows is None:
            self.swing_lows = []
        if self.key_zones is None:
            self.key_zones = []
        if self.order_blocks is None:
            self.order_blocks = []
        if self.fvgs is None:
            self.fvgs = []


def calculate_vwma(df: pl.DataFrame, period: int = 50) -> pl.DataFrame:
    """Calculate Volume-Weighted Moving Average."""
    if df.is_empty() or len(df) < period:
        return df.with_columns(pl.lit(0.0).alias("vwma_50"))
    
    # Typical price * volume
    tpv = (df["open"] + df["high"] + df["low"] + df["close"]) / 4 * df["volume"]
    
    df = df.with_columns([
        tpv.rolling_sum(period).alias("tpv_sum"),
        df["volume"].rolling_sum(period).alias("vol_sum"),
    ])
    
    df = df.with_columns([
        (pl.col("tpv_sum") / pl.col("vol_sum")).alias("vwma_50")
    ])
    
    return df


def calculate_atr(df: pl.DataFrame, period: int = 14) -> pl.DataFrame:
    """Calculate Average True Range."""
    if df.is_empty() or len(df) < period:
        return df.with_columns(pl.lit(0.0).alias("atr_14"))
    
    # True Range components
    df = df.with_columns([
        (df["high"] - df["low"]).alias("tr1"),
        (df["high"] - df["close"].shift(1)).abs().alias("tr2"),
        (df["low"] - df["close"].shift(1)).abs().alias("tr3"),
    ])
    
    df = df.with_columns([
        pl.max_horizontal("tr1", "tr2", "tr3").alias("true_range")
    ])
    
    df = df.with_columns([
        pl.col("true_range").rolling_mean(period).alias("atr_14")
    ])
    
    return df


def calculate_pz(close: float, vwma: float, atr: float) -> float:
    """Premium/Discount Zone position: (price - VWMA) / ATR."""
    if atr <= 0 or vwma <= 0:
        return 0.0
    return (close - vwma) / atr


def detect_swings(df: pl.DataFrame, lookback: int = 5) -> Tuple[List, List]:
    """
    Detect swing highs and lows using fractal pattern.
    Swing high: high[i] > high[i-n] and high[i] > high[i+n] for n in 1..lookback
    Swing low: low[i] < low[i-n] and low[i] < low[i+n] for n in 1..lookback
    """
    if df.is_empty() or len(df) < 2 * lookback + 1:
        return [], []
    
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    indices = df["timestamp"].to_numpy() if "timestamp" in df.columns else np.arange(len(df))
    
    swing_highs = []
    swing_lows = []
    
    for i in range(lookback, len(df) - lookback):
        # Swing high
        is_high = all(highs[i] > highs[i - n] for n in range(1, lookback + 1)) and \
                  all(highs[i] > highs[i + n] for n in range(1, lookback + 1))
        if is_high:
            swing_highs.append((i, float(highs[i])))
        
        # Swing low
        is_low = all(lows[i] < lows[i - n] for n in range(1, lookback + 1)) and \
                 all(lows[i] < lows[i + n] for n in range(1, lookback + 1))
        if is_low:
            swing_lows.append((i, float(lows[i])))
    
    return swing_highs, swing_lows


def detect_market_structure(swing_highs: List, swing_lows: List) -> Dict:
    """
    Determine market structure from swing points.
    Returns: trend direction, higher highs, lower lows, etc.
    """
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"trend": "UNDEFINED", "structure": "CHOP"}
    
    # Get last few swings
    recent_highs = swing_highs[-3:]
    recent_lows = swing_lows[-3:]
    
    # Check for higher highs and higher lows (bullish)
    hh = all(recent_highs[i][1] > recent_highs[i-1][1] for i in range(1, len(recent_highs)))
    hl = all(recent_lows[i][1] > recent_lows[i-1][1] for i in range(1, len(recent_lows)))
    
    # Check for lower highs and lower lows (bearish)
    lh = all(recent_highs[i][1] < recent_highs[i-1][1] for i in range(1, len(recent_highs)))
    ll = all(recent_lows[i][1] < recent_lows[i-1][1] for i in range(1, len(recent_lows)))
    
    if hh and hl:
        return {"trend": "BULLISH", "structure": "TRENDING"}
    elif lh and ll:
        return {"trend": "BEARISH", "structure": "TRENDING"}
    else:
        return {"trend": "MIXED", "structure": "CHOP"}


def detect_key_zones(df: pl.DataFrame, swing_highs: List, swing_lows: List, atr: float) -> List[Dict]:
    """
    Detect key zones from swing clusters.
    Zones form where multiple swings cluster within ATR distance.
    """
    if df.is_empty() or atr <= 0:
        return []
    
    zones = []
    all_swings = [(idx, price, "high") for idx, price in swing_highs] + \
                 [(idx, price, "low") for idx, price in swing_lows]
    all_swings.sort(key=lambda x: x[1])  # Sort by price
    
    # Cluster swings within 0.5 * ATR
    cluster_threshold = 0.5 * atr
    current_cluster = []
    
    for swing in all_swings:
        if not current_cluster:
            current_cluster = [swing]
        elif abs(swing[1] - current_cluster[-1][1]) <= cluster_threshold:
            current_cluster.append(swing)
        else:
            # Finalize cluster if it has 2+ swings
            if len(current_cluster) >= 2:
                prices = [s[1] for s in current_cluster]
                zones.append({
                    "type": "key_zone",
                    "high": max(prices),
                    "low": min(prices),
                    "mid": np.mean(prices),
                    "strength": len(current_cluster),
                    "swing_indices": [s[0] for s in current_cluster]
                })
            current_cluster = [swing]
    
    # Check last cluster
    if len(current_cluster) >= 2:
        prices = [s[1] for s in current_cluster]
        zones.append({
            "type": "key_zone",
            "high": max(prices),
            "low": min(prices),
            "mid": np.mean(prices),
            "strength": len(current_cluster),
            "swing_indices": [s[0] for s in current_cluster]
        })
    
    return zones


def detect_order_blocks(df: pl.DataFrame, swing_highs: List, swing_lows: List, atr: float) -> List[Dict]:
    """
    Detect order blocks: strong moves that create institutional footprints.
    Bullish OB: Down candle followed by strong up move breaking recent low
    Bearish OB: Up candle followed by strong down move breaking recent high
    """
    if df.is_empty() or atr <= 0 or len(df) < 20:
        return []
    
    obs = []
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    
    # Look for order blocks in last 50 bars
    lookback = min(50, len(df) - 1)
    
    for i in range(len(df) - lookback, len(df) - 1):
        body = abs(closes[i] - opens[i])
        range_ = highs[i] - lows[i]
        
        # Significant candle (body > 0.5 * ATR)
        if body < 0.5 * atr:
            continue
        
        # Bullish OB: bearish candle followed by bullish breakout
        if closes[i] < opens[i]:  # Bearish candle
            # Check if next few candles break above this candle's high
            for j in range(i + 1, min(i + 5, len(df))):
                if closes[j] > highs[i] and (closes[j] - opens[j]) > 0.5 * atr:
                    obs.append({
                        "type": "bullish_ob",
                        "index": i,
                        "price_high": highs[i],
                        "price_low": lows[i],
                        "trigger_index": j,
                        "strength": body / atr
                    })
                    break
        
        # Bearish OB: bullish candle followed by bearish breakout
        elif closes[i] > opens[i]:  # Bullish candle
            for j in range(i + 1, min(i + 5, len(df))):
                if closes[j] < lows[i] and (opens[j] - closes[j]) > 0.5 * atr:
                    obs.append({
                        "type": "bearish_ob",
                        "index": i,
                        "price_high": highs[i],
                        "price_low": lows[i],
                        "trigger_index": j,
                        "strength": body / atr
                    })
                    break
    
    return obs


def detect_fvgs(df: pl.DataFrame, atr: float) -> List[Dict]:
    """
    Detect Fair Value Gaps (FVGs).
    Bullish FVG: low[i+1] > high[i] (gap up)
    Bearish FVG: high[i+1] < low[i] (gap down)
    """
    if df.is_empty() or atr <= 0 or len(df) < 3:
        return []
    
    fvgs = []
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    
    for i in range(len(df) - 2):
        # Bullish FVG
        if lows[i + 2] > highs[i]:
            gap_size = lows[i + 2] - highs[i]
            if gap_size > 0.1 * atr:  # Minimum gap size
                fvgs.append({
                    "type": "bullish_fvg",
                    "index": i,
                    "gap_high": lows[i + 2],
                    "gap_low": highs[i],
                    "size": gap_size,
                    "filled": False
                })
        
        # Bearish FVG
        elif highs[i + 2] < lows[i]:
            gap_size = lows[i] - highs[i + 2]
            if gap_size > 0.1 * atr:
                fvgs.append({
                    "type": "bearish_fvg",
                    "index": i,
                    "gap_high": lows[i],
                    "gap_low": highs[i + 2],
                    "size": gap_size,
                    "filled": False
                })
    
    return fvgs


def compute_all_indicators(df: pl.DataFrame) -> pl.DataFrame:
    """Compute all indicators for a DataFrame."""
    if df.is_empty():
        return df
    
    df = calculate_vwma(df, 50)
    df = calculate_atr(df, 14)
    
    # Calculate PZ for each row
    df = df.with_columns([
        ((pl.col("close") - pl.col("vwma_50")) / pl.col("atr_14")).alias("pz")
    ])
    
    return df


if __name__ == "__main__":
    # Test with sample data
    import numpy as np
    from datetime import datetime, timedelta
    
    # Create test data manually
    n = 1000
    base_time = datetime(2024, 1, 1)
    dates = [base_time + timedelta(minutes=i) for i in range(n)]
    
    np.random.seed(42)
    close = 1.1000 + np.cumsum(np.random.randn(n) * 0.0001)
    
    test_df = pl.DataFrame({
        "timestamp": dates,
        "open": close + np.random.randn(n) * 0.00005,
        "high": close + np.abs(np.random.randn(n)) * 0.0001,
        "low": close - np.abs(np.random.randn(n)) * 0.0001,
        "close": close,
        "volume": np.random.randint(100, 1000, n),
        "spread": np.full(n, 1.0)
    })
    
    # Ensure high >= low and high >= close/open, low <= close/open
    test_df = test_df.with_columns([
        pl.max_horizontal("high", "open", "close").alias("high"),
        pl.min_horizontal("low", "open", "close").alias("low"),
    ])
    
    result = compute_all_indicators(test_df)
    print(f"Rows: {len(result)}")
    print(f"VWMA last: {result['vwma_50'][-1]:.5f}")
    print(f"ATR last: {result['atr_14'][-1]:.5f}")
    print(f"PZ last: {result['pz'][-1]:.2f}")
    
    # Test swings
    swings_h, swings_l = detect_swings(result)
    print(f"Swing highs: {len(swings_h)}, Swing lows: {len(swings_l)}")
    
    # Test structure
    structure = detect_market_structure(swings_h, swings_l)
    print(f"Structure: {structure}")
    
    # Test key zones
    zones = detect_key_zones(result, swings_h, swings_l, float(result["atr_14"][-1]))
    print(f"Key zones: {len(zones)}")
    
    # Test OBs
    obs = detect_order_blocks(result, swings_h, swings_l, float(result["atr_14"][-1]))
    print(f"Order blocks: {len(obs)}")
    
    # Test FVGs
    fvgs = detect_fvgs(result, float(result["atr_14"][-1]))
    print(f"FVGs: {len(fvgs)}")