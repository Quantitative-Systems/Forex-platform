"""
Market data quality assurance, empirical spread analysis, and gap classification.
Calculates session-specific rolling median spreads and validates institutional OTC feed integrity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from forex_platform.core.sessions import ForexSessionEngine
from forex_platform.market_data.loader import DataGap, MarketDataLoader


class PriceAnomaly(BaseModel):
    """Detected price integrity anomaly in OHLCV bar."""
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    anomaly_type: str
    details: str


class QualityAuditReport(BaseModel):
    """Comprehensive data quality audit report for historical FX data."""
    model_config = ConfigDict(frozen=True)

    symbol: str
    total_bars: int
    start_time: datetime
    end_time: datetime
    weekend_gaps_count: int
    midweek_drops_count: int
    anomalies_count: int
    anomalies: List[PriceAnomaly] = Field(default_factory=list)
    session_median_spreads: Dict[str, float] = Field(default_factory=dict)
    rolling_20_median_spread_avg: float = 0.0
    is_valid: bool = True
    summary: str = ""


class DataQualityAuditor:
    """
    Auditor for institutional Forex market datasets.
    - Validates OHLC consistency (high >= max(open, close), low <= min(open, close)).
    - Classifies time discontinuities into valid weekend closures vs mid-week outages.
    - Calculates rolling 20-period median spread per trading session.
    """

    @classmethod
    def audit(
        cls,
        df: pl.DataFrame,
        symbol: str = "EURUSD",
        expected_interval: timedelta = timedelta(minutes=15),
    ) -> QualityAuditReport:
        if df.height == 0:
            raise ValueError("Cannot audit an empty market data DataFrame.")

        # 1. Price Integrity Checks
        anomalies: List[PriceAnomaly] = []
        for row in df.iter_rows(named=True):
            ts = row["timestamp"]
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]
            dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
            dt_utc = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                anomalies.append(PriceAnomaly(
                    timestamp=dt_utc,
                    anomaly_type="ZERO_OR_NEGATIVE_PRICE",
                    details=f"O={o}, H={h}, L={l}, C={c}",
                ))
            if h < l:
                anomalies.append(PriceAnomaly(
                    timestamp=dt_utc,
                    anomaly_type="HIGH_LESS_THAN_LOW",
                    details=f"High ({h}) < Low ({l})",
                ))
            if h < max(o, c):
                anomalies.append(PriceAnomaly(
                    timestamp=dt_utc,
                    anomaly_type="HIGH_LESS_THAN_BODY",
                    details=f"High ({h}) < max(Open, Close) ({max(o, c)})",
                ))
            if l > min(o, c):
                anomalies.append(PriceAnomaly(
                    timestamp=dt_utc,
                    anomaly_type="LOW_GREATER_THAN_BODY",
                    details=f"Low ({l}) > min(Open, Close) ({min(o, c)})",
                ))

        # 2. Gap Detection & Classification
        gaps = MarketDataLoader.detect_gaps(df, expected_interval=expected_interval)
        weekend_gaps = [g for g in gaps if g.is_weekend_gap]
        midweek_drops = [g for g in gaps if not g.is_weekend_gap]

        # 3. Rolling 20-period Median Spread
        if "spread" in df.columns:
            rolling_spread = df.select(
                pl.col("spread").rolling_median(window_size=20, min_samples=1).alias("r_med")
            )["r_med"]
            rolling_avg = float(rolling_spread.mean() or 0.0)
        else:
            rolling_avg = 1.0

        # 4. Session-Specific Median Spread
        session_spreads = cls.calculate_session_spreads(df)

        start_ts = df["timestamp"][0]
        end_ts = df["timestamp"][-1]
        start_dt = start_ts if isinstance(start_ts, datetime) else datetime.fromisoformat(str(start_ts))
        end_dt = end_ts if isinstance(end_ts, datetime) else datetime.fromisoformat(str(end_ts))

        is_valid = (len(anomalies) == 0) and (len(midweek_drops) == 0)
        summary = (
            f"Audit for {symbol}: {df.height} bars from {start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}. "
            f"Weekend gaps: {len(weekend_gaps)} (valid). Midweek drops: {len(midweek_drops)}. "
            f"Price anomalies: {len(anomalies)}. Quality: {'PASSED' if is_valid else 'FLAGS_DETECTED'}."
        )

        return QualityAuditReport(
            symbol=symbol,
            total_bars=df.height,
            start_time=start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc),
            end_time=end_dt if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc),
            weekend_gaps_count=len(weekend_gaps),
            midweek_drops_count=len(midweek_drops),
            anomalies_count=len(anomalies),
            anomalies=anomalies[:50],  # cap at 50 for reporting
            session_median_spreads=session_spreads,
            rolling_20_median_spread_avg=round(rolling_avg, 3),
            is_valid=is_valid,
            summary=summary,
        )

    @classmethod
    def calculate_session_spreads(cls, df: pl.DataFrame) -> Dict[str, float]:
        """
        Calculates median spread partitioned across the 4 major sessions:
        Sydney, Tokyo, London, New York.
        """
        if "spread" not in df.columns or df.height == 0:
            return {"SYDNEY": 2.5, "TOKYO": 2.0, "LONDON": 0.8, "NEW_YORK": 1.0}

        sydney_spreads: List[float] = []
        tokyo_spreads: List[float] = []
        london_spreads: List[float] = []
        ny_spreads: List[float] = []

        for row in df.iter_rows(named=True):
            ts = row["timestamp"]
            dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
            dt_utc = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            spread = float(row["spread"])

            state = ForexSessionEngine.get_session_state(dt_utc)
            sess_names = [s.value for s in state.active_sessions]

            if "SYDNEY" in sess_names:
                sydney_spreads.append(spread)
            if "TOKYO" in sess_names:
                tokyo_spreads.append(spread)
            if "LONDON" in sess_names:
                london_spreads.append(spread)
            if "NEW_YORK" in sess_names:
                ny_spreads.append(spread)

        def _calc_median(vals: List[float], default: float) -> float:
            if not vals:
                return default
            return round(float(pl.Series("v", vals).median() or default), 3)

        return {
            "SYDNEY": _calc_median(sydney_spreads, 2.5),
            "TOKYO": _calc_median(tokyo_spreads, 2.0),
            "LONDON": _calc_median(london_spreads, 0.8),
            "NEW_YORK": _calc_median(ny_spreads, 1.0),
        }
