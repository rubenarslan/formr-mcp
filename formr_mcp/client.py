from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin, quote

import httpx

log = logging.getLogger("formr_mcp.client")


def resolve_timeout() -> httpx.Timeout:
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

from formr_mcp.utils import validate_run_name, validate_survey_name
from .auth import AuthError, OAuthToken, get_token


def _log_path(path: str) -> str:
    """A request path for the log: a session's code is a login to the
    participant's session, so only its start goes there."""
    return re.sub(r"(sessions/)([^/?]{6})[^/?]*", r"\1\2…", path)


class FormrClientError(Exception):
    """`status` is formr's HTTP status where it answered, else None."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


class FormrAnswerLost(FormrClientError):
    """The request reached formr but its answer did not come back (a read
    timeout, a dropped connection): formr may have carried it out."""
    pass


class FormrAuthExpired(FormrClientError):
    """A caller's own token (HTTP mode) was refused: the client must sign in again."""
    pass


class FormrPermissionError(FormrClientError):
    """Raised on a 403 from formr — the token lacks a scope or is not
    authorized for the run/survey. Carries an actionable hint so the
    model can explain the limit instead of surfacing a raw status code."""
    pass


class FormrConflict(FormrClientError):
    """A 409 from formr. `body` is its JSON answer: a structure upload that
    would remove units participants are in names them (units_in_use)."""

    def __init__(self, message: str, body: dict | None = None):
        super().__init__(message)
        self.body = body or {}


def _segment(value: str) -> str:
    """One segment of a formr API path. `.` and `..` would be dot segments,
    which urljoin resolves: a file named `..` made DELETE runs/<run>/files/..
    a DELETE of the run itself."""
    value = str(value)
    if value in ("", ".", ".."):
        raise ValueError(f"{value!r} is not a valid name here.")
    return quote(value, safe="")


def _code_segment(code: str) -> str:
    """A session's code in a formr API path. formr reads it from the raw
    request path, undecoded (SessionResource), so a '+' the code may hold stays
    a '+' (it is one in a path); everything else is encoded as in _segment."""
    return _segment(code).replace("%2B", "+")


