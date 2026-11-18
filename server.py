from __future__ import annotations

import asyncio
import base64
import binascii
import http.cookiejar
import json
import logging
import mimetypes
import os
import re
import weakref
import webbrowser
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Literal

import httpx
from pydantic import Field

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context
from mcp.types import ToolAnnotations
from starlette.requests import Request

# Load .env before importing project modules — they read env vars at import time
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

from formr_mcp.auth import AuthError, check_credentials
from formr_mcp.client import (
    FormrAnswerLost,
    FormrAuthExpired,
    FormrClient,
    FormrClientError,
    FormrConflict,
    FormrPermissionError,
    resolve_timeout,
)
from formr_mcp import data_access as gate
from formr_mcp import documentation as doc
from formr_mcp import google_sheets as gsheets
from formr_mcp import logsetup

logsetup.setup_logging()
log = logging.getLogger("formr_mcp.server")
from formr_mcp import patterns as patterns_lib
from formr_mcp.analysis import analyze_run as run_analysis
from formr_mcp.editing import (
    add_run_unit as editing_add_run_unit,
    duplicate_run_units as editing_duplicate_run_units,
    remove_run_unit as editing_remove_run_unit,
    renormalize_positions as editing_renormalize_positions,
    shift_run_positions as editing_shift_run_positions,
)
from formr_mcp.summarize import find_items, summarize_run_structure
from formr_mcp.utils import (
    MEMORY_STRUCTURES,
    run_filepath,
    save_structure,
    validate_run_name,
    validate_new_run_name,
    workspace_file,
)
from formr_mcp.validation import SURVEY_UNIT_TYPES, get_unit_type_schemas, validate_structure

RunName = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{2,44}$", min_length=3, max_length=45)]

BASE_URL = os.getenv("FORMR_BASE_URL", "")
CLIENT_ID = os.getenv("FORMR_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("FORMR_CLIENT_SECRET", "")
FLOWCHART_URL = os.getenv("FLOWCHART_URL", "https://formr-flowchart-test.tim-seidel.workers.dev")

VALID_SETTINGS = {
    "title", "description", "footer_text", "public_blurb",
    "privacy", "tos", "header_image_path", "custom_css", "custom_js",
    "custom_r", "cron_active", "expiresOn",
    "expire_cookie_value", "expire_cookie_unit", "public", "locked",
}

# OAuth scope each API-backed tool needs. Used by whoami to report which
# tools this token can actually use. Local/file/doc tools are omitted
# (they need no API scope and are always available).
TOOL_SCOPES = {
    "list_runs": "run:read",
    "get_run": "run:read",
    "get_run_structure_to_file": "run:read",
    "create_run": "run:write",
    "delete_run": "run:delete",
    "update_run_settings": "run:write",
    "update_run_structure_from_file": "run:write",
    "list_sessions": "session:read",
    "get_session": "session:read",
    "list_unit_sessions": "session:read",
    "get_run_results": "data:read",
    "list_run_files": "file:read",
    "upload_run_file": "file:write",
    "delete_run_file": "file:write",
    "create_test_sessions": "session:write",
    "test_session_action": "session:write",
    "get_run_logs": "data:read",
    "get_survey_structure": "survey:read",
    "get_run_structure": "run:read",
    "update_run_structure": "run:write",
}
# Over HTTP these read a run's structure from formr (run:read), and these edit
# and upload it (run:write, and run:read: they read it first, and an upload is
# checked by reading it back).
_HTTP_STRUCTURE_READ_TOOLS = ("summarize_run", "find_run_items", "analyze_run")
_HTTP_STRUCTURE_EDIT_TOOLS = ("add_run_unit", "remove_run_unit", "duplicate_run_units",
                              "shift_run_positions", "renormalize_positions")
_WRITE_SCOPES = {"run:write", "run:delete", "survey:write", "session:write", "user:write", "file:write"}
# Tools that also call an endpoint of another scope. They read first where
# they can (create_test_sessions, test_session_action), and where the read
# comes after the write (update_run_settings, upload_run_file) a refused read
# is no failure: the write is reported done.
_ALSO_NEEDS = {
    "get_run_logs": "session:read",
    "create_test_sessions": "run:read",
    "upload_run_file": "file:read",
    "test_session_action": "session:read",
    # it answers with the run as saved (GET), after the PATCH
    "update_run_settings": "run:read",
}


def _build_capability_report(caps: dict, token_scopes: list[str]) -> dict:
    """Turn raw /user/me capabilities into a usable report: granted scopes,
    a plain-English access summary, the data ceiling, and a per-tool map."""
    scopes = caps.get("scopes") if isinstance(caps.get("scopes"), list) else token_scopes
    scope_set = set(scopes or [])
    allowed_runs = caps.get("allowed_runs")  # None = unrestricted, else [{id,name}]
    mode = gate.data_access_mode()

    parts = []
    if scope_set & _WRITE_SCOPES:
        parts.append("read-write")
    else:
        parts.append("READ-ONLY (no write scopes)")
    if allowed_runs:
        names = ", ".join(r.get("name", "?") for r in allowed_runs)
        parts.append(f"limited to run(s): {names}")
    else:
        parts.append("all owned runs")
    summary = "; ".join(parts)

    tool_scopes = dict(TOOL_SCOPES)
    if HTTP_MODE:
        tool_scopes.update({tool: "run:read" for tool in _HTTP_STRUCTURE_READ_TOOLS})
        tool_scopes.update({tool: "run:write" for tool in _HTTP_STRUCTURE_EDIT_TOOLS})
    uploads = ("update_run_structure", "update_run_structure_from_file",
               *(_HTTP_STRUCTURE_EDIT_TOOLS if HTTP_MODE else ()))

    tool_access = {}
    for tool, scope in tool_scopes.items():
        if HTTP_MODE and tool in _LOCAL_ONLY_TOOLS:
            continue
        if tool in uploads and scope in scope_set and "run:read" not in scope_set:
            tool_access[tool] = "blocked: requires 'run:read' and 'run:write' scopes"
        elif scope not in scope_set:
            if tool == "delete_run" and "run:write" in scope_set:
                # formr before v2.0.0-rc13 has no run:delete: run:write deletes there
                tool_access[tool] = ("requires 'run:delete' scope (on formr before v2.0.0-rc13, "
                                     "run:write, which this token has)")
            else:
                tool_access[tool] = f"blocked: requires '{scope}' scope"
        elif tool in _ALSO_NEEDS and _ALSO_NEEDS[tool] not in scope_set:
            tool_access[tool] = f"blocked: requires '{_ALSO_NEEDS[tool]}' and '{scope}' scopes"
        elif tool == "get_run_results" and "session:read" not in scope_set:
            if mode == gate.TEST_ONLY:
                # test_only finds the run's test sessions first (data_access.get_results_gated)
                tool_access[tool] = ("blocked: requires 'session:read' and 'data:read' scopes "
                                     "(FORMR_DATA_ACCESS=test_only looks up the test sessions first)")
            else:
                # a testing filter finds the matching sessions first (data_access.get_results_gated)
                tool_access[tool] = ("available without `testing`; testing=true/false requires "
                                     "'session:read' too (it looks up the sessions first)")
        elif tool in ("get_run_results", "get_run_logs") and mode == gate.TEST_ONLY:
            tool_access[tool] = "available: TEST sessions only (FORMR_DATA_ACCESS=test_only)"
        else:
            tool_access[tool] = "available"

    return {
        "scopes": sorted(scope_set),
        "access_summary": summary,
        "data_access_mode": mode,
        "allowed_runs": allowed_runs,
        "tool_access": tool_access,
    }


# Set by http_server.configure_http_mode(): every caller brings its own formr
# token (the connection its researcher made by signing in to formr), checked
# by FormrTokenVerifier, and the server keeps no workspace and no credential.
HTTP_MODE = False
_shared_http: httpx.AsyncClient | None = None


def shared_http() -> httpx.AsyncClient:
    """One connection pool for every caller's requests to formr (HTTP mode).
    It holds no caller's state: each request carries its caller's token, and
    it keeps no cookie (formr's session cookie, kept, would put every caller in
    one PHP session, whose lock serialises them all)."""
    global _shared_http
    if _shared_http is None:
        _shared_http = httpx.AsyncClient(
            timeout=resolve_timeout(),
            cookies=http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])))
    return _shared_http


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[FormrClient | None]:
    if HTTP_MODE:
        yield None
        return
    err = check_credentials(BASE_URL, CLIENT_ID, CLIENT_SECRET)
    if err:
        # Logged to stderr (not print -> stdout, which would corrupt the
        # stdio JSON-RPC channel). Collapse the multi-line guidance to keep
        # it on one record.
        log.warning("AUTHENTICATION NOT CONFIGURED — %s", " ".join(err.split()))
        log.warning("MCP tools will fail with auth errors until this is resolved.")

    client = FormrClient(BASE_URL, CLIENT_ID, CLIENT_SECRET)
    try:
        yield client
    finally:
        await client.aclose()


