"""Authenticated Windows-side MT5 gateway.

The agent owns the MetaTrader 5 terminal and broker credentials. It never
fabricates fills: missing MetaTrader5 or a failed terminal operation is an
explicit error that the Linux control plane must handle.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import hmac
import http.server
import json
import os
import ssl
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


class AgentError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class MT5Gateway:
    """Authenticated gateway around the official MT5 Python API."""

    def __init__(self) -> None:
        self.token = os.environ.get("MT5_AGENT_TOKEN", "")
        self.hmac_secret = os.environ.get("MT5_AGENT_HMAC_SECRET", "")
        self.expected_account = os.environ.get("MT5_AGENT_ACCOUNT_ID", "").strip()
        self.clock_skew = int(os.environ.get("MT5_AGENT_CLOCK_SKEW_SECONDS", "300"))
        self.max_nonces = int(os.environ.get("MT5_AGENT_MAX_NONCES", "10000"))
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self._nonce_lock = threading.Lock()
        self._account_lock = threading.RLock()
        self.connected_account: str | None = None
        if len(self.token) < 24 or len(self.hmac_secret) < 32 or not self.expected_account:
            raise RuntimeError(
                "MT5_AGENT_TOKEN, MT5_AGENT_HMAC_SECRET, and MT5_AGENT_ACCOUNT_ID "
                "must be securely configured"
            )

    def authenticate(self, method: str, headers: dict[str, str], body: bytes, path: str) -> str:
        timestamp = headers.get("X-FP-Timestamp", "")
        nonce = headers.get("X-FP-Nonce", "")
        account = headers.get("X-FP-Account", "")
        try:
            age = abs(time.time() - int(timestamp))
        except ValueError as exc:
            raise AgentError("invalid timestamp") from exc
        if age > self.clock_skew:
            raise AgentError("request timestamp outside allowed clock skew", 401)
        if not nonce or not account:
            raise AgentError("missing request authentication fields", 401)
        if account != self.expected_account:
            raise AgentError("requested account is not served by this agent", 403)
        with self._nonce_lock:
            if nonce in self._nonces:
                raise AgentError("replayed request nonce", 401)
            self._nonces[nonce] = time.time()
            while len(self._nonces) > self.max_nonces:
                self._nonces.popitem(last=False)
        if self.token and headers.get("Authorization", "") != f"Bearer {self.token}":
            raise AgentError("invalid bearer token", 401)
        signed = f"{method.upper()}.{path}.{timestamp}.{nonce}.{account}.".encode("utf-8") + body
        expected = hmac.new(self.hmac_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("X-FP-Signature", ""), expected):
            raise AgentError("invalid request signature", 401)
        return account

    def require_mt5(self) -> None:
        if mt5 is None:
            raise AgentError("MetaTrader5 Python package is not installed", 503)

    def require_connected(self) -> None:
        if self.connected_account is None:
            raise AgentError("agent is not connected", 409)

    def connect(self, account: str) -> dict[str, Any]:
        self.require_mt5()
        with self._account_lock:
            if not mt5.initialize():
                raise AgentError(f"MT5 initialize failed: {mt5.last_error()}", 503)
            info = mt5.account_info()
            if info is None:
                raise AgentError("MT5 account information unavailable", 503)
            if account.isdigit() and str(getattr(info, "login", account)) != str(account):
                raise AgentError("requested account does not match logged-in account", 403)
            self.connected_account = account
            return {"connected": True, "account": account, "login": int(info.login)}

    def account(self) -> dict[str, Any]:
        self.require_mt5(); self.require_connected()
        info = mt5.account_info()
        if info is None:
            raise AgentError("MT5 account information unavailable", 503)
        return {"currency": str(info.currency), "balance": float(info.balance),
                "equity": float(info.equity), "margin": float(info.margin),
                "free_margin": float(info.margin_free),
                "permissions": ["TRADE", "MARKET_DATA", "READ_ONLY"]}

    def positions(self) -> list[dict[str, Any]]:
        self.require_mt5(); self.require_connected()
        positions = mt5.positions_get()
        if positions is None:
            raise AgentError(f"MT5 positions_get failed: {mt5.last_error()}", 503)
        return [{"ticket_id": str(p.ticket), "symbol": p.symbol,
                 "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                 "units": int(p.volume * 100000), "open_price": float(p.price_open),
                 "unrealized_pnl": float(p.profit),
                 "timestamp": datetime.fromtimestamp(p.time, timezone.utc).isoformat()}
                for p in positions]

    def _symbol_info(self, symbol: str):
        info = mt5.symbol_info(symbol)
        if info is None:
            raise AgentError(f"MT5 symbol not found: {symbol}", 404)
        return info

    def order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_mt5(); self.require_connected()
        symbol = str(payload["symbol"])
        side = str(payload["side"]).upper()
        order_type = str(payload.get("type", "MARKET")).upper()
        units = int(payload["units"])
        info = self._symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise AgentError(f"MT5 tick unavailable for {symbol}", 503)
        side_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        request: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL if order_type == "MARKET" else mt5.TRADE_ACTION_PENDING,
            "symbol": symbol, "volume": units / 100000.0, "type": side_type,
            "magic": int(os.environ.get("MT5_AGENT_MAGIC", "20260925")),
            "comment": str(payload.get("client_order_id", payload.get("intent_id", "forex-platform")))[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if order_type == "MARKET":
            request["price"] = float(tick.ask if side == "BUY" else tick.bid)
        else:
            price = payload.get("price")
            if price is None:
                raise AgentError("pending order requires price")
            request["price"] = float(price)
            request["type"] = {
                ("BUY", "LIMIT"): mt5.ORDER_TYPE_BUY_LIMIT,
                ("SELL", "LIMIT"): mt5.ORDER_TYPE_SELL_LIMIT,
                ("BUY", "STOP"): mt5.ORDER_TYPE_BUY_STOP,
                ("SELL", "STOP"): mt5.ORDER_TYPE_SELL_STOP,
            }.get((side, order_type), side_type)
        if payload.get("stop_loss") is not None:
            request["sl"] = float(payload["stop_loss"])
        if payload.get("take_profit") is not None:
            request["tp"] = float(payload["take_profit"])
        request["deviation"] = int(os.environ.get("MT5_AGENT_DEVIATION", "20"))
        if not info.visible or not info.trade_mode:
            raise AgentError(f"MT5 symbol is not tradable: {symbol}", 409)
        result = mt5.order_send(request)
        if result is None:
            raise AgentError(f"MT5 order_send failed: {mt5.last_error()}", 503)
        return {"ticket": int(result.order), "status": "FILLED" if int(result.volume) > 0 else "SUBMITTED",
                "filled_units": int(result.volume * 100000), "fill_price": float(result.price),
                "message": str(result.comment or "accepted by MT5")}

    def cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_mt5(); self.require_connected()
        ticket = int(payload.get("ticket", payload.get("order_id", 0)))
        if ticket <= 0:
            raise AgentError("ticket is required")
        if mt5.order_delete(ticket):
            return {"cancelled": True, "success": True, "ticket": ticket}
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            raise AgentError(f"MT5 cancel/close failed: {mt5.last_error()}", 409)
        position = positions[0]
        tick = mt5.symbol_info_tick(position.symbol)
        if tick is None:
            raise AgentError("MT5 tick unavailable for position close", 503)
        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                                 "volume": position.volume, "type": close_type,
                                 "price": tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask,
                                 "position": ticket, "deviation": 20, "magic": 20260925,
                                 "comment": "forex-platform-close", "type_filling": mt5.ORDER_FILLING_IOC})
        if result is None:
            raise AgentError(f"MT5 close failed: {mt5.last_error()}", 503)
        return {"cancelled": True, "success": True, "ticket": ticket}

    def candles(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        self.require_mt5(); self.require_connected()
        tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
              "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
              "D1": mt5.TIMEFRAME_D1}.get(timeframe.upper())
        if tf is None:
            raise AgentError("unsupported timeframe")
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, max(1, min(count, 5000)))
        if rates is None:
            raise AgentError(f"MT5 candles unavailable: {mt5.last_error()}", 503)
        return [{"timestamp": datetime.fromtimestamp(int(r[0]), timezone.utc).isoformat(),
                 "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                 "close": float(r[4]), "volume": int(r[5]), "spread": int(r[7])} for r in rates]

    def ticks(self, symbol: str, count: int) -> dict[str, Any]:
        self.require_mt5(); self.require_connected()
        ticks = mt5.copy_ticks_from_pos(symbol, 0, max(1, min(count, 100)))
        if ticks is None:
            raise AgentError(f"MT5 ticks unavailable: {mt5.last_error()}", 503)
        return {"ticks": [{"timestamp": datetime.fromtimestamp(int(t[0]), timezone.utc).isoformat(),
                           "bid": float(t[1]), "ask": float(t[2])} for t in ticks]}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ForexMT5Agent/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        try:
            raw_len = int(self.headers.get("Content-Length", "0"))
            if raw_len > 1_048_576:
                raise AgentError("request too large", 413)
            body = self.rfile.read(raw_len) if raw_len else b""
            account = self.gateway.authenticate(method, dict(self.headers.items()), body, self.path)
            payload = json.loads(body) if body else {}
            if payload.get("account_id", account) != account:
                raise AgentError("account mismatch", 403)
            path = urlparse(self.path).path
            if method == "GET" and path == "/v1/health":
                result = {"healthy": mt5 is not None, "connected": self.gateway.connected_account == account}
            elif method == "POST" and path == "/v1/connect":
                result = self.gateway.connect(account)
            elif method == "POST" and path == "/v1/disconnect":
                self.gateway.require_connected()
                self.gateway.connected_account = None
                result = {"connected": False}
            elif method == "GET" and path == "/v1/account":
                result = self.gateway.account()
            elif method == "GET" and path == "/v1/positions":
                result = {"positions": self.gateway.positions()}
            elif method == "POST" and path == "/v1/orders":
                result = self.gateway.order(payload)
            elif method == "POST" and path == "/v1/orders/cancel":
                result = self.gateway.cancel(payload)
            elif method == "GET" and path == "/v1/candles":
                query = parse_qs(urlparse(self.path).query)
                result = {"candles": self.gateway.candles(query.get("symbol", [""])[0], query.get("timeframe", ["M1"])[0], int(query.get("count", ["500"])[0]))}
            elif method == "GET" and path == "/v1/ticks":
                query = parse_qs(urlparse(self.path).query)
                result = self.gateway.ticks(query.get("symbol", [""])[0], int(query.get("count", ["1"])[0]))
            else:
                raise AgentError("not found", 404)
            self._reply(200, result)
        except AgentError as exc:
            self._reply(exc.status, {"error": str(exc)})
        except Exception as exc:
            self._reply(500, {"error": f"agent failure: {type(exc).__name__}"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def main() -> int:
    parser = argparse.ArgumentParser(description="Authenticated MetaTrader 5 gateway")
    parser.add_argument("--host", default=os.environ.get("MT5_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MT5_AGENT_PORT", "8788")))
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    args = parser.parse_args()
    Handler.gateway = MT5Gateway()
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    if args.tls_cert or args.tls_key:
        if not args.tls_cert or not args.tls_key:
            raise SystemExit("both --tls-cert and --tls-key are required")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"MT5 agent listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
