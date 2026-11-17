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
from formr_mcp.auth import FormrTokenVerifier, FormrUnavailable
from formr_mcp.client import FormrAnswerLost, FormrAuthExpired, FormrClient, FormrClientError, FormrConflict

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

    @pytest.mark.parametrize("down", [httpx.ConnectError("down"), httpx.Response(503), httpx.Response(404)])
    def test_formr_not_answering_is_no_bad_token(self, down):
        # a 401 would send the client to sign in again (a new connection)
        def handler(request):
            if isinstance(down, Exception):
                raise down
            return down
        verifier = FormrTokenVerifier(BASE, _formr(handler), resource=MCP_URL)
        with pytest.raises(FormrUnavailable):
            _run(verifier.verify_token(TOKEN))

    def test_while_formr_is_down_its_last_answer_stands(self):
        up = [True]

        def handler(request):
            if not up[0]:
                raise httpx.ConnectError("down")
            return httpx.Response(200, json=_token_info())
        verifier = FormrTokenVerifier(BASE, _formr(handler), resource=MCP_URL, ttl=0)
        access = _run(verifier.verify_token(TOKEN))
        up[0] = False
        assert _run(verifier.verify_token(TOKEN)) == access

    def test_while_formr_hangs_one_request_waits_not_every_one(self):
        calls = []
        up = [True]

        def handler(request):
            calls.append(1)
            if not up[0]:
                raise httpx.ReadTimeout("hangs")
            return httpx.Response(200, json=_token_info())
        verifier = FormrTokenVerifier(BASE, _formr(handler), resource=MCP_URL, ttl=60)
        access = _run(verifier.verify_token(TOKEN))
        verifier._cache[verifier._key(TOKEN)] = (0, access)  # the minute is over
        up[0] = False
        assert _run(verifier.verify_token(TOKEN)) == access
        assert _run(verifier.verify_token(TOKEN)) == access
        assert len(calls) == 2, "the stale answer stands for another minute"

    @pytest.mark.parametrize("response", [
        httpx.Response(200, json=None),
        httpx.Response(200, json=[1]),
        httpx.Response(200, content=json.dumps(_token_info()).replace("4102444800", "1e400").encode(),
                       headers={"content-type": "application/json"}),
    ])
    def test_an_answer_that_is_no_token_information_is_no_access(self, response):
        verifier = FormrTokenVerifier(BASE, _formr(lambda r: response), resource=MCP_URL)
        assert _run(verifier.verify_token(TOKEN)) is None

    def test_a_token_that_cannot_be_sent_is_no_access(self):
        verifier = FormrTokenVerifier(BASE, _formr(lambda r: httpx.Response(200, json=_token_info())),
                                      resource=MCP_URL)
        assert _run(verifier.verify_token("ab\xe9")) is None

    def test_a_token_formr_refused_since_is_checked_again(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=_token_info())
        verifier = FormrTokenVerifier(BASE, _formr(handler), resource=MCP_URL)
        _run(verifier.verify_token(TOKEN))
        verifier.forget(TOKEN)
        _run(verifier.verify_token(TOKEN))
        assert len(calls) == 2

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
        with pytest.raises(FormrAuthExpired, match="gets a new token by itself"):
            _run(client.get_runs())
        assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen)
        assert not any(r.url.path.endswith("access_token") for r in seen)

    def test_a_refused_token_is_reported_back(self):
        refused = []
        client = FormrClient(BASE, http_client=_formr(lambda r: httpx.Response(401, json={})),
                             access_token=TOKEN, on_token_refused=lambda: refused.append(1))
        with pytest.raises(FormrAuthExpired):
            _run(client.get_runs())
        assert refused == [1]

    def test_run_ids_without_names_are_runs_still(self):
        def handler(request):
            return httpx.Response(200, json=_token_info(allowed_runs=[7, 9], email=None))
        client = FormrClient(BASE, http_client=_formr(handler), access_token=TOKEN)
        caps = _run(client.get_capabilities())
        assert caps["allowed_runs"] == [{"id": 7, "name": "run #7"}, {"id": 9, "name": "run #9"}]
        assert caps["email"] is None