mcp = FastMCP(
    "formr-mcp",
    lifespan=lifespan,
    instructions="""You help design and manage formr survey runs and their structure.

SETUP: The user must configure a .env file with formr API credentials:
  FORMR_BASE_URL, FORMR_CLIENT_ID, FORMR_CLIENT_SECRET
Scopes are fixed per credential. Design/edit needs run:read + run:write; reading
data needs session:read (sessions), data:read (results), file:read (files),
survey:read (item defs). A token may be READ-ONLY or limited to specific runs.
Call whoami FIRST — it reports this token's granted scopes, run allowlist, and
which tools are usable. If tools return auth errors, .env is missing/misconfigured.

CAPABILITIES & LIMITS: Tokens carry fixed scopes (read-only vs read-write) and an
optional run allowlist (e.g. a single run). whoami surfaces all of this so you can
tell the user up front what they can do, instead of hitting opaque 403s.

READING DATA — six read tools inspect a live run (handy for designing/debugging):
  list_sessions / get_session     who is in the run, position, testing flag
  list_unit_sessions              per-unit history: progress, dropout, trajectories
  get_run_results                 raw survey responses (filter by survey/session/item)
  list_run_files                  uploaded-file metadata
  get_survey_structure            item tables + choice lists (definitions, not data)
  get_run_logs                    the engine's logs: unit results + R errors, email, push
TESTING A RUN — create_test_sessions(name) returns test codes with their links; open a link
in a browser (Playwright) to take the run as that participant; test_session_action moves a
test session on (advance past a Pause, move_to_position, execute); then list_unit_sessions,
get_run_results and get_run_logs show what happened. test_session_action refuses real participants' sessions.
RUN FILES — upload_run_file (from the workspace, text or base64) / delete_run_file.
DATA GATE (GDPR): FORMR_DATA_ACCESS sets a HARD ceiling on what this server's read tools
return, default `test_only` — they return test sessions (testing=1) only and refuse real
participant data until the server's operator sets FORMR_DATA_ACCESS=all and restarts. For
full-scale analysis prefer emitting R code for RStudio (see data-access docs).

formr is a survey framework for psychology research. Runs are ordered
compositions of units (Surveys, Pages, Emails, Branches, etc.) where
execution flows by position number. Branching uses R expressions in
`condition` and jumps to the `if_true` position.

WORKFLOW — editing run structures, two paths:
  With filesystem access (a coding agent or editor: Claude Code, Codex, Cursor, VS Code)
  — preferred, best for large runs:
    1. get_run_structure_to_file(name) → .formr/<name>.json (backs up existing)
    2. edit .formr/<name>.json with Read/Edit
    3. update_run_structure_from_file(name) → validates and uploads
  Without filesystem access (a chat app such as Claude or ChatGPT) — inline JSON text:
    1. get_run_structure(name) → returns the structure as JSON text
    2. edit that JSON in your reply
    3. update_run_structure(name, structure_json) → validates and uploads
  Keep each unit's run_unit_id and the structure's "version" as fetched: formr keeps those
  units, and their participants stay on them, and refuses an upload made from a copy that has
  changed since. Leave run_unit_id out for a new unit, never copy it. Removing a unit
  participants are in needs confirm_removal=true, which only the user's go-ahead justifies.

PATTERNS — For complex runs (condition/covariate balancing, waiting rooms, loading screens,
live aggregate feedback, adaptive loops, personalized emails, external API/SMS calls, DRY R
functions), call list_patterns() then get_pattern(name) to learn the proven approach (structure
+ R idioms + gotchas) instead of re-deriving it. Patterns inform; you build the units. See
get_documentation("patterns") and get_documentation("custom-r-and-secrets").

DATA ACCESS — survey_unit_sessions / survey data frames = current participant only.
For all-participant data: formr_api_authenticate() with NO ARGUMENTS works in ALL run
R contexts (conditions, item values/showif, labels, page/email bodies, External units)
— formr auto-injects a run-scoped token (180 s, data:read). Exception: email subjects
/ push titles (plaintext, no R eval). For a DIFFERENT run: store credentials as run
secrets and pass them explicitly. Never use formr_connect() / formr_raw_results().
See get_documentation("data-access").""",
)

# Log every tool call (name, args, duration, success/error) to stderr.
logsetup.install_tool_logging(mcp)


def _client(ctx: Context) -> FormrClient:
    """The formr client for this call: over stdio the one of the .env
    credential; over HTTP one with the caller's own token, taken from this
    very request (not the session's first: the client refreshes its token)."""
    if not HTTP_MODE:
        return ctx.request_context.lifespan_context
    user = getattr(ctx.request_context.request, "user", None)
    access = getattr(user, "access_token", None)
    if access is None:
        raise FormrAuthExpired("This request carries no formr connection. Connect the app to formr.")
    forget = getattr(getattr(mcp, "_token_verifier", None), "forget", None)
    return FormrClient(BASE_URL, http_client=shared_http(), access_token=access.token,
                       on_token_refused=(lambda: forget(access.token)) if forget else None)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def whoami(ctx: Context = None) -> dict:
    """Identify the authenticated user AND report this token's capabilities.

    Call this first. Returns the user profile plus a `capabilities` block:
      - scopes: the OAuth scopes this token was granted
      - access_summary: plain-English (e.g. "READ-ONLY; limited to run(s): foo")
      - data_access_mode: test_only (default) or all — the GDPR ceiling
      - allowed_runs: null if unrestricted, else the runs this token may touch
      - tool_access: each data/run tool -> available | blocked (missing scope) | test-only

    Use this to tell the user up front what they can and cannot do, rather than
    discovering limits through 403 errors mid-task.
    """
    client = _client(ctx)
    caps = await client.get_capabilities()
    token_scopes = await client.scopes()
    profile = {k: v for k, v in caps.items() if k not in ("scopes", "allowed_runs", "admin")}
    if "admin" in caps:
        profile["admin"] = caps["admin"]
    profile["capabilities"] = _build_capability_report(caps, token_scopes)
    return profile


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_runs(name: RunName | None = None, ctx: Context = None) -> list[dict]:
    """List all runs. Optionally filter by exact name."""
    if name is not None:
        validate_run_name(name)
    return await _client(ctx).get_runs(name)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def create_run(name: RunName, ctx: Context = None) -> dict:
    """Create a new run. Name must start with a letter, contain only letters, digits, hyphens, and be 3-45 chars.

    Returns the created run name and link on success. Requires `run:write` OAuth scope.
    """
    validate_new_run_name(name)
    client = _client(ctx)
    return await client.create_run(name)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def delete_run(name: RunName, ctx: Context = None) -> str:
    """Permanently delete a run and all its data.
    """
    validate_run_name(name)
    await _client(ctx).delete_run(name)
    return f"Run '{name}' has been deleted."


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_run(name: RunName, ctx: Context = None) -> dict:
    """Get a single run by exact name."""
    validate_run_name(name)
    return await _client(ctx).get_run(name)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def update_run_settings(name: RunName, settings: dict, ctx: Context = None) -> dict:
    """Update a run's settings. Pass only the settings you want to change.

    Common settings: title, description, public (0=admin/test-only, 2=link-accessible), locked (0/1),
    custom_css, custom_js, custom_r, cron_active, expiresOn, footer_text, public_blurb, privacy,
    tos, header_image_path, expire_cookie_value, expire_cookie_unit.

    custom_r holds run-level R functions/globals injected before every R evaluation — define
    repeating helpers here once and call them by name across units (DRY). See
    get_documentation("custom-r-and-secrets"). Note: secret VALUES are write-only and set in the
    admin UI, not here; the structure export lists their names under settings.secrets.

    Returns the full updated run with all settings.
    """
    validate_run_name(name)
    val = settings.get("use_material_design")
    if val is not None and val != 0:
        raise ValueError(
            "'use_material_design' must be 0 or omitted. "
            "Material Design is a legacy theme that is not supported. "
            "Use custom_css to style the survey instead."
        )
    settings.pop("use_material_design", None)
    unknown = set(settings) - VALID_SETTINGS
    if unknown:
        raise ValueError(
            f"Unknown settings: {', '.join(sorted(unknown))}. "
            f"Valid settings: {', '.join(sorted(VALID_SETTINGS))}"
        )
    client = _client(ctx)
    if HTTP_MODE:
        # not in the middle of a unit tool's fetch, edit, upload of this run,
        # whose upload carries the run's settings as they were
        async with _run_edit_lock(name, _caller_account(ctx)):
            answer = await client.patch_run(name, settings)
    else:
        answer = await client.patch_run(name, settings)
    # formr saves what it can and says what it skipped or changed (an expiry
    # date in the past, unreadable or beyond the maximum, a file it could not
    # write, a public run made private): the run read back does not say why
    warnings = answer.get("warnings") if isinstance(answer, dict) else None
    try:
        run = await client.get_run(name)
    except FormrPermissionError:
        # saved all the same: a 403 here was taken for a failed save, and repeated
        if not warnings:
            return {"name": name, "updated": sorted(settings),
                    "note": "Saved. Reading the run back needs the 'run:read' scope, which this token lacks."}
        return {"name": name, "updated": sorted(settings), "warnings": warnings,
                "note": ("Saved, but formr skipped or changed some of it: see warnings. Reading the "
                         "run back needs the 'run:read' scope, which this token lacks.")}
    except (FormrClientError, httpx.HTTPError) as e:
        said = f" (formr says: {warnings})" if warnings else ""
        raise ValueError(
            f"Run '{name}': formr saved the settings{said}, but reading the run back failed ({e}). "
            "Do not repeat the change: get_run shows the run as saved."
        ) from e
    if warnings and isinstance(run, dict):
        return {**run, "warnings": warnings}
    return run


