import hashlib
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from mcp.server.auth.provider import AccessToken
from starlette.authentication import AuthenticationError

log = logging.getLogger("formr_mcp.auth")


class AuthError(Exception):
    pass


@dataclass
class OAuthToken:
    access_token: str
    token_type: str
    scope: str
    expires_in: int
    expires_at: float = field(init=False)

    def __post_init__(self):
        self.expires_at = time.time() + self.expires_in

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at - 60


def check_credentials(base_url: str, client_id: str, client_secret: str) -> str | None:
    """Return a user-facing error message if credentials are clearly misconfigured, or None."""
    if not base_url or not base_url.strip():
        return (
            "FORMR_BASE_URL is not set. "
            "Create a .env file with:\n"
            "  FORMR_BASE_URL=http://localhost\n"
            "  FORMR_CLIENT_ID=your_32_hex_client_id\n"
            "  FORMR_CLIENT_SECRET=your_64_hex_client_secret\n"
            "\n"
            "Get API credentials at <base_url>/admin/account#api"
            " (requires admin >= 2, scopes: survey:read, run:read, data:read)"
        )
    if not client_id or not client_id.strip():
        return (
            "FORMR_CLIENT_ID is not set. "
            "Add it to .env — generate one at your formr instance's /admin/account#api"
        )
    if not client_secret or not client_secret.strip():
        return (
            "FORMR_CLIENT_SECRET is not set. "
            "Add it to .env — generate one at your formr instance's /admin/account#api"
        )
    return None


async def get_token(
    base_url: str,
    client_id: str,
    client_secret: str,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthToken:
    err = check_credentials(base_url, client_id, client_secret)
    if err:
        raise AuthError(err)

    url = urljoin(base_url.rstrip("/") + "/", "api/oauth/access_token")
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    client = http_client or httpx.AsyncClient()
    try:
        resp = await client.post(url, data=data)
        if resp.status_code == 400:
            raise AuthError(
                "Authentication failed (400 Bad Request). "
                "The formr server rejected the credentials. "
                "Check that your FORMR_CLIENT_ID and FORMR_CLIENT_SECRET are correct "
                "and were generated at <base_url>/admin/account#api."
            )
        if resp.status_code == 401:
            raise AuthError(
                "Authentication failed (401 Unauthorized). "
                "Your FORMR_CLIENT_ID and FORMR_CLIENT_SECRET were rejected. "
                "Verify they are correct and have the required scopes "
                "(survey:read, run:read, run:write, data:read)."
            )
        resp.raise_for_status()
        body = resp.json()
        return OAuthToken(
            access_token=body["access_token"],
            token_type=body["token_type"],
            scope=body.get("scope", ""),
            expires_in=body["expires_in"],
        )
    except httpx.ConnectError:
        raise AuthError(
            f"Cannot connect to formr server at {base_url}.\n"
            "Make sure the server is running and reachable.\n"
            "If running locally, start it with your dev server.\n"
            "Check FORMR_BASE_URL in .env (default: http://localhost)."
        )
    finally:
        if http_client is None:
            await client.aclose()


class FormrUnavailable(AuthenticationError):
    """formr did not answer a token check (down, restarting, an error): the
    token may well be good, so this is no 401 that sends the client to sign in
    again (http_server answers 503)."""


class FormrTokenVerifier:
    """The HTTP server's check of every bearer token a client sends: formr,
    which issued it when its researcher connected the app, says whose it is
    and what it may do (GET /api/v1/user/token). Only a connection's token is
    taken (one formr issued for this MCP server: connector_client_id set), not
    a token of a credential made for a script. Answers are kept for `ttl`
    seconds by the token's hash, never past the token's expiry, so a connection
    deleted in formr stops here within that time (and at formr at once; a tool
    call that formr refuses drops the answer, forget()). While formr does not
    answer, a token's last answer stands until the token expires, and a token
    with none gets FormrUnavailable (503), never the 401 that sends the client
    to sign in again.
    """

    def __init__(self, base_url: str, http_client: httpx.AsyncClient, resource: str,
                 ttl: float = 60.0, max_entries: int = 10000):
        self._url = urljoin(base_url.rstrip("/") + "/", "api/v1/user/token")
        self._http = http_client
        self._resource = resource
        self._ttl = ttl
        self._max_entries = max_entries
        self._cache: dict[str, tuple[float, AccessToken]] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def forget(self, token: str) -> None:
        """Drop a token formr has refused since it was checked (FormrClient's
        401: its connection was changed or deleted): the client's next request
        with it gets the 401 that makes it refresh, not the cached answer."""
        self._cache.pop(self._key(token), None)

    async def verify_token(self, token: str) -> AccessToken | None:
        # A token formr refuses, or an answer that is no connection's, is no
        # access (a 401 that sends the client to sign in). formr not answering
        # is not that: FormrUnavailable (a 503), or the last answer while the
        # token is unexpired. Nothing else raises (no 500).
        key = self._key(token)
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            resp = await self._http.get(self._url, headers={"Authorization": f"Bearer {token}"})
        except ValueError as e:
            # a token httpx can't put in a header (non-ASCII): no token of formr's
            log.info("token check: refused an unusable token (%s)", type(e).__name__)
            return None
        except httpx.HTTPError as e:
            return self._while_unavailable(key, hit, now, type(e).__name__)
        if resp.status_code in (400, 401, 403):
            self._cache.pop(key, None)
            return None
        if resp.status_code != 200:
            return self._while_unavailable(key, hit, now, f"HTTP {resp.status_code}")
        try:
            info = resp.json()
            if not isinstance(info, dict) or not info.get("connector_client_id"):
                log.info("token check: refused a token that is not a connection's")
                self._cache.pop(key, None)
                return None
            expires = int(info["expires"]) if info.get("expires") else None
            access = AccessToken(
                token=token,
                client_id=str(info["client_id"]),
                scopes=[str(s) for s in (info.get("scopes") or [])],
                expires_at=expires,
                resource=self._resource,
                subject=str(info["account_id"]),
                claims={"connector_client_id": info["connector_client_id"]},
            )
        except Exception as e:
            log.warning("token check: unexpected answer from formr (%s)", type(e).__name__)
            self._cache.pop(key, None)
            return None
        if len(self._cache) >= self._max_entries:
            # the expired first, all only when every entry is live
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if len(self._cache) >= self._max_entries:
                self._cache.clear()
        until = now + self._ttl
        if expires is not None:
            until = min(until, expires)
        self._cache[key] = (until, access)
        return access

    def _while_unavailable(self, key: str, hit, now: float, why: str) -> AccessToken:
        """formr did not answer: its last answer for this token while the token
        itself is unexpired (formr checks it again on every tool call anyway),
        else FormrUnavailable."""
        if hit is not None and hit[1].expires_at and hit[1].expires_at > now:
            log.warning("token check: formr unavailable (%s), its last answer is used", why)
            # for another while: the next request would wait on formr as long again
            self._cache[key] = (min(now + self._ttl, hit[1].expires_at), hit[1])
            return hit[1]
        log.warning("token check: formr unavailable (%s)", why)
        raise FormrUnavailable(f"formr is not answering ({why}); try again shortly.")