def test_the_shared_client_keeps_no_cookie(monkeypatch):
    # formr's session cookie, kept, would put every researcher in one PHP session
    monkeypatch.setattr(server_mod, "_shared_http", None)
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(401, headers={"set-cookie": "formr_session=abc; path=/; secure; HttpOnly"})
    shared = server_mod.shared_http()
    monkeypatch.setattr(shared, "_transport", httpx.MockTransport(handler))
    _run(shared.get(BASE + "/api/v1/user/token"))
    _run(shared.get(BASE + "/api/v1/user/token"))
    assert seen == [None, None]


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

        async def put(name, structure, confirm_removal=False):
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

    def test_an_edit_that_changes_nothing_uploads_nothing(self, http_mode):
        # an upload costs a round trip and, on an older formr, every participant's step
        c = self._client(http_mode)
        out = _run(server_mod.renormalize_positions("demo", ctx=MagicMock()))
        assert "already clean" in out and "nothing was uploaded" in out
        c.put_run_structure.assert_not_awaited()

    def test_parallel_edits_of_a_run_both_land(self, http_mode):
        c = self._client(http_mode)
        get = c.get_run_structure.side_effect

        async def slow_get(name):
            # what formr had when asked, answered a little later
            structure = await get(name)
            await asyncio.sleep(0.01)
            return structure
        c.get_run_structure.side_effect = slow_get

        async def both():
            return await asyncio.gather(
                server_mod.add_run_unit("demo", "Pause", 3, unit_fields={"wait_minutes": 1}, ctx=MagicMock()),
                server_mod.add_run_unit("demo", "Pause", 6, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))
        _run(both())
        stored = _run(c.get_run_structure("demo"))
        assert sorted(u["position"] for u in stored["units"]) == [3, 6, 10]

    def test_a_removal_is_confirmed_only_when_asked(self, http_mode):
        c = self._client(http_mode, {"name": "demo", "units": [
            {"type": "Pause", "position": 5, "wait_minutes": 1, "run_unit_id": 7},
            {"type": "Pause", "position": 6, "wait_minutes": 1, "run_unit_id": 9},
            {"type": "Endpage", "position": 10, "body": "Bye", "run_unit_id": 8}]})
        _run(server_mod.add_run_unit("demo", "Pause", 3, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))
        assert c.put_run_structure.await_args.kwargs["confirm_removal"] is False
        # as formr's editor asks first: the user hears of participants before they are moved on
        _run(server_mod.remove_run_unit("demo", 5, ctx=MagicMock()))
        assert c.put_run_structure.await_args.kwargs["confirm_removal"] is False
        sent = c.put_run_structure.await_args.args[1]
        assert [u.get("run_unit_id") for u in sent["units"]] == [None, 9, 8], "the kept units keep their ids"
        _run(server_mod.remove_run_unit("demo", 3, confirm_removal=True, ctx=MagicMock()))
        assert c.put_run_structure.await_args.kwargs["confirm_removal"] is True

    def test_overwriting_a_unit_with_one_of_its_type_keeps_it(self, http_mode):
        c = self._client(http_mode, {"name": "demo", "units": [
            {"type": "Pause", "position": 5, "wait_minutes": 1, "run_unit_id": 7},
            {"type": "Endpage", "position": 10, "body": "Bye", "run_unit_id": 8}]})
        _run(server_mod.add_run_unit("demo", "Pause", 5, insert_mode="overwrite",
                                     unit_fields={"wait_minutes": 120}, ctx=MagicMock()))
        pause = c.put_run_structure.await_args.args[1]["units"][0]
        assert (pause["run_unit_id"], pause["wait_minutes"]) == (7, 120)
        _run(server_mod.add_run_unit("demo", "Page", 10, insert_mode="overwrite",
                                     unit_fields={"body": "Hi"}, ctx=MagicMock()))
        assert c.put_run_structure.await_args.args[1]["units"][1]["run_unit_id"] == 8, "a Page is an Endpage"
        _run(server_mod.add_run_unit("demo", "External", 5, insert_mode="overwrite",
                                     unit_fields={"address": "https://example.org"}, ctx=MagicMock()))
        assert "run_unit_id" not in c.put_run_structure.await_args.args[1]["units"][0], "another type is a new unit"

    def test_a_removal_formr_refuses_names_the_units_and_asks_first(self, http_mode):
        c = self._client(http_mode)
        c.put_run_structure.side_effect = FormrConflict("PUT ... -> 409", {
            "message": "...", "units_in_use": [{"run_unit_id": 8, "position": 10, "type": "Page", "participants": 3}]})
        upload = lambda: _run(server_mod.update_run_structure("demo", json.dumps({"units": [
            {"type": "Pause", "position": 5, "wait_minutes": 1}]}), ctx=MagicMock()))
        with pytest.raises(ValueError) as refused:
            upload()
        assert "nothing was uploaded" in str(refused.value)
        assert "Page at position 10;" in str(refused.value) or str(refused.value).count("Page at position 10") == 1
        assert "participant(s)" not in str(refused.value), "how many: real participants' data, under test_only"
        assert "only with their go-ahead, upload again with confirm_removal=true" in str(refused.value)
        http_mode.setenv("FORMR_DATA_ACCESS", "all")
        with pytest.raises(ValueError) as refused:
            upload()
        assert "Page at position 10 (3 participant(s))" in str(refused.value)

    def test_an_upload_made_from_an_old_copy_says_fetch_again(self, http_mode):
        c = self._client(http_mode)
        c.put_run_structure.side_effect = FormrConflict("PUT ... -> 409", {"message": "...", "version": "abc"})
        with pytest.raises(ValueError, match="units or settings have changed in formr since it was fetched") as stale:
            _run(server_mod.update_run_structure("demo", json.dumps({"version": "old", "units": [
                {"type": "Pause", "position": 5, "wait_minutes": 1}]}), ctx=MagicMock()))
        assert "update_run_settings" in str(stale.value) and "Fetch it again" in str(stale.value)
        # a unit tool fetches by itself: the run changed while it ran
        with pytest.raises(ValueError, match="changed while this tool ran: call the tool again"):
            _run(server_mod.add_run_unit("demo", "Pause", 5, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))

    def test_a_change_formr_applied_is_not_reported_as_failed(self, http_mode):
        c = self._client(http_mode)
        c.get_run_structure.side_effect = [json.loads(json.dumps(self.STRUCTURE)),
                                           FormrClientError("GET ... timed out")]
        with pytest.raises(ValueError, match="formr applied the upload .* Do not repeat the change"):
            _run(server_mod.add_run_unit("demo", "Pause", 5, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))

    def test_the_answer_says_what_formr_did(self, monkeypatch):
        answer = {"units_imported": 3, "kept": 2, "moved": 1, "created": 1,
                  "removed": 0, "participants_moved": 4, "steps_ended": 0}
        monkeypatch.setenv("FORMR_DATA_ACCESS", "all")
        merged = server_mod._merge_summary(answer)
        assert merged.startswith("Kept 2 unit(s) in place (1 moved), created 1, removed 0.")
        assert "4 participant(s) moved along with their unit" in merged
        monkeypatch.delenv("FORMR_DATA_ACCESS")
        assert "Participants moved along with their unit" in server_mod._merge_summary(answer)
        assert "replaced the whole structure" in server_mod._merge_summary({"units_imported": 3})
        assert "replaced the whole structure" in server_mod._merge_summary(None)

    def test_formrs_warnings_reach_the_model(self, http_mode):
        c = self._client(http_mode)
        put = c.put_run_structure.side_effect

        async def put_warning(name, structure, confirm_removal=False):
            await put(name, structure, confirm_removal)
            return {"units_imported": 2, "kept": 1, "created": 1, "warnings": "The expiry date cannot be set to a past date."}
        c.put_run_structure.side_effect = put_warning
        out = _run(server_mod.add_run_unit("demo", "Pause", 5, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))
        assert "formr says: The expiry date cannot be set to a past date." in out

    def test_the_edit_locks_keep_no_names(self):
        import gc
        lock = server_mod._run_edit_lock("some-run")
        assert server_mod._run_edit_lock("SOME-run") is lock
        del lock
        gc.collect()
        assert "some-run" not in server_mod._RUN_EDIT_LOCKS
        with pytest.raises(ValueError):
            server_mod._run_edit_lock("../x")

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
    # build_app sets it: put back after (the integration tests use it)
    monkeypatch.setattr(server_mod, "BASE_URL", server_mod.BASE_URL)
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

    def test_formr_down_is_a_503_not_a_sign_in(self, app, monkeypatch):
        client, _ = app

        def down(request):
            raise httpx.ConnectError("down")
        monkeypatch.setattr(server_mod.shared_http(), "_transport", httpx.MockTransport(down))
        r = _call(client, "list_runs", {}, token="never-seen")
        assert r.status_code == 503
        assert "resource_metadata" not in r.headers.get("www-authenticate", "")

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
        tools = {t["name"]: t for t in r.json()["result"]["tools"]}
        for name in ("get_run_structure", "update_run_structure", "get_pattern", "add_run_unit"):
            assert "_from_file" not in tools[name]["description"] and ".formr/" not in tools[name]["description"], name
        for name in ("add_run_unit", "remove_run_unit", "renormalize_positions", "update_run_structure"):
            assert tools[name]["annotations"]["destructiveHint"] is True, name


