"""
Production control plane for Forex Platform.

This package contains the operational layers that turn the research/strategy
engine into a deployable, auditable, multi-broker trading service:

- validated runtime settings and environment separation
- structured logging, metrics, and health checks
- API-key/password/session authentication with RBAC
- a fail-closed live-trading authorization gate
- durable SQLite WAL persistence and audit trail
- broker registry with paper, remote-MT5, and FIX adapters
- production execution/risk services
- news/fundamentals calendar and event blackout policy
- a governed offline learning/champion-challenger pipeline
- an HTTP API and web dashboard
"""

from __future__ import annotations

__version__ = "1.0.0"
