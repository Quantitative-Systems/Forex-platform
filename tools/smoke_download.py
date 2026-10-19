"""Quick smoke test: fetch 1 day of M1 for EURUSD to validate the Dukascopy path."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from forex_platform.market_data.download_dukascopy_m1 import download_pair


def main() -> None:
    start = datetime(2024, 6, 3, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 6, 4, 0, 0, 0, tzinfo=timezone.utc)
    cache = Path("data/cache/dukascopy_smoke")
    path = download_pair("EURUSD", start, end, cache)
    print("OK ->", path, Path(path).stat().st_size, "bytes")


if __name__ == "__main__":
    main()