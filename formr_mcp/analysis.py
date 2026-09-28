from __future__ import annotations

import re
import subprocess
from typing import Any

from formr_mcp.utils import load_structure
from formr_mcp.validation import SURVEY_UNIT_TYPES

_R_AVAILABLE: bool | None = None


def _r_available() -> bool:
    global _R_AVAILABLE
    if _R_AVAILABLE is None:
        try:
            result = subprocess.run(
                ["R", "--vanilla", "--slave", "-e", "cat('ok')"],
                capture_output=True, timeout=5,
            )
            _R_AVAILABLE = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _R_AVAILABLE = False
    return _R_AVAILABLE


# ── R expression extraction ──────────────────────────────────────────
_DOLLAR_RE = re.compile(r"\b(\w+)\$(\w+R?)\b")
# Local R assignments: `x <- ...`, `x = ...` (but not ==, <=, >=, !=). Used to tell
# locally-built data frames (df, survey_results, past_data, ...) apart from survey references.
_ASSIGN_RE = re.compile(r"\b(\w+)\s*(?:<-|(?<![=!<>])=(?!=))")
_INLINE_R_RE = re.compile(r"`r\s+([^`]+)`")
_R_CHUNK_RE = re.compile(r"```\{r[^}]*}\s*\n(.*?)```", re.DOTALL)


def _extract_r_expressions(structure: dict) -> list[dict]:
    sources: list[dict] = []
    units = structure.get("units", [])

    for unit in units:
        pos = unit.get("position", "?")
        utype = unit.get("type", "?")
        desc = unit.get("description", "")

        if utype in ("Branch", "SkipForward", "SkipBackward"):
            condition = unit.get("condition", "")
            if condition and condition.strip():
                sources.append({
                    "expr": condition,
                    "location": f"Branch/Skip condition at position {pos}",
                    "type": "condition",
                })

        if utype in ("Pause", "Wait"):
            relative_to = unit.get("relative_to", "")
            if relative_to and relative_to.strip():
                sources.append({
                    "expr": relative_to,
                    "location": f"Pause/Wait relative_to at position {pos}",
                    "type": "relative_to",
                })

        if utype == "External":
            address = unit.get("address", "")
            if address and address.strip() and not address.startswith("http"):
                sources.append({
                    "expr": address,
                    "location": f"External address at position {pos}",
                    "type": "address",
                })

        if utype in ("Page", "Endpage"):
            body = unit.get("body", "")
            if body:
                for m in _INLINE_R_RE.finditer(body):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"{utype} body at position {pos} (inline R)",
                        "type": "knitr_inline",
                    })
                for m in _R_CHUNK_RE.finditer(body):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"{utype} body at position {pos} (R chunk)",
                        "type": "knitr_chunk",
                    })

        if utype == "Email":
            body = unit.get("body", "")
            if body:
                for m in _INLINE_R_RE.finditer(body):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"Email body at position {pos} (inline R)",
                        "type": "knitr_inline",
                    })
            subject = unit.get("subject", "")
            if subject:
                for m in _INLINE_R_RE.finditer(subject):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"Email subject at position {pos} (inline R)",
                        "type": "knitr_inline",
                    })

        if utype == "Pause":
            body = unit.get("body", "")
            if body:
                for m in _INLINE_R_RE.finditer(body):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"Pause body at position {pos} (inline R)",
                        "type": "knitr_inline",
                    })
                for m in _R_CHUNK_RE.finditer(body):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"Pause body at position {pos} (R chunk)",
                        "type": "knitr_chunk",
                    })

        if utype in SURVEY_UNIT_TYPES:
            sd = unit.get("survey_data")
            if not isinstance(sd, dict):
                continue
            sname = sd.get("name", "(unnamed)")
            items = sd.get("items", [])
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                iname = item.get("name", "(unnamed)")
                itype = item.get("type", "")

                showif = item.get("showif", "")
                if showif and showif.strip():
                    js_prefix = "//js_only"
                    expr = showif
                    stype = "showif"
                    if expr.startswith(js_prefix):
                        expr = expr[len(js_prefix):].strip()
                        stype = "showif (js_only, R portion)"
                    sources.append({
                        "expr": expr,
                        "location": f"Item '{iname}' showif in survey '{sname}' (position {pos})",
                        "type": stype,
                    })

                value = item.get("value", "")
                if value and value.strip() and value.strip() != "sticky":
                    sources.append({
                        "expr": value,
                        "location": f"Item '{iname}' value in survey '{sname}' (position {pos})",
                        "type": "value",
                    })

                label = item.get("label", "") or ""
                for m in _INLINE_R_RE.finditer(label):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"Item '{iname}' label in survey '{sname}' (position {pos})",
                        "type": "knitr_inline",
                    })
                for m in _R_CHUNK_RE.finditer(label):
                    sources.append({
                        "expr": m.group(1),
                        "location": f"Item '{iname}' label in survey '{sname}' (position {pos})",
                        "type": "knitr_chunk",
                    })

    return sources


