"""Central data-access gate for participant data.

GDPR posture: an operator sets the hard ceiling via the FORMR_DATA_ACCESS
env var. The model can filter *within* the ceiling but can never exceed it.

  test_only  (default)  Only test sessions (survey_run_sessions.testing = 1)
                        are ever readable. Requests for real data are refused.
  all                   Real participant data is readable; the model may still
                        filter to test-only / real-only per call.

The single most important rule lives in get_results_gated(): the formr
`/results` endpoint has NO testing filter, so test-only enforcement is done
by first resolving the allowed session codes via the sessions endpoint and
passing them as the `sessions` filter. Without this, test_only would leak
real data through results.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import FormrClient

TEST_ONLY = "test_only"
ALL = "all"

# Page size for resolving session codes. The API caps `limit` at 10000.
_SESSION_PAGE = 10000
# Hard stop so a misbehaving backend can't loop forever (~5M sessions).
_MAX_PAGES = 500


class DataAccessError(Exception):
    """Raised when a request would exceed the configured data-access ceiling."""
    pass


def is_test_session(session: dict) -> bool:
    """True when a session object's testing flag marks it as a test session.
    Tolerates the flag arriving as int, bool, or string across API shapes."""
    val = session.get("testing") if isinstance(session, dict) else None
    return val in (1, True, "1", "true", "True")


def data_access_mode() -> str:
    """Resolve the ceiling from the environment. Unknown values fail safe
    to test_only — the conservative choice for an access ceiling."""
    raw = (os.getenv("FORMR_DATA_ACCESS") or TEST_ONLY).strip().lower()
    if raw in ("all", "real", "any"):
        return ALL
    return TEST_ONLY


def resolve_testing(requested: bool | None) -> bool | None:
    """Map a requested testing filter to the effective one under the ceiling.

    Returns the testing value to enforce (True=test only, False=real only,
    None=no filter / both). Raises DataAccessError if the request asks for
    real data the ceiling forbids.
    """
    mode = data_access_mode()
    if mode == TEST_ONLY:
        if requested is False:
            raise DataAccessError(
                "This MCP is configured FORMR_DATA_ACCESS=test_only; real "
                "participant data is not accessible. Only test sessions "
                "(testing=1) can be read. Set FORMR_DATA_ACCESS=all on the "
                "MCP server and restart to enable real-data access."
            )
        # None (both) or True (test) both collapse to "test only".
        return True
    # mode == ALL: pass the caller's intent through verbatim.
    return requested


async def _resolve_session_codes(client: "FormrClient", run: str, testing: bool) -> list[str]:
    """All session codes for `run` matching the testing flag, paged."""
    codes: list[str] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        page = await client.get_sessions(
            run, testing=testing, limit=_SESSION_PAGE, offset=offset
        )
        if not isinstance(page, list) or not page:
            break
        codes.extend(s["session"] for s in page if isinstance(s, dict) and s.get("session"))
        if len(page) < _SESSION_PAGE:
            break
        offset += _SESSION_PAGE
    return codes


async def get_results_gated(
    client: "FormrClient",
    run: str,
    *,
    surveys: list[str] | None = None,
    sessions: list[str] | None = None,
    items: list[str] | None = None,
    testing: bool | None = None,
) -> dict:
    """Fetch run results with the test/real ceiling enforced.

    Because `/results` cannot filter by testing, when a testing constraint
    applies we resolve the matching session codes first and pass them as
    the `sessions` filter (intersecting any caller-supplied sessions).
    """
    effective = resolve_testing(testing)

    if effective is None:
        # mode=all, no testing constraint — pass through unchanged.
        return await client.get_results(
            run, surveys=surveys, sessions=sessions, items=items
        )

    allowed = await _resolve_session_codes(client, run, effective)
    if sessions:
        allowed_set = set(allowed)
        allowed = [s for s in sessions if s in allowed_set]
    if not allowed:
        # No sessions match the constraint (e.g. no test sessions yet) —
        # return the empty shape rather than calling /results unfiltered
        # (an empty `sessions` filter would return ALL sessions).
        return {}
    return await client.get_results(
        run, surveys=surveys, sessions=allowed, items=items
    )
