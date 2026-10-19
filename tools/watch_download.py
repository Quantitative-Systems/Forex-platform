"""
Watch the Dukascopy M1 download and automatically launch the validation campaign
once the ``FULL_DOWNLOAD.done`` marker appears.

Usage:
    python tools/watch_download.py [--poll-seconds 60]

The watcher polls the download log for the ``DONE failures=[]`` line. When it
arrives it launches ``research/supertrend_stochastic_campaign.py`` on all 4
forex pairs and writes results to ``research/results/supertrend_stochastic_campaign.json``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_LOG = ROOT / "data" / "cache" / "dukascopy" / "FULL_DOWNLOAD.log"
DOWNLOAD_DONE = ROOT / "data" / "cache" / "dukascopy" / "FULL_DOWNLOAD.done"
CAMPAIGN = ROOT / "research" / "supertrend_stochastic_campaign.py"
RESULTS = ROOT / "research" / "results" / "supertrend_stochastic_campaign.json"


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}"
    print(line, flush=True)


def wait_for_download(poll_seconds: int = 60) -> bool:
    """Poll until the download done marker appears. Returns True on success."""
    _log(f"Watching {DOWNLOAD_LOG} (poll every {poll_seconds}s)...")
    while True:
        if DOWNLOAD_DONE.exists():
            content = DOWNLOAD_DONE.read_text(encoding="utf-8").strip()
            _log(f"Done marker found: {content}")
            return "failures=[]" in content or "OK" in content
        # If the log says DONE but marker is missing (crash), treat as failure
        if DOWNLOAD_LOG.exists():
            tail = DOWNLOAD_LOG.read_text(encoding="utf-8")[-500:]
            if "DONE failures=" in tail:
                _log("Log reports DONE but marker missing — treating as failed download.")
                return False
        time.sleep(poll_seconds)


def launch_campaign(symbols: list[str]) -> int:
    cmd = [
        sys.executable,
        str(CAMPAIGN),
        "--symbols",
        *symbols,
        "--results",
        str(RESULTS),
        "--minimum-years",
        "2.0",
    ]
    _log(f"Launching campaign: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch download, then run campaign")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    )
    args = parser.parse_args(argv)

    if not wait_for_download(args.poll_seconds):
        _log("Download did not complete successfully. Aborting campaign.")
        return 1

    return launch_campaign(args.symbols)


if __name__ == "__main__":
    sys.exit(main())
