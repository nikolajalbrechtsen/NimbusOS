# =============================================================================
# nimbus_control/auth.py: who may talk to the Control Center
# =============================================================================
# What: - TokenStore: the per-boot token in /run/nimbus-control/token (0640,
#         group nimbus-control, on tmpfs, so a new one every boot). Made at
#         startup if missing; kept across service restarts.
#       - LaunchCodes: one-time codes for the launcher. A code works once and
#         for 30 seconds; the page trades it for the token, so the token never
#         appears on a command line (docs/PLAN.md NOTE-14).
#       - SecurityMiddleware, in front of everything:
#           Host must be 127.0.0.1:<port> or localhost:<port> (DNS rebinding);
#           Origin, when sent (always for WebSockets), must be that origin;
#           /api/* needs "Authorization: Bearer <token>", except the code
#           exchange; WebSockets offer the subprotocols "nimbus.v1" and
#           "nimbus.token.<token>"; static files are public (no secrets);
#           every HTTP response gets a strict CSP and related headers (its
#           connect-src also allows websockify's port, for VM consoles).
#       - the session routes: launch-code (needs the token) and exchange.
# Why:  spec section 3 (Security) and docs/PLAN.md §4.3. Docker and libvirt
#       access are effectively root, so every call is authenticated.
# =============================================================================

import hmac
import json
import os
import re
import secrets
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .errors import ApiError

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
WS_PROTOCOL = "nimbus.v1"
WS_TOKEN_PREFIX = "nimbus.token."
PUBLIC_EXACT = {"/", "/index.html", "/style.css", "/app.js", "/logo.svg", "/api/session/exchange"}
PUBLIC_PREFIXES = ("/lib/", "/views/", "/fonts/", "/catalog-icons/", "/novnc/")


class TokenStore:
    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, "token")
        self.token = self._load_or_create()

    def _load_or_create(self):
        try:
            with open(self.path, encoding="ascii") as f:
                token = f.read().strip()
            if TOKEN_RE.fullmatch(token):
                return token
        except (FileNotFoundError, UnicodeDecodeError):
            pass
        token = secrets.token_urlsafe(32)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        try:
            os.write(fd, (token + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.path)
        return token

    def check(self, presented):
        return isinstance(presented, str) and hmac.compare_digest(presented.encode(), self.token.encode())


class LaunchCodes:
    TTL = 30.0
    MAX_PENDING = 16

    def __init__(self, clock=time.monotonic):
        self._codes = {}
        self._clock = clock

    def issue(self):
        now = self._clock()
        self._codes = {c: exp for c, exp in self._codes.items() if exp > now}
        while len(self._codes) >= self.MAX_PENDING:
            self._codes.pop(next(iter(self._codes)))
        code = secrets.token_urlsafe(32)
        self._codes[code] = now + self.TTL
        return code

    def redeem(self, code):
        expiry = self._codes.pop(code, None) if isinstance(code, str) else None
        return expiry is not None and expiry > self._clock()


def security_headers(port, ws_ports=()):
    # WebSockets: the backend's own, and websockify's for the VM consoles.
    origins = " ".join(f"ws://{host}:{p}" for p in (port, *ws_ports) for host in ("127.0.0.1", "localhost"))
    csp = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
           f"connect-src 'self' {origins}; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; "
           "form-action 'self'; object-src 'none'")
    return [
        (b"content-security-policy", csp.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"cross-origin-opener-policy", b"same-origin"),
    ]


class SecurityMiddleware:
    def __init__(self, app, tokens, port, ws_ports=()):
        self.app = app
        self.tokens = tokens
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.headers = security_headers(port, ws_ports)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")

        if headers.get("host") not in self.hosts:
            return await self._reject(scope, send, 403, "This address is not allowed. Open the Control Center from the app menu.")
        origin = headers.get("origin")
        if (kind == "websocket" and origin not in self.origins) or (origin is not None and origin not in self.origins):
            return await self._reject(scope, send, 403, "Requests from other web sites are not allowed.")

        if kind == "websocket":
            offered = scope.get("subprotocols") or []
            tokens = [p[len(WS_TOKEN_PREFIX):] for p in offered if p.startswith(WS_TOKEN_PREFIX)]
            if WS_PROTOCOL not in offered or len(tokens) != 1 or not self.tokens.check(tokens[0]):
                return await self._reject(scope, send, 401, "Not signed in.")
            return await self.app(scope, receive, send)

        public = path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES) or not path.startswith("/api/")
        if not public:
            auth = headers.get("authorization", "")
            if not (auth.startswith("Bearer ") and self.tokens.check(auth[7:])):
                return await self._reject(scope, send, 401,
                                          "Not signed in. Open the Control Center again from the app menu.")

        extra = list(self.headers)
        if path.startswith("/api/"):
            extra.append((b"cache-control", b"no-store"))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        return await self.app(scope, receive, send_with_headers)

    async def _reject(self, scope, send, status, message):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4000 + status})
            return
        body = json.dumps({"message": message}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *self.headers]})
        await send({"type": "http.response.body", "body": body})


# --- session routes --------------------------------------------------------------
router = APIRouter()


class ExchangeBody(BaseModel):
    code: str


@router.post("/api/session/launch-code")
async def launch_code(request: Request):
    return {"code": request.app.state.codes.issue()}


@router.post("/api/session/exchange")
async def exchange(body: ExchangeBody, request: Request):
    if not request.app.state.codes.redeem(body.code):
        raise ApiError(403, "This link has expired or was already used. Open the Control Center again from the app menu.")
    return {"token": request.app.state.tokens.token}
