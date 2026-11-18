"""Streamable-HTTP entrypoint for formr-mcp: one server for every researcher
of a formr instance, who adds it to their AI app (Claude, Claude Code,
ChatGPT, Codex, VS Code) by its URL.

formr is the OAuth authorization server (formr's OAuthConnectors): a client
without a token gets a 401 naming this server's protected resource metadata
(/.well-known/oauth-protected-resource/mcp), which names formr's admin origin
as the issuer; the researcher signs in on formr and chooses the scopes and
runs the connection gets. Every request then carries that connection's
token: FormrTokenVerifier asks formr whose it is (GET /api/v1/user/token),
and the tools call formr's API with that same token, so formr's scopes, run
allowlist and run ownership decide what each call may do. This server holds
no credential, no secret and no files, and keeps no session state
(stateless_http): any replica can answer any request.

Environment:
  FORMR_BASE_URL   formr's admin origin (the issuer), e.g. https://formr.example.org
  MCP_PUBLIC_URL   this server's URL as researchers add it, e.g. https://formr.example.org/mcp
                   (formr's FORMR_MCP_URL must be the same)
  MCP_HTTP_HOST / MCP_HTTP_PORT / MCP_LOG_LEVEL
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

# Triggers server.py's load_dotenv() + builds the FastMCP instance (`mcp`).
import server
from formr_mcp.auth import FormrTokenVerifier, FormrUnavailable


class HealthzMiddleware:
    """Answers /healthz without a token (the container's healthcheck); passes
    everything else, `lifespan` included, to the app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _auth_error(conn, exc):
    """formr not answering the token check is a 503 to retry, not a bad token."""
    if isinstance(exc, FormrUnavailable):
        return JSONResponse({"error": "temporarily_unavailable", "error_description": str(exc)},
                            status_code=503, headers={"Retry-After": "5"})
    return PlainTextResponse(str(exc), status_code=400)


def build_app():
    base_url = os.environ.get("FORMR_BASE_URL", "").strip().rstrip("/")
    public_url = os.environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/")
    if not base_url or not public_url:
        raise SystemExit("FORMR_BASE_URL and MCP_PUBLIC_URL must be set.")
    mcp_path = urlparse(public_url).path
    if not mcp_path or mcp_path == "/":
        # the server answers at a path of its own, and the URL researchers add
        # (formr's mcp_url) is that path: without one they POST to / and get a 404
        raise SystemExit("MCP_PUBLIC_URL must name the MCP's path, e.g. https://formr.example.org/mcp.")

    # the tools call formr at the same address the tokens are checked with
    server.BASE_URL = base_url
    server.configure_http_mode()
    mcp = server.mcp
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.streamable_http_path = mcp_path
    # required_scopes stays None: the SDK enforces it on every request, and
    # each tool's scope is formr's to check (whoami lists them)
    mcp.settings.auth = AuthSettings(issuer_url=base_url, resource_server_url=public_url)
    verifier = FormrTokenVerifier(base_url, server.shared_http(), resource=public_url)
    # FastMCP takes a verifier only in its constructor (with auth), and server.mcp
    # is built at import for stdio too: set it where 1.27's streamable_http_app() reads it
    mcp._token_verifier = verifier

    # DNS-rebinding protection defaults to localhost-only; behind a reverse
    # proxy the public Host header must be allowlisted or every request 421s.
    host = urlparse(public_url).hostname or ""
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, f"{host}:*"],
        allowed_origins=[f"https://{host}", f"https://{host}:*"],
    )
    app = mcp.streamable_http_app()
    for middleware in _require_token_check(app, verifier, mcp_path):
        middleware.kwargs["on_error"] = _auth_error
    return HealthzMiddleware(app)


def _require_token_check(app, verifier, path):
    """The SDK's middleware that checks every token with `verifier`, and `path`
    behind its 401: an SDK that no longer reads FastMCP._token_verifier would
    serve /mcp to anyone; it does not start instead."""
    auth = [m for m in app.user_middleware if m.cls is AuthenticationMiddleware
            and getattr(m.kwargs.get("backend"), "token_verifier", None) is verifier]
    guarded = any(getattr(r, "path", None) == path
                  and isinstance(getattr(r, "endpoint", None), RequireAuthMiddleware)
                  for r in app.routes)
    if not auth or not guarded:
        raise SystemExit("formr-mcp: this mcp SDK did not install formr's token check "
                         "(FastMCP._token_verifier); not serving /mcp without it.")
    return auth


def main() -> None:
    uvicorn.run(
        build_app(),
        host=os.environ.get("MCP_HTTP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_HTTP_PORT", "8000")),
        log_level=os.environ.get("MCP_LOG_LEVEL", "info"),
        # behind Traefik: the client's address and scheme from its headers
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("MCP_FORWARDED_ALLOW_IPS", "*"),
    )


if __name__ == "__main__":
    main()
