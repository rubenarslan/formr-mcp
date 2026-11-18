"""Tools for incrementally editing a local run structure file.

These operate on .formr/<name>.json (which must exist, fetched via
get_run_structure_to_file). After editing, call update_run_structure_from_file
to upload.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from formr_mcp.utils import load_structure, save_structure
from formr_mcp.validation import UNIT_SCHEMAS

# Fields that must be integers
INT_FIELDS = {"position", "if_true", "account_id", "cron_only", "automatically_jump", "automatically_go_on", "api_end", "expire_after", "groups", "time_to_live", "badge_count"}


def _coerce_field(key: str, value: Any) -> Any:
    """Coerce a field value to the expected type for formr."""
    if key in INT_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if key == "position" and isinstance(value, str) and value.isdigit():
        return int(value)
    if key == "if_true" and isinstance(value, str) and value.isdigit():
        return int(value)
    return value


# Position-reference fields: keys that contain integer positions pointing to
# other units (not the unit's own position).
POSITION_REF_FIELDS = {
    ("Branch", "if_true"),
    ("SkipForward", "if_true"),
    ("SkipBackward", "if_true"),
    ("Wait", "body"),
}


def _check_dangling_references(units: list[dict], removed_positions: set[int]) -> list[str]:
    """Find position references pointing to any of the removed_positions.

    Returns a list of warning strings describing each dangling reference.
    """
    warnings = []
    all_positions = {u.get("position") for u in units if isinstance(u.get("position"), int)}
    for u in units:
        p = u.get("position")
        if not isinstance(p, int):
            continue
        utype = u.get("type", "")
        for ref_type, ref_field in POSITION_REF_FIELDS:
            if utype != ref_type:
                continue
            value = u.get(ref_field)
            if value is None:
                continue
            try:
                int_value = int(value)
            except (ValueError, TypeError):
                continue
            if int_value in removed_positions:
                warnings.append(
                    f"{utype} at position {p} has {ref_field}={int_value} "
                    f"referencing a removed position"
                )
    return warnings


def _update_position_references(units: list[dict], position_map: dict[int, int]) -> int:
    """Remap position references (if_true, Wait body) using position_map.

    For each unit, if it has a position-reference field whose value is a key in
    position_map, replace it with the mapped value. Returns the number of
    references updated.
    """
    updated = 0
    for unit in units:
        utype = unit.get("type", "")
        for ref_type, ref_field in POSITION_REF_FIELDS:
            if utype != ref_type:
                continue
            value = unit.get(ref_field)
            if value is None:
                continue
            try:
                int_value = int(value)
            except (ValueError, TypeError):
                continue
            if int_value in position_map:
                unit[ref_field] = position_map[int_value]
                updated += 1
    return updated


def _build_unit(unit_type: str, position: int, **kwargs) -> dict:
    """Build a unit dict with defaults for the given type."""
    if unit_type not in UNIT_SCHEMAS:
        raise ValueError(
            f"Unknown unit type '{unit_type}'. "
            f"Valid types: {', '.join(sorted(UNIT_SCHEMAS))}"
        )

    schema = UNIT_SCHEMAS[unit_type]
    unit: dict[str, Any] = {"type": unit_type, "position": position}

    # Add description if provided
    if "description" in kwargs:
        unit["description"] = kwargs.pop("description")
    elif "special" not in kwargs:
        unit["description"] = ""

    # Add special if provided
    if "special" in kwargs:
        unit["special"] = kwargs.pop("special")

    # Add position-specific defaults
    if unit_type == "SkipForward":
        unit.setdefault("automatically_jump", 1)
        unit.setdefault("automatically_go_on", 1)

    # Add remaining kwargs
    for key, value in kwargs.items():
        unit[key] = _coerce_field(key, value)

    # Ensure required fields are present
    for req in schema["required"]:
        if req not in unit:
            if req == "type":
                pass  # already set
            elif req == "position":
                pass  # already set
            elif req == "condition":
                raise ValueError(f"Branch/Skip unit requires 'condition'")
            elif req == "if_true":
                raise ValueError(f"Branch/Skip unit requires 'if_true'")

    return unit


def _same_survey(replaced: dict, fields: dict) -> bool:
    """Whether an overwrite of a Survey/Form keeps its study: formr keeps the
    placement only then, and the validator takes a unit with a run_unit_id
    for a survey formr has (its use_paging is formr's to keep). Another study
    is a new unit (whose use_paging the validator checks). Other units: True."""
    new = fields.get("survey_data")
    if not isinstance(new, dict):
        return True
    old = replaced.get("survey_data")
    old_name = old.get("name") if isinstance(old, dict) else None
    return old_name is not None and new.get("name") == old_name


def _unit_kind(unit_type: str | None) -> str | None:
    """formr's "same kind of unit" for a placement it keeps (Run::isSamePlacedUnit):
    a Page and an Endpage are one, so are a Survey and a Form (the study decides)."""
    return {"Endpage": "Page", "Form": "Survey"}.get(unit_type, unit_type)


# What a new unit of each type has in the fields formr exports for it, which
# a structure upload leaves as they are on a unit formr keeps (Run::saveKeptUnit):
# an overwrite that keeps the unit sends these for the fields it isn't given.
# Not None throughout: formr refuses NULL for some (a PushMessage's priority, an
# Email's cron_only). A survey or form keeps its study and takes only the description.
_NEW_UNIT_FIELDS: dict[str, dict[str, Any]] = {
    "Pause": {"body": "", "wait_until_time": None, "wait_until_date": None,
              "wait_minutes": None, "relative_to": None},
    "Email": {"subject": None, "account_id": None, "recipient_field": None, "body": None, "cron_only": 0},
    "External": {"address": None, "api_end": 0, "expire_after": 0},
    "Page": {"body": None, "title": None},
    "PushMessage": {"message": "", "topic": None, "priority": "normal", "time_to_live": 86400,
                    "badge_count": 1, "vibrate": 1, "require_interaction": 0, "renotify": 0, "silent": 0},
    "SkipForward": {"automatically_jump": 1, "automatically_go_on": 1},
    "SkipBackward": {"automatically_jump": 1, "automatically_go_on": 1},
    "Shuffle": {"groups": 2},
    "Privacy": {"privacy_label": "", "tos_label": ""},
}
# a Wait's body is the position a participant who comes back early is sent
# to (none: formr stores NULL for '' too), not a Pause's text
_NEW_UNIT_FIELDS["Wait"] = dict(_NEW_UNIT_FIELDS["Pause"], body=None)
_NEW_UNIT_FIELDS["Endpage"] = _NEW_UNIT_FIELDS["Page"]


# ── Public API ────────────────────────────────────────────────────────

def add_run_unit(name: str, unit_type: str, position: int, **kwargs) -> str:
    """Add a unit to the local run structure file.

    If insert_mode is 'shift' and the position is already occupied,
    all units at that position or higher are shifted up by 10.
    If insert_mode is 'overwrite', any existing unit at that position is replaced.
    Default is 'shift'.

    When shifting existing units, position references (if_true, Wait body) are
    automatically updated to reflect the new positions.
    """
    insert_mode = kwargs.pop("insert_mode", "shift")
    structure = load_structure(name)
    units = structure.get("units", [])

    unit = _build_unit(unit_type, position, **kwargs)

    existing_positions = {u.get("position") for u in units if isinstance(u.get("position"), int)}

    shifted = False
    if position in existing_positions:
        if insert_mode == "overwrite":
            replaced = [u for u in units if u.get("position") == position]
            units = [u for u in units if u.get("position") != position]
            # the same kind of unit, changed: formr saves it in place, and
            # whoever is at it stays (another kind is a new unit; for a
            # survey formr also asks that it is the same study)
            if (replaced and "run_unit_id" in replaced[0] and _unit_kind(replaced[0].get("type")) == _unit_kind(unit_type)
                    and _same_survey(replaced[0], kwargs)):
                unit["run_unit_id"] = replaced[0]["run_unit_id"]
                # formr keeps what an upload leaves out of a unit it keeps:
                # the fields not given get a new unit's values, so that the
                # overwrite replaces the unit, not just the fields given
                for field, value in _NEW_UNIT_FIELDS.get(unit_type, {}).items():
                    unit.setdefault(field, value)
        elif insert_mode == "shift":
            position_map = {}
            for u in units:
                p = u.get("position")
                if isinstance(p, int) and p >= position:
                    position_map[p] = p + 10
                    u["position"] = p + 10
            _update_position_references(units, position_map)
            shifted = True
        else:
            raise ValueError(f"Unknown insert_mode '{insert_mode}'. Use 'shift' or 'overwrite'.")

    units.append(unit)
    units.sort(key=lambda u: u.get("position", 0))
    structure["units"] = units

    saved = save_structure(name, structure)
    desc = unit.get("description", "")
    return (
        f"Added {unit_type} at position {position}"
        + (f" ('{desc}')" if desc else "")
        + (f" (shifted existing positions >= {position} up by 10)" if shifted else "")
        + f". {saved}"
    )


def remove_run_unit(name: str, position: int, compact: bool = False) -> str:
    """Remove a unit at the given position from the local run structure file.

    If compact is True, shifts all units at higher positions down by 1 to fill the gap.
    When compacting, position references (if_true, Wait body) are automatically
    updated. Dangling references to the removed position are detected and reported.
    """
    structure = load_structure(name)
    units = structure.get("units", [])

    removed = None
    remaining = []
    for u in units:
        p = u.get("position")
        if p == position:
            removed = u
        else:
            remaining.append(u)

    if removed is None:
        raise ValueError(f"No unit found at position {position}")

    warnings = []

    if compact:
        position_map: dict[int, int] = {}
        for u in remaining:
            p = u.get("position")
            if isinstance(p, int) and p > position:
                position_map[p] = p - 1
                u["position"] = p - 1
        _update_position_references(remaining, position_map)

    dangling = _check_dangling_references(remaining, {position})
    warnings.extend(dangling)

    structure["units"] = remaining
    saved = save_structure(name, structure)

    unit_type = removed.get("type", "?")
    desc = removed.get("description", "")
    parts = [f"Removed {unit_type} at position {position}"]
    if desc:
        parts[0] += f" ('{desc}')"
    if compact:
        parts.append("(compacted positions to fill gap)")
    if warnings:
        parts.append(f"WARNING: {'; '.join(warnings)}")
    parts.append(saved)
    return " ".join(parts)


def duplicate_run_units(name: str, from_positions: list[int], to_start_position: int,
                         shift_existing: bool = True) -> str:
    """Copy units at from_positions to new positions starting at to_start_position.

    Units are copied in order of their original positions. New positions are
    assigned sequentially starting at to_start_position, with gaps incrementing
    by default (10, 20, 30...).

    If shift_existing is True and any new position conflicts with existing units,
    all existing units at >= to_start_position are shifted up to make room.

    Position references (if_true, Wait body) are updated in three ways:
    1. Internal references within the copied block are remapped to the new positions.
    2. References in existing units that are shifted are updated to reflect new positions.
    3. References in copied units pointing to shifted external positions are updated.
    """
    structure = load_structure(name)
    units = structure.get("units", [])

    units_by_pos = {u.get("position"): u for u in units if isinstance(u.get("position"), int)}

    # Find source units
    source_units = []
    for p in sorted(from_positions):
        if p not in units_by_pos:
            raise ValueError(f"No unit found at position {p}")
        source_units.append(copy.deepcopy(units_by_pos[p]))

    if not source_units:
        raise ValueError("from_positions is empty")

    # Calculate new positions
    # Use a step of 10 between each copied unit group, preserving relative order
    new_positions = []
    for i in range(len(source_units)):
        new_positions.append(to_start_position + (i * 10))

    # Build mapping from source positions to new positions (for internal remap)
    source_to_dest: dict[int, int] = {}
    for src_pos, new_pos in zip(sorted(from_positions), new_positions):
        source_to_dest[src_pos] = new_pos

    # Check for conflicts and shift if needed
    existing_positions = {u.get("position") for u in units if isinstance(u.get("position"), int)}
    shift_map: dict[int, int] = {}

    if shift_existing:
        # Find the minimum existing position that conflicts
        conflicts = [p for p in existing_positions if to_start_position <= p]
        if conflicts:
            shift_by = max(new_positions) - min(conflicts) + 10
            for u in units:
                p = u.get("position")
                # the originals too: copied ahead of themselves, they keep
                # their order behind the copies
                if isinstance(p, int) and p >= min(conflicts):
                    shift_map[p] = p + shift_by
                    u["position"] = p + shift_by
    else:
        for np in new_positions:
            if np in existing_positions:
                raise ValueError(
                    f"Position {np} already occupied. Use shift_existing=True or choose a different to_start_position."
                )

    # Update position references in existing (shifted) units
    if shift_map:
        _update_position_references(units, shift_map)

    # Create new units with remapped internal references
    for src, new_pos in zip(source_units, new_positions):
        new_unit = copy.deepcopy(src)
        new_unit["position"] = new_pos
        # a copy is a new unit: formr would take it for the original
        new_unit.pop("run_unit_id", None)
        # Add "copy" prefix to description to distinguish from originals
        # But don't stack "copy:" prefixes on re-duplication
        if "description" in new_unit and new_unit["description"]:
            desc = new_unit["description"]
            if not desc.startswith("copy: "):
                new_unit["description"] = f"copy: {desc}"
        # Remap internal references (if_true, Wait body) within the source block
        utype = new_unit.get("type", "")
        for ref_type, ref_field in POSITION_REF_FIELDS:
            if utype != ref_type:
                continue
            value = new_unit.get(ref_field)
            if value is None:
                continue
            try:
                int_value = int(value)
            except (ValueError, TypeError):
                continue
            if int_value in source_to_dest:
                # Internal reference: remap to corresponding new position
                new_unit[ref_field] = source_to_dest[int_value]
            elif int_value in shift_map:
                # External reference to a shifted position: update it
                new_unit[ref_field] = shift_map[int_value]
            # Otherwise: reference to a position outside both blocks, leave as-is
        units.append(new_unit)

    units.sort(key=lambda u: u.get("position", 0))
    structure["units"] = units
    saved = save_structure(name, structure)

    parts = [
        f"Duplicated {len(source_units)} unit(s) from positions "
        f"{','.join(str(p) for p in sorted(from_positions))} → "
        f"new positions {','.join(str(p) for p in new_positions)}."
    ]
    if shift_map:
        parts.append(f"Updated {len(shift_map)} existing position(s) for room.")
    parts.append(saved)
    return " ".join(parts)


def shift_run_positions(name: str, from_position: int, delta: int) -> str:
    """Shift all units at positions >= from_position by delta.

    Positive delta shifts positions up (making room). Negative delta shifts
    positions down (closing gaps). Useful for inserting groups of units or
    compacting gaps.

    Also updates position references (if_true on Branch/Skip units, body on
    Wait units) to reflect the new positions.
    """
    if delta == 0:
        return "No change: delta is 0."

    structure = load_structure(name)
    units = structure.get("units", [])

    position_map: dict[int, int] = {}
    affected = 0
    for u in units:
        p = u.get("position")
        if isinstance(p, int) and p >= from_position:
            position_map[p] = p + delta
            u["position"] = p + delta
            affected += 1

    if affected == 0:
        return f"No units at positions >= {from_position} to shift."

    ref_updated = _update_position_references(units, position_map)

    units.sort(key=lambda u: u.get("position", 0))
    structure["units"] = units
    saved = save_structure(name, structure)

    direction = "up" if delta > 0 else "down"
    parts = [f"Shifted {affected} unit(s) at positions >= {from_position} {direction} by {abs(delta)}."]
    if ref_updated:
        parts.append(f"Updated {ref_updated} position reference(s) (if_true, Wait body).")
    parts.append(saved)
    return " ".join(parts)


def renormalize_positions(name: str, spacing: int = 10) -> str:
    """Renumber all unit positions to clean multiples of spacing while preserving order.

    Assigns new positions: spacing, spacing*2, spacing*3, ... based on the current
    sorted order. All position references (if_true on Branch/Skip units, body on
    Wait units) are automatically updated to reflect the new positions.

    This is useful after a series of edits that leave positions with gaps or
    irregular spacing (e.g., after compact removal which shifts by 1).

    Safe to call on already-clean structures — positions already at clean multiples
    of spacing that are in the right order will remain (or nearly remain) the same.
    """
    if spacing < 1:
        raise ValueError(f"spacing must be >= 1, got {spacing}")

    structure = load_structure(name)
    units = structure.get("units", [])
    if not units:
        return "No units to renormalize."

    sorted_units = sorted(units, key=lambda u: u.get("position", 0))

    old_positions = []
    for u in sorted_units:
        p = u.get("position")
        if isinstance(p, int):
            old_positions.append(p)
        elif isinstance(p, str) and p.isdigit():
            old_positions.append(int(p))
            u["position"] = int(p)
        else:
            old_positions.append(0)
            u["position"] = 0

    position_map: dict[int, int] = {}
    for i, old_pos in enumerate(old_positions):
        new_pos = (i + 1) * spacing
        if old_pos != new_pos:
            position_map[old_pos] = new_pos
        u = sorted_units[i]
        u["position"] = new_pos

    ref_updated = _update_position_references(sorted_units, position_map)

    structure["units"] = sorted_units
    saved = save_structure(name, structure)

    parts = [f"Renormalized {len(sorted_units)} unit(s) to positions {spacing}, {spacing*2}, ..."]
    if position_map:
        parts.append(f"Changed {len(position_map)} position(s), updated {ref_updated} reference(s).")
    else:
        parts.append("No position changes needed — already clean.")
    parts.append(saved)
    return " ".join(parts)
