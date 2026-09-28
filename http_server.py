"""Streamable-HTTP entrypoint for formr-mcp with a static bearer-token gate.

Serves the same FastMCP tool surface as `server.py` (stdio), but over HTTP at
`/mcp`, so the server can be reached remotely (behind a reverse proxy / TLS).
Every request except `/healthz` must carry `Authorization: Bearer <token>`,
where <token> == $MCP_BEARER_TOKEN. This is deliberately a single shared
secret — coarse but sufficient as the network edge gate; the formr API itself
is still protected by the per-credential OAuth scopes in the bind-mounted .env.

Importing `server` runs its load_dotenv(), so $MCP_BEARER_TOKEN and the
FORMR_* credentials are read from the same .env file.
"""
from __future__ import annotations

import hmac
import os
from urllib.parse import urlparse

import uvicorn
from starlette.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

# Triggers server.py's load_dotenv() + builds the FastMCP instance (`mcp`).
from server import mcp


class BearerAuthMiddleware:
    """Pure-ASGI guard. Passes through non-HTTP scopes (notably `lifespan`, so
    the wrapped app's session-manager + FormrClient lifespan still runs)."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}"
        self._enabled = bool(token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == "/healthz":
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        if not self._enabled or not hmac.compare_digest(provided, self._expected):
            await JSONResponse(
                {"error": "unauthorized", "message": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        await self.app(scope, receive, send)


def main() -> None:
    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if not token:
        raise SystemExit("MCP_BEARER_TOKEN is not set — refusing to start an unauthenticated server.")

    host = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_HTTP_PORT", "8000"))

    # DNS-rebinding protection defaults to localhost-only; behind a reverse
    # proxy the public Host header must be allowlisted or every request 421s
    # with "Invalid Host header". Default to the FORMR_BASE_URL host (the /mcp
    # endpoint lives on the same admin domain).
    allowed_host = os.environ.get("MCP_ALLOWED_HOST", "").strip()
    if not allowed_host:
        allowed_host = urlparse(os.environ.get("FORMR_BASE_URL", "")).hostname or ""
    if allowed_host:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[allowed_host, f"{allowed_host}:*"],
            allowed_origins=[f"https://{allowed_host}", f"https://{allowed_host}:*"],
        )

    app = mcp.streamable_http_app()  # mounts the MCP endpoint at settings.streamable_http_path (/mcp)
    uvicorn.run(
        BearerAuthMiddleware(app, token),
        host=host,
        port=port,
        log_level=os.environ.get("MCP_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
