from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urljoin, quote

import httpx

log = logging.getLogger("formr_mcp.client")


def _resolve_timeout() -> httpx.Timeout:
    """Per-request timeout for formr API calls.

    httpx defaults to 5s on every phase, which is too tight for slower or
    remote instances: run creation, structure upload, and results export do
    real synchronous server work (DB writes, markdown parsing, OpenCPU
    renders) that a browser waits out but a 5s client aborts with a timeout.
    Default to a generous read/write budget, keep connect short so a dead
    host still fails fast, and let operators tune it via FORMR_HTTP_TIMEOUT
    (seconds, applied to read/write/pool).
    """
    try:
        seconds = float(os.getenv("FORMR_HTTP_TIMEOUT", "60"))
    except ValueError:
        seconds = 60.0
    return httpx.Timeout(seconds, connect=min(10.0, seconds))

from formr_mcp.utils import validate_run_name
from .auth import AuthError, OAuthToken, get_token


class FormrClientError(Exception):
    pass


class FormrPermissionError(FormrClientError):
    """Raised on a 403 from formr — the token lacks a scope or is not
    authorized for the run/survey. Carries an actionable hint so the
    model can explain the limit instead of surfacing a raw status code."""
    pass


class FormrClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = http_client or httpx.AsyncClient(timeout=_resolve_timeout())
        self._token: OAuthToken | None = None
        self._owns_http = http_client is None
        self._capabilities: dict | None = None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> FormrClient:
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    async def _ensure_token(self) -> str:
        if self._token is None or self._token.is_expired:
            log.info("auth: requesting OAuth token from %s", self.base_url)
            self._token = await get_token(
                self.base_url,
                self.client_id,
                self.client_secret,
                self._http,
            )
            log.info("auth: token acquired (scopes: %s)", self._token.scope or "(none)")
        return self._token.access_token

    async def request(
        self,
        method: str,
        path: str,
        *,
        retried: bool = False,
        **kwargs: Any,
    ) -> Any:
        url = urljoin(self.base_url, path.lstrip("/"))
        token = await self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        t0 = time.perf_counter()
        try:
            resp = await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as e:
            read_to = getattr(self._http.timeout, "read", None)
            log.warning("formr %s %s timed out after ~%ss", method, path, read_to)
            raise FormrClientError(
                f"{method} {path} timed out (client limit ~{read_to}s). The formr server "
                "accepted the connection but did not respond in time. If reads (e.g. get_run) "
                "are instant but this WRITE hangs, the instance is stalling on the write itself "
                "— most often a database lock (a running backup / FLUSH TABLES WITH READ LOCK, "
                "or a long-running transaction blocking writes while reads pass), less often a "
                "slow OpenCPU render — not the MCP or your token. Raise FORMR_HTTP_TIMEOUT to "
                "wait longer, and check the instance's DB locks (SHOW PROCESSLIST) and logs."
            ) from e

        log.info("formr %s %s -> %s (%.0fms)", method, path, resp.status_code,
                 (time.perf_counter() - t0) * 1000)

        if resp.status_code == 401 and not retried:
            self._token = None
            return await self.request(method, path, retried=True, **kwargs)

        if resp.status_code == 204:
            return None
        if resp.status_code == 403:
            raise FormrPermissionError(self._permission_message(method, path, resp))
        if not resp.is_success:
            body = resp.text[:500]
            raise FormrClientError(
                f"{method} {path} -> {resp.status_code}: {body}"
            )

        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return resp.json()
        return resp.text

    async def get_surveys(self, name: str | None = None) -> list[dict]:
        params = {"name": name} if name else {}
        return await self.request("GET", "api/v1/surveys", params=params)

    async def get_survey(self, name: str, format: str = "json") -> Any:
        validate_run_name(name)
        return await self.request(
            "GET", f"api/v1/surveys/{quote(name, safe='')}", params={"format": format}
        )

    async def get_runs(self, name: str | None = None) -> list[dict]:
        params = {"name": name} if name else {}
        return await self.request("GET", "api/v1/runs", params=params)

    async def get_run(self, name: str) -> dict:
        validate_run_name(name)
        return await self.request("GET", f"api/v1/runs/{quote(name, safe='')}")

    async def get_run_structure(self, name: str) -> dict:
        validate_run_name(name)
        return await self.request("GET", f"api/v1/runs/{quote(name, safe='')}/structure")

    async def put_run_structure(self, name: str, structure: dict) -> dict:
        validate_run_name(name)
        return await self.request(
            "PUT", f"api/v1/runs/{quote(name, safe='')}/structure", json=structure
        )

    async def create_run(self, name: str) -> dict:
        validate_run_name(name)
        return await self.request("POST", f"api/v1/runs/{quote(name, safe='')}")

    async def patch_run(self, name: str, settings: dict) -> dict:
        validate_run_name(name)
        return await self.request("PATCH", f"api/v1/runs/{quote(name, safe='')}", json=settings)

    async def delete_run(self, name: str) -> None:
        validate_run_name(name)
        return await self.request("DELETE", f"api/v1/runs/{quote(name, safe='')}")

    async def get_user_me(self) -> dict:
        return await self.request("GET", "api/v1/user/me")

    # --- capability / permission introspection ---

    async def scopes(self) -> list[str]:
        """The scopes granted to the current token, from the OAuth grant
        response (no extra request). Reliable for pre-checks."""
        await self._ensure_token()
        raw = (self._token.scope or "").strip()
        return raw.split() if raw else []

    async def get_capabilities(self, *, refresh: bool = False) -> dict:
        """Identity + capabilities from the extended GET /user/me
        (admin, scopes, allowed_runs). Memoized for the client's life."""
        if self._capabilities is None or refresh:
            self._capabilities = await self.get_user_me()
        return self._capabilities

    def _permission_message(self, method: str, path: str, resp: httpx.Response) -> str:
        server_msg = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                server_msg = body.get("message", "") or ""
        except Exception:
            server_msg = resp.text[:300]
        base = self.base_url.rstrip("/")
        detail = f" {server_msg}" if server_msg else ""
        return (
            f"Permission denied (403) for {method} {path}.{detail}\n"
            f"This API token is missing the required scope, or is not authorized for this run. "
            f"Run `whoami` to see this token's granted scopes and run allowlist. "
            f"To widen access, generate credentials with the needed scopes / run allowlist at "
            f"{base}/admin/account#api (requires admin >= 2)."
        )

    # --- data reading (V1) ---

    async def get_sessions(
        self,
        run: str,
        *,
        active: bool | None = None,
        testing: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        validate_run_name(run)
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active is not None:
            params["active"] = "true" if active else "false"
        if testing is not None:
            params["testing"] = "true" if testing else "false"
        return await self.request(
            "GET", f"api/v1/runs/{quote(run, safe='')}/sessions", params=params
        )

    async def get_session(self, run: str, code: str) -> dict:
        validate_run_name(run)
        return await self.request(
            "GET", f"api/v1/runs/{quote(run, safe='')}/sessions/{quote(code, safe='')}"
        )

    async def get_unit_sessions(
        self,
        run: str,
        *,
        session: str | None = None,
        testing: bool | None = None,
        since: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict]:
        validate_run_name(run)
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if session:
            params["session"] = session
        if testing is not None:
            params["testing"] = "true" if testing else "false"
        if since:
            params["since"] = since
        return await self.request(
            "GET", f"api/v1/runs/{quote(run, safe='')}/unit_sessions", params=params
        )

    async def get_results(
        self,
        run: str,
        *,
        surveys: list[str] | None = None,
        sessions: list[str] | None = None,
        items: list[str] | None = None,
    ) -> dict:
        validate_run_name(run)
        params: dict[str, Any] = {}
        if surveys:
            params["surveys"] = ",".join(surveys)
        if sessions:
            params["sessions"] = ",".join(sessions)
        if items:
            params["items"] = ",".join(items)
        return await self.request(
            "GET", f"api/v1/runs/{quote(run, safe='')}/results", params=params
        )

    async def get_run_files(self, run: str) -> list[dict]:
        validate_run_name(run)
        return await self.request("GET", f"api/v1/runs/{quote(run, safe='')}/files")
