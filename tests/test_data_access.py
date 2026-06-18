import os
import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_mod
from formr_mcp import data_access as gate
from formr_mcp.data_access import (
    DataAccessError, resolve_testing, get_results_gated, is_test_session,
)
from formr_mcp.client import FormrClient, FormrPermissionError


class TestResolveTesting:
    def test_test_only_forces_true(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        assert resolve_testing(None) is True
        assert resolve_testing(True) is True

    def test_test_only_refuses_real(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        with pytest.raises(DataAccessError, match="test_only"):
            resolve_testing(False)

    def test_all_passes_through(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "all")
        assert resolve_testing(None) is None
        assert resolve_testing(True) is True
        assert resolve_testing(False) is False

    def test_default_is_test_only(self, monkeypatch):
        monkeypatch.delenv("FORMR_DATA_ACCESS", raising=False)
        assert gate.data_access_mode() == gate.TEST_ONLY

    def test_unknown_fails_safe(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "garbage")
        assert gate.data_access_mode() == gate.TEST_ONLY


class TestIsTestSession:
    def test_variants(self):
        assert is_test_session({"testing": 1})
        assert is_test_session({"testing": True})
        assert is_test_session({"testing": "1"})
        assert not is_test_session({"testing": 0})
        assert not is_test_session({})


class TestGetResultsGated:
    def test_test_only_filters_to_test_codes(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        client = AsyncMock()
        client.get_sessions.return_value = [{"session": "TESTAAA"}, {"session": "TESTBBB"}]
        client.get_results.return_value = {"s1": []}
        asyncio.run(get_results_gated(client, "my-run", testing=None))
        client.get_sessions.assert_called_with("my-run", testing=True, limit=10000, offset=0)
        _, kwargs = client.get_results.call_args
        assert set(kwargs["sessions"]) == {"TESTAAA", "TESTBBB"}

    def test_empty_returns_empty_without_results_call(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        client = AsyncMock()
        client.get_sessions.return_value = []
        out = asyncio.run(get_results_gated(client, "my-run"))
        assert out == {}
        client.get_results.assert_not_called()

    def test_all_mode_no_session_prequery(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "all")
        client = AsyncMock()
        client.get_results.return_value = {"s": []}
        asyncio.run(get_results_gated(client, "my-run", testing=None))
        client.get_sessions.assert_not_called()
        client.get_results.assert_called_once()

    def test_drops_real_codes_from_provided_sessions(self, monkeypatch):
        # The gate leak check: a real session code passed by the caller must
        # not survive into the /results call under test_only.
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        client = AsyncMock()
        client.get_sessions.return_value = [{"session": "TESTAAA"}]
        client.get_results.return_value = {}
        asyncio.run(get_results_gated(client, "my-run", sessions=["TESTAAA", "REALBBB"]))
        _, kwargs = client.get_results.call_args
        assert kwargs["sessions"] == ["TESTAAA"]

    def test_pages_past_10000(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        client = AsyncMock()
        page1 = [{"session": f"T{i}"} for i in range(10000)]
        page2 = [{"session": "TLAST"}]
        client.get_sessions.side_effect = [page1, page2]
        client.get_results.return_value = {}
        asyncio.run(get_results_gated(client, "my-run"))
        assert client.get_sessions.call_count == 2
        _, kwargs = client.get_results.call_args
        assert "TLAST" in kwargs["sessions"]
        assert len(kwargs["sessions"]) == 10001


class TestPermissionError:
    def test_403_becomes_permission_error_with_hint(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                403, json={"message": "Insufficient permissions: 'data:read' scope required."}
            )
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = FormrClient("https://formr.example.org", "cid", "secret", http_client=http)

        async def fake_token():
            return "tok"
        monkeypatch.setattr(client, "_ensure_token", fake_token)

        with pytest.raises(FormrPermissionError) as exc:
            asyncio.run(client.get_results("my-run"))
        msg = str(exc.value)
        assert "data:read" in msg
        assert "admin/account#api" in msg
        asyncio.run(http.aclose())


class TestCapabilityReport:
    def test_readonly_test_only(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        caps = {"scopes": ["run:read", "session:read", "data:read"],
                "allowed_runs": [{"id": 1, "name": "foo"}]}
        rep = server_mod._build_capability_report(caps, [])
        assert "READ-ONLY" in rep["access_summary"]
        assert "foo" in rep["access_summary"]
        assert rep["data_access_mode"] == "test_only"
        assert rep["tool_access"]["create_run"].startswith("blocked")
        assert rep["tool_access"]["list_sessions"] == "available"
        assert "TEST sessions only" in rep["tool_access"]["get_run_results"]
        assert rep["tool_access"]["list_run_files"].startswith("blocked")

    def test_full_access_all_mode(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "all")
        caps = {"scopes": ["run:read", "run:write", "data:read", "session:read",
                           "file:read", "survey:read"], "allowed_runs": None}
        rep = server_mod._build_capability_report(caps, [])
        assert "read-write" in rep["access_summary"]
        assert "all owned runs" in rep["access_summary"]
        assert rep["tool_access"]["get_run_results"] == "available"
        assert rep["tool_access"]["create_run"] == "available"

    def test_falls_back_to_token_scopes(self, monkeypatch):
        # Old formr without the user/me capability fields: scopes come from token.
        monkeypatch.setenv("FORMR_DATA_ACCESS", "all")
        rep = server_mod._build_capability_report({}, ["run:read"])
        assert rep["scopes"] == ["run:read"]


class TestServerGate:
    def test_get_session_blocks_real_in_test_only(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        mock_client = AsyncMock()
        mock_client.get_session.return_value = {"session": "REAL", "testing": 0}
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        with pytest.raises(DataAccessError):
            asyncio.run(server_mod.get_session("my-run", "REAL", ctx=MagicMock()))

    def test_get_session_allows_test_in_test_only(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        mock_client = AsyncMock()
        mock_client.get_session.return_value = {"session": "TEST", "testing": 1}
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        out = asyncio.run(server_mod.get_session("my-run", "TEST", ctx=MagicMock()))
        assert out["session"] == "TEST"

    def test_list_sessions_refuses_explicit_real(self, monkeypatch):
        monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")
        mock_client = AsyncMock()
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        with pytest.raises(DataAccessError):
            asyncio.run(server_mod.list_sessions("my-run", testing=False, ctx=MagicMock()))
        mock_client.get_sessions.assert_not_called()
