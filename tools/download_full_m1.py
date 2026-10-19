"""Download full-range Dukascopy M1 (BID+ASK) for the 4 research pairs.

Logs progress to ``data/cache/dukascopy/FULL_DOWNLOAD.log`` and writes a DONE
marker at the end. Designed to be launched detached so the strategy build can
proceed in parallel.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from forex_platform.market_data.download_dukascopy_m1 import download_pair

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 18, 0, 0, 0, tzinfo=timezone.utc)  # exclusive; matches existing dataset boundary
CACHE = Path("data/cache/dukascopy")
LOG = CACHE / "FULL_DOWNLOAD.log"


def _say(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main() -> int:
    if LOG.exists():
        LOG.unlink()
    _say(f"START {START.isoformat()} -> {END.isoformat()} pairs={PAIRS}")
    failures: list[str] = []
    for symbol in PAIRS:
        try:
            path = download_pair(symbol, START, END, CACHE)
            _say(f"OK {symbol} rows_written={Path(path).stat().st_size}B {path}")
        except Exception as exc:  # noqa: BLE001
            _say(f"ERROR {symbol}: {type(exc).__name__}: {exc}")
            _say(traceback.format_exc(limit=3))
            failures.append(symbol)
    marker = CACHE / "FULL_DOWNLOAD.done"
    marker.write_text(("OK\n" if not failures else f"PARTIAL failures={failures}\n"), encoding="utf-8")
    _say(f"DONE failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())