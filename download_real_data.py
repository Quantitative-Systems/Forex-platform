import sys
sys.path.insert(0, r'c:\Users\nares\Workspace\Forex-platform')

from forex_platform.market_data.historical_downloader import HistoricalDownloader
from forex_platform.market_data.provenance import DataProvenance
import polars as pl

async def download_real_eurusd():
    """Download 3 years of real EURUSD M15 data from Dukascopy."""
    downloader = HistoricalDownloader(
        cache_dir=r'c:\Users\nares\Workspace\Forex-platform\data\cache',
        max_concurrent_downloads=1,
        request_timeout=120,
        retry_attempts=3,
        retry_delay=3.0
    )
    
    results = await downloader.download_historical_data(
        symbols=['EURUSD'],
        timeframes=[downloader.Timeframe.M15],
        years=3,
        max_concurrent=1,
        allow_synthetic_fallback=False
    )
    
    for symbol, tf_data in results.items():
        for tf, df in tf_data.items():
            print(f'{symbol} {tf.value}: {df.height} bars cached')
            if df.height > 0:
                print(f'Date range: {df["timestamp"].min()} to {df["timestamp"].max()}')
                
                # Check for NaN values
                nan_count = df.select(pl.col('*').is_null().sum()).sum().item()
                print(f'NaN values: {nan_count}')
                
                # Check timestamps are ascending
                timestamps = df['timestamp'].to_list()
                is_ascending = all(timestamps[i] <= timestamps[i+1] for i in range(len(timestamps)-1))
                print(f'Timestamps ascending: {is_ascending}')
                
                # Verify provenance - should be REAL_VENDOR if from Dukascopy
                print(f'Data columns: {df.columns[:5]}')
                
                # Cache provenance check
                cache_path = HistoricalECNFetcher.get_cache_path(symbol, tf, downloader.cache_dir)
                if cache_path.exists():
                    meta = HistoricalECNFetcher.load_provenance(symbol, tf, downloader.cache_dir)
                    print(f'Cache provenance: {meta}')
    
    # Verify final cache state
    print('\\n--- Final Cache Verification ---')
    cache_dir = r'c:\Users\nares\Workspace\Forex-platform\data\cache'
    import os
    for f in os.listdir(cache_dir):
        fp = os.path.join(cache_dir, f)
        if f.endswith('.parquet'):
            import polars as pl
            df = pl.read_parquet(fp)
            meta_path = os.path.join(cache_dir, f.replace('.parquet', '.meta.json'))
            if os.path.exists(meta_path):
                import json
                with open(meta_path) as mf:
                    meta = json.load(mf)
                print(f'{f}: {df.height} bars, provenance={meta.get("classification")}, source={meta.get("source")}')

asyncio.download_real_eurusd()