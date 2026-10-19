"""
Evaluate SuperTrend-Stochastic campaign results against pre-declared institutional gates.

Reads ``research/results/supertrend_stochastic_campaign.json`` and scores every
cell (symbol × set) against the hard gates declared in
``research/verdicts/INSTITUTIONAL_VERDICT.md``.

Usage:
    python research/evaluate_campaign_results.py [--results path/to/json]
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

# Pre-declared institutional gates (mirrors INSTITUTIONAL_VERDICT.md Section 5)
GATES = {
    "minimum_trades": 100,
    "win_rate_min": 0.40,
    "profit_factor_min": Decimal("1.5"),
    "expectancy_min_r": Decimal("0.5"),
    "max_drawdown_max": Decimal("0.15"),
    "sharpe_min": Decimal("1.0"),
    "min_rr_tp3": Decimal("6.0"),
}


def _pass_fail(metric: Decimal | float | None, threshold: Decimal | float, higher_better: bool) -> bool:
    if metric is None:
        return False
    m = Decimal(str(metric))
    t = Decimal(str(threshold))
    return m >= t if higher_better else m <= t


def evaluate(results: list[dict]) -> dict:
    cell_results: list[dict] = []
    total_trades = 0
    cells_passed = 0
    cells_evaluated = 0

    for r in results:
        trades = r.get("total_trades", 0)
        total_trades += trades

        pf = Decimal(str(r.get("profit_factor") or 0))
        expectancy = Decimal(str(r.get("expectancy") or 0))
        dd = Decimal(str(r.get("max_drawdown_pct") or 0))
        sharpe = Decimal(str(r.get("sharpe_ratio") or 0))
        win_rate = Decimal(str(r.get("win_rate") or 0))

        g_min_trades = trades >= GATES["minimum_trades"]
        g_win_rate = _pass_fail(win_rate, GATES["win_rate_min"], True)
        g_pf = _pass_fail(pf, GATES["profit_factor_min"], True)
        g_expectancy = _pass_fail(expectancy, GATES["expectancy_min_r"], True)
        g_dd = _pass_fail(dd, GATES["max_drawdown_max"], False)
        g_sharpe = _pass_fail(sharpe, GATES["sharpe_min"], True)

        gates = {
            "minimum_trades": g_min_trades,
            "win_rate_min": g_win_rate,
            "profit_factor_min": g_pf,
            "expectancy_min_r": g_expectancy,
            "max_drawdown_max": g_dd,
            "sharpe_min": g_sharpe,
        }
        all_pass = all(gates.values())

        if trades > 0:
            cells_evaluated += 1
            if all_pass:
                cells_passed += 1

        cell_results.append(
            {
                "symbol": r.get("symbol"),
                "set_key": r.get("set_key"),
                "total_trades": trades,
                "win_rate": float(win_rate),
                "profit_factor": float(pf),
                "expectancy_r": float(expectancy),
                "max_drawdown_pct": float(dd),
                "sharpe_ratio": float(sharpe),
                "data_years": r.get("data_years"),
                "gates": gates,
                "all_gates_pass": all_pass,
            }
        )

    summary = {
        "total_trades_all_cells": total_trades,
        "cells_evaluated": cells_evaluated,
        "cells_passed_all_gates": cells_passed,
        "gate_definitions": {k: str(v) for k, v in GATES.items()},
    }

    return {"summary": summary, "cells": cell_results}


def main(argv: list[str] | None = None) -> int:
    results_file = Path(argv[0]) if argv else Path("research/results/supertrend_stochastic_campaign.json")
    if not results_file.exists():
        print(f"ERROR: results file not found: {results_file}", file=sys.stderr)
        return 1

    raw = json.loads(results_file.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "cells" in raw:
        results = raw["cells"]
    elif isinstance(raw, list):
        results = raw
    else:
        print("ERROR: unrecognized results format", file=sys.stderr)
        return 1

    evaluation = evaluate(results)

    out = results_file.parent / "campaign_gate_evaluation.json"
    out.write_text(json.dumps(evaluation, indent=2, default=str), encoding="utf-8")

    s = evaluation["summary"]
    print(f"\n{'='*60}")
    print("CAMPAIGN GATE EVALUATION")
    print(f"{'='*60}")
    print(f"Total trades across all cells: {s['total_trades_all_cells']}")
    print(f"Cells with trades evaluated:   {s['cells_evaluated']}")
    print(f"Cells passing ALL gates:       {s['cells_passed_all_gates']}")
    print(f"{'='*60}\n")

    for cell in evaluation["cells"]:
        status = "PASS" if cell["all_gates_pass"] else "FAIL"
        flagged = [g for g, v in cell["gates"].items() if not v]
        print(
            f"{cell['symbol']:8s} {cell['set_key']:6s} | "
            f"trades={cell['total_trades']:4d} | "
            f"PF={cell['profit_factor']:.2f} | "
            f"expR={cell['expectancy_r']:.2f} | "
            f"DD={cell['max_drawdown_pct']:.2%} | "
            f"WR={cell['win_rate']:.2%} | "
            f"{status}"
        )
        if flagged:
            print(f"{'':16s}  failed gates: {', '.join(flagged)}")

    print(f"\nFull evaluation written to: {out}")

    if s["cells_passed_all_gates"] == 0:
        print("\nWARNING: No cell passed all institutional gates.")
        print("Next step: review failing gates, adjust parameters, re-run.")
        return 0
    print(f"\n{s['cells_passed_all_gates']} cell(s) passed all gates — eligible for shadow trading.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