def _format_size(bytes: int) -> str:
    if bytes < 1024:
        return f"{bytes} B"
    if bytes < 1024 * 1024:
        return f"{bytes / 1024:.1f} KB"
    return f"{bytes / 1024 / 1024:.1f} MB"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_run_structure_to_file(name: RunName, ctx: Context = None) -> str:
    """Download the full run structure to .formr/<name>.json. Use this before editing.

    If the file already exists, the previous version is backed up to .formr/<name>.json.bak.
    Returns a short summary — the full structure is on disk, not in the response.
    """
    validate_run_name(name)
    client = _client(ctx)
    filepath = run_filepath(name)
    bak_path = filepath.with_suffix(".json.bak")

    if filepath.exists():
        os.replace(str(filepath), str(bak_path))

    structure = await client.get_run_structure(name)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2)

    units = len(structure.get("units", []))
    size = os.path.getsize(filepath)
    backup_note = f"\nBackup saved to {bak_path}" if bak_path.exists() else ""

    return f"Run '{name}': wrote {units} units ({_format_size(size)}) to {filepath}{backup_note}"


def _normalize_survey_choices(structure: dict) -> None:
    """Ensure inline survey items with 'choices' also carry 'choice_list' (set to
    the item's name), matching the round-trip format that formr's createFromData()
    expects.  Without this, items with choices but no choice_list are silently
    dropped by the server."""
    for unit in structure.get("units", []):
        # a type that is no string (a list: unhashable) is the validator's to report
        if isinstance(unit.get("type"), str) and unit["type"] in SURVEY_UNIT_TYPES and isinstance(unit.get("survey_data"), dict):
            items = unit["survey_data"].get("items", [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("choices") and not item.get("choice_list"):
                        item_name = item.get("name")
                        if item_name:
                            item["choice_list"] = item_name


def _unit_signature(structure: dict) -> list[tuple]:
    """(position, type) per unit. Page is exported as Endpage, and a unit placing
    a study as Survey or Form by the study's engine, whatever it was sent as:
    compare each pair as one."""
    sig = []
    for u in structure.get("units", []) or []:
        if isinstance(u, dict):
            t = {"Page": "Endpage", "Form": "Survey"}.get(u.get("type"), u.get("type"))
            pos = u.get("position")
            # formr accepts numeric strings; compare "10" and 10 as one position.
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                pass
            sig.append((pos, t))
    return sorted(sig, key=lambda x: (not isinstance(x[0], int), x[0] if isinstance(x[0], int) else 0, str(x)))


async def _upload_structure(client: FormrClient, name: str, structure: dict,
                            confirm_removal: bool = False,
                            confirm_hint: str = "upload again with confirm_removal=true",
                            stale_hint: str = "Fetch it again and make the change on that") -> tuple[str, dict]:
    """Validate a run structure and PUT it, then check what formr stored.
    Returns the message and the stored structure (its new units now have
    their run_unit_id, and it carries formr's new version). confirm_hint: how
    the caller's tool confirms a removal formr refused; stale_hint: what it
    does when formr says the copy is stale."""
    _normalize_survey_choices(structure)
    errors = validate_structure(structure)
    if errors:
        raise ValueError(
            "Structure validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    # formr also takes the go-ahead from the body: only the tool's parameter,
    # which the user sees, gives it
    structure.pop("confirm_removal", None)
    try:
        answer = await client.put_run_structure(name, structure, confirm_removal=confirm_removal)
    except FormrConflict as e:
        units = e.body.get("units_in_use")
        if units:
            # formr saves a survey's changed items before the structure
            saved = [str(s) for s in (e.body.get("surveys_saved") or [])]
            done = ("nothing was uploaded" if not saved else
                    "the run's units did not change, but the items of the survey(s) "
                    f"{', '.join(saved)} were saved before that")
            raise ValueError(
                f"Run '{name}': {done}. It would remove units that participants are in: "
                f"{_units_in_use(units)}. Their current step there would end, and they would go on to what "
                "then stands in its place. If a unit was left out by mistake, keep it (with its run_unit_id). "
                "Otherwise tell the user which units, and that participants are at them; only with their "
                f"go-ahead, {confirm_hint}."
                + (" The saved items changed the run's version: fetch the run again and make the change on "
                   "that, or formr refuses the copy as stale." if saved else "")
            ) from e
        if "version" in e.body:
            # the version covers the run's units and settings (not its files)
            raise ValueError(
                f"Run '{name}': nothing was uploaded. The run's units or settings have changed in formr since "
                "it was fetched (in formr's editor, by another upload, or by update_run_settings). "
                f"{stale_hint}: uploading the old copy would undo those changes. (A structure copied "
                'from another run carries that run\'s "version": leave "version" out to put it in this one.)'
            ) from e
        raise
    except FormrClientError as e:
        # A refusal (401, 403, another 4xx, formr's own 500) changed nothing.
        # An answer lost on the way back (a time-out, a dropped connection),
        # or a proxy's 502-504 while formr_app restarted, may have come after
        # formr applied it: the edit tools fetch afresh, so a retry would
        # apply the change a second time.
        if isinstance(e, FormrAnswerLost) or e.status in (502, 503, 504):
            raise ValueError(
                f"Run '{name}': the upload's answer was lost ({e}). formr may have applied it: fetch the "
                "structure and check before repeating the change, or it is made twice."
            ) from e
        raise
    try:
        stored = await client.get_run_structure(name)
    except (FormrClientError, httpx.HTTPError) as e:
        raise ValueError(
            f"Run '{name}': formr applied the upload ({_merge_summary(answer)}), but reading it back "
            f"failed ({e}). Do not repeat the change: fetch the structure to see it."
        ) from e
    _verify_upload(name, structure, stored)
    units = len(stored.get("units", []))
    message = f"Run '{name}': successfully updated ({units} units). " + _merge_summary(answer)
    if isinstance(answer, dict) and answer.get("warnings"):
        warnings = str(answer["warnings"])
        if gate.data_access_mode() == gate.TEST_ONLY:
            # formr's counts (pending item views after a changed showif) take
            # in real participants too
            warnings = re.sub(r"\b\d+\b", "#", warnings) + " (counts hidden: FORMR_DATA_ACCESS=test_only)"
        message += f" formr says: {warnings}"
    if structure.get("version") is None and isinstance(stored, dict) and stored.get("version"):
        # formr checks for a stale copy only when the upload names the version
        # it was made from (StructureResource: isset($jsonData->version)); a
        # formr from before v2.0.0-rc13 exports none, and there is nothing to say
        message += (' The upload had no "version", so formr did not check whether the run had changed since '
                    "it was fetched: any change made in formr since then is undone. Keep the "
                    '"version" as fetched.')
    return message, stored


def _units_in_use(units: list) -> str:
    """The units a refused upload would remove. How many participants are at
    each only where real participants' data may reach the model."""
    show_counts = gate.data_access_mode() != gate.TEST_ONLY
    return "; ".join(
        f"{u.get('type')} at position {u.get('position')}"
        + (f" ({u.get('participants')} participant(s))" if show_counts else "")
        for u in units if isinstance(u, dict))


def _merge_summary(answer) -> str:
    """What formr's upload answer says it did to the run and its participants
    (their numbers only where real participants' data may reach the model)."""
    if not isinstance(answer, dict) or "kept" not in answer:
        # a formr from before v2.0.0-rc13 replaces the whole structure
        return ("This formr replaced the whole structure and ended every participant's current "
                "step: each starts it again (a survey from its first page, a Pause from its "
                "start) at their next visit.")
    n = {k: int(answer.get(k) or 0) for k in
         ("kept", "moved", "created", "removed", "participants_moved", "steps_ended")}
    text = (f"Kept {n['kept']} unit(s) in place ({n['moved']} moved), created {n['created']}, "
            f"removed {n['removed']}.")
    show_counts = gate.data_access_mode() != gate.TEST_ONLY
    if n["participants_moved"]:
        text += (f" {n['participants_moved']} participant(s)" if show_counts else " Participants") + \
            " moved along with their unit."
    if n["steps_ended"]:
        text += (f" {n['steps_ended']} participant step(s)" if show_counts else " Participants' steps") + \
            " at removed units ended; formr moves them on to what now stands there."
    return text


# Over HTTP an edit of a run is fetch, edit, upload (a whole-structure PUT):
# two at once (parallel tool calls) would both edit the same fetched
# structure, and the later upload would drop the earlier edit. One edit of a
# run at a time, in this process, per account: only its owner's connections
# can edit a run, and another researcher's calls with its name (formr refuses
# them, but only after they held the lock) must not hold the owner's edits
# up. A lock lives while a call holds it: the map keeps no name nobody is
# editing.
_RUN_EDIT_LOCKS: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


def _caller_account(ctx: Context | None) -> str:
    """The formr account whose connection made this request (HTTP mode: the
    token's subject, FormrTokenVerifier), or '' where there is none."""
    try:
        subject = ctx.request_context.request.user.access_token.subject
    except (AttributeError, ValueError):  # no request: no caller
        return ""
    return str(subject) if isinstance(subject, (str, int)) else ""


def _run_edit_lock(name: str, account: str = "") -> asyncio.Lock:
    validate_run_name(name)
    key = (account, name.lower())
    lock = _RUN_EDIT_LOCKS.get(key)
    if lock is None:
        lock = _RUN_EDIT_LOCKS[key] = asyncio.Lock()
    return lock


async def _caller_gone(ctx: Context | None) -> bool:
    """Whether the HTTP client that sent this tool call has hung up. The SDK
    does not cancel a stateless JSON call whose client went, nor can the
    client's notifications/cancelled reach it (another request, another
    session): its upload would land after the client gave up, and the
    client's retry, fetching afresh, would make the change a second time."""
    try:
        request = ctx.request_context.request
    except (AttributeError, ValueError):  # no request: stdio
        return False
    return isinstance(request, Request) and await request.is_disconnected()


@asynccontextmanager
async def _run_structure(ctx: Context, name: str, *, upload: bool = False, confirm_removal: bool = False,
                         confirm_hint: str = "remove them with remove_run_unit(name, position, confirm_removal=true)"):
    """Over HTTP the server keeps no workspace: the run's structure is fetched
    with the caller's token for this one call (utils.MEMORY_STRUCTURES), so
    formr decides whether the caller may see it, and an edit is validated and
    uploaded after (yields a list that receives the upload's message). formr
    keeps the units that come back with their run_unit_id; one the edit drops
    is removed, which formr refuses where participants are unless
    confirm_removal. An edit that changed nothing is not uploaded. Over stdio
    the tools use the workspace files as before."""
    uploaded: list[str] = []
    if not HTTP_MODE:
        yield uploaded
        return
    client = _client(ctx)
    lock = _run_edit_lock(name, _caller_account(ctx)) if upload else None
    if lock is not None:
        await lock.acquire()
    try:
        store = {name: await client.get_run_structure(name)}
        fetched = store[name]
        reset = MEMORY_STRUCTURES.set(store)
        try:
            yield uploaded
        finally:
            MEMORY_STRUCTURES.reset(reset)
        if upload and store[name] is not fetched:
            if store[name] == fetched:
                uploaded.append(f"Run '{name}' is unchanged: nothing was uploaded to formr.")
            elif await _caller_gone(ctx):
                raise ValueError(f"Run '{name}': the client hung up before the upload: nothing was uploaded.")
            else:
                message, _ = await _upload_structure(client, name, store[name], confirm_removal=confirm_removal,
                                                     confirm_hint=confirm_hint,
                                                     stale_hint="It changed while this tool ran: call the tool again")
                uploaded.append(message)
    finally:
        if lock is not None:
            lock.release()


def _with_upload(result: str, uploaded: list[str]) -> str:
    """An edit tool's answer, and over HTTP what the upload after it said."""
    return result + (f"\n{uploaded[0]}" if uploaded else "")


def _verify_upload(name: str, sent: dict, stored: dict) -> None:
    """formr can answer 200 for an import it rolled back (the old structure
    had the same unit count). Compare what was sent with what is stored."""
    want, got = _unit_signature(sent), _unit_signature(stored)
    if want != got:
        missing = sorted(set(want) - set(got), key=str)
        extra = sorted(set(got) - set(want), key=str)
        raise ValueError(
            f"Run '{name}': formr accepted the upload but the stored structure differs from what was sent. "
            f"Missing (position, type): {missing or 'none'}; unexpected: {extra or 'none'}. "
            f"The import was likely rejected or partially applied — check the unit types "
            f"(get_unit_types) and fix, then upload again."
        )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def update_run_structure_from_file(name: RunName, confirm_removal: bool = False, ctx: Context = None) -> str:
    """Read a run structure from .formr/<name>.json, validate, and upload to formr.

    formr keeps every unit that keeps its run_unit_id, with its participants;
    a unit without one is new. Removing a unit participants are in is refused
    unless confirm_removal=True: pass it only after the user agreed.
    Returns a summary on success, or validation errors to fix and retry.
    On success the file is rewritten with what formr stored (new units get
    their run_unit_id) and the backup file (.formr/<name>.json.bak) is removed.
    """
    validate_run_name(name)
    filepath = run_filepath(name)
    bak_path = filepath.with_suffix(".json.bak")

    if not filepath.exists():
        raise FileNotFoundError(
            f"No local file for run '{name}' at {filepath}. "
            f"Call get_run_structure_to_file(\"{name}\") first."
        )

    client = _client(ctx)

    with open(filepath, encoding="utf-8") as f:
        structure = json.load(f)

    message, stored = await _upload_structure(
        client, name, structure, confirm_removal=confirm_removal,
        confirm_hint=f'upload again with update_run_structure_from_file("{name}", confirm_removal=true)')
    # the next edit starts from formr's ids and version, or its new units
    # would be new again and the next upload refused as made from an old copy
    save_structure(name, stored)

    if bak_path.exists():
        bak_path.unlink()

    return message


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_run_structure(name: RunName, ctx: Context = None) -> str:
    """Fetch a run's full structure and return it as JSON text (NOT written to a file).

    Use this when you don't have filesystem access — e.g. a chat app such as Claude
    or ChatGPT. Edit the returned JSON inline, then push it back with
    update_run_structure. For editor/CLI workflows with file access, prefer
    get_run_structure_to_file + update_run_structure_from_file (better for large runs).
    """
    validate_run_name(name)
    structure = await _client(ctx).get_run_structure(name)
    # compact: C encoder, and ~1/3 fewer characters for the model to read
    return json.dumps(structure, ensure_ascii=False, separators=(",", ":"))


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def update_run_structure(name: RunName, structure_json: str, confirm_removal: bool = False,
                               ctx: Context = None) -> str:
    """Validate a run structure passed as JSON text and upload it to formr.

    Inline counterpart to get_run_structure for clients without filesystem access.
    Pass the FULL structure as a JSON string — an object with a "units" array. It is
    validated exactly like the file-based path; on failure you get the errors to fix
    and retry (nothing is uploaded). Keep each unit's run_unit_id as fetched: formr
    keeps those units, with their participants; leave it out for a new unit, and
    never copy it. Keep "version": formr refuses a copy that has changed since.
    Removing a unit participants are in is refused unless confirm_removal=True: pass
    it only after the user agreed. Fetch the structure again before the next edit
    (new units get their run_unit_id, the run a new version). For editor/CLI
    workflows, update_run_structure_from_file is usually more convenient.
    """
    validate_run_name(name)
    try:
        structure = json.loads(structure_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"structure_json is not valid JSON: {e}")
    if not isinstance(structure, dict) or "units" not in structure:
        raise ValueError("Run structure must be a JSON object with a 'units' array.")

    client = _client(ctx)
    if HTTP_MODE:
        # not in the middle of a unit tool's fetch, edit, upload of this run
        async with _run_edit_lock(name, _caller_account(ctx)):
            message, _ = await _upload_structure(client, name, structure, confirm_removal=confirm_removal,
                                                 confirm_hint="upload again with confirm_removal=true")
            return message
    message, _ = await _upload_structure(client, name, structure, confirm_removal=confirm_removal,
                                         confirm_hint="upload again with confirm_removal=true")
    return message


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_unit_types(ctx: Context = None) -> dict:
    """Get all supported run unit types and their required/optional fields. Use this to understand what can be created."""
    return get_unit_type_schemas()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_documentation(
    topic: Literal["item-types", "run-concepts", "r-code", "survey-json", "examples", "best-practices", "editing-tools", "unit-types-advanced", "data-access", "patterns", "custom-r-and-secrets", "form-v2"],
    section: str | None = None,
    ctx: Context = None,
) -> str:
    """Get formr design documentation. Call get_documentation_sections(topic) first to list
    available section names, then pass section= to retrieve just that H2 block.
    Omit section to get the full topic."""
    return doc.get_documentation(topic, section)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_documentation_topics(ctx: Context = None) -> list[dict]:
    """List all available documentation topics."""
    return doc.get_topics()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_documentation_sections(
    topic: Literal["item-types", "run-concepts", "r-code", "survey-json", "examples", "best-practices", "editing-tools", "unit-types-advanced", "data-access", "patterns", "custom-r-and-secrets", "form-v2"],
    ctx: Context = None,
) -> list[dict]:
    """List the H2 sections available within a documentation topic.
    Use this before get_documentation to find the specific section you need,
    then pass its name as the section argument."""
    return doc.get_sections(topic)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_patterns(ctx: Context = None) -> list[dict]:
    """List the available run-design patterns for complex runs.

    Each entry has a name, a title, and the problem it solves. When a request matches one
    (balancing/condition assignment, waiting rooms, loading screens, live aggregate feedback,
    adaptive loops, personalized emails, external API/SMS calls, DRY R functions), call
    get_pattern(name) to learn the approach instead of re-deriving it from scratch."""
    return patterns_lib.list_patterns()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_pattern(name: str, ctx: Context = None) -> dict:
    """Get a run-design pattern's approach: the problem it solves, when to use it, the
    structure (which units/fields), how it works, the reusable R idioms (key_r), and gotchas.

    Informational, not a copy-paste template — real runs differ too much for a fixed unit
    blueprint. Read it, then build the units adapted to the run with add_run_unit / Edit,
    reusing the key_r snippets and renaming to the run's real survey/item names. Finish with
    analyze_run + update_run_structure_from_file. Call list_patterns() first to see names."""
    return patterns_lib.get_pattern(name)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def summarize_run(name: RunName, detail: Literal["units", "items"] = "items", ctx: Context = None) -> str:
    """Summarize a run structure from the local file. Returns a readable overview of units and their items.

    Locally (stdio) call get_run_structure_to_file(name) first; a hosted server reads the run from formr itself.

    Use detail='units' for just unit-level info (no items), or detail='items' (default) to include all survey items.
    Strips HTML from labels for readability.
    """
    validate_run_name(name)
    async with _run_structure(ctx, name):
        return summarize_run_structure(name, detail=detail)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def find_run_items(name: RunName, query: str | None = None, item_type: str | None = None, ctx: Context = None) -> str:
    """Search for items across all surveys in a run. Returns matching items with survey context.

    Locally (stdio) call get_run_structure_to_file(name) first; a hosted server reads the run from formr itself.

    Filters:
    - query: search item names and labels (case-insensitive substring match)
    - item_type: filter by item type (e.g. 'mc', 'text', 'note', 'calculate')
    At least one filter is recommended; both can be combined.
    """
    validate_run_name(name)
    async with _run_structure(ctx, name):
        return find_items(name, query=query, item_type=item_type)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def analyze_run(name: RunName, ctx: Context = None) -> str:
    """Analyze a run structure for errors and warnings. Checks R syntax, variable references, branch flow, item consistency, and common mistakes.

    Locally (stdio) call get_run_structure_to_file(name) first; a hosted server reads the run from formr itself.

    Returns a structured report with error/warning counts. R syntax validation requires R to be installed.
    """
    validate_run_name(name)
    async with _run_structure(ctx, name):
        # off the event loop: its R syntax check starts R once per expression,
        # which held up every other researcher's calls (a thread carries the
        # structure fetched for this call, MEMORY_STRUCTURES, along)
        return await asyncio.to_thread(run_analysis, name)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def add_run_unit(name: RunName, unit_type: str, position: int, description: str = "",
                insert_mode: Literal["shift", "overwrite"] = "shift",
                unit_fields: dict | None = None, confirm_removal: bool = False, ctx: Context = None) -> str:
    """Add a unit to a run structure. Locally the file must exist (fetch with get_run_structure_to_file first); a hosted server edits the run in formr, validated and uploaded in one step.

    unit_fields — unit-type-specific fields (see get_unit_types() for full schema):
      SkipForward/SkipBackward: condition (R expr), if_true (int position)
      Email: subject, body, account_id (int), recipient_field
      Survey: study_id (int) or survey_data (dict)
      Form: study_id (int) — a study on form_v2 (its rendering_mode decides; placing it changes nothing)
      External: address (URL or R code)
      Wait: body (int position — NOT display content)
      Page/Endpage: body (markdown)
      Pause: wait_minutes, wait_until_time, wait_until_date, relative_to

    insert_mode: 'shift' (default) shifts units at >= position up by 10; 'overwrite' replaces
    the unit there: one of the same kind (a Page/Endpage, a Survey/Form of the same study) in
    place, keeping its participants, with every field not given set as a new unit has it;
    one of another kind is removed. Position references are auto-remapped. See
    get_documentation("form-v2") for Form units.
    Hosted, formr refuses an overwrite that removes a unit participants are at unless
    confirm_removal=True: tell the user, and pass it only with their go-ahead.
    """
    validate_run_name(name)
    fields = dict(unit_fields or {})
    # what the unit is and where it goes are this tool's own arguments (and a
    # run_unit_id is formr's): set in unit_fields they clashed with them, or
    # turned the unit into another type or placement
    taken = sorted(k for k in fields if k in ("type", "run_unit_id", "position", "insert_mode", "name", "unit_type"))
    if taken:
        raise ValueError(f"unit_fields may not set {', '.join(taken)}: give unit_type, position and insert_mode "
                         "as the tool's arguments, and leave run_unit_id to formr.")
    if "description" in fields:
        if description and fields["description"] != description:
            raise ValueError("description is given twice (as an argument and in unit_fields), differently.")
        description = fields.pop("description")
    async with _run_structure(ctx, name, upload=True, confirm_removal=confirm_removal,
                              confirm_hint="call add_run_unit again with confirm_removal=true") as uploaded:
        result = editing_add_run_unit(name, unit_type, position, description=description,
                                      insert_mode=insert_mode, **fields)
    return _with_upload(result, uploaded)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def remove_run_unit(name: RunName, position: int, compact: bool = False,
                          confirm_removal: bool = False, ctx: Context = None) -> str:
    """Remove a unit at the given position from the local run structure file.

    Locally the file must exist (fetch with get_run_structure_to_file first); a hosted server edits the run in formr, validated and uploaded in one step.
    Locally, call update_run_structure_from_file to upload after removing.
    compact=True shifts higher positions down by 1 and remaps references; dangling references are reported.
    Participants at the removed unit end their step there and go on to what then stands in
    its place. formr refuses to remove a unit participants are at, as its run editor asks
    first: tell the user, and only with their go-ahead call again with confirm_removal=True.
    """
    validate_run_name(name)
    async with _run_structure(ctx, name, upload=True, confirm_removal=confirm_removal,
                              confirm_hint="call remove_run_unit again with confirm_removal=true") as uploaded:
        result = editing_remove_run_unit(name, position, compact=compact)
    return _with_upload(result, uploaded)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def duplicate_run_units(name: RunName, from_positions: list[int], to_start_position: int,
                        shift_existing: bool = True, ctx: Context = None) -> str:
    """Copy units at from_positions to new positions starting at to_start_position in the local run structure file.

    Copies units in original order with gaps of 10 (e.g., 100, 110, 120...).
    shift_existing=True (default) shifts conflicting units up; False errors on conflicts.
    Position references are automatically remapped. Locally the file must exist (fetch with get_run_structure_to_file first); a hosted server edits the run in formr, validated and uploaded in one step.
    """
    validate_run_name(name)
    async with _run_structure(ctx, name, upload=True) as uploaded:
        result = editing_duplicate_run_units(name, from_positions, to_start_position,
                                             shift_existing=shift_existing)
    return _with_upload(result, uploaded)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def shift_run_positions(name: RunName, from_position: int, delta: int, ctx: Context = None) -> str:
    """Shift all units at positions >= from_position by delta in the local run structure file.

    Positive delta shifts up (making room), negative shifts down (closing gaps).
    Position references are automatically remapped. Locally the file must exist (fetch with get_run_structure_to_file first); a hosted server edits the run in formr, validated and uploaded in one step.
    """
    validate_run_name(name)
    async with _run_structure(ctx, name, upload=True) as uploaded:
        result = editing_shift_run_positions(name, from_position, delta)
    return _with_upload(result, uploaded)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def renormalize_positions(name: RunName, spacing: Annotated[int, Field(ge=1)] = 10, ctx: Context = None) -> str:
    """Renumber all unit positions to clean multiples of spacing while preserving order.

    Safe to call on already-clean structures. Position references are automatically remapped.
    Useful after edits that leave gaps (e.g., compact removal shifts by 1).
    Locally the file must exist (fetch with get_run_structure_to_file first); a hosted server edits the run in formr, validated and uploaded in one step.
    """
    validate_run_name(name)
    async with _run_structure(ctx, name, upload=True) as uploaded:
        result = editing_renormalize_positions(name, spacing=spacing)
    return _with_upload(result, uploaded)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def open_flowchart(name: RunName, ctx: Context = None) -> str:
    """Open a flowchart visualization of a run in the browser.

    Uploads the local run structure to the formr Flowchart Generator (an external
    Cloudflare Worker) and returns a shareable URL. The link expires after 24 hours.
    """
    validate_run_name(name)

    filepath = run_filepath(name)

    if not filepath.exists():
        raise FileNotFoundError(
            f"No local file for run '{name}'. "
            f"Call get_run_structure_to_file(\"{name}\") first."
        )

    with open(filepath, encoding="utf-8") as f:
        structure = json.load(f)

    if not isinstance(structure, dict) or "units" not in structure:
        raise ValueError(f"Invalid run structure in {filepath}: missing 'units' key")

    payload = json.dumps(structure, separators=(",", ":"))
    if len(payload) > 1024 * 1024:
        raise ValueError(
            f"Run structure is too large ({len(payload) / 1024:.0f} KB). "
            f"Maximum size is 1 MB."
        )

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{FLOWCHART_URL}/api/share",
            content=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()

    webbrowser.open(result["url"])

    return f"Flowchart link for run '{name}' (expires in 24h):\n\n  {result['url']}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_sessions(name: RunName, active: bool | None = None, testing: bool | None = None,
                        limit: int = 100, offset: int = 0, ctx: Context = None) -> list[dict]:
    """List participant sessions in a run: session code, position, created/last_access/ended,
    testing flag, and current unit. Useful for checking who's in the run and how far they got.

    Filters: active (True=ongoing, False=finished, None=all); testing (True=test, False=real,
    None=both) — but the FORMR_DATA_ACCESS ceiling applies (default test_only forces test-only
    and refuses testing=False). Paginate with limit (max 10000) / offset. Scope: session:read.
    """
    validate_run_name(name)
    effective = gate.resolve_testing(testing)
    return await _client(ctx).get_sessions(name, active=active, testing=effective,
                                           limit=limit, offset=offset)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_session(name: RunName, code: str, ctx: Context = None) -> dict:
    """Get one session in a run by its session code (full detail incl. current unit).

    Under FORMR_DATA_ACCESS=test_only, fetching a real participant's session is refused.
    Scope: session:read.
    """
    validate_run_name(name)
    sess = await _client(ctx).get_session(name, code)
    if gate.data_access_mode() == gate.TEST_ONLY and not gate.is_test_session(sess):
        raise gate.DataAccessError(
            f"Session '{code[:6]}…' is a real participant's; FORMR_DATA_ACCESS=test_only blocks "
            "reading real participant data. Only the MCP server's operator can lift that "
            "(FORMR_DATA_ACCESS=all; FORMR_MCP_DATA_ACCESS in formr-docker), for everyone using it."
        )
    return sess


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_unit_sessions(name: RunName, session: str | None = None, testing: bool | None = None,
                             since: str | None = None, limit: int = 1000, offset: int = 0,
                             ctx: Context = None) -> list[dict]:
    """Per-unit interaction history (one row per participant x unit x iteration): unit type,
    position, created/ended/expired, result, state. The basis for progress, dropout, and
    trajectory (Sankey/alluvial) analysis.

    Filters: session (comma-separated codes); testing (subject to the FORMR_DATA_ACCESS ceiling);
    since (ISO 8601, for incremental polling). Paginate with limit/offset. Scope: session:read.
    """
    validate_run_name(name)
    effective = gate.resolve_testing(testing)
    return await _client(ctx).get_unit_sessions(name, session=session, testing=effective,
                                                since=since, limit=limit, offset=offset)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_run_results(name: RunName, surveys: list[str] | None = None,
                          sessions: list[str] | None = None, items: list[str] | None = None,
                          testing: bool | None = None, ctx: Context = None) -> dict:
    """Fetch raw survey responses for a run, as an object keyed by survey name (each value a
    list of response rows). Filter by surveys, sessions (codes), and/or items.

    GDPR gate: under FORMR_DATA_ACCESS=test_only (default) only test-session rows are returned —
    enforced by resolving test session codes first and passing them as the session filter, since
    the results endpoint has no testing filter of its own. testing=False is refused in test_only
    mode. Returns {} when no sessions match the constraint. Scope: data:read, and session:read
    under test_only or with `testing` (the matching sessions are looked up first).
    """
    validate_run_name(name)
    return await gate.get_results_gated(_client(ctx), name, surveys=surveys,
                                        sessions=sessions, items=items, testing=testing)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_run_files(name: RunName, ctx: Context = None) -> list[dict]:
    """List files uploaded to a run — metadata only (id, name, path, url, timestamps).
    Does not download file contents. Scope: file:read.
    """
    validate_run_name(name)
    return await _client(ctx).get_run_files(name)


# --- test sessions (write) ---

_MAX_NEW_SESSIONS = 50
_SESSION_ACTIONS = ("advance", "execute", "move_to_position", "end_external")


def _test_link(run_link: str | None, code: str) -> str | None:
    if not run_link:
        return None
    return f"{run_link}{'&' if '?' in run_link else '?'}code={code}"


async def _require_test_session(client: FormrClient, name: str, code: str) -> dict:
    """The session, if it is a test session. The MCP changes nothing of a real
    participant's, whatever FORMR_DATA_ACCESS says: that ceiling is about
    reading, and moving someone through a live study is the researcher's call."""
    sess = await client.get_session(name, code)
    if not gate.is_test_session(sess):
        raise gate.DataAccessError(
            f"Session '{code}' is not a test session. The MCP acts on test sessions only; "
            "move real participants in formr's admin area (the run's Sessions page)."
        )
    return sess


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def create_test_sessions(name: RunName, count: int = 1, codes: list[str] | None = None,
                               ctx: Context = None) -> dict:
    """Create test sessions in a run (always testing=1) and return each code with its test
    link, `<run link>?code=<code>`. Open a link in a browser (e.g. Playwright) to go through
    the run as that participant; the session's data is then readable under the default
    FORMR_DATA_ACCESS=test_only, and test_session_action moves it along (advance a Pause,
    jump to a position, run the engine).

    Either `count` random codes (1-50), or your own `codes`, which must match the
    instance's session-code pattern (by default 64 characters) and have only A-Z a-z 0-9 _ - ~
    (formr refuses '+' here). Scopes: session:write and run:read (the run's link).
    """
    validate_run_name(name)
    if codes and len(codes) > _MAX_NEW_SESSIONS:
        raise ValueError(f"At most {_MAX_NEW_SESSIONS} codes per call.")
    if not codes and not 1 <= count <= _MAX_NEW_SESSIONS:
        raise ValueError(f"count must be between 1 and {_MAX_NEW_SESSIONS}.")
    client = _client(ctx)
    # the run's link first: read after the sessions were made, a refusal
    # (no run:read) or a time-out lost their codes, and a retry made more
    link = (await client.get_run(name)).get("link")
    created: list[str] = []
    errors: list = []
    if codes:
        resp = await client.create_sessions(name, codes=codes, testing=True)
        created = list((resp or {}).get("sessions") or [])
        errors = list((resp or {}).get("errors") or [])
    else:
        for _ in range(count):
            try:
                resp = await client.create_sessions(name, testing=True)
            except (FormrClientError, httpx.HTTPError) as e:
                if not created:
                    raise
                # the ones made so far exist: hand their codes over
                errors.append({"code": None, "reason": f"stopped after {len(created)} of {count}: {e}"})
                break
            created.extend((resp or {}).get("sessions") or [])
    out: dict = {"created": [{"code": c, "test_link": _test_link(link, c)} for c in created]}
    if errors:
        out["errors"] = errors
    return out


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, openWorldHint=False))
async def test_session_action(name: RunName, code: str,
                              action: Literal["advance", "execute", "move_to_position", "end_external"],
                              position: int | None = None, ctx: Context = None) -> dict:
    """Move a TEST session through a run, as the admin's test tools do:

      advance           end the current unit (result 'moved') and go on to the next one —
                        e.g. skip a Pause or Wait instead of waiting it out
      execute           run the engine for the session now, as the daemon would
      move_to_position  send the session to `position` (required) and run that unit
      end_external      end the current External unit, as its service's callback would

    Refuses real participants' sessions, whatever FORMR_DATA_ACCESS allows. Returns formr's
    answer and the session afterwards (position, current unit). Scopes: session:write and
    session:read.
    """
    validate_run_name(name)
    if action == "move_to_position" and position is None:
        raise ValueError("move_to_position needs a position.")
    client = _client(ctx)
    await _require_test_session(client, name, code)
    try:
        resp = await client.session_action(
            name, code, action, position=position if action == "move_to_position" else None
        )
    except FormrClientError as e:
        # as in _upload_structure: a lost answer may come after formr did it
        if isinstance(e, FormrAnswerLost) or e.status in (502, 503, 504):
            raise ValueError(
                f"Session '{code}': the answer to '{action}' was lost ({e}). formr may have done it: "
                "check the session (get_session) before repeating it, or it is done twice."
            ) from e
        raise
    out: dict = {"action": action, "response": resp or "ok"}
    try:
        out["session"] = await client.get_session(name, code)
    except (FormrClientError, httpx.HTTPError) as e:
        out["session"] = None
        out["note"] = (f"formr did '{action}', but reading the session back failed ({e}). "
                       "Do not repeat it: check the session with get_session.")
    return out


