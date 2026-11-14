from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
import os
import webbrowser
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Literal

from pydantic import Field

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context
from mcp.types import ToolAnnotations

# Load .env before importing project modules — they read env vars at import time
dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)

from formr_mcp.auth import AuthError, check_credentials
from formr_mcp.client import FormrClient, FormrClientError, FormrPermissionError
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
    run_filepath,
    validate_run_name,
    workspace_file,
)
from formr_mcp.validation import get_unit_type_schemas, validate_structure

RunName = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]{2,255}$", min_length=3, max_length=256)]

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
    "delete_run": "run:write",
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
}
_WRITE_SCOPES = {"run:write", "survey:write", "session:write", "user:write", "file:write"}


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

    tool_access = {}
    for tool, scope in TOOL_SCOPES.items():
        if scope not in scope_set:
            tool_access[tool] = f"blocked: requires '{scope}' scope"
        elif tool == "get_run_logs" and "session:read" not in scope_set:
            tool_access[tool] = "blocked: requires 'session:read' and 'data:read' scopes"
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


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[FormrClient]:
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
get_run_results and get_run_logs show what happened. Real participants are never changed.
RUN FILES — upload_run_file (from the workspace, text or base64) / delete_run_file.
DATA GATE (GDPR): FORMR_DATA_ACCESS sets a HARD ceiling, default `test_only` —
only test sessions (testing=1) are ever readable; reads of real participant data
are refused until an operator sets FORMR_DATA_ACCESS=all and restarts. For
full-scale analysis prefer emitting R code for RStudio (see data-access docs).

formr is a survey framework for psychology research. Runs are ordered
compositions of units (Surveys, Pages, Emails, Branches, etc.) where
execution flows by position number. Branching uses R expressions in
`condition` and jumps to the `if_true` position.

