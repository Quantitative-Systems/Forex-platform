"""
Unit tests for unified Forex Platform CLI subcommands:
- status
- screen-sessions
- inspect-pair
- sweep
- forward-paper
- discover-arb
"""

import sys
from io import StringIO
import pytest

from forex_platform.cli import main


class TestUnifiedCLI:

    def test_cli_status(self, monkeypatch):
        stdout_capture = StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_capture)

        exit_code = main(["status"])
        assert exit_code == 0
        output = stdout_capture.getvalue()
        assert "FOREX PLATFORM — SYSTEM STATUS" in output
        assert "Live Capital Status:      $0.00 LOCKED" in output
        assert "Execution Routing:        FAIL-CLOSED SANDBOX / PAPER ONLY" in output

    def test_cli_screen_sessions(self, monkeypatch):
        stdout_capture = StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_capture)

        exit_code = main(["screen-sessions", "--date", "2026-01-06"])
        assert exit_code == 0
        output = stdout_capture.getvalue()
        assert "FOREX 24-HOUR SESSION SCHEDULE (2026-01-06)" in output
        assert "GLOBAL PEAK OVERLAP" in output
        assert "Weekend Closure:" in output

    def test_cli_inspect_pair(self, monkeypatch):
        stdout_capture = StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_capture)

        exit_code = main(["inspect-pair", "EURUSD"])
        assert exit_code == 0
        output = stdout_capture.getvalue()
        assert "CURRENCY PAIR SPECIFICATION: EURUSD" in output
        assert "1 Pip Value (in USD):     $10.00 USD" in output
        assert "DYNAMIC SPREAD REGIMES:" in output

    def test_cli_inspect_pair_jpy_cross(self, monkeypatch):
        stdout_capture = StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_capture)

        exit_code = main(["inspect-pair", "USDJPY"])
        assert exit_code == 0
        output = stdout_capture.getvalue()
        assert "CURRENCY PAIR SPECIFICATION: USDJPY" in output
        assert "JPY Cross:                True" in output

    def test_cli_sweep(self, monkeypatch):
        stdout_capture = StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_capture)

        exit_code = main(["sweep", "--symbol", "EURUSD", "--bars", "100"])
        assert exit_code == 0
        output = stdout_capture.getvalue()
        assert "DISCOVERY SWEEP:" in output
        assert "G1-G7 QUALIFICATION GATES:" in output

    def test_cli_forward_paper(self, monkeypatch):
        stdout_capture = StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_capture)

        exit_code = main(["forward-paper", "--symbol", "EURUSD", "--ticks", "3", "--db-path", ":memory:"])
        assert exit_code == 0
        output = stdout_capture.getvalue()
        assert "FORWARD PAPER TRADING DAEMON INITIALIZATION" in output
        assert "PAPER TRADING EXECUTION SUMMARY:" in output
        assert "SQLite Records Stored:" in output

    def test_cli_discover_arb(self, monkeypatch):
        stdout_capture = StringIO()
        monkeypatch.setattr(sys, "stdout", stdout_capture)

        exit_code = main(["discover-arb", "--pair-a", "EURUSD", "--pair-b", "GBPUSD", "--pair-c", "EURGBP"])
        assert exit_code == 0
        output = stdout_capture.getvalue()
        assert "TRIANGULAR STATISTICAL ARBITRAGE SCANNER" in output
        assert "Triangular Divergence:" in output
        assert "Parity Regime:" in output