# --- logs ---

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_run_logs(name: RunName, kind: Literal["unit", "email", "push"] = "unit",
                       session: str | None = None, testing: bool | None = None,
                       since: str | None = None, only_with_log: bool = True,
                       limit: int = 1000, offset: int = 0, ctx: Context = None) -> dict:
    """What the run's engine logged, as formr's admin log pages show it — the place to look
    when a test session did something unexpected:

      unit   per unit session: result and result_log (an R error and its output, a
             branch's verdict, why an email was skipped); only rows with a log unless
             only_with_log=False
      email  the Email log: to, subject, status (sent / failed / invalid recipient …),
             tries, sent, with the unit session's result and result_log
      push   the Push log: message, status, error, attempt

    Filters: session (comma-separated codes), testing, since (ISO 8601); limit/offset page
    through the rows before only_with_log is applied; an answer with next_offset had a full
    page, so call again from there. FORMR_DATA_ACCESS=test_only (default) restricts every
    kind to test sessions. Email and push logs need a formr with the logs endpoint (not in
    v2.0.0-rc13); the unit log's result_log likewise. Scopes: session:read and data:read.
    """
    validate_run_name(name)
    effective = gate.resolve_testing(testing)
    client = _client(ctx)
    note = None
    page_full = False
    if kind == "unit":
        rows = await client.get_unit_sessions(name, session=session, testing=effective,
                                              since=since, limit=limit, offset=offset,
                                              include_log=True)
        # formr pages before only_with_log is applied (its limit clamped to 1..10000)
        page_full = len(rows or []) >= min(max(limit, 1), 10000)
        if rows and not any("result_log" in r for r in rows if isinstance(r, dict)):
            note = ("This formr does not return result_log yet (it ignores include_log); "
                    "only results are shown.")
        elif only_with_log:
            rows = [r for r in rows if isinstance(r, dict) and r.get("result_log")]
    else:
        try:
            rows = await client.get_logs(name, kind, session=session, testing=effective,
                                         since=since, limit=limit, offset=offset)
        except FormrClientError as e:
            # formr without the endpoint: 'Run sub-resource not found'; with
            # it, a 404 for a run that doesn't exist says so ('Run not found')
            if e.status == 404 and "sub-resource" in str(e):
                raise FormrClientError(
                    f"This formr instance has no {kind} log endpoint yet "
                    f"(GET /api/v1/runs/{{name}}/logs/{kind}); it needs a newer formr."
                ) from e
            raise
    if gate.data_access_mode() == gate.TEST_ONLY:
        # The server filtered by testing already; a row that isn't a test
        # session's (a push log whose unit session is gone) never passes.
        rows = [r for r in rows if gate.is_test_session(r)]
    out: dict = {"kind": kind, "count": len(rows), "rows": rows}
    if note:
        out["note"] = note
    if page_full:
        out["next_offset"] = offset + min(max(limit, 1), 10000)
        out["more"] = (f"formr's page of unit sessions was full: later ones, logged ones included, "
                       f"are not in this answer. Call again with offset={out['next_offset']}.")
    return out