class FormrClient:
    def __init__(
        self,
        base_url: str,
        client_id: str | None = None,
        client_secret: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        *,
        access_token: str | None = None,
        on_token_refused: Callable[[], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.client_id = client_id
        self.client_secret = client_secret
        # A caller's own formr token (HTTP mode: the bearer token a client sent
        # the MCP server, which formr issued it when its researcher connected).
        # Used as it is: it is the client's to refresh, never this server's.
        self._access_token = access_token
        # Told when formr refuses that token (HTTP mode: the token verifier
        # forgets its cached answer, so the client's next request gets a 401
        # and refreshes).
        self._on_token_refused = on_token_refused
        self._http = http_client or httpx.AsyncClient(timeout=resolve_timeout())
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
        if self._access_token is not None:
            return self._access_token
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
            # not connected: nothing was sent; else formr may have done it
            lost = not isinstance(e, (httpx.ConnectTimeout, httpx.PoolTimeout))
            raise (FormrAnswerLost if lost else FormrClientError)(
                f"{method} {path} timed out (client limit ~{read_to}s). The formr server "
                "accepted the connection but did not respond in time. If reads (e.g. get_run) "
                "are instant but this WRITE hangs, the instance is stalling on the write itself "
                "— most often a database lock (a running backup / FLUSH TABLES WITH READ LOCK, "
                "or a long-running transaction blocking writes while reads pass), less often a "
                "slow OpenCPU render — not the MCP or your token. Raise FORMR_HTTP_TIMEOUT to "
                "wait longer, and check the instance's DB locks (SHOW PROCESSLIST) and logs."
            ) from e
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.DecodingError) as e:
            # sent, and the connection went before the answer (formr_app
            # restarting, a proxy cut), or the answer came with a broken
            # Content-Encoding: formr may have carried it out
            log.warning("formr %s %s: no answer (%s)", method, path, type(e).__name__)
            raise FormrAnswerLost(f"{method} {path}: formr's answer was lost ({type(e).__name__}: {e}).") from e

        log.info("formr %s %s -> %s (%.0fms)", method, _log_path(path), resp.status_code,
                 (time.perf_counter() - t0) * 1000)

        if resp.status_code == 401 and self._access_token is not None:
            if self._on_token_refused is not None:
                self._on_token_refused()
            raise FormrAuthExpired(
                "formr no longer accepts this connection's token: it expired, the connection's "
                "access was changed, or the connection was deleted under Account -> API. Try "
                "again: the app gets a new token by itself. Only if formr asks to sign in, "
                "connect the app again."
            )
        if resp.status_code == 401 and not retried:
            self._token = None
            return await self.request(method, path, retried=True, **kwargs)

        if resp.status_code == 204:
            return None
        if resp.status_code == 403:
            raise FormrPermissionError(self._permission_message(method, path, resp))
        if resp.status_code == 409:
            try:
                body = resp.json()
            except ValueError:
                body = None
            raise FormrConflict(f"{method} {path} -> 409: {resp.text[:500]}",
                                body if isinstance(body, dict) else None)
        if not resp.is_success:
            body = resp.text[:500]
            raise FormrClientError(
                f"{method} {path} -> {resp.status_code}: {body}", status=resp.status_code
            )

        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            try:
                return resp.json()
            except ValueError as e:  # JSONDecodeError, UnicodeDecodeError
                # a 2xx: formr carried it out, only its answer can't be read
                # (no excerpt: a 2xx body may hold participants' data)
                log.warning("formr %s %s -> %s: unreadable JSON (%s)", method, _log_path(path),
                            resp.status_code, type(e).__name__)
                raise FormrAnswerLost(
                    f"{method} {path} -> {resp.status_code}, but formr's answer could not be read "
                    f"({type(e).__name__}: {e}).", status=resp.status_code) from e
        return resp.text

    async def get_surveys(self, name: str | None = None) -> list[dict]:
        params = {"name": name} if name else {}
        return await self.request("GET", "api/v1/surveys", params=params)

    async def get_survey(self, name: str, format: str = "json") -> Any:
        validate_survey_name(name)
        return await self.request(
            "GET", f"api/v1/surveys/{_segment(name)}", params={"format": format}
        )

    async def get_runs(self, name: str | None = None) -> list[dict]:
        params = {"name": name} if name else {}
        return await self.request("GET", "api/v1/runs", params=params)

    async def get_run(self, name: str) -> dict:
        validate_run_name(name)
        return await self.request("GET", f"api/v1/runs/{_segment(name)}")

    async def get_run_structure(self, name: str) -> dict:
        validate_run_name(name)
        return await self.request("GET", f"api/v1/runs/{_segment(name)}/structure")

    async def put_run_structure(self, name: str, structure: dict, confirm_removal: bool = False) -> dict:
        """formr keeps the units sent back with their run_unit_id; removing
        units participants are in is a 409 (FormrConflict) unless confirmed."""
        validate_run_name(name)
        return await self.request(
            "PUT", f"api/v1/runs/{_segment(name)}/structure", json=structure,
            params={"confirm_removal": 1} if confirm_removal else None,
        )

    async def create_run(self, name: str) -> dict:
        validate_run_name(name)
        return await self.request("POST", f"api/v1/runs/{_segment(name)}")

    async def patch_run(self, name: str, settings: dict) -> dict:
        validate_run_name(name)
        return await self.request("PATCH", f"api/v1/runs/{_segment(name)}", json=settings)

    async def delete_run(self, name: str) -> None:
        validate_run_name(name)
        return await self.request("DELETE", f"api/v1/runs/{_segment(name)}")

    async def get_user_me(self) -> dict:
        return await self.request("GET", "api/v1/user/me")

    async def get_token_info(self) -> dict:
        """GET /user/token: the calling token's account, scopes, expiry and runs
        (no scope needed), the endpoint the HTTP server's FormrTokenVerifier
        checks every bearer token with."""
        return await self.request("GET", "api/v1/user/token")

    # --- capability / permission introspection ---

    async def scopes(self) -> list[str]:
        """The scopes granted to the current token: from the OAuth grant
        response (no extra request), or for a caller's own token (HTTP mode)
        from GET /user/token. Reliable for pre-checks."""
        if self._access_token is not None:
            return list((await self.get_capabilities()).get("scopes") or [])
        await self._ensure_token()
        raw = (self._token.scope or "").strip()
        return raw.split() if raw else []

    async def get_capabilities(self, *, refresh: bool = False) -> dict:
        """Identity + capabilities from the extended GET /user/me
        (admin, scopes, allowed_runs). Memoized for the client's life."""
        if self._capabilities is None or refresh:
            if self._access_token is not None:
                # a connection's token may lack user:read; /user/token needs none
                self._capabilities = await self._token_capabilities()
            else:
                try:
                    self._capabilities = await self.get_user_me()
                except FormrPermissionError:
                    # a credential without user:read (no guide lists it): what
                    # /user/token says, which needs none; whoami's 403 sent the
                    # model to whoami
                    self._capabilities = await self._token_capabilities()
        return self._capabilities

    async def _token_capabilities(self) -> dict:
        info = await self.get_token_info()
        if not isinstance(info, dict):
            raise FormrClientError("formr's GET /api/v1/user/token answered with no token information.")
        return {
            "id": info.get("account_id"),
            "email": info.get("email"),
            "connection": info.get("label"),
            "scopes": info.get("scopes") or [],
            # names only with run:read; else formr gives the ids
            "allowed_runs": [r if isinstance(r, dict) else {"id": r, "name": f"run #{r}"}
                             for r in info["allowed_runs"]] if info.get("allowed_runs") else None,
        }

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
        if self._access_token is not None:
            return (
                f"Permission denied (403) for {method} {path}.{detail}\n"
                f"This connection was not given the scope this needs, or not this run. "
                f"Run `whoami` to see what it may do. The researcher can change that at "
                f"{base}/admin/account#api (Connected apps), or connect the app again."
            )
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
            "GET", f"api/v1/runs/{_segment(run)}/sessions", params=params
        )

    async def get_session(self, run: str, code: str) -> dict:
        validate_run_name(run)
        return await self.request(
            "GET", f"api/v1/runs/{_segment(run)}/sessions/{_code_segment(code)}"
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
        include_log: bool = False,
    ) -> list[dict]:
        validate_run_name(run)
        params = self._log_params(session, testing, since, limit, offset)
        if include_log:
            params["include_log"] = "true"
        return await self.request(
            "GET", f"api/v1/runs/{_segment(run)}/unit_sessions", params=params
        )

    @staticmethod
    def _log_params(session: str | None, testing: bool | None, since: str | None,
                    limit: int, offset: int) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if session:
            params["session"] = session
        if testing is not None:
            params["testing"] = "true" if testing else "false"
        if since:
            params["since"] = since
        return params

    async def get_logs(
        self,
        run: str,
        kind: str,
        *,
        session: str | None = None,
        testing: bool | None = None,
        since: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict]:
        """GET /runs/{run}/logs/{email|push} (formr with the logs endpoint)."""
        validate_run_name(run)
        if kind not in ("email", "push"):
            raise ValueError("kind must be 'email' or 'push'")
        return await self.request(
            "GET", f"api/v1/runs/{_segment(run)}/logs/{kind}",
            params=self._log_params(session, testing, since, limit, offset),
        )

    # --- writing sessions (V1) ---

    async def create_sessions(
        self, run: str, *, codes: list[str] | None = None, testing: bool = True
    ) -> dict:
        """POST /runs/{run}/sessions. Without codes formr makes one random
        code; with codes, one session per code (207 when some failed)."""
        validate_run_name(run)
        body: dict[str, Any] = {"testing": testing}
        if codes:
            body["code"] = codes
        return await self.request(
            "POST", f"api/v1/runs/{_segment(run)}/sessions", json=body
        )

    async def session_action(
        self, run: str, code: str, action: str, *, position: int | None = None
    ) -> Any:
        validate_run_name(run)
        body: dict[str, Any] = {"action": action}
        if position is not None:
            body["position"] = position
        return await self.request(
            "POST",
            f"api/v1/runs/{_segment(run)}/sessions/{_code_segment(code)}/actions",
            json=body,
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
            "GET", f"api/v1/runs/{_segment(run)}/results", params=params
        )

    async def get_run_files(self, run: str) -> list[dict]:
        validate_run_name(run)
        return await self.request("GET", f"api/v1/runs/{_segment(run)}/files")

    async def upload_run_file(
        self, run: str, filename: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> dict:
        """POST /runs/{run}/files as multipart/form-data, field "file".
        Bytes, not a file object, so the 401 retry can send them again."""
        validate_run_name(run)
        return await self.request(
            "POST", f"api/v1/runs/{_segment(run)}/files",
            files={"file": (filename, content, content_type)},
        )

    async def delete_run_file(self, run: str, filename: str) -> Any:
        validate_run_name(run)
        return await self.request(
            "DELETE", f"api/v1/runs/{_segment(run)}/files/{_segment(filename)}"
        )
