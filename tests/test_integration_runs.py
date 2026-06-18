"""Live integration tests: create a run, exercise tools against it, delete it.

These hit the formr instance configured in .env (FORMR_BASE_URL / CLIENT_ID /
CLIENT_SECRET) and require run:write. They are skipped automatically when no
credentials are present, so the offline unit suite is unaffected.

Every run created here is named `mcptest-<uuid>` and is torn down in a finally
block; a module-scoped sweep retries any that a crash left behind.
"""
import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_mod
from formr_mcp.client import FormrClient, FormrClientError

HAVE_CREDS = bool(server_mod.BASE_URL and server_mod.CLIENT_ID and server_mod.CLIENT_SECRET)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not HAVE_CREDS, reason="live formr credentials not configured in .env"),
]

# Run names created this session — for a crash-safe sweep at module teardown.
_created: set[str] = set()


def _make_client() -> FormrClient:
    return FormrClient(server_mod.BASE_URL, server_mod.CLIENT_ID, server_mod.CLIENT_SECRET)


def _unique_name() -> str:
    # Starts with a letter, lowercase, hyphen + hex -> matches the run-name pattern.
    return f"mcptest-{uuid.uuid4().hex[:12]}"


@asynccontextmanager
async def _temp_run(client: FormrClient, name: str):
    """Create a run, yield its name, and delete it on exit — even on error."""
    await client.create_run(name)
    _created.add(name)
    try:
        yield name
    finally:
        try:
            await client.delete_run(name)
            _created.discard(name)
        except Exception:
            pass  # leave tracked so the module sweep can retry it


@pytest.fixture(scope="module", autouse=True)
def _sweep_leftovers():
    yield
    if not _created:
        return

    async def _sweep():
        client = _make_client()
        try:
            for name in list(_created):
                try:
                    await client.delete_run(name)
                    _created.discard(name)
                except Exception:
                    pass
        finally:
            await client.aclose()

    asyncio.run(_sweep())


def test_create_then_delete_round_trip():
    """A created run is visible via get_run/list_runs, then gone after teardown."""
    async def body():
        client = _make_client()
        try:
            name = _unique_name()
            async with _temp_run(client, name):
                got = await client.get_run(name)
                assert got["name"] == name
                listed = await client.get_runs(name)
                assert any(r["name"] == name for r in listed)
            # After the context exits the run has been deleted.
            with pytest.raises(FormrClientError):
                await client.get_run(name)
            assert name not in _created
        finally:
            await client.aclose()

    asyncio.run(body())


def test_read_tools_on_fresh_run(monkeypatch):
    """The read tools work against a real (empty) run under the default gate."""
    monkeypatch.setenv("FORMR_DATA_ACCESS", "test_only")

    async def body():
        client = _make_client()
        monkeypatch.setattr(server_mod, "_client", lambda ctx: client)
        try:
            name = _unique_name()
            async with _temp_run(client, name):
                assert await server_mod.list_sessions(name, ctx=None) == []
                assert await server_mod.list_unit_sessions(name, ctx=None) == []
                # test_only resolves zero test sessions -> {} (no /results call).
                assert await server_mod.get_run_results(name, ctx=None) == {}
                assert isinstance(await server_mod.list_run_files(name, ctx=None), list)
                struct = await client.get_run_structure(name)
                assert "units" in struct
        finally:
            await client.aclose()

    asyncio.run(body())


def test_teardown_happens_even_on_failure():
    """An error inside the run's lifetime must not leak the run."""
    async def body():
        client = _make_client()
        try:
            name = _unique_name()
            with pytest.raises(RuntimeError, match="boom"):
                async with _temp_run(client, name):
                    raise RuntimeError("boom")
            # Despite the error, the finally deleted the run.
            with pytest.raises(FormrClientError):
                await client.get_run(name)
            assert name not in _created
        finally:
            await client.aclose()

    asyncio.run(body())