class TestOverwritesAndLostAnswers:
    """An overwrite that formr keeps in place still replaces the unit; a Form and
    a Survey of one study are one unit to formr; an upload whose answer is lost
    may have landed; another researcher's calls don't hold up a run's owner."""

    STRUCTURE = TestNoWorkspace.STRUCTURE
    _client = TestNoWorkspace._client

    def test_an_overwrite_replaces_the_unit_it_keeps(self, http_mode):
        c = self._client(http_mode, {"name": "demo", "units": [
            {"type": "Pause", "position": 5, "description": "wait", "body": "Come back tomorrow",
             "wait_minutes": 1440, "wait_until_time": None, "wait_until_date": None,
             "relative_to": "diary$created", "run_unit_id": 7},
            {"type": "Endpage", "position": 10, "body": "Bye", "run_unit_id": 8}]})
        _run(server_mod.add_run_unit("demo", "Pause", 5, insert_mode="overwrite",
                                     unit_fields={"wait_until_time": "10:00"}, ctx=MagicMock()))
        pause = c.put_run_structure.await_args.args[1]["units"][0]
        # formr keeps what an upload leaves out of a unit it keeps: the old wait and text go
        assert pause["run_unit_id"] == 7
        assert (pause["wait_until_time"], pause["wait_minutes"], pause["relative_to"], pause["body"]) == \
            ("10:00", None, None, "")

    def test_a_form_overwritten_as_a_survey_of_its_study_stays(self, http_mode):
        c = self._client(http_mode, {"name": "demo", "units": [
            {"type": "Form", "position": 5, "description": "diary", "run_unit_id": 7},
            {"type": "Endpage", "position": 10, "body": "Bye", "run_unit_id": 8}]})
        _run(server_mod.add_run_unit("demo", "Survey", 5, insert_mode="overwrite",
                                     unit_fields={"study_id": 30}, ctx=MagicMock()))
        survey = c.put_run_structure.await_args.args[1]["units"][0]
        assert survey["run_unit_id"] == 7, "formr decides by the study whether it is the same placement"
        assert "body" not in survey

    def test_an_overwrite_can_confirm_a_removal(self, http_mode):
        c = self._client(http_mode)
        _run(server_mod.add_run_unit("demo", "Pause", 10, insert_mode="overwrite",
                                     unit_fields={"wait_minutes": 1}, confirm_removal=True, ctx=MagicMock()))
        assert c.put_run_structure.await_args.kwargs["confirm_removal"] is True

    @pytest.mark.parametrize("lost", [FormrAnswerLost("PUT api/v1/runs/demo/structure: formr's answer was lost"),
                                      FormrClientError("PUT api/v1/runs/demo/structure -> 502: Bad Gateway", status=502)])
    def test_an_upload_whose_answer_is_lost_may_have_landed(self, http_mode, lost):
        c = self._client(http_mode)
        put = c.put_run_structure.side_effect

        async def put_then_lose(name, structure, confirm_removal=False):
            await put(name, structure, confirm_removal)
            raise lost
        c.put_run_structure.side_effect = put_then_lose
        with pytest.raises(ValueError, match="may have applied it"):
            _run(server_mod.add_run_unit("demo", "Pause", 5, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))

    def test_a_refusal_is_passed_on_as_it_is(self, http_mode):
        c = self._client(http_mode)
        c.put_run_structure.side_effect = FormrClientError("PUT ... -> 400: Import failed", status=400)
        with pytest.raises(FormrClientError, match="400"):
            _run(server_mod.add_run_unit("demo", "Pause", 5, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))

    def test_a_read_back_that_fails_says_not_to_repeat_the_change(self, http_mode):
        c = self._client(http_mode)
        get = c.get_run_structure.side_effect
        calls = []

        async def get_once(name):
            calls.append(name)
            if len(calls) > 1:
                raise httpx.ConnectError("Connection refused")
            return await get(name)
        c.get_run_structure.side_effect = get_once
        with pytest.raises(ValueError, match="Do not repeat"):
            _run(server_mod.add_run_unit("demo", "Pause", 5, unit_fields={"wait_minutes": 1}, ctx=MagicMock()))

    def test_a_refused_removal_names_the_surveys_saved_all_the_same(self, http_mode):
        c = self._client(http_mode)
        c.put_run_structure.side_effect = FormrConflict("PUT ... -> 409", {
            "message": "...", "units_in_use": [{"run_unit_id": 8, "position": 10, "type": "Page", "participants": 3}],
            "surveys_saved": ["diary"]})
        with pytest.raises(ValueError) as refused:
            _run(server_mod.update_run_structure("demo", json.dumps({"units": [
                {"type": "Pause", "position": 5, "wait_minutes": 1}]}), ctx=MagicMock()))
        assert "diary" in str(refused.value) and "nothing was uploaded" not in str(refused.value)

    def test_edit_locks_are_each_accounts_own(self):
        mine = server_mod._run_edit_lock("some-run", "5")
        assert server_mod._run_edit_lock("SOME-run", "5") is mine
        assert server_mod._run_edit_lock("some-run", "6") is not mine, "another researcher's calls don't wait for it"
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            user=SimpleNamespace(access_token=SimpleNamespace(subject="5")))))
        assert server_mod._caller_account(ctx) == "5"
        assert server_mod._caller_account(None) == ""

    def test_a_dropped_connection_is_a_lost_answer(self):
        def handler(request):
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        client = FormrClient(BASE, http_client=_formr(handler), access_token=TOKEN)
        with pytest.raises(FormrAnswerLost):
            _run(client.put_run_structure("demo", {"units": []}))


