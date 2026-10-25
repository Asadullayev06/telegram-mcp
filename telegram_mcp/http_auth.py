"""Bearer-token auth for the HTTP MCP transports (streamable-http and SSE).

When MCP_AUTH_TOKEN is set, every request must carry
``Authorization: Bearer <token>`` except the public ``/healthz`` probe and
CORS preflight. When the token is unset the server runs unauthenticated and
logs a warning — fine for stdio or a trusted private network, risky on a
public URL.
"""

from __future__ import annotations

import hmac
import os
import sys

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route


def _log(msg: str) -> None:
    print(f"[auth] {msg}", file=sys.stderr, flush=True)


class BearerAuthMiddleware:
    """ASGI middleware enforcing a static bearer token."""

    def __init__(self, app, token: str, exempt_paths=frozenset()):
        self.app = app
        self.expected = f"Bearer {token}"
        self.exempt_paths = set(exempt_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "OPTIONS" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        if not hmac.compare_digest(provided, self.expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _health(_request):
    return PlainTextResponse("ok")


def install_http_auth(app, *, endpoint_label: str):
    """Add a public /healthz route and, if configured, bearer auth."""
    app.router.routes.append(Route("/healthz", endpoint=_health, methods=["GET"]))

    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if token:
        app.add_middleware(
            BearerAuthMiddleware,
            token=token,
            exempt_paths={"/healthz"},
        )
        _log(f"Bearer auth ENABLED for {endpoint_label} (/healthz stays public).")
    else:
        _log(
            f"WARNING: MCP_AUTH_TOKEN not set — {endpoint_label} is UNAUTHENTICATED. "
            "Set MCP_AUTH_TOKEN to require a token."
        )
    return app
