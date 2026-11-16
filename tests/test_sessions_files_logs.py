"""Test sessions, run files and logs: the tools' guards and the requests the
client sends."""
import asyncio
import base64
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_mod
from formr_mcp import data_access as gate
from formr_mcp import utils
from formr_mcp.client import FormrClient, FormrClientError


def _with_client(monkeypatch, client):
    monkeypatch.setattr(server_mod, "_client", lambda ctx: client)
    return client


def _run(coro):
    return asyncio.run(coro)


class TestCreateTestSessions:
    def test_random_codes_are_test_sessions_with_links(self, monkeypatch):
        c = _with_client(monkeypatch, AsyncMock())
        c.create_sessions.side_effect = [{"sessions": ["a" * 64]}, {"sessions": ["b" * 64]}]
        c.get_run.return_value = {"link": "https://study.example.org/demo/"}
        out = _run(server_mod.create_test_sessions("demo", count=2, ctx=MagicMock()))
        for call in c.create_sessions.await_args_list:
            assert call.kwargs["testing"] is True
        assert out["created"][1] == {
            "code": "b" * 64, "test_link": "https://study.example.org/demo/?code=" + "b" * 64}

    def test_given_codes_go_in_one_request_and_errors_come_back(self, monkeypatch):
        c = _with_client(monkeypatch, AsyncMock())
        c.create_sessions.return_value = {"sessions": ["x" * 64],
                                          "errors": [{"code": "bad", "reason": "format"}]}
        c.get_run.return_value = {"link": None}
        out = _run(server_mod.create_test_sessions("demo", codes=["x" * 64, "bad"], ctx=MagicMock()))
        c.create_sessions.assert_awaited_once_with("demo", codes=["x" * 64, "bad"], testing=True)
        assert out["created"] == [{"code": "x" * 64, "test_link": None}]
        assert out["errors"][0]["code"] == "bad"

    @pytest.mark.parametrize("count", [0, 51])
    def test_count_is_bounded(self, monkeypatch, count):
        _with_client(monkeypatch, AsyncMock())
        with pytest.raises(ValueError):
            _run(server_mod.create_test_sessions("demo", count=count, ctx=MagicMock()))


class TestSessionAction:
    def test_refuses_a_real_participant_even_with_full_data_access(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "all")
        c = _with_client(monkeypatch, AsyncMock())
        c.get_session.return_value = {"session": "r", "testing": 0}
        with pytest.raises(gate.DataAccessError, match="not a test session"):
            _run(server_mod.test_session_action("demo", "r", "advance", ctx=MagicMock()))
        c.session_action.assert_not_awaited()

    def test_advances_a_test_session_and_returns_its_state(self, monkeypatch):
        c = _with_client(monkeypatch, AsyncMock())
        c.get_session.side_effect = [{"testing": 1, "position": 10}, {"testing": 1, "position": 20}]
        c.session_action.return_value = []
        out = _run(server_mod.test_session_action("demo", "t", "advance", position=99, ctx=MagicMock()))
        c.session_action.assert_awaited_once_with("demo", "t", "advance", position=None)
        assert out["session"]["position"] == 20

    def test_move_needs_a_position(self, monkeypatch):
        _with_client(monkeypatch, AsyncMock())
        with pytest.raises(ValueError, match="position"):
            _run(server_mod.test_session_action("demo", "t", "move_to_position", ctx=MagicMock()))


