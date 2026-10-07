import sys
import os
# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forex_platform.market_data.historical_fetcher import HistoricalECNFetcher
from forex_platform.market_data.provenance import DataProvenance

# Generate 10,000 bars of EURUSD M15 synthetic ECN history (covers ~2+ years)
df = HistoricalECNFetcher.generate_synthetic_ecn_history(
    symbol='EURUSD', timeframe='M15', num_bars=10000
)

# Cache it to data/cache
cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'cache')
os.makedirs(cache_dir, exist_ok=True)

cache_path = HistoricalECNFetcher.cache_to_parquet(
    df, 'EURUSD', 'M15',
    cache_dir=cache_dir,
    provenance=DataProvenance.SYNTHETIC,
    source='2year_synthetic_ecn_generator'
)

print(f'Generated {df.height} bars')
print(f'Cached to: {cache_path}')
ts_min = df['timestamp'].min()
ts_max = df['timestamp'].max()
print(f'Date range: {ts_min} to {ts_max}')