class TestHostedToolsSayWhatTheyDo:
    """whoami's map and the hosted server's tool descriptions match what each
    tool does there; analyze_run keeps the event loop free."""
    STRUCTURE = TestNoWorkspace.STRUCTURE
    _client = TestNoWorkspace._client

    def test_a_settings_update_needs_run_read_too(self, monkeypatch):
        monkeypatch.setattr(server_mod, "HTTP_MODE", True)
        report = server_mod._build_capability_report({"scopes": ["run:write"]}, ["run:write"])
        assert report["tool_access"]["update_run_settings"].startswith("blocked: requires 'run:read'")
        monkeypatch.setattr(server_mod.gate, "data_access_mode", lambda: server_mod.gate.TEST_ONLY)
        report = server_mod._build_capability_report({"scopes": ["data:read"]}, ["data:read"])
        assert "session:read" in report["tool_access"]["get_run_results"]

    def test_a_saved_setting_is_no_failure_without_run_read(self, monkeypatch):
        c = AsyncMock()
        c.get_run.side_effect = server_mod.FormrPermissionError("GET ... 403")
        monkeypatch.setattr(server_mod, "_client", lambda ctx: c)
        out = _run(server_mod.update_run_settings("demo", {"title": "New"}, ctx=MagicMock()))
        c.patch_run.assert_awaited_once()
        assert out["updated"] == ["title"] and "Saved" in out["note"]

    def test_hosted_descriptions_offer_no_workspace(self, monkeypatch):
        tools = dict(server_mod.mcp._tool_manager._tools)
        instructions = server_mod.mcp._mcp_server.instructions
        monkeypatch.setattr(server_mod, "HTTP_MODE", False)
        try:
            server_mod.configure_http_mode()
            hosted = server_mod.mcp._tool_manager._tools
            for name in ("upload_run_file", "fetch_google_sheet", "summarize_run", "remove_run_unit",
                         "duplicate_run_units", "shift_run_positions"):
                description = hosted[name].description
                assert "workspace" not in description and ".formr" not in description and "local" not in description, name
            assert "path" not in hosted["upload_run_file"].parameters["properties"]
            assert "to_file" not in hosted["fetch_google_sheet"].parameters["properties"]
            assert hosted["fetch_google_sheet"].parameters["properties"]["format"]["enum"] == ["csv"]
            assert "from the workspace" not in server_mod.mcp._mcp_server.instructions
        finally:
            server_mod.mcp._tool_manager._tools.clear()
            server_mod.mcp._tool_manager._tools.update(tools)
            server_mod.mcp._mcp_server.instructions = instructions

    def test_analyze_run_keeps_the_event_loop_free(self, http_mode):
        import time
        self._client(http_mode)
        seen = []

        def slow_analysis(name):
            # R's syntax check, one process per expression, in a thread now
            seen.append(utils.load_structure(name)["units"][0]["type"])
            time.sleep(0.3)
            return "report"
        http_mode.setattr(server_mod, "run_analysis", slow_analysis)

        async def both():
            # another researcher's call, already going while this one analyses
            ticks = []
            done = asyncio.Event()

            async def heartbeat():
                while True:
                    ticks.append(time.monotonic())
                    if done.is_set():
                        return
                    await asyncio.sleep(0.01)
            beat = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.03)
            report = await server_mod.analyze_run("demo", ctx=MagicMock())
            done.set()
            await beat
            return report, ticks
        report, ticks = _run(both())
        assert report == "report" and seen == ["Endpage"], "the thread sees the structure fetched for the call"
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.2, "no 0.3 s stall"
