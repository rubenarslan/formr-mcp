"""Fetch public Google Sheets as CSV or XLSX.

Claude Desktop can't turn a Google Sheets share link into the export URL it
needs, so this module canonicalises any sheet link into the right `/export`
(or published `/pub`) endpoint and fetches it. CSV can be returned as text —
the useful mode when there is no filesystem connector — while XLSX is binary
and is written to a file.

Only link-shared / published sheets work: unauthenticated export of a private
sheet returns Google's HTML sign-in page, which we detect and turn into a
clear, actionable error.
"""
from __future__ import annotations

import re

import httpx

from . import utils

CSV = "csv"
XLSX = "xlsx"

_GID_RE = re.compile(r"[?#&]gid=(\d+)")
_PUBLISHED_RE = re.compile(r"/spreadsheets/d/e/([a-zA-Z0-9_-]+)")
_STANDARD_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_BARE_ID_RE = re.compile(r"[a-zA-Z0-9_-]{20,}")


class GoogleSheetError(Exception):
    pass


def parse_sheet_url(url: str) -> dict:
    """Extract {kind, id, gid} from a Google Sheets URL (or a bare ID)."""
    u = url.strip()
    gid_m = _GID_RE.search(u)
    gid = gid_m.group(1) if gid_m else None
    # Published-to-web links use /d/e/<id>/ — check before the standard form,
    # which would otherwise capture the literal "e" as the id.
    m = _PUBLISHED_RE.search(u)
    if m:
        return {"kind": "published", "id": m.group(1), "gid": gid}
    m = _STANDARD_RE.search(u)
    if m:
        return {"kind": "standard", "id": m.group(1), "gid": gid}
    if _BARE_ID_RE.fullmatch(u):
        return {"kind": "standard", "id": u, "gid": gid}
    raise GoogleSheetError(
        "Could not find a Google Sheets ID in the input. Paste a link like "
        "https://docs.google.com/spreadsheets/d/<ID>/edit#gid=<GID>"
    )


def export_url(parsed: dict, fmt: str) -> str:
    """Build the export URL. gid selects a single tab for CSV; XLSX always
    exports the whole workbook, so gid is dropped there."""
    ext = CSV if fmt == CSV else XLSX
    sid, gid, kind = parsed["id"], parsed["gid"], parsed["kind"]
    if kind == "published":
        url = f"https://docs.google.com/spreadsheets/d/e/{sid}/pub?output={ext}"
    else:
        url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format={ext}"
    if fmt == CSV and gid:
        url += f"&gid={gid}"
    return url


def _not_shared(export: str) -> GoogleSheetError:
    return GoogleSheetError(
        "Google returned a sign-in/permission page instead of sheet data. The "
        "sheet must be link-accessible: in Google Sheets choose Share -> General "
        "access -> 'Anyone with the link' -> Viewer (or File -> Share -> Publish "
        f"to web). Export URL tried: {export}"
    )


async def _fetch(url: str, fmt: str, http_client: httpx.AsyncClient | None):
    parsed = parse_sheet_url(url)
    export = export_url(parsed, fmt)
    client = http_client or httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(60.0, connect=10.0)
    )
    try:
        resp = await client.get(export)
    finally:
        if http_client is None:
            await client.aclose()

    if resp.status_code in (401, 403):
        raise _not_shared(export)
    if resp.status_code == 404:
        raise GoogleSheetError(f"Sheet not found (404). Check the ID/URL. Tried: {export}")
    if not resp.is_success:
        raise GoogleSheetError(f"Failed to fetch sheet ({resp.status_code}). Tried: {export}")
    if "text/html" in resp.headers.get("content-type", ""):
        raise _not_shared(export)
    return resp.content, export, parsed


async def fetch_google_sheet(
    url: str,
    fmt: str = CSV,
    to_file: bool = False,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Fetch a sheet. CSV returns text (unless to_file); XLSX is always a file."""
    if fmt not in (CSV, XLSX):
        raise GoogleSheetError(f"format must be 'csv' or 'xlsx', got {fmt!r}")
    content, export, parsed = await _fetch(url, fmt, http_client)

    if fmt == CSV and not to_file:
        return content.decode("utf-8-sig", errors="replace")

    fname = parsed["id"]
    if fmt == CSV and parsed["gid"]:
        fname += f"_{parsed['gid']}"
    dest = utils.WORKSPACE_DIR / "sheets" / f"{fname}.{fmt}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    note = " (xlsx is binary — written to a file, not returned as text)" if fmt == XLSX else ""
    return f"Fetched {fmt.upper()} from {export}\nWrote {len(content)} bytes to {dest}{note}"
