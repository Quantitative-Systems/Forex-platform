"""
HTTP API and web control plane.

Built on the standard library (`ThreadingHTTPServer`) so the platform has no
unavailable framework dependency. Every mutating endpoint is authenticated and
role-gated; every privileged action is written to the audit log.
"""

from __future__ import annotations

import json
import re
import signal
import ssl
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from forex_platform.production.brokers import BrokerError
from forex_platform.production.learning import CandidateStage
from forex_platform.production.observability import get_logger
from forex_platform.production.runtime import TradingPlatform
from forex_platform.production.security import AuthError, AuthorizationError, LiveTradingDenied

logger = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "web"

JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class ApiHandler(BaseHTTPRequestHandler):
    """Request handler for the production control plane."""

    server_version = "ForexPlatform/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def platform(self) -> TradingPlatform:
        return self.server.platform  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("HTTP %s - %s", self.address_string(), format % args)

    def _headers_common(self) -> Dict[str, str]:
        headers = dict(SECURITY_HEADERS)
        origins = self.platform.settings.cors_allow_origins
        origin = self.headers.get("Origin", "")
        if origins and origin in origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-API-Key, X-Request-Id"
            )
            headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return headers

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        for key, value in {**JSON_HEADERS, **self._headers_common()}.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._send_json(status, {"error": message, **extra})

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        if length > self.platform.settings.max_request_body_bytes:
            raise ValueError("Request body too large.")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object.")
        return payload

    def _request_id(self) -> str:
        return self.headers.get("X-Request-Id") or f"req-{uuid.uuid4().hex[:16]}"

    def _client_ip(self) -> str:
        if self.platform.settings.trust_proxy_headers:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _authenticate(self, required_role: str = "viewer") -> Optional[Any]:
        try:
            principal = self.platform.auth.authenticate(self.headers)
            self.platform.auth.require(principal, required_role)
            return principal
        except AuthError as exc:
            self._error(401, str(exc))
        except AuthorizationError as exc:
            self._error(403, str(exc))
        return None

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        for key, value in self._headers_common().items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        start = time.monotonic()
        request_id = self._request_id()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._dispatch(method, path, query, request_id)
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak stack traces to clients
            logger.exception("Unhandled API error on %s %s: %s", method, path, exc)
            self._error(500, "Internal server error", request_id=request_id)
        finally:
            self.platform.metrics.observe(
                "forex_http_request_seconds",
                time.monotonic() - start,
                {"method": method, "path": path},
            )
            self.platform.metrics.increment(
                "forex_http_requests_total", labels={"method": method, "path": path}
            )

    def _dispatch(
        self, method: str, path: str, query: Dict[str, List[str]], request_id: str
    ) -> None:
        if method == "GET" and path in ("/", "/index.html"):
            return self._serve_web("index.html", "text/html; charset=utf-8")
        if method == "GET" and path == "/healthz":
            return self._send_json(200, {"status": "ok"})
        if method == "GET" and path == "/readyz":
            snapshot = self.platform.readiness()
            return self._send_json(200 if snapshot.get("ready") else 503, snapshot)
        if method == "GET" and path == "/metrics":
            body = self.platform.metrics.render_prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            for key, value in self._headers_common().items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "POST" and path == "/api/v1/auth/token":
            return self._handle_token(request_id)
        if path.startswith("/static/"):
            filename = path[len("/static/") :]
            return self._serve_web(filename, self._content_type(filename))

        role = self._required_role(method, path)
        principal = self._authenticate(role)
        if principal is None:
            return
        identity = principal.actor or self._client_ip()
        if not self.platform.rate_limiter.allow(identity):
            return self._error(429, "Rate limit exceeded.")

        if self._dispatch_get(method, path, query, principal, request_id):
            return
        if self._dispatch_post(method, path, principal, request_id):
            return
        self._error(404, f"No route for {method} {path}")

    @staticmethod
    def _required_role(method: str, path: str) -> str:
        if method == "POST" and path == "/api/v1/orders":
            return "trader"
        if path.startswith("/api/v1/keys") or path.startswith("/api/v1/audit"):
            return "admin"
        if path.startswith("/api/v1/learning/promote") or path.startswith(
            "/api/v1/learning/rollback"
        ):
            return "admin"
        if path.startswith("/api/v1/risk/"):
            return "operator"
        if re.fullmatch(r"/api/v1/accounts/[^/]+/(enable|connect|disconnect)", path):
            return "operator"
        if method == "POST" and path.endswith("/cancel"):
            return "trader"
        return "viewer"

    def _dispatch_get(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        principal: Any,
        request_id: str,
    ) -> bool:
        if method != "GET":
            return False
        if path == "/api/v1/status":
            self._send_json(200, self.platform.status())
            return True
        if path == "/api/v1/accounts":
            self._send_json(200, {"accounts": self.platform.accounts_repo.list()})
            return True
        if path == "/api/v1/positions":
            self._send_json(200, self.platform.positions())
            return True
        if path == "/api/v1/orders":
            limit = int(query.get("limit", ["100"])[0])
            status_filter = query.get("status", [None])[0]
            self._send_json(
                200, {"orders": self.platform.orders_repo.list(limit=limit, status=status_filter)}
            )
            return True
        if path == "/api/v1/fills":
            self._send_json(200, {"fills": self.platform.orders_repo.list_fills(limit=200)})
            return True
        if path == "/api/v1/risk":
            self._send_json(200, self.platform.risk.snapshot())
            return True
        if path == "/api/v1/news":
            hours = int(query.get("hours", ["24"])[0])
            self._send_json(
                200,
                {
                    "status": self.platform.news.status(),
                    "upcoming": self.platform.news.upcoming(hours=hours),
                    "fundamentals": self.platform.fundamentals.snapshot(),
                },
            )
            return True
        if path == "/api/v1/learning":
            self._send_json(200, self.platform.learning.status())
            return True
        if path == "/api/v1/audit":
            limit = int(query.get("limit", ["100"])[0])
            self._send_json(200, {"entries": self.platform.audit.list(limit=limit)})
            return True
        if path == "/api/v1/keys":
            self._send_json(200, {"keys": self.platform.auth.list_api_keys()})
            return True
        quote_match = re.fullmatch(r"/api/v1/quotes/([A-Za-z0-9]+)", path)
        if quote_match:
            mode = query.get("mode", [None])[0]
            quote = self.platform.market_data.quote(quote_match.group(1), mode)
            if quote is None:
                self._error(503, "No live quote available for this symbol/mode.")
            else:
                self._send_json(200, quote)
            return True
        return False

    def _dispatch_post(
        self, method: str, path: str, principal: Any, request_id: str
    ) -> bool:
        if method not in ("POST", "DELETE"):
            return False
        if method == "POST" and path == "/api/v1/orders":
            payload = self._read_json()
            try:
                result = self.platform.submit_order(payload)
            except (ValueError, KeyError) as exc:
                self._error(400, str(exc))
                return True
            self.platform.audit.record(
                actor=principal.actor,
                role=principal.role,
                action="api.orders.submit",
                target=result.intent_id,
                details={"approved": result.approved, "reason": result.reason},
                ip=self._client_ip(),
                request_id=request_id,
            )
            self._send_json(
                200 if result.approved else 422, result.model_dump(mode="json")
            )
            return True
        if method == "POST" and path == "/api/v1/risk/kill-switch":
            payload = self._read_json()
            action = str(payload.get("action", "arm")).lower()
            scope = str(payload.get("scope", "global"))
            target = str(payload.get("target", ""))
            if action == "arm":
                self.platform.arm_kill_switch(str(payload.get("reason", "API")), scope, target)
            elif action == "disarm":
                self.platform.disarm_kill_switch(scope, target)
            else:
                self._error(400, "action must be 'arm' or 'disarm'.")
                return True
            self._send_json(200, {"ok": True, "action": action, "scope": scope})
            return True
        if method == "POST" and path == "/api/v1/risk/flatten":
            payload = self._read_json()
            result = self.platform.flatten_all(
                reason=str(payload.get("reason", "API flatten")), mode=payload.get("mode")
            )
            self._send_json(200, result)
            return True
        if method == "POST" and path == "/api/v1/learning/promote":
            payload = self._read_json()
            try:
                candidate = self.platform.learning.promote(
                    str(payload["candidate_id"]),
                    CandidateStage(str(payload["to_stage"])),
                    approved_by=principal.actor,
                    notes=str(payload.get("notes", "")),
                )
            except (KeyError, ValueError, PermissionError) as exc:
                self._error(400, str(exc))
                return True
            self._send_json(200, candidate)
            return True
        if method == "POST" and path == "/api/v1/learning/rollback":
            payload = self._read_json()
            restored = self.platform.learning.rollback(
                str(payload["strategy_id"]), principal.actor, str(payload.get("notes", ""))
            )
            if restored is None:
                self._error(404, "No champion history to roll back to.")
            else:
                self._send_json(200, {"restored": restored})
            return True
        if method == "POST" and path == "/api/v1/keys":
            payload = self._read_json()
            try:
                api_key = self.platform.auth.register_api_key(
                    str(payload.get("name", "api-key")), str(payload.get("role", "viewer"))
                )
            except ValueError as exc:
                self._error(400, str(exc))
                return True
            self.platform.audit.record(
                actor=principal.actor,
                role=principal.role,
                action="api.keys.create",
                target=str(payload.get("name", "")),
                details={"role": payload.get("role", "viewer")},
                ip=self._client_ip(),
                request_id=request_id,
            )
            self._send_json(
                201,
                {"api_key": api_key, "note": "Store this key now; it cannot be shown again."},
            )
            return True
        if method == "DELETE" and path.startswith("/api/v1/keys/"):
            key_id = path.rsplit("/", 1)[-1]
            if not key_id.isdigit():
                self._error(400, "Invalid key id.")
                return True
            revoked = self.platform.auth.revoke_api_key(int(key_id))
            self.platform.audit.record(
                actor=principal.actor,
                role=principal.role,
                action="api.keys.revoke",
                target=key_id,
                ip=self._client_ip(),
                request_id=request_id,
            )
            self._send_json(200, {"revoked": revoked})
            return True
        account_match = re.fullmatch(
            r"/api/v1/accounts/([^/]+)/(enable|connect|disconnect)", path
        )
        if method == "POST" and account_match:
            account_id, action = account_match.group(1), account_match.group(2)
            if action == "enable":
                payload = self._read_json()
                self.platform.enable_account(
                    account_id, bool(payload.get("enabled", True)), principal.actor
                )
                self._send_json(200, {"ok": True, "account_id": account_id})
                return True
            if action == "connect":
                try:
                    connected = self.platform.brokers.connect(account_id)
                except (BrokerError, LiveTradingDenied) as exc:
                    self._error(400, str(exc))
                    return True
                self._send_json(200, {"connected": connected, "account_id": account_id})
                return True
            self.platform.brokers.disconnect(account_id)
            self._send_json(200, {"disconnected": True, "account_id": account_id})
            return True
        cancel_match = re.fullmatch(r"/api/v1/orders/([^/]+)/cancel", path)
        if method == "POST" and cancel_match:
            payload = self._read_json()
            account_id = str(payload.get("account_id", ""))
            if not account_id:
                self._error(400, "account_id is required.")
                return True
            cancelled = self.platform.execution.cancel(cancel_match.group(1), account_id)
            self._send_json(200, {"cancelled": cancelled})
            return True
        if method == "POST" and path == "/api/v1/auth/logout":
            authorization = self.headers.get("Authorization", "")
            if authorization.lower().startswith("bearer "):
                self.platform.auth.revoke_session(authorization[7:].strip())
            self._send_json(200, {"logged_out": True})
            return True
        return False

    @staticmethod
    def _content_type(filename: str) -> str:
        if filename.endswith(".css"):
            return "text/css; charset=utf-8"
        if filename.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if filename.endswith(".svg"):
            return "image/svg+xml"
        return "text/plain; charset=utf-8"

    def _serve_web(self, filename: str, content_type: str) -> None:
        candidate = (WEB_DIR / filename).resolve()
        if not str(candidate).startswith(str(WEB_DIR.resolve())) or not candidate.exists():
            self._error(404, "Not found")
            return
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        for key, value in self._headers_common().items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_token(self, request_id: str) -> None:
        if not self.platform.rate_limiter.allow(f"token:{self._client_ip()}"):
            self._error(429, "Rate limit exceeded.")
            return
        payload = self._read_json()
        api_key = str(payload.get("api_key", "")).strip()
        if not api_key:
            self._error(400, "api_key is required.")
            return
        try:
            token, expires_at = self.platform.auth.issue_session(api_key)
        except AuthError as exc:
            self._error(401, str(exc))
            return
        principal = self.platform.auth.authenticate({"Authorization": f"Bearer {token}"})
        self.platform.audit.record(
            actor=principal.actor,
            role=principal.role,
            action="api.auth.token_issued",
            ip=self._client_ip(),
            request_id=request_id,
        )
        self._send_json(
            200,
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_at": expires_at.isoformat(),
                "role": principal.role,
            },
        )


def build_server(platform: TradingPlatform) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((platform.settings.host, platform.settings.port), ApiHandler)
    server.platform = platform  # type: ignore[attr-defined]
    if platform.settings.tls_cert_file and platform.settings.tls_key_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(platform.settings.tls_cert_file, platform.settings.tls_key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve_forever(platform: TradingPlatform, start_platform: bool = True) -> None:
    """Run the control plane until SIGINT/SIGTERM or an unrecoverable server error."""
    if start_platform:
        platform.start()
    server = build_server(platform)

    def request_shutdown(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down", signum)
        # shutdown() must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, name="fp-http-shutdown", daemon=True).start()

    previous_handlers: Dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    scheme = "https" if platform.settings.tls_cert_file else "http"
    logger.info(
        "Control plane listening on %s://%s:%d",
        scheme,
        platform.settings.host,
        platform.settings.port,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        server.server_close()
        if start_platform:
            platform.stop()
