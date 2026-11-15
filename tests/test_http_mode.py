"""The hosted (HTTP) mode: formr's tokens checked by FormrTokenVerifier, a
client per request with the caller's token, no workspace, the SDK's 401 and
resource metadata, and a tool call end to end through the ASGI app."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_mod
from formr_mcp import utils
from formr_mcp.auth import FormrTokenVerifier
from formr_mcp.client import FormrAuthExpired, FormrClient

BASE = "https://formr.example.org"
MCP_URL = BASE + "/mcp"
TOKEN = "connection-token"


def _run(coro):
    return asyncio.run(coro)


def _token_info(**changes):
    info = {"active": True, "account_id": 5, "email": "r@example.org", "client_id": "c0nnection",
            "label": "Claude Code (claude.ai) · 2026-11-20 10:00",
            "connector_client_id": "https://claude.ai/oauth/claude-code-client-metadata",
            "scopes": ["run:read", "run:write"], "expires": 4102444800, "allowed_runs": None}
    info.update(changes)
    return info


def _formr(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestTokenVerifier:
    def test_a_connections_token_becomes_an_access_token_and_is_cached(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=_token_info())
        verifier = FormrTokenVerifier(BASE, _formr(handler), resource=MCP_URL)
        access = _run(verifier.verify_token(TOKEN))
        assert access.client_id == "c0nnection" and access.subject == "5"
        assert access.scopes == ["run:read", "run:write"] and access.resource == MCP_URL
        assert seen[0].url.path == "/api/v1/user/token"
        assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
        assert _run(verifier.verify_token(TOKEN)) == access
        assert len(seen) == 1, "the second check comes from the cache"

    @pytest.mark.parametrize("response", [
        httpx.Response(401, json={"error": "invalid_token"}),
        httpx.Response(200, json=_token_info(connector_client_id=None)),
        httpx.Response(200, text="not json"),
    ])
    def test_refused_unless_formr_says_it_is_a_connection(self, response):
        verifier = FormrTokenVerifier(BASE, _formr(lambda r: response), resource=MCP_URL)
        assert _run(verifier.verify_token(TOKEN)) is None

    def test_formr_unreachable_is_no_access(self):
        def handler(request):
            raise httpx.ConnectError("down")
        verifier = FormrTokenVerifier(BASE, _formr(handler), resource=MCP_URL)
        assert _run(verifier.verify_token(TOKEN)) is None

    def test_never_cached_past_the_tokens_expiry(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=_token_info(expires=1))
        verifier = FormrTokenVerifier(BASE, _formr(handler), resource=MCP_URL)
        _run(verifier.verify_token(TOKEN))
        _run(verifier.verify_token(TOKEN))
        assert len(calls) == 2


class TestBearerClient:
    def test_uses_the_callers_token_and_never_refreshes_it(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.url.path.endswith("/user/token"):
                return httpx.Response(200, json=_token_info())
            return httpx.Response(401, json={})
        client = FormrClient(BASE, http_client=_formr(handler), access_token=TOKEN)
        assert _run(client.scopes()) == ["run:read", "run:write"]
        assert _run(client.get_capabilities())["connection"].startswith("Claude Code")
        with pytest.raises(FormrAuthExpired, match="Reconnect"):
            _run(client.get_runs())
        assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen)
        assert not any(r.url.path.endswith("access_token") for r in seen)

    def test_run_ids_without_names_are_runs_still(self):
        def handler(request):
            return httpx.Response(200, json=_token_info(allowed_runs=[7, 9], email=None))
        client = FormrClient(BASE, http_client=_formr(handler), access_token=TOKEN)
        caps = _run(client.get_capabilities())
        assert caps["allowed_runs"] == [{"id": 7, "name": "run #7"}, {"id": 9, "name": "run #9"}]
        assert caps["email"] is None


@pytest.fixture
def http_mode(monkeypatch):
    monkeypatch.setattr(server_mod, "HTTP_MODE", True)
    return monkeypatch


class TestNoWorkspace:
    STRUCTURE = {"name": "demo", "units": [{"type": "Endpage", "position": 10, "description": "end", "body": "Bye"}]}

    def _client(self, monkeypatch, structure=None):
        c = AsyncMock()
        stored = {"demo": json.loads(json.dumps(structure or self.STRUCTURE))}

        async def get(name):
            return json.loads(json.dumps(stored[name]))

        async def put(name, structure):
            stored[name] = json.loads(json.dumps(structure))
        c.get_run_structure.side_effect = get
        c.put_run_structure.side_effect = put
        monkeypatch.setattr(server_mod, "_client", lambda ctx: c)
        return c

    def test_read_tools_get_the_run_from_formr(self, http_mode, tmp_path):
        http_mode.setattr(utils, "WORKSPACE_DIR", tmp_path)
        c = self._client(http_mode)
        out = _run(server_mod.summarize_run("demo", ctx=MagicMock()))
        assert "Endpage" in out
        c.get_run_structure.assert_awaited_with("demo")
        assert not list(tmp_path.iterdir()), "nothing is written"

    def test_an_edit_is_validated_and_uploaded(self, http_mode, tmp_path):
        http_mode.setattr(utils, "WORKSPACE_DIR", tmp_path)
        c = self._client(http_mode)
        out = _run(server_mod.add_run_unit("demo", "Pause", 5, description="wait",
                                           unit_fields={"wait_minutes": 60}, ctx=MagicMock()))
        sent = c.put_run_structure.await_args.args[1]
        assert [u["type"] for u in sent["units"]] == ["Pause", "Endpage"]
        assert "successfully updated" in out
        assert not list(tmp_path.iterdir())
        assert utils.MEMORY_STRUCTURES.get() is None, "nothing kept after the call"

    def test_a_refused_edit_uploads_nothing(self, http_mode):
        c = self._client(http_mode)
        with pytest.raises(Exception):
            _run(server_mod.remove_run_unit("demo", 999, ctx=MagicMock()))
        c.put_run_structure.assert_not_awaited()

    def test_no_server_paths_and_no_files(self, http_mode):
        self._client(http_mode)
        with pytest.raises(ValueError, match="no workspace"):
            _run(server_mod.upload_run_file("demo", path=".env", ctx=MagicMock()))
        with pytest.raises(ValueError, match="no workspace"):
            _run(server_mod.fetch_google_sheet("https://docs.google.com/spreadsheets/d/x/edit", to_file=True))

    def test_a_request_without_a_connection_is_refused(self, http_mode):
        ctx = MagicMock()
        ctx.request_context.request = SimpleNamespace(user=None)
        with pytest.raises(FormrAuthExpired):
            server_mod._client(ctx)

    def test_the_client_is_the_requests_own(self, http_mode):
        ctx = MagicMock()
        ctx.request_context.request = SimpleNamespace(user=SimpleNamespace(access_token=SimpleNamespace(token="t1")))
        client = server_mod._client(ctx)
        assert _run(client._ensure_token()) == "t1"


@pytest.fixture
def app(monkeypatch):
    """http_server's app, against a stand-in formr; the module-level server put back after."""
    import http_server
    from starlette.testclient import TestClient

    mcp = server_mod.mcp
    saved = (dict(mcp._tool_manager._tools), mcp._mcp_server.instructions, mcp.settings.model_copy(),
             mcp._token_verifier, mcp._session_manager, server_mod.HTTP_MODE, server_mod._shared_http)
    seen = []

    def formr(request):
        seen.append(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "invalid_token"})
        if request.url.path == "/api/v1/user/token":
            return httpx.Response(200, json=_token_info())
        if request.url.path == "/api/v1/runs":
            return httpx.Response(200, json=[{"name": "demo"}])
        return httpx.Response(404, json={})

    monkeypatch.setenv("FORMR_BASE_URL", BASE)
    monkeypatch.setenv("MCP_PUBLIC_URL", MCP_URL)
    server_mod._shared_http = _formr(formr)
    mcp._session_manager = None
    try:
        with TestClient(http_server.build_app(), base_url="https://formr.example.org") as client:
            yield client, seen
    finally:
        tools, instructions, settings, verifier, manager, mode, shared = saved
        mcp._tool_manager._tools = tools
        mcp._mcp_server.instructions = instructions
        mcp.settings = settings
        mcp._token_verifier = verifier
        mcp._session_manager = manager
        server_mod.HTTP_MODE = mode
        server_mod._shared_http = shared


