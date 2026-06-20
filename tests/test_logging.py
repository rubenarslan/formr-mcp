import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formr_mcp import logsetup


class TestLevelFromEnv:
    def test_default_info(self, monkeypatch):
        monkeypatch.delenv("FORMR_MCP_LOG_LEVEL", raising=False)
        assert logsetup._level_from_env() == logging.INFO

    def test_debug(self, monkeypatch):
        monkeypatch.setenv("FORMR_MCP_LOG_LEVEL", "debug")
        assert logsetup._level_from_env() == logging.DEBUG

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("FORMR_MCP_LOG_LEVEL", "nonsense")
        assert logsetup._level_from_env() == logging.INFO


class TestPreview:
    def test_redacts_secrets(self):
        out = logsetup._preview({"client_secret": "abc123", "access_token": "t"})
        assert "client_secret=***" in out
        assert "access_token=***" in out
        assert "abc123" not in out

    def test_keeps_normal_args(self):
        out = logsetup._preview({"name": "my-run", "position": 10})
        assert "name='my-run'" in out and "position=10" in out

    def test_truncates_long_values(self):
        out = logsetup._preview({"structure_json": "x" * 500})
        assert "…" in out and len(out) <= 201

    def test_non_dict(self):
        assert logsetup._preview("hello") == "'hello'"


class _FakeTM:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc

    async def call_tool(self, name, arguments, **kw):
        if self.exc:
            raise self.exc
        return self.result


class _FakeMcp:
    def __init__(self, tm):
        self._tool_manager = tm


class TestInstallToolLogging:
    def test_logs_success(self, caplog):
        tm = _FakeTM(result="OK")
        logsetup.install_tool_logging(_FakeMcp(tm))
        with caplog.at_level(logging.INFO, logger="formr_mcp.tools"):
            out = asyncio.run(tm.call_tool("create_run", {"name": "x"}))
        assert out == "OK"
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "tool create_run(name='x')" in msgs
        assert "ok in" in msgs

    def test_logs_and_reraises_failure(self, caplog):
        tm = _FakeTM(exc=ValueError("boom"))
        logsetup.install_tool_logging(_FakeMcp(tm))
        with caplog.at_level(logging.INFO, logger="formr_mcp.tools"):
            with pytest.raises(ValueError, match="boom"):
                asyncio.run(tm.call_tool("delete_run", {"name": "y"}))
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "tool delete_run" in msgs and "FAILED" in msgs and "boom" in msgs

    def test_idempotent(self):
        tm = _FakeTM(result="OK")
        mcp = _FakeMcp(tm)
        logsetup.install_tool_logging(mcp)
        wrapped = tm.call_tool
        logsetup.install_tool_logging(mcp)  # second call must be a no-op
        assert tm.call_tool is wrapped

    def test_no_tool_manager_is_safe(self):
        class Empty:
            _tool_manager = None
        logsetup.install_tool_logging(Empty())  # must not raise