# --- run files (write) ---

@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def upload_run_file(name: RunName, path: str | None = None, filename: str | None = None,
                          text: str = "", content_base64: str = "",
                          ctx: Context = None) -> dict:
    """Upload a file to a run (an image, a CSS or JS file, a PDF, a CSV the run's R reads)
    and return its entry with the URL to use in the run. Give exactly one of:

      path            a file in the MCP workspace (FORMR_WORKSPACE_DIR, default .formr/),
                      relative to it or absolute inside it; nothing outside is read
      text            the file's content as text (needs filename)
      content_base64  binary content, base64-encoded (needs filename)

    formr keeps letters, digits, '.', '_' and '-' of the name and replaces the rest with
    '_'; it refuses file types it doesn't take. An existing file of that name is replaced.
    Scope: file:write (and file:read for the file's URL in the answer).
    """
    validate_run_name(name)
    # text and content_base64 are plain `str`: the SDK reads any other
    # annotation's string as JSON first, so a file whose text is a JSON object
    # or array arrived as a dict and was refused, and 'null' as None
    given = [x for x in (path, text, content_base64) if x]
    if len(given) != 1:
        raise ValueError("Give exactly one of path, text or content_base64.")
    if path is not None:
        if HTTP_MODE:
            raise ValueError("A hosted MCP server has no workspace: pass the file's content as text or content_base64.")
        file = workspace_file(path)
        content = file.read_bytes()
        filename = filename or file.name
    else:
        if not filename:
            raise ValueError("filename is required with text or content_base64.")
        if text:
            content = text.encode("utf-8")
        else:
            try:
                content = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ValueError(f"content_base64 is not valid base64: {e}") from e
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    client = _client(ctx)
    resp = await client.upload_run_file(name, filename, content, content_type)
    stored = (resp or {}).get("file") or filename
    out: dict = {"file": stored, "bytes": len(content), "messages": (resp or {}).get("messages"),
                 "entry": None}
    try:
        files = await client.get_run_files(name) or []
    except (FormrClientError, httpx.HTTPError) as e:
        # stored all the same (a 403 here is a token without file:read)
        out["note"] = (f"Stored. Listing the run's files for its URL failed ({e}); "
                       "list_run_files shows it; no need to upload it again.")
        return out
    out["entry"] = next((f for f in files if isinstance(f, dict) and f.get("name") == stored), None)
    return out


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True, openWorldHint=False))
async def delete_run_file(name: RunName, filename: str, ctx: Context = None) -> str:
    """Delete a file uploaded to a run, by its name as list_run_files shows it. A study page
    still linking it then shows a broken image or loses its script. Scope: file:write.
    """
    validate_run_name(name)
    await _client(ctx).delete_run_file(name, filename)
    return f"Deleted '{filename}' from run '{name}'."


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_survey_structure(survey: str, ctx: Context = None) -> dict:
    """Get a survey's item definitions as JSON: item names, types, labels, and choice lists.
    This is survey DESIGN (definitions), not participant data, so it is not gated. Use it to
    inspect items/choices while designing or debugging a run. Scope: survey:read.
    """
    return await _client(ctx).get_survey(survey, "json")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def fetch_google_sheet(
    url: str,
    format: Literal["csv", "xlsx"] = "csv",
    to_file: bool = False,
    ctx: Context = None,
) -> str:
    """Fetch a Google Sheet by its share/edit link as CSV or XLSX.

    A chat app can't convert a Google Sheets link into the CSV/XLSX export
    URL itself — this tool does that canonicalisation and fetches the data. Pass
    any sheet link: .../spreadsheets/d/<ID>/edit#gid=<GID>, a sharing link, or a
    published /d/e/... link. The gid (tab) is honored for CSV.

    - format="csv" (default): returns the sheet as CSV TEXT inline, so a client
      with no filesystem connector can read it. Set to_file=True to instead write
      a .csv into the workspace (.formr/sheets/) and return its path.
    - format="xlsx": the whole workbook is binary, so it is always written to a
      file in the workspace and the path is returned (to_file is implied).

    Requires the sheet to be link-accessible ('Anyone with the link -> Viewer')
    or published to the web; a private sheet returns a clear sharing error.
    Handy for importing survey item tables authored in Google Sheets — read the
    CSV here to inspect/convert, or grab the XLSX to upload via formr's survey
    import.
    """
    if HTTP_MODE and (to_file or format == "xlsx"):
        raise ValueError("A hosted MCP server has no workspace to write the sheet to: fetch it as CSV text (format='csv', to_file=False).")
    if HTTP_MODE:
        return await gsheets.fetch_google_sheet(url, format, to_file, http_client=shared_http(),
                                                max_bytes=gsheets.MAX_SHEET_BYTES)
    return await gsheets.fetch_google_sheet(url, format, to_file)