WORKFLOW — editing run structures, two paths:
  With filesystem access (Claude Code / editors) — preferred, best for large runs:
    1. get_run_structure_to_file(name) → .formr/<name>.json (backs up existing)
    2. edit .formr/<name>.json with Read/Edit
    3. update_run_structure_from_file(name) → validates and uploads
  Without filesystem access (e.g. Claude Desktop) — inline JSON text:
    1. get_run_structure(name) → returns the structure as JSON text
    2. edit that JSON in your reply
    3. update_run_structure(name, structure_json) → validates and uploads

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
    return ctx.request_context.lifespan_context


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
    """Create a new run. Name must start with a letter, contain only letters, digits, hyphens, and be 3-256 chars.

    Returns the created run name and link on success. Requires `run:write` OAuth scope.
    """
    validate_run_name(name)
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


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))
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
    await client.patch_run(name, settings)
    return await client.get_run(name)


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
        if unit.get("type") == "Survey" and isinstance(unit.get("survey_data"), dict):
            items = unit["survey_data"].get("items", [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("choices") and not item.get("choice_list"):
                        item_name = item.get("name")
                        if item_name:
                            item["choice_list"] = item_name


def _unit_signature(structure: dict) -> list[tuple]:
    """(position, type) per unit; Page is exported as Endpage, so compare them as one."""
    sig = []
    for u in structure.get("units", []) or []:
        if isinstance(u, dict):
            t = "Endpage" if u.get("type") == "Page" else u.get("type")
            pos = u.get("position")
            # formr accepts numeric strings; compare "10" and 10 as one position.
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                pass
            sig.append((pos, t))
    return sorted(sig, key=lambda x: (not isinstance(x[0], int), x[0] if isinstance(x[0], int) else 0, str(x)))


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


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def update_run_structure_from_file(name: RunName, ctx: Context = None) -> str:
    """Read a run structure from .formr/<name>.json, validate, and upload to formr.

    Returns a summary on success, or validation errors to fix and retry.
    On success, the backup file (.formr/<name>.json.bak) is removed.
    To inspect the uploaded result, call get_run_structure_to_file again.
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

    _normalize_survey_choices(structure)
    errors = validate_structure(structure)
    if errors:
        raise ValueError(
            "Structure validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    await client.put_run_structure(name, structure)
    result = await client.get_run_structure(name)
    _verify_upload(name, structure, result)
    units = len(result.get("units", []))

    if bak_path.exists():
        bak_path.unlink()

    return f"Run '{name}': successfully updated ({units} units)."


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_run_structure(name: RunName, ctx: Context = None) -> str:
    """Fetch a run's full structure and return it as JSON text (NOT written to a file).

    Use this when you don't have filesystem access — e.g. Claude Desktop without a
    filesystem connector. Edit the returned JSON inline, then push it back with
    update_run_structure. For editor/CLI workflows with file access, prefer
    get_run_structure_to_file + update_run_structure_from_file (better for large runs).
    """
    validate_run_name(name)
    structure = await _client(ctx).get_run_structure(name)
    return json.dumps(structure, indent=2, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def update_run_structure(name: RunName, structure_json: str, ctx: Context = None) -> str:
    """Validate a run structure passed as JSON text and upload it to formr.

    Inline counterpart to get_run_structure for clients without filesystem access.
    Pass the FULL structure as a JSON string — an object with a "units" array. It is
    validated exactly like the file-based path; on failure you get the errors to fix
    and retry (nothing is uploaded). For editor/CLI workflows, update_run_structure_from_file
    is usually more convenient.
    """
    validate_run_name(name)
    try:
        structure = json.loads(structure_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"structure_json is not valid JSON: {e}")
    if not isinstance(structure, dict) or "units" not in structure:
        raise ValueError("Run structure must be a JSON object with a 'units' array.")

    _normalize_survey_choices(structure)
    errors = validate_structure(structure)
    if errors:
        raise ValueError(
            "Structure validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    client = _client(ctx)
    await client.put_run_structure(name, structure)
    result = await client.get_run_structure(name)
    _verify_upload(name, structure, result)
    units = len(result.get("units", []))
    return f"Run '{name}': successfully updated ({units} units)."


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_unit_types(ctx: Context = None) -> dict:
    """Get all supported run unit types and their required/optional fields. Use this to understand what can be created."""
    return get_unit_type_schemas()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_documentation(
    topic: Literal["item-types", "run-concepts", "r-code", "survey-json", "examples", "best-practices", "editing-tools", "unit-types-advanced", "data-access", "patterns", "custom-r-and-secrets"],
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
    topic: Literal["item-types", "run-concepts", "r-code", "survey-json", "examples", "best-practices", "editing-tools", "unit-types-advanced", "data-access", "patterns", "custom-r-and-secrets"],
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
def summarize_run(name: RunName, detail: Literal["units", "items"] = "items", ctx: Context = None) -> str:
    """Summarize a run structure from the local file. Returns a readable overview of units and their items.

    Must call get_run_structure_to_file(name) first to fetch the structure.

    Use detail='units' for just unit-level info (no items), or detail='items' (default) to include all survey items.
    Strips HTML from labels for readability.
    """
    validate_run_name(name)
    return summarize_run_structure(name, detail=detail)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_run_items(name: RunName, query: str | None = None, item_type: str | None = None, ctx: Context = None) -> str:
    """Search for items across all surveys in a run. Returns matching items with survey context.

    Must call get_run_structure_to_file(name) first to fetch the structure.

    Filters:
    - query: search item names and labels (case-insensitive substring match)
    - item_type: filter by item type (e.g. 'mc', 'text', 'note', 'calculate')
    At least one filter is recommended; both can be combined.
    """
    validate_run_name(name)
    return find_items(name, query=query, item_type=item_type)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def analyze_run(name: RunName, ctx: Context = None) -> str:
    """Analyze a run structure for errors and warnings. Checks R syntax, variable references, branch flow, item consistency, and common mistakes.

    Must call get_run_structure_to_file(name) first to fetch the structure.

    Returns a structured report with error/warning counts. R syntax validation requires R to be installed.
    """
    validate_run_name(name)
    return run_analysis(name)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
def add_run_unit(name: RunName, unit_type: str, position: int, description: str = "",
                insert_mode: Literal["shift", "overwrite"] = "shift",
                unit_fields: dict | None = None, ctx: Context = None) -> str:
    """Add a unit to a local run structure file. The file must exist (fetch with get_run_structure_to_file first).

    unit_fields — unit-type-specific fields (see get_unit_types() for full schema):
      Branch/SkipForward/SkipBackward: condition (R expr), if_true (int position)
      Email: subject, body, account_id (int), recipient_field
      Survey: study_id (int) or survey_data (dict)
      External: address (URL or R code)
      Wait: body (int position — NOT display content)
      Page/Endpage: body (markdown)
      Pause: wait_minutes, wait_until_time, wait_until_date, relative_to

    insert_mode: 'shift' (default) shifts units at >= position up by 10; 'overwrite' replaces. Position references are auto-remapped.
    """
    validate_run_name(name)
    return editing_add_run_unit(name, unit_type, position, description=description,
                               insert_mode=insert_mode, **(unit_fields or {}))


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False, openWorldHint=False))
def remove_run_unit(name: RunName, position: int, compact: bool = False, ctx: Context = None) -> str:
    """Remove a unit at the given position from the local run structure file.

    The file must exist (fetch with get_run_structure_to_file first).
    After removing, call update_run_structure_from_file to upload.
    compact=True shifts higher positions down by 1 and remaps references; dangling references are reported.
    """
    validate_run_name(name)
    return editing_remove_run_unit(name, position, compact=compact)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
def duplicate_run_units(name: RunName, from_positions: list[int], to_start_position: int,
                        shift_existing: bool = True, ctx: Context = None) -> str:
    """Copy units at from_positions to new positions starting at to_start_position in the local run structure file.

    Copies units in original order with gaps of 10 (e.g., 100, 110, 120...).
    shift_existing=True (default) shifts conflicting units up; False errors on conflicts.
    Position references are automatically remapped. The file must exist (fetch with get_run_structure_to_file first).
    """
    validate_run_name(name)
    return editing_duplicate_run_units(name, from_positions, to_start_position,
                                       shift_existing=shift_existing)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
def shift_run_positions(name: RunName, from_position: int, delta: int, ctx: Context = None) -> str:
    """Shift all units at positions >= from_position by delta in the local run structure file.

    Positive delta shifts up (making room), negative shifts down (closing gaps).
    Position references are automatically remapped. The file must exist (fetch with get_run_structure_to_file first).
    """
    validate_run_name(name)
    return editing_shift_run_positions(name, from_position, delta)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False, openWorldHint=False))
def renormalize_positions(name: RunName, spacing: Annotated[int, Field(ge=1)] = 10, ctx: Context = None) -> str:
    """Renumber all unit positions to clean multiples of spacing while preserving order.

    Safe to call on already-clean structures. Position references are automatically remapped.
    Useful after edits that leave gaps (e.g., compact removal shifts by 1).
    The file must exist (fetch with get_run_structure_to_file first).
    """
    validate_run_name(name)
    return editing_renormalize_positions(name, spacing=spacing)


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
            f"Session '{code}' is a real participant; FORMR_DATA_ACCESS=test_only blocks "
            "reading real participant data. Set FORMR_DATA_ACCESS=all to enable."
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
    mode. Returns {} when no sessions match the constraint. Scope: data:read.
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
    instance's session-code pattern (by default 64 characters of A-Z a-z 0-9 _ - ~ +).
    Scope: session:write.
    """
    validate_run_name(name)
    client = _client(ctx)
    created: list[str] = []
    errors: list = []
    if codes:
        if len(codes) > _MAX_NEW_SESSIONS:
            raise ValueError(f"At most {_MAX_NEW_SESSIONS} codes per call.")
        resp = await client.create_sessions(name, codes=codes, testing=True)
        created = list((resp or {}).get("sessions") or [])
        errors = list((resp or {}).get("errors") or [])
    else:
        if not 1 <= count <= _MAX_NEW_SESSIONS:
            raise ValueError(f"count must be between 1 and {_MAX_NEW_SESSIONS}.")
        for _ in range(count):
            resp = await client.create_sessions(name, testing=True)
            created.extend((resp or {}).get("sessions") or [])
    link = (await client.get_run(name)).get("link")
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
    answer and the session afterwards (position, current unit). Scope: session:write.
    """
    validate_run_name(name)
    if action == "move_to_position" and position is None:
        raise ValueError("move_to_position needs a position.")
    client = _client(ctx)
    await _require_test_session(client, name, code)
    resp = await client.session_action(
        name, code, action, position=position if action == "move_to_position" else None
    )
    return {"action": action, "response": resp or "ok", "session": await client.get_session(name, code)}


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
    through the rows before only_with_log is applied. FORMR_DATA_ACCESS=test_only (default)
    restricts every kind to test sessions. Email and push logs need a formr with the logs
    endpoint (v2 line after rc12); the unit log's result_log likewise. Scopes: session:read
    and data:read.
    """
    validate_run_name(name)
    effective = gate.resolve_testing(testing)
    client = _client(ctx)
    note = None
    if kind == "unit":
        rows = await client.get_unit_sessions(name, session=session, testing=effective,
                                              since=since, limit=limit, offset=offset,
                                              include_log=True)
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
            if "-> 404" in str(e) and "logs" in str(e):
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
    return out


# --- run files (write) ---

@mcp.tool(annotations=ToolAnnotations(destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def upload_run_file(name: RunName, path: str | None = None, filename: str | None = None,
                          text: str | None = None, content_base64: str | None = None,
                          ctx: Context = None) -> dict:
    """Upload a file to a run (an image, a CSS or JS file, a PDF, a CSV the run's R reads)
    and return its entry with the URL to use in the run. Give exactly one of:

      path            a file in the MCP workspace (FORMR_WORKSPACE_DIR, default .formr/),
                      relative to it or absolute inside it; nothing outside is read
      text            the file's content as text (needs filename)
      content_base64  binary content, base64-encoded (needs filename)

    formr keeps letters, digits, '.', '_' and '-' of the name and replaces the rest with
    '_'; it refuses file types it doesn't take. An existing file of that name is replaced.
    Scope: file:write.
    """
    validate_run_name(name)
    given = [x for x in (path, text, content_base64) if x is not None]
    if len(given) != 1:
        raise ValueError("Give exactly one of path, text or content_base64.")
    if path is not None:
        file = workspace_file(path)
        content = file.read_bytes()
        filename = filename or file.name
    else:
        if not filename:
            raise ValueError("filename is required with text or content_base64.")
        if text is not None:
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
    entry = next((f for f in (await client.get_run_files(name) or [])
                  if isinstance(f, dict) and f.get("name") == stored), None)
    return {"file": stored, "bytes": len(content), "messages": (resp or {}).get("messages"),
            "entry": entry}


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

    Claude Desktop can't convert a Google Sheets link into the CSV/XLSX export
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
    return await gsheets.fetch_google_sheet(url, format, to_file)


if __name__ == "__main__":
    mcp.run()
