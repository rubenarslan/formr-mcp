"""Streamable-HTTP entrypoint for formr-mcp: one server for every researcher
of a formr instance, who adds it to Claude by its URL.

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
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

# Triggers server.py's load_dotenv() + builds the FastMCP instance (`mcp`).
import server
from formr_mcp.auth import FormrTokenVerifier


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


def build_app():
    base_url = os.environ.get("FORMR_BASE_URL", "").strip().rstrip("/")
    public_url = os.environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/")
    if not base_url or not public_url:
        raise SystemExit("FORMR_BASE_URL and MCP_PUBLIC_URL must be set.")
    mcp_path = urlparse(public_url).path or "/mcp"

    server.configure_http_mode()
    mcp = server.mcp
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.streamable_http_path = mcp_path
    # required_scopes stays None: the SDK enforces it on every request, and
    # each tool's scope is formr's to check (whoami lists them)
    mcp.settings.auth = AuthSettings(issuer_url=base_url, resource_server_url=public_url)
    mcp._token_verifier = FormrTokenVerifier(base_url, server.shared_http(), resource=public_url)

    # DNS-rebinding protection defaults to localhost-only; behind a reverse
    # proxy the public Host header must be allowlisted or every request 421s.
    host = urlparse(public_url).hostname or ""
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, f"{host}:*"],
        allowed_origins=[f"https://{host}", f"https://{host}:*"],
    )
    return HealthzMiddleware(mcp.streamable_http_app())


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