def _call(client, tool, arguments, token=TOKEN):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    return client.post("/mcp", headers=headers, json=body)


class TestHostedServer:
    def test_without_a_token_the_client_is_sent_to_sign_in(self, app):
        client, _ = app
        r = _call(client, "list_runs", {}, token=None)
        assert r.status_code == 401
        assert 'resource_metadata="https://formr.example.org/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
        meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == MCP_URL
        assert meta["authorization_servers"] == [BASE + "/"], "formr's issuer, with the slash formr's metadata names it with"
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_a_token_formr_refuses_is_a_401(self, app):
        client, _ = app
        assert _call(client, "list_runs", {}, token="stolen").status_code == 401

    def test_a_tool_call_uses_the_callers_own_token(self, app):
        client, seen = app
        r = _call(client, "list_runs", {})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        assert not result.get("isError"), result
        assert "demo" in json.dumps(result)
        assert [q.url.path for q in seen] == ["/api/v1/user/token", "/api/v1/runs"]

    def test_the_workspace_tools_are_not_offered(self, app):
        client, _ = app
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "Authorization": f"Bearer {TOKEN}"}
        r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "update_run_structure" in names and "add_run_unit" in names
        assert not names & {"get_run_structure_to_file", "update_run_structure_from_file", "open_flowchart"}