# Over HTTP: tools that need the server's own workspace (each caller's files
# would be every caller's) or send a run to a third party.
_LOCAL_ONLY_TOOLS = ("get_run_structure_to_file", "update_run_structure_from_file", "open_flowchart")

_HTTP_SETUP = """SETUP: This is a hosted formr MCP server. The user connected it to their formr
account by signing in on formr and choosing its scopes and runs there; every call acts
as them, with no more than that. Call whoami FIRST — it reports the connection's scopes,
run allowlist, and which tools are usable. To change what it may do, the user edits the
connection under Account -> API (Connected apps) in formr, or connects again; deleting it
there ends it at once."""

_HTTP_WORKFLOW = """WORKFLOW — editing run structures (this server keeps no files):
    1. get_run_structure(name) → returns the structure as JSON text
    2. edit that JSON in your reply
    3. update_run_structure(name, structure_json) → validates and uploads
  The unit tools (add_run_unit, remove_run_unit, duplicate_run_units, shift_run_positions,
  renormalize_positions) fetch the run, edit, validate and upload it in one step; summarize_run,
  find_run_items and analyze_run read it from formr. Upload files with upload_run_file(text=…)
  or content_base64.
  Uploads change only what changed: formr keeps every unit sent back with its run_unit_id
  (get_run_structure gives it), and participants stay on it, moving with it when its position
  changes. Leave run_unit_id out for a new unit and never copy it to another. Keep "version"
  too: formr refuses an upload made from a copy that has changed since (fetch again then). A
  unit left out is removed, and its participants go on to what then stands in its place: formr
  refuses that where participants are, as its editor asks first. Tell the user which units,
  and pass confirm_removal=true (remove_run_unit, update_run_structure) only with their go-ahead.
  A formr older than v2.0.0-rc13 (get_run_structure gives no run_unit_id) replaces the whole
  structure on every upload and restarts every participant's current step: there, make all
  changes in one get_run_structure + update_run_structure, and tell the user first."""

