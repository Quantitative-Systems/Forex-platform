"""
ACID-Compliant SQLite Paper Trading Persistence Ledger.
Configured with Write-Ahead Logging (WAL) mode for high-throughput concurrency
and crash recovery without state divergence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import threading
from typing import Dict, List, Optional, Tuple

from forex_platform.core.domain import (
    CurrencyPair,
    ExecutionOrder,
    Fill,
    LotSize,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    to_decimal,
)
from forex_platform.risk_engine.firewall import RiskDecision


class SQLitePaperLedger:
    """
    ACID persistence layer for orders, fills, positions, and equity curves.
    """

    def __init__(self, db_path: str | Path = "data/paper_ledger.db"):
        self.is_memory = str(db_path) == ":memory:"
        if self.is_memory:
            self.db_path = Path(":memory:")
            self._mem_conn: Optional[sqlite3.Connection] = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._mem_conn = None
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self.is_memory and self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    intent_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    limit_price TEXT,
                    stop_loss TEXT,
                    take_profit TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    fill_price TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    commission TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    liquidity_flag TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    average_entry_price TEXT NOT NULL,
                    current_price TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    unrealized_pnl TEXT NOT NULL,
                    total_commission TEXT NOT NULL,
                    total_swap TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_open INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    balance TEXT NOT NULL,
                    equity TEXT NOT NULL,
                    unrealized_pnl TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS risk_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    tier_failed INTEGER,
                    reason TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
            """)

    def save_order(self, order: ExecutionOrder) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, intent_id, symbol, side, order_type, units,
                    limit_price, stop_loss, take_profit, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    order.order_id,
                    order.intent_id,
                    order.symbol,
                    order.side.value,
                    order.order_type.value,
                    order.lot_size.units,
                    str(order.limit_price) if order.limit_price is not None else None,
                    str(order.stop_loss) if order.stop_loss is not None else None,
                    str(order.take_profit) if order.take_profit is not None else None,
                    order.status.value,
                    order.created_at.isoformat(),
                    order.updated_at.isoformat(),
                ),
            )

    def save_fill(self, fill: Fill) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fills (
                    fill_id, order_id, symbol, side, fill_price,
                    units, commission, timestamp, liquidity_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.symbol,
                    fill.side.value,
                    str(fill.fill_price),
                    fill.units,
                    str(fill.commission),
                    fill.timestamp.isoformat(),
                    fill.liquidity_flag,
                ),
            )

    def save_position(self, pos: Position) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO positions (
                    position_id, symbol, side, units, average_entry_price, current_price,
                    realized_pnl, unrealized_pnl, total_commission, total_swap,
                    opened_at, updated_at, is_open
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.position_id,
                    pos.symbol,
                    pos.side.value,
                    pos.units,
                    str(pos.average_entry_price),
                    str(pos.current_price),
                    str(pos.realized_pnl),
                    str(pos.unrealized_pnl),
                    str(pos.total_commission),
                    str(pos.total_swap),
                    pos.opened_at.isoformat(),
                    pos.updated_at.isoformat(),
                    1 if pos.is_open else 0,
                ),
            )

    def save_equity_snapshot(
        self,
        timestamp: datetime,
        balance: Decimal,
        equity: Decimal,
        unrealized_pnl: Decimal,
        realized_pnl: Decimal,
    ) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO equity_snapshots (timestamp, balance, equity, unrealized_pnl, realized_pnl)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    str(balance),
                    str(equity),
                    str(unrealized_pnl),
                    str(realized_pnl),
                ),
            )

    def save_risk_decision(self, intent_id: str, decision: RiskDecision, timestamp: datetime) -> None:
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO risk_decisions (intent_id, approved, tier_failed, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    1 if decision.approved else 0,
                    decision.tier_failed,
                    decision.reason,
                    timestamp.isoformat(),
                ),
            )

    def recover_state(self) -> Tuple[List[ExecutionOrder], List[Position], Decimal]:
        """
        Crash recovery: reconstructs active orders, open positions, and latest balance.
        """
        with self._lock, self._get_connection() as conn:
            # 1. Recover active orders (PENDING or SUBMITTED)
            order_rows = conn.execute(
                "SELECT * FROM orders WHERE status IN ('PENDING', 'SUBMITTED', 'PARTIALLY_FILLED')"
            ).fetchall()
            active_orders: List[ExecutionOrder] = []
            for r in order_rows:
                ord_ = ExecutionOrder(
                    order_id=r["order_id"],
                    intent_id=r["intent_id"],
                    symbol=r["symbol"],
                    side=OrderSide(r["side"]),
                    order_type=OrderType(r["order_type"]),
                    lot_size=LotSize.from_units(r["units"]),
                    limit_price=to_decimal(r["limit_price"]) if r["limit_price"] else None,
                    stop_loss=to_decimal(r["stop_loss"]) if r["stop_loss"] else None,
                    take_profit=to_decimal(r["take_profit"]) if r["take_profit"] else None,
                    status=OrderStatus(r["status"]),
                    created_at=datetime.fromisoformat(r["created_at"]),
                    updated_at=datetime.fromisoformat(r["updated_at"]),
                    remaining_units=r["units"],
                )
                active_orders.append(ord_)

            # 2. Recover open positions
            pos_rows = conn.execute("SELECT * FROM positions WHERE is_open = 1").fetchall()
            open_positions: List[Position] = []
            for r in pos_rows:
                p = Position(
                    position_id=r["position_id"],
                    symbol=r["symbol"],
                    side=OrderSide(r["side"]),
                    units=r["units"],
                    average_entry_price=to_decimal(r["average_entry_price"]),
                    current_price=to_decimal(r["current_price"]),
                    realized_pnl=to_decimal(r["realized_pnl"]),
                    unrealized_pnl=to_decimal(r["unrealized_pnl"]),
                    total_commission=to_decimal(r["total_commission"]),
                    total_swap=to_decimal(r["total_swap"]),
                    opened_at=datetime.fromisoformat(r["opened_at"]),
                    updated_at=datetime.fromisoformat(r["updated_at"]),
                    is_open=True,
                )
                open_positions.append(p)

            # 3. Recover latest balance
            snap = conn.execute("SELECT balance FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            recovered_balance = to_decimal(snap["balance"]) if snap else Decimal("100000.00")

            return active_orders, open_positions, recovered_balance
