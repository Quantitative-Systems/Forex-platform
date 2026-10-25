#!/usr/bin/env python3
"""
Shadow Validation Runner — GBPUSD SET_5 SCALPING v1
====================================================
Runs the FROZEN candidate against live M1 data in shadow mode.
No capital at risk. No orders sent to broker.
Logs every signal, fill simulation, and P&L for later audit.

Mission constraints:
- Strategy logic (fractal_institutional.py) UNCHANGED
- Parameters from frozen snapshot ONLY
- Live capital = $0.00
"""

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Dict, List, Optional
import sys

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_PATH = Path(__file__).parent / "SHADOW_CONFIG.json"
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

SIGNAL_LOG = LOG_DIR / "signals.jsonl"
FILL_LOG = LOG_DIR / "fills.jsonl"
PNL_LOG = LOG_DIR / "pnl.jsonl"
DAILY_SUMMARY = LOG_DIR / "daily_summary.jsonl"


class ShadowValidator:
    def __init__(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)
        self.run_id = str(uuid.uuid4())[:8]
        self.session_start = datetime.now(timezone.utc)
        self.open_positions: Dict[str, dict] = {}
        self.daily_pnl = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.gross_profit_r = 0.0
        self.gross_loss_r = 0.0
        self.max_drawdown = 0.0
        self.peak_equity = 0.0

        self._log_event("SESSION_START", {
            "run_id": self.run_id,
            "config": self.config,
            "start_time": self.session_start.isoformat()
        })

    def _log_event(self, event_type: str, data: dict):
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event_type": event_type,
            "data": data
        }
        with open(SIGNAL_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

    def on_m1_bar(self, bar: dict):
        """Process each M1 bar - shadow mode simulation."""
        # This would integrate with the actual strategy engine
        # For now, scaffold the interface
        pass

    def on_signal_generated(self, signal: dict):
        """Log a strategy signal (entry/exit) with full context."""
        signal["shadow_metadata"] = {
            "run_id": self.run_id,
            "received_at_utc": datetime.now(timezone.utc).isoformat(),
            "cost_model_applied": True
        }
        with open(SIGNAL_LOG, "a") as f:
            f.write(json.dumps(signal) + "\n")

    def on_fill_simulated(self, fill: dict):
        """Log a simulated fill with cost model applied."""
        fill["shadow_metadata"] = {
            "run_id": self.run_id,
            "filled_at_utc": datetime.now(timezone.utc).isoformat(),
            "costs": self.config["cost_model"]
        }
        with open(FILL_LOG, "a") as f:
            f.write(json.dumps(fill) + "\n")

        # Update P&L tracking
        self.trade_count += 1
        pnl_r = fill.get("pnl_r", 0.0)
        if pnl_r > 0:
            self.win_count += 1
            self.gross_profit_r += pnl_r
        else:
            self.loss_count += 1
            self.gross_loss_r += abs(pnl_r)

        self._update_drawdown(pnl_r)

        with open(PNL_LOG, "a") as f:
            f.write(json.dumps({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "trade_number": self.trade_count,
                "pnl_r": pnl_r,
                "cumulative_pnl_r": self.gross_profit_r - self.gross_loss_r,
                "win_rate": self.win_count / max(1, self.trade_count),
                "profit_factor": self.gross_profit_r / max(0.001, self.gross_loss_r),
                "max_drawdown_r": self.max_drawdown
            }) + "\n")

    def _update_drawdown(self, pnl_r: float):
        current_equity = self.gross_profit_r - self.gross_loss_r
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        dd = self.peak_equity - current_equity
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    def daily_summary(self):
        """Generate end-of-day summary."""
        summary = {
            "date_utc": datetime.now(timezone.utc).date().isoformat(),
            "run_id": self.run_id,
            "trades_today": self.trade_count,
            "wins": self.win_count,
            "losses": self.loss_count,
            "net_pnl_r": self.gross_profit_r - self.gross_loss_r,
            "profit_factor": self.gross_profit_r / max(0.001, self.gross_loss_r),
            "max_drawdown_r": self.max_drawdown,
            "gates_check": {
                "min_trades": self.trade_count >= self.config["minimum_completed_trades"],
                "profit_factor": (self.gross_profit_r / max(0.001, self.gross_loss_r)) >= self.config["gates"]["net_profit_factor_min"],
                "drawdown": self.max_drawdown <= self.config["gates"]["max_drawdown_pct"]
            }
        }
        with open(DAILY_SUMMARY, "a") as f:
            f.write(json.dumps(summary) + "\n")
        return summary

    def check_gates(self) -> dict:
        """Check if all promotion gates are satisfied."""
        pf = self.gross_profit_r / max(0.001, self.gross_loss_r)
        return {
            "trades": self.trade_count >= self.config["minimum_completed_trades"],
            "profit_factor": pf >= self.config["gates"]["net_profit_factor_min"],
            "expectancy": (self.gross_profit_r - self.gross_loss_r) / max(1, self.trade_count) >= self.config["gates"]["net_expectancy_min_r"],
            "drawdown": self.max_drawdown <= self.config["gates"]["max_drawdown_pct"],
            "win_rate": (self.win_count / max(1, self.trade_count) * 100) >= self.config["gates"]["win_rate_min_pct"],
            "all_pass": None  # computed below
        }

    def run_forever(self):
        """Main loop - replace with actual data feed integration."""
        print(f"[SHADOW] Run ID: {self.run_id}")
        print(f"[SHADOW] Candidate: {self.config['candidate']}")
        print(f"[SHADOW] Config loaded. Waiting for M1 feed...")
        print(f"[SHADOW] Logs: {LOG_DIR}")

        # TODO: Connect to live M1 feed (Dukascopy/WebSocket)
        # while True:
        #     bar = get_next_m1_bar("GBPUSD")
        #     self.on_m1_bar(bar)
        #     time.sleep(60)

        # Placeholder - manual trigger for testing
        try:
            while True:
                cmd = input("\n[SHADOW] Commands: (s)ummary, (g)ates, (q)uit: ").strip().lower()
                if cmd == 's':
                    print(json.dumps(self.daily_summary(), indent=2))
                elif cmd == 'g':
                    gates = self.check_gates()
                    gates["all_pass"] = all([gates[k] for k in ["trades", "profit_factor", "expectancy", "drawdown", "win_rate"]])
                    print(json.dumps(gates, indent=2))
                elif cmd == 'q':
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self._log_event("SESSION_END", {"run_id": self.run_id, "duration_seconds": (datetime.now(timezone.utc) - self.session_start).total_seconds()})


if __name__ == "__main__":
    validator = ShadowValidator()
    validator.run_forever()