class TestUploadRunFile:
    def _client(self, monkeypatch):
        c = _with_client(monkeypatch, AsyncMock())
        c.upload_run_file.return_value = {"file": "logo.png", "messages": []}
        c.get_run_files.return_value = [{"name": "logo.png", "url": "https://x/logo.png"}]
        return c

    def test_reads_a_workspace_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(utils, "WORKSPACE_DIR", tmp_path)
        (tmp_path / "logo.png").write_bytes(b"\x89PNG")
        c = self._client(monkeypatch)
        out = _run(server_mod.upload_run_file("demo", path="logo.png", ctx=MagicMock()))
        c.upload_run_file.assert_awaited_once_with("demo", "logo.png", b"\x89PNG", "image/png")
        assert out["entry"]["url"] == "https://x/logo.png"

    def test_refuses_files_outside_the_workspace(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        secret = tmp_path / ".env"
        secret.write_text("FORMR_CLIENT_SECRET=x")
        (ws / "link.txt").symlink_to(secret)
        monkeypatch.setattr(utils, "WORKSPACE_DIR", ws)
        c = self._client(monkeypatch)
        for p in (str(secret), "../.env", "link.txt"):
            with pytest.raises(ValueError, match="outside the MCP workspace"):
                _run(server_mod.upload_run_file("demo", path=p, ctx=MagicMock()))
        c.upload_run_file.assert_not_awaited()

    def test_text_and_base64(self, monkeypatch):
        c = self._client(monkeypatch)
        _run(server_mod.upload_run_file("demo", filename="s.css", text="body{}", ctx=MagicMock()))
        assert c.upload_run_file.await_args.args[1:] == ("s.css", b"body{}", "text/css")
        _run(server_mod.upload_run_file("demo", filename="d.bin",
                                        content_base64=base64.b64encode(b"\0\1").decode(),
                                        ctx=MagicMock()))
        assert c.upload_run_file.await_args.args[2] == b"\0\1"

    def test_exactly_one_source_and_a_name(self, monkeypatch):
        self._client(monkeypatch)
        with pytest.raises(ValueError, match="exactly one"):
            _run(server_mod.upload_run_file("demo", ctx=MagicMock()))
        with pytest.raises(ValueError, match="exactly one"):
            _run(server_mod.upload_run_file("demo", text="a", content_base64="YQ==",
                                            filename="a", ctx=MagicMock()))
        with pytest.raises(ValueError, match="filename"):
            _run(server_mod.upload_run_file("demo", text="a", ctx=MagicMock()))
        with pytest.raises(ValueError, match="base64"):
            _run(server_mod.upload_run_file("demo", filename="a", content_base64="!!",
                                            ctx=MagicMock()))


class TestGetRunLogs:
    def test_unit_log_asks_for_result_log_test_only_and_keeps_rows_with_a_log(self, monkeypatch):
        monkeypatch.delenv("FORMR_DATA_ACCESS", raising=False)
        c = _with_client(monkeypatch, AsyncMock())
        c.get_unit_sessions.return_value = [
            {"testing": True, "result_log": "Error in R"},
            {"testing": True, "result_log": None},
            {"testing": False, "result_log": "leaked"},
        ]
        out = _run(server_mod.get_run_logs("demo", ctx=MagicMock()))
        kw = c.get_unit_sessions.await_args.kwargs
        assert kw["include_log"] is True and kw["testing"] is True
        assert [r["result_log"] for r in out["rows"]] == ["Error in R"]

    def test_unit_log_on_an_older_formr_says_so(self, monkeypatch):
        c = _with_client(monkeypatch, AsyncMock())
        c.get_unit_sessions.return_value = [{"testing": True, "result": "ended"}]
        out = _run(server_mod.get_run_logs("demo", ctx=MagicMock()))
        assert "result_log" in out["note"] and out["count"] == 1

    def test_real_data_refused_under_test_only(self, monkeypatch):
        monkeypatch.delenv("FORMR_DATA_ACCESS", raising=False)
        _with_client(monkeypatch, AsyncMock())
        with pytest.raises(gate.DataAccessError):
            _run(server_mod.get_run_logs("demo", kind="email", testing=False, ctx=MagicMock()))

    def test_email_log_passes_filters_and_all_mode_passes_real_rows(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "all")
        c = _with_client(monkeypatch, AsyncMock())
        c.get_logs.return_value = [{"testing": False, "to": "p@example.org"}]
        out = _run(server_mod.get_run_logs("demo", kind="email", session="abc",
                                           since="2026-11-01", ctx=MagicMock()))
        assert c.get_logs.await_args.args == ("demo", "email")
        assert c.get_logs.await_args.kwargs["session"] == "abc"
        assert c.get_logs.await_args.kwargs["testing"] is None
        assert out["count"] == 1

    def test_missing_endpoint_is_explained(self, monkeypatch):
        c = _with_client(monkeypatch, AsyncMock())
        c.get_logs.side_effect = FormrClientError("GET api/v1/runs/demo/logs/push -> 404: {}")
        with pytest.raises(FormrClientError, match="no push log endpoint"):
            _run(server_mod.get_run_logs("demo", kind="push", ctx=MagicMock()))


class TestClientRequests:
    def _client(self, monkeypatch, seen):
        def handler(request):
            seen.append(request)
            return httpx.Response(201, json={"ok": True})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = FormrClient("https://x.example.org", "cid", "secret", http_client=http)

        async def fake_token():
            return "tok"
        monkeypatch.setattr(client, "_ensure_token", fake_token)
        return client

    def test_upload_is_multipart_field_file(self, monkeypatch):
        seen = []
        client = self._client(monkeypatch, seen)
        _run(client.upload_run_file("demo", "a.css", b"body{}", "text/css"))
        req = seen[0]
        assert req.method == "POST" and req.url.path == "/api/v1/runs/demo/files"
        body = req.read()
        assert b'name="file"; filename="a.css"' in body and b"body{}" in body

    def test_sessions_actions_and_logs_paths(self, monkeypatch):
        seen = []
        client = self._client(monkeypatch, seen)
        _run(client.create_sessions("demo", testing=True))
        _run(client.session_action("demo", "c0de", "move_to_position", position=30))
        _run(client.get_logs("demo", "email", testing=True, limit=5))
        _run(client.delete_run_file("demo", "a b.css"))
        assert json.loads(seen[0].read()) == {"testing": True}
        assert seen[1].url.path == "/api/v1/runs/demo/sessions/c0de/actions"
        assert json.loads(seen[1].read()) == {"action": "move_to_position", "position": 30}
        assert seen[2].url.path == "/api/v1/runs/demo/logs/email"
        assert seen[2].url.params["testing"] == "true" and seen[2].url.params["limit"] == "5"
        assert seen[3].method == "DELETE" and seen[3].url.raw_path == b"/api/v1/runs/demo/files/a%20b.css"

    def test_dot_segments_are_refused(self, monkeypatch):
        # urljoin resolves them: a file named ".." was a DELETE of the run
        seen = []
        client = self._client(monkeypatch, seen)
        for name in ("..", ".", ""):
            with pytest.raises(ValueError):
                _run(client.delete_run_file("demo", name))
            with pytest.raises(ValueError):
                _run(client.get_session("demo", name))
        assert seen == []
