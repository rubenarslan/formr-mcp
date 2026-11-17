from __future__ import annotations

import re
from typing import Literal

from formr_mcp.utils import load_structure, validate_run_name
from formr_mcp.validation import SURVEY_UNIT_TYPES


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^<]+?>", " ", str(text))
    text = " ".join(text.split())
    return text


def _truncate(text: str, max_len: int = 120) -> str:
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _item_label(item: dict) -> str:
    label = item.get("label", "")
    cleaned = _strip_html(label)
    itype = item.get("type", "")
    if not cleaned:
        if itype == "note" and "<img" in str(item.get("label", "")):
            return "(image)"
        if itype == "calculate":
            val = item.get("value", "")
            if val:
                return f"= {val}"
        return ""
    return _truncate(cleaned)


def summarize_run_structure(
    name: str,
    detail: Literal["units", "items"] = "items",
) -> str:
    data = load_structure(name)

    units = data.get("units", [])
    settings = data.get("settings") or {}
    lines: list[str] = []

    lines.append(f"Run '{name}' — {len(units)} units")

    title = settings.get("title", "")
    if title:
        lines.append(f"Title: {title}")
    public = settings.get("public")
    if public is not None:
        vis = {0: "admin/test-users only", 2: "link-accessible"}.get(
            public, f"level {public}"
        )
        lines.append(f"Visibility: {vis}")

    # Run-level custom R helpers (DRY) and secret names — so callers know what's available
    # before writing R that should reuse them.
    custom_r = settings.get("custom_r") or ""
    if custom_r.strip():
        defined = re.findall(r"\b(\w+)\s*(?:<-|=)\s*function\b", custom_r)
        if defined:
            lines.append(f"custom_r functions: {', '.join(dict.fromkeys(defined))}")
        else:
            lines.append("custom_r: present (see settings.custom_r)")
    secrets = settings.get("secrets") or []
    if secrets:
        names = ", ".join(f".formr$secret_{n}" for n in secrets)
        lines.append(f"Secrets available in R: {names}")
    lines.append("")

    for unit in units:
        utype = unit.get("type", "?")
        pos = unit.get("position", "?")
        desc = unit.get("description", "")

        if utype in SURVEY_UNIT_TYPES:
            sd = unit.get("survey_data") or {}
            sname = sd.get("name", "(unnamed)")
            items = sd.get("items") or []
            n_items = len(items) if isinstance(items, list) else 0

            lines.append(f'### Position {pos}: {utype} "{sname}" ({n_items} items)')
            if desc:
                lines.append(f"  Description: {desc}")

            if detail == "items" and isinstance(items, list):
                for item in items:
                    itype = item.get("type", "?")
                    iname = item.get("name", "(unnamed)")
                    label_str = _item_label(item)
                    lines.append(f"  [{itype}] {iname}: {label_str}")

            lines.append("")

        elif utype in ("SkipForward", "SkipBackward", "Branch"):
            condition = unit.get("condition", "")
            if_true = unit.get("if_true", "")
            lines.append(f"### Position {pos}: {utype} → {if_true}")
            lines.append(f"  Condition: {condition}")
            if desc:
                lines.append(f"  Description: {desc}")
            auto_jump = unit.get("automatically_jump")
            auto_go = unit.get("automatically_go_on")
            if auto_jump is not None:
                lines.append(f"  Auto-jump: {auto_jump}")
            if auto_go is not None:
                lines.append(f"  Auto-go-on: {auto_go}")
            lines.append("")

        elif utype == "Endpage":
            body = unit.get("body", "")
            body_clean = _truncate(_strip_html(body)) if body else ""
            lines.append(f"### Position {pos}: Endpage")
            if body_clean:
                lines.append(f"  Body: {body_clean}")
            lines.append("")

        elif utype == "External":
            address = unit.get("address", "")
            lines.append(f"### Position {pos}: External")
            if address:
                lines.append(f"  Address: {address}")
            if desc:
                lines.append(f"  Description: {desc}")
            lines.append("")

        elif utype in ("Pause", "Wait"):
            wait_min = unit.get("wait_minutes")
            wait_date = unit.get("wait_until_date", "")
            wait_time = unit.get("wait_until_time", "")
            lines.append(f"### Position {pos}: {utype}")
            if wait_min:
                lines.append(f"  Wait: {wait_min} minutes")
            if wait_date:
                lines.append(f"  Until date: {wait_date}")
            if wait_time:
                lines.append(f"  Until time: {wait_time}")
            if desc:
                lines.append(f"  Description: {desc}")
            lines.append("")

        elif utype == "Email":
            subject = unit.get("subject", "")
            lines.append(f"### Position {pos}: Email")
            if subject:
                lines.append(f"  Subject: {subject}")
            lines.append("")

        elif utype == "Shuffle":
            groups = unit.get("groups", "")
            lines.append(f"### Position {pos}: Shuffle")
            if groups:
                lines.append(f"  Groups: {groups}")
            lines.append("")

        else:
            lines.append(f"### Position {pos}: {utype}")
            if desc:
                lines.append(f"  Description: {desc}")
            lines.append("")

    return "\n".join(lines)


def find_items(
    name: str,
    query: str | None = None,
    item_type: str | None = None,
) -> str:
    data = load_structure(name)

    units = data.get("units", [])
    survey_results: list[tuple] = []
    total = 0

    for unit in units:
        if unit.get("type") not in SURVEY_UNIT_TYPES:
            continue
        sd = unit.get("survey_data")
        if not isinstance(sd, dict):
            continue
        sname = sd.get("name", "(unnamed)")
        pos = unit.get("position", "?")
        items = sd.get("items")
        if not isinstance(items, list):
            continue

        matches: list[dict] = []
        for item in items:
            iname = item.get("name", "")
            itype = item.get("type", "")
            label_clean = _strip_html(item.get("label", ""))

            hit = True
            if query:
                q = query.lower()
                if q not in iname.lower() and q not in label_clean.lower():
                    hit = False
            if item_type:
                if itype.lower() != item_type.lower():
                    hit = False
            if hit:
                matches.append(item)
                total += 1

        if matches:
            survey_results.append((pos, sname, matches))

    if total == 0:
        parts = []
        if query:
            parts.append(f"name or label containing '{query}'")
        if item_type:
            parts.append(f"type '{item_type}'")
        filter_desc = " with " + " and ".join(parts) if parts else ""
        return f"No items found{filter_desc} in run '{name}'."

    lines: list[str] = []
    lines.append(f"Found {total} matching item(s) in run '{name}':\n")

    for pos, sname, items in survey_results:
        lines.append(
            f'Survey "{sname}" (position {pos}, {len(items)} match(es)):'
        )
        for item in items:
            itype = item.get("type", "?")
            iname = item.get("name", "(unnamed)")
            label_str = _item_label(item)
            lines.append(f"  [{itype}] {iname}: {label_str}")
        lines.append("")

    return "\n".join(lines)