# Tools that upload a run structure over HTTP: each call changes the live run.
_HTTP_UPLOAD_TOOLS = ("add_run_unit", "remove_run_unit", "duplicate_run_units",
                      "shift_run_positions", "renormalize_positions", "update_run_structure")

# What the remaining tools' descriptions say about the stdio workspace, as the
# hosted server has to say it (the file tools are not offered there).
# Each (old, new) must still be in the description: a reworded docstring stops
# the server's start rather than go on advertising the workspace.
_HTTP_DESCRIPTION_EDITS = {
    "get_run_structure": [(" For editor/CLI workflows with file access, prefer\n"
                           "    get_run_structure_to_file + update_run_structure_from_file (better for large runs).", "")],
    "update_run_structure": [(" For editor/CLI\n"
                              "    workflows, update_run_structure_from_file is usually more convenient.", "")],
    "get_pattern": [("analyze_run + update_run_structure_from_file", "analyze_run + update_run_structure")],
    "upload_run_file": [("      path            a file in the MCP workspace (FORMR_WORKSPACE_DIR, default .formr/),\n"
                         "                      relative to it or absolute inside it; nothing outside is read\n", "")],
    "fetch_google_sheet": [
        (" Set to_file=True to instead write\n      a .csv into the workspace (.formr/sheets/) and return its path.", ""),
        ('    - format="xlsx": the whole workbook is binary, so it is always written to a\n'
         "      file in the workspace and the path is returned (to_file is implied).\n", ""),
        ("read the\n    CSV here to inspect/convert, or grab the XLSX to upload via formr's survey\n    import.",
         "read the\n    CSV here to inspect or convert it."),
    ],
    "summarize_run": [("Summarize a run structure from the local file.", "Summarize a run's structure.")],
    "remove_run_unit": [("from the local run structure file.", "from a run's structure.")],
    "duplicate_run_units": [("to_start_position in the local run structure file.", "to_start_position in a run's structure.")],
    "shift_run_positions": [("by delta in the local run structure file.", "by delta in a run's structure.")],
}

