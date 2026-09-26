"""
Causal market data pipeline, historical ECN fetcher, and quality auditing engine.
"""

from forex_platform.market_data.loader import (
    DataGap,
    DuplicateTimestampError,
    MarketDataError,
    MarketDataLoader,
    MonotonicityError,
)
from forex_platform.market_data.causal_aligner import (
    CausalAligner,
    CausalLookaheadViolationError,
    Timeframe,
)
from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.provenance import DataProvenance, fingerprint_dataframe
from forex_platform.market_data.quality import (
    DataQualityAuditor,
    PriceAnomaly,
    QualityAuditReport,
)

__all__ = [
    "CausalAligner",
    "CausalLookaheadViolationError",
    "DataGap",
    "DataProvenance",
    "fingerprint_dataframe",
    "DataQualityAuditor",
    "DuplicateTimestampError",
    "HistoricalECNFetcher",
    "MarketDataError",
    "MarketDataLoader",
    "MonotonicityError",
    "PriceAnomaly",
    "QualityAuditReport",
    "Timeframe",
]
