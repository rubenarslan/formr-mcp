import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formr_mcp.client import FormrClient, FormrClientError, _resolve_timeout


class TestHttpTimeout:
    """Regression for the 5s-default-timeout bug: create_run (and other
    write/heavy calls) timed out against slower/remote instances because the
    client used httpx's 5s default. We now default to a generous, tunable
    timeout while keeping connect short."""

    def test_default_is_generous_not_five_seconds(self, monkeypatch):
        monkeypatch.delenv("FORMR_HTTP_TIMEOUT", raising=False)
        t = _resolve_timeout()
        assert t.read == 60.0
        assert t.write == 60.0
        assert t.connect == 10.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("FORMR_HTTP_TIMEOUT", "120")
        t = _resolve_timeout()
        assert t.read == 120.0
        assert t.connect == 10.0  # capped at min(10, 120)

    def test_small_timeout_also_caps_connect(self, monkeypatch):
        monkeypatch.setenv("FORMR_HTTP_TIMEOUT", "3")
        t = _resolve_timeout()
        assert t.read == 3.0
        assert t.connect == 3.0

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("FORMR_HTTP_TIMEOUT", "not-a-number")
        assert _resolve_timeout().read == 60.0

    def test_client_uses_resolved_timeout(self, monkeypatch):
        monkeypatch.delenv("FORMR_HTTP_TIMEOUT", raising=False)
        client = FormrClient("https://x.example.org", "cid", "secret")
        assert client._http.timeout.read == 60.0

    def test_injected_client_timeout_is_respected(self):
        custom = httpx.AsyncClient(timeout=7.0)
        client = FormrClient("https://x.example.org", "cid", "secret", http_client=custom)
        assert client._http.timeout.read == 7.0

    def test_timeout_raises_actionable_error(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("simulated stall", request=request)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = FormrClient("https://x.example.org", "cid", "secret", http_client=http)

        async def fake_token():
            return "tok"
        monkeypatch.setattr(client, "_ensure_token", fake_token)

        with pytest.raises(FormrClientError, match="timed out"):
            asyncio.run(client.create_run("my-run"))
        msg = None
        try:
            asyncio.run(client.create_run("my-run"))
        except FormrClientError as e:
            msg = str(e)
        assert "database lock" in msg and "FORMR_HTTP_TIMEOUT" in msg
        asyncio.run(http.aclose())
