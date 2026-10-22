"""Single-user local authentication + CSRF/Origin guard (DESIGN §8, §12).

* The server binds to localhost by default.
* ``GET /api/auth/bootstrap`` (loopback clients only) sets an HttpOnly,
  SameSite=Strict session cookie holding a random per-process token.
* Every other ``/api/*`` request must carry that cookie; state-changing requests
  must additionally present an allowed ``Origin`` (or none, e.g. curl/tests) and
  the ``X-Requested-With: DebateStudio`` header (blocks simple cross-site forms).

Implemented as a pure ASGI middleware so long-lived SSE streams are untouched.
"""
from __future__ import annotations

import ipaddress
import json
import secrets
from http.cookies import SimpleCookie

COOKIE = "ds_session"
OPEN_PATHS = {"/api/auth/bootstrap", "/api/health"}


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_host(value: str) -> tuple[str, str]:
    """'127.0.0.1:8000' -> ('127.0.0.1', '8000'); '[::1]:8000' -> ('::1', '8000')."""
    v = (value or "").strip().rstrip("/")
    if v.startswith("["):
        host, _, rest = v[1:].partition("]")
        return host, rest.lstrip(":")
    if v.count(":") == 1:
        host, port = v.split(":")
        return host, port
    return v, ""


class AuthMiddleware:
    def __init__(self, app, token: str, allowed_origins: tuple[str, ...], allow_remote: bool = False):
        self.app = app
        self.token = token
        self.allowed_origins = set(allowed_origins)
        self.allow_remote = allow_remote

    def _origin_ok(self, origin: str, host_hdr: str) -> bool:
        if origin in self.allowed_origins:
            return True
        # same-origin on any port (e.g. --port 8123): http://127.0.0.1:8123 with Host 127.0.0.1:8123
        try:
            scheme, rest = origin.split("://", 1)
        except ValueError:
            return False
        if scheme not in ("http", "https"):
            return False
        o_host, o_port = split_host(rest)
        h_host, h_port = split_host(host_hdr)
        return is_loopback(o_host) and is_loopback(h_host) and o_port == h_port

    async def _deny(self, send, status: int, message: str) -> None:
        body = json.dumps({"detail": {"message": message}}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        client = (scope.get("client") or (None, None))[0]
        if not self.allow_remote and not is_loopback(client):
            return await self._deny(send, 403, "localhost からのみ利用できます")
        host_hdr = headers.get("host", "")
        if not self.allow_remote and not is_loopback(split_host(host_hdr)[0]):
            # DNS rebinding defence: a page on evil.example resolved to 127.0.0.1 sends Host: evil.example
            return await self._deny(send, 403, "Host ヘッダーが localhost ではありません")
        origin = headers.get("origin")
        if origin and not self._origin_ok(origin, host_hdr):
            return await self._deny(send, 403, "許可されていない Origin です")
        if scope["path"] in OPEN_PATHS:
            return await self.app(scope, receive, send)
        jar = SimpleCookie()
        try:
            jar.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001
            pass
        cookie = jar.get(COOKIE).value if jar.get(COOKIE) else None
        if not cookie or not secrets.compare_digest(cookie, self.token):
            return await self._deny(send, 401, "認証が必要です（/api/auth/bootstrap）")
        if scope["method"] not in ("GET", "HEAD", "OPTIONS") and headers.get("x-requested-with") != "DebateStudio":
            return await self._deny(send, 403, "CSRF対策ヘッダーがありません")
        return await self.app(scope, receive, send)