# ── R syntax validation ───────────────────────────────────────────────

def _validate_r_syntax(expressions: list[str]) -> dict[str, str | None]:
    if not _r_available():
        return {}
    if not expressions:
        return {}

    errors: dict[str, str | None] = {}
    for expr in expressions:
        escaped = expr.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        r_code = f"tryCatch(parse(text='{escaped}'), error=function(e) cat(conditionMessage(e)))"
        try:
            result = subprocess.run(
                ["R", "--vanilla", "--slave", "-e", r_code],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        output = result.stdout.strip()
        if not output:
            errors[expr] = None
        elif output.startswith("expression(") or output.startswith("expression\n"):
            errors[expr] = None
        else:
            errors[expr] = output.split("\n")[0].strip()

    return errors


# ── Variable reference checker ───────────────────────────────────────

def _extract_dollar_refs(expr: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _DOLLAR_RE.finditer(expr)]


def _check_variable_references(structure: dict) -> list[dict]:
    findings: list[dict] = []

    survey_items: dict[str, set[str]] = {}
    units = structure.get("units", [])
    survey_positions: dict[str, int] = {}

    for unit in units:
        if unit.get("type") not in SURVEY_UNIT_TYPES:
            continue
        sd = unit.get("survey_data")
        if not isinstance(sd, dict):
            continue
        sname = sd.get("name", "")
        items = sd.get("items", [])
        pos = unit.get("position", "?")
        if sname:
            survey_positions[sname] = pos
            names = set()
            for item in items:
                if isinstance(item, dict):
                    iname = item.get("name", "")
                    if iname:
                        names.add(iname)
            survey_items[sname] = names

    builtin_tables = {
        "survey_unit_sessions", "survey_run_sessions", "survey_users",
        "externals", "shuffle",
        # `.formr` system object (e.g. .formr$run_name); the regex drops the leading dot.
        "formr",
    }

    FORMR_SYSTEM_COLUMNS = {"created", "modified", "ended", "expired"}

    # Functions / globals defined in the run's custom_r store are injected before every R
    # evaluation, so `name$...` where name comes from custom_r is not a survey reference.
    custom_r = structure.get("settings", {}).get("custom_r") or ""
    custom_r_names = {m.group(1) for m in _ASSIGN_RE.finditer(custom_r)}

    expressions = _extract_r_expressions(structure)
    seen_issues: set[tuple[str, str, str]] = set()

    for source in expressions:
        # Variables assigned inside this expression are R locals (data frames, intermediates),
        # not survey references — e.g. `df <- formr_api_fetch_results(...)` then `df$col`.
        locals_here = {m.group(1) for m in _ASSIGN_RE.finditer(source["expr"])}
        refs = _extract_dollar_refs(source["expr"])
        for survey_name, var_name in refs:
            if survey_name in builtin_tables:
                continue
            if survey_name in locals_here or survey_name in custom_r_names:
                continue
            if var_name in FORMR_SYSTEM_COLUMNS:
                continue
            if survey_name not in survey_items:
                key = (source["location"], survey_name, "missing_survey")
                if key not in seen_issues:
                    seen_issues.add(key)
                    findings.append({
                        "severity": "error",
                        "message": f"Survey '{survey_name}' referenced in {source['location']} but no survey unit with that name exists",
                        "location": source["location"],
                    })
            elif var_name not in survey_items[survey_name]:
                key = (source["location"], survey_name, var_name)
                if key not in seen_issues:
                    seen_issues.add(key)
                    findings.append({
                        "severity": "error",
                        "message": f"Item '{var_name}' referenced from survey '{survey_name}' not found — "
                                    f"{source['location']}",
                        "location": source["location"],
                    })

    return findings


# ── Branch flow validator ────────────────────────────────────────────

def _check_branch_flow(structure: dict) -> list[dict]:
    findings: list[dict] = []
    units = structure.get("units", [])
    positions: dict[int, dict] = {}
    branch_targets: list[tuple[int, int, str]] = []

    for unit in units:
        pos = unit.get("position")
        if not isinstance(pos, int):
            continue
        positions[pos] = unit
        utype = unit.get("type", "")

        if utype in ("Branch", "SkipForward", "SkipBackward"):
            if_true = unit.get("if_true")
            if isinstance(if_true, int):
                branch_targets.append((pos, if_true, utype))

    all_positions = set(positions.keys())

    for pos, target, utype in branch_targets:
        if target not in all_positions:
            findings.append({
                "severity": "error",
                "message": f"{utype} at position {pos} targets if_true={target}, but no unit exists at that position",
                "location": f"{utype} at position {pos}",
            })

    if not positions:
        return findings

    min_pos = min(all_positions)
    first_unit = positions.get(min_pos, {})
    first_type = first_unit.get("type", "")
    if first_type in ("Page", "Endpage"):
        findings.append({
            "severity": "error",
            "message": f"The first unit (position {min_pos}) is a '{first_type}' — "
                        f"runs must start with a content unit like Survey or Privacy, "
                        f"not an endpoint",
            "location": f"Position {min_pos} ({first_type})",
        })

    successors: dict[int, set[int]] = {p: set() for p in all_positions}
    sorted_pos = sorted(all_positions)
    for i, p in enumerate(sorted_pos):
        if i + 1 < len(sorted_pos):
            successors[p].add(sorted_pos[i + 1])

    for pos, target, utype in branch_targets:
        successors[pos].add(target)

    reachable = set()
    stack = [min_pos]
    while stack:
        p = stack.pop()
        if p in reachable:
            continue
        reachable.add(p)
        for s in successors.get(p, set()):
            if s not in reachable:
                stack.append(s)

    # Check for Page/Endpage units that block the flow
    sorted_positions = sorted(all_positions)
    for p in sorted_positions:
        unit = positions[p]
        utype = unit.get("type", "")
        if utype in ("Page", "Endpage"):
            # Find the next position after this Page/Endpage
            next_positions = [sp for sp in sorted_positions if sp > p]
            if next_positions:
                next_pos = next_positions[0]
                next_unit = positions[next_pos]
                next_desc = next_unit.get("description", "")
                # Check if any branch explicitly targets this Page/Endpage
                targeted_by_branch = any(t == p for _, t, _ in branch_targets)
                # Check if any branch targets a position beyond the Page/Endpage
                # that requires passing through it
                findings.append({
                    "severity": "warning",
                    "message": f"{utype} at position {p} permanently ends the run session. "
                               f"Unit at position {next_pos} "
                               f"('{next_desc or next_unit.get('type', '')}') "
                               f"is sequenced after it and will be unreachable "
                               f"unless a branch explicitly skips past position {p}.",
                    "location": f"Position {p} ({utype})",
                })

    # Check Wait body values (should be integer positions, not display content)
    for p, unit in positions.items():
        utype = unit.get("type", "")
        if utype == "Wait":
            body = unit.get("body")
            if body is not None:
                try:
                    body_int = int(body)
                    if body_int not in all_positions:
                        findings.append({
                            "severity": "warning",
                            "message": f"Wait body value '{body}' at position {p} is not a valid position "
                                       f"in this run. Valid positions: {sorted(all_positions)}. "
                                       f"Wait body is the position to jump to on click, not display content.",
                            "location": f"Wait body at position {p}",
                        })
                except (ValueError, TypeError):
                    findings.append({
                        "severity": "error",
                        "message": f"Wait body should be an integer position number, not '{body}'. "
                                   f"Wait body is the position to jump to when the participant clicks through, "
                                   f"NOT display content. Use Pause units for display text.",
                        "location": f"Wait body at position {p}",
                    })

    for p, unit in positions.items():
        utype = unit.get("type", "")
        if p not in reachable and utype not in ("Page", "Endpage"):
            desc = unit.get("description", "")
            label = f"'{desc}'" if desc else f"({utype})"
            findings.append({
                "severity": "warning",
                "message": f"Position {p} {label} appears unreachable — no path from the first unit reaches it",
                "location": f"Position {p} ({utype})",
            })

    return findings


# ── Item consistency checker ─────────────────────────────────────────

def _check_item_consistency(structure: dict) -> list[dict]:
    findings: list[dict] = []
    units = structure.get("units", [])

    for unit in units:
        if unit.get("type") not in SURVEY_UNIT_TYPES:
            continue
        sd = unit.get("survey_data")
        if not isinstance(sd, dict):
            continue
        sname = sd.get("name", "(unnamed)")
        pos = unit.get("position", "?")
        items = sd.get("items", [])
        if not isinstance(items, list):
            continue

        item_names = set()
        choice_lists: dict[str, dict] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            iname = item.get("name", "")
            itype = item.get("type", "")
            if iname:
                item_names.add(iname)

            choices = item.get("choices")
            cl = item.get("choice_list")
            if cl and isinstance(cl, str):
                choice_lists[cl] = item

            if itype == "calculate":
                value = item.get("value", "")
                if not value or not value.strip():
                    findings.append({
                        "severity": "warning",
                        "message": f"Calculate item '{iname}' in survey '{sname}' has empty value — "
                                    f"calculate items need an R expression",
                        "location": f"Survey '{sname}' at position {pos}, item '{iname}'",
                    })

        for item in items:
            if not isinstance(item, dict):
                continue
            cl = item.get("choice_list")
            if cl and isinstance(cl, str) and cl != item.get("name"):
                if cl not in item_names:
                    findings.append({
                        "severity": "error",
                        "message": f"Item '{item.get('name')}' references choice_list '{cl}' "
                                    f"but no item named '{cl}' exists in survey '{sname}'",
                        "location": f"Survey '{sname}' at position {pos}",
                    })

        settings = sd.get("settings")
        if isinstance(settings, dict):
            use_paging = settings.get("use_paging")
            if use_paging is not None and use_paging != 0:
                findings.append({
                    "severity": "error",
                    "message": f"Survey '{sname}' has use_paging={use_paging}. "
                                f"Custom Paging is legacy and unsupported — "
                                f"use 'submit' items to create page breaks.",
                    "location": f"Survey '{sname}' at position {pos}",
                })
            if "page_items" in settings:
                findings.append({
                    "severity": "error",
                    "message": f"Survey '{sname}' uses 'page_items' in settings. "
                                f"page_items is not supported — "
                                f"use 'submit' items in the item list to create page breaks.",
                    "location": f"Survey '{sname}' at position {pos}",
                })

    return findings


# ── Common mistakes detector ──────────────────────────────────────────

_ASSIGNMENT_IN_CONDITION_RE = re.compile(r"(?:^|[^=!<>])(?<!=)=(?!=)")


def _check_common_mistakes(structure: dict) -> list[dict]:
    findings: list[dict] = []
    units = structure.get("units", [])

    for unit in units:
        pos = unit.get("position", "?")
        utype = unit.get("type", "")

        if utype in ("Branch", "SkipForward", "SkipBackward"):
            condition = unit.get("condition", "")
            # Skip multi-statement R conditions that use real assignment or define functions:
            # their `=` signs are named arguments (e.g. tryCatch(error = function(e) ...)),
            # not `=`-instead-of-`==` typos. This heuristic only targets simple conditions.
            uses_real_r = "<-" in condition or "function(" in condition
            if condition and condition.strip() and not uses_real_r:
                m = _ASSIGNMENT_IN_CONDITION_RE.search(condition)
                if m and "^" not in condition:
                    # The = sign is at m.end() - 1 (the match includes
                    # an optional preceding char from [^=!<>] or ^).
                    eq_pos = m.end() - 1
                    char_before_eq = condition[eq_pos - 1] if eq_pos > 0 else ""
                    char_after_eq = condition[eq_pos + 1] if eq_pos + 1 < len(condition) else ""
                    is_named_arg = char_before_eq.isalnum() or char_before_eq == "_"
                    if char_before_eq != "!" and char_after_eq != "=" and not is_named_arg:
                        findings.append({
                            "severity": "warning",
                            "message": f"{utype} at position {pos}: condition may contain "
                                        f"assignment (=) instead of comparison (==): "
                                        f"'{condition}'",
                            "location": f"{utype} condition at position {pos}",
                        })

    run_settings = structure.get("settings", {})
    md = run_settings.get("use_material_design")
    if md is not None and md != 0:
        findings.append({
            "severity": "error",
            "message": f"Run-level 'use_material_design' is set to {md}. "
                        f"Material Design is a legacy theme that is not supported.",
            "location": "Run settings",
        })

    expressions = _extract_r_expressions(structure)
    for source in expressions:
        expr = source["expr"].strip()
        if not expr:
            findings.append({
                "severity": "warning",
                "message": f"Empty or whitespace-only R expression in {source['location']}",
                "location": source["location"],
            })

    return findings


# ── Main analysis function ───────────────────────────────────────────

def analyze_run(name: str) -> str:
    structure = load_structure(name)
    units = structure.get("units", [])
    run_name = structure.get("name", name)

    # Run all checks first to determine if there are any issues
    expressions = _extract_r_expressions(structure)
    var_findings = _check_variable_references(structure)
    flow_findings = _check_branch_flow(structure)
    item_findings = _check_item_consistency(structure)
    mistake_findings = _check_common_mistakes(structure)

    total_errors = (
        sum(1 for f in var_findings if f["severity"] == "error")
        + sum(1 for f in flow_findings if f["severity"] == "error")
        + sum(1 for f in item_findings if f["severity"] == "error")
        + sum(1 for f in mistake_findings if f["severity"] == "error")
    )
    total_warnings = (
        sum(1 for f in var_findings if f["severity"] == "warning")
        + sum(1 for f in flow_findings if f["severity"] == "warning")
        + sum(1 for f in item_findings if f["severity"] == "warning")
        + sum(1 for f in mistake_findings if f["severity"] == "warning")
    )

    # R syntax check
    r_errors = 0
    r_results = None
    if expressions and _r_available():
        r_results = _validate_r_syntax(list(dict.fromkeys(e["expr"] for e in expressions)))
        r_errors = sum(1 for v in r_results.values() if v is not None)

    # Short result for clean runs
    r_validation_ok = _r_available() or not expressions
    if total_errors == 0 and total_warnings == 0 and r_errors == 0 and r_validation_ok:
        return f"✅ Run '{run_name}': no issues found (0 errors, 0 warnings)."

    lines: list[str] = []
    lines.append(f"Run '{run_name}' — Analysis Report")
    lines.append(f"{'=' * 60}")
    lines.append("")

    # 1. R syntax validation
    lines.append("## R Syntax Validation")
    if not expressions:
        lines.append("  No R expressions found in this run.")
    elif _r_available():
        unique_exprs = list(dict.fromkeys(e["expr"] for e in expressions))
        expr_to_sources: dict[str, list[str]] = {}
        for e in expressions:
            expr_to_sources.setdefault(e["expr"], []).append(e["location"])

        results = r_results or {}
        has_errors = any(v is not None for v in results.values())
        if not results:
            lines.append(f"  ⚠ R is available but validation returned no results.")
        elif not has_errors:
            lines.append(f"  ✅ All {len(unique_exprs)} R expressions have valid syntax.")
        else:
            error_count = sum(1 for v in results.values() if v is not None)
            lines.append(f"  ❌ {error_count} expression(s) with syntax errors:")
            for expr, error in results.items():
                if error is not None:
                    sources = expr_to_sources.get(expr, [])
                    truncated = expr[:80] + ("..." if len(expr) > 80 else "")
                    lines.append(f"    • \"{truncated}\"")
                    lines.append(f"      → {error}")
                    for src in sources[:3]:
                        lines.append(f"        (used in: {src})")
            ok_count = sum(1 for v in results.values() if v is None)
            if ok_count:
                lines.append(f"  ✅ {ok_count} expression(s) passed syntax check.")
    else:
        lines.append("  ⚠ R syntax validation skipped — R is not installed.")
        lines.append(f"  Found {len(expressions)} R expression(s) that could not be checked.")

    lines.append("")

    # 2. Variable reference checker
    lines.append("## Variable References")
    if not var_findings:
        lines.append("  ✅ All survey$variable references point to existing items.")
    else:
        for f in var_findings:
            icon = "❌" if f["severity"] == "error" else "⚠"
            lines.append(f"  {icon} {f['message']}")
    lines.append("")

    # 3. Branch flow validator
    lines.append("## Branch Flow")
    if not flow_findings:
        lines.append("  ✅ All branch targets are valid, no unreachable units detected.")
    else:
        for f in flow_findings:
            icon = "❌" if f["severity"] == "error" else "⚠"
            lines.append(f"  {icon} {f['message']}")
    lines.append("")

    # 4. Item consistency checker
    lines.append("## Item Consistency")
    if not item_findings:
        lines.append("  ✅ All item references are consistent.")
    else:
        for f in item_findings:
            icon = "❌" if f["severity"] == "error" else "⚠"
            lines.append(f"  {icon} {f['message']}")
    lines.append("")

    # 5. Common mistakes
    lines.append("## Common Mistakes")
    if not mistake_findings:
        lines.append("  ✅ No common mistakes detected.")
    else:
        for f in mistake_findings:
            icon = "❌" if f["severity"] == "error" else "⚠"
            lines.append(f"  {icon} {f['message']}")
    lines.append("")

    # Summary
    lines.append(f"{'=' * 60}")
    lines.append(f"Summary: {r_errors} R syntax errors, {total_errors} errors, {total_warnings} warnings")
    if not _r_available() and expressions:
        lines.append(f"  (R syntax validation was skipped — {len(expressions)} expressions unchecked)")

    return "\n".join(lines)