# Parameters a hosted server has no use for (it has no workspace): left out of
# the tools' input schemas (None), or narrowed; a call that sends them anyway
# is refused by the tool.
_HTTP_PARAMETER_EDITS = {
    "upload_run_file": {"path": None},
    "fetch_google_sheet": {"to_file": None, "format": {"enum": ["csv"]}},
}


def configure_http_mode() -> None:
    """Serve many researchers over HTTP (http_server.py): each request carries its
    caller's formr token, the server keeps no workspace, and the tools that need one
    are not offered."""
    global HTTP_MODE
    HTTP_MODE = True
    for tool in _LOCAL_ONLY_TOOLS:
        mcp.remove_tool(tool)
    # the tools are replaced by edited copies, not changed in place
    tools = mcp._tool_manager._tools
    for name in _HTTP_UPLOAD_TOOLS:
        tool = tools[name]
        tools[name] = tool.model_copy(update={"annotations": (tool.annotations or ToolAnnotations()).model_copy(
            update={"destructiveHint": True})})
    for name, edits in _HTTP_DESCRIPTION_EDITS.items():
        description = tools[name].description or ""
        for old, new in edits:
            if old not in description:
                raise RuntimeError(f"configure_http_mode: the description of {name} no longer says {old!r}")
            description = description.replace(old, new)
        tools[name] = tools[name].model_copy(update={"description": description})
    for name, changes in _HTTP_PARAMETER_EDITS.items():
        schema = json.loads(json.dumps(tools[name].parameters))
        for param, change in changes.items():
            if param not in schema.get("properties", {}):
                raise RuntimeError(f"configure_http_mode: {name} no longer has a parameter {param!r}")
            if change is None:
                del schema["properties"][param]
                if param in schema.get("required", []):
                    schema["required"].remove(param)
            else:
                schema["properties"][param].update(change)
        tools[name] = tools[name].model_copy(update={"parameters": schema})
    text = mcp._mcp_server.instructions
    setup_start = text.index("SETUP:")
    setup_end = text.index("CAPABILITIES & LIMITS:")
    text = text[:setup_start] + _HTTP_SETUP + "\n\n" + text[setup_end:]
    flow_start = text.index("WORKFLOW — editing run structures")
    flow_end = text.index("PATTERNS —")
    text = text[:flow_start] + _HTTP_WORKFLOW + "\n\n" + text[flow_end:]
    old = "upload_run_file (from the workspace, text or base64)"
    if old not in text:
        raise RuntimeError(f"configure_http_mode: the instructions no longer say {old!r}")
    text = text.replace(old, "upload_run_file (text or base64)")
    mcp._mcp_server.instructions = text


if __name__ == "__main__":
    mcp.run()
