"""Automatic promotion pipeline for engines/crypto_engine.

Scans research/results/*.json for PASS cells, freezes their configs into the
engine manifest, and records REJECTED cells into the failed registry. The
engine folder only ever contains what survived every gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RESEARCH_RESULTS = Path("research/results")
ENGINE_DIR = Path("engines/crypto_engine")
MANIFEST_PATH = ENGINE_DIR / "MANIFEST.json"

GATES = {"min_trades": 30, "min_expectancy": 0.0, "min_pf": 1.2, "max_dd": 0.20}


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {
        "engine": "crypto_engine",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "production_cells": [],     # only PASSed, frozen configs
        "shadow_cells": [],         # passed dev, awaiting validation
    }


def evaluate_cell(cell: dict) -> tuple[bool, list[str]]:
    reasons = []
    if (cell.get("trades") or 0) < GATES["min_trades"]:
        reasons.append(f"trades<{GATES['min_trades']}")
    if (cell.get("expectancy_r") is None or cell["expectancy_r"] <= GATES["min_expectancy"]):
        reasons.append("expectancy<=0")
    pf = cell.get("profit_factor") or 0
    if pf != float("inf") and pf < GATES["min_pf"]:
        reasons.append(f"pf<{GATES['min_pf']}")
    if (cell.get("max_dd") or 1) > GATES["max_dd"]:
        reasons.append(f"dd>{GATES['max_dd']}")
    return (len(reasons) == 0, reasons)


def promote(results_file: Path = RESEARCH_RESULTS / "style_matrix.json") -> dict:
    manifest = load_manifest()
    if not results_file.exists():
        return {"error": f"missing {results_file}"}

    cells = json.loads(results_file.read_text(encoding="utf-8"))
    report = {"promoted": [], "shadow": [], "failed": []}

    for cell in cells:
        ok, reasons = evaluate_cell(cell)
        key = f"{cell['pair']}|{cell['set_key']}|{cell['style']}"
        record = {
            "key": key,
            "pair": cell["pair"],
            "set_key": cell["set_key"],
            "style": cell["style"],
            "metrics": {k: cell.get(k) for k in
                        ("trades", "win_rate", "expectancy_r", "profit_factor", "max_dd")},
            "promoted_utc": datetime.now(timezone.utc).isoformat(),
        }
        if ok:
            # -> shadow first: production requires a separate validation-window run
            record["status"] = "SHADOW_AWAITING_VALIDATION"
            manifest["shadow_cells"].append(record)
            report["shadow"].append(record)
        else:
            record["status"] = "REJECTED"
            record["reasons"] = reasons
            fail_dir = Path("research/failed")
            fail_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            (fail_dir / f"STYLE_CELL_{key.replace('|','_')}_{stamp}.json").write_text(
                json.dumps(record, indent=2, default=str)
            )
            report["failed"].append(record)

    # dedupe manifest by key
    seen = {}
    for c in manifest["shadow_cells"]:
        seen[c["key"]] = c
    manifest["shadow_cells"] = list(seen.values())
    manifest["updated_utc"] = datetime.now(timezone.utc).isoformat()
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return report


if __name__ == "__main__":
    r = promote()
    print(f"Shadow (awaiting validation): {len(r['shadow'])}")
    print(f"Failed (archived to research/failed): {len(r['failed'])}")
    for c in r["shadow"]:
        print(f"  SHADOW {c['key']} exp={c['metrics']['expectancy_r']}")
