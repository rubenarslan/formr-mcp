"""Shared utilities for run name validation and file path handling.

These are used across server.py, editing.py, analysis.py, and summarize.py.
"""

from __future__ import annotations

import contextvars
import copy
import json
import os
import re
import shutil
from pathlib import Path

_env_workspace = os.environ.get("FORMR_WORKSPACE_DIR")
WORKSPACE_DIR = Path(_env_workspace) if _env_workspace else Path(".formr")

# Mirror formr's own rule exactly (RunResource.php: /^[a-zA-Z][a-zA-Z0-9-]{2,255}$/)
# so the MCP is never stricter than the server — formr allows uppercase and up
# to 256 chars. Reserved-name checks are left to formr (being more permissive
# here just defers to its server-side validation).
VALID_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{2,255}$")


# formr's SurveyStudy::NAME_PATTERN: a survey is named after its file, so
# underscores, not hyphens, and at most 65 characters
VALID_SURVEY_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,64}\Z")


def validate_survey_name(name: str) -> None:
    """Validate a survey name as formr does. Raises ValueError if invalid."""
    if not isinstance(name, str) or not VALID_SURVEY_NAME.match(name):
        raise ValueError(
            f"Invalid survey name {name!r}. A survey's name starts with a letter and has "
            f"only letters, digits and underscores, 3-65 characters."
        )


def validate_run_name(name: str) -> None:
    """Validate a run name. Raises ValueError if invalid.

    Name must start with a letter (a-z or A-Z), contain only letters, digits,
    and hyphens, and be 3-256 characters long.
    """
    if not VALID_NAME.match(name):
        raise ValueError(
            f"Invalid run name '{name}'. "
            f"Name must start with a letter (a-z or A-Z), contain only letters, "
            f"digits, and hyphens, and be 3-256 characters long."
        )


def safe_run_filepath(name: str) -> Path:
    """Validate name and return a safe file path within the workspace.

    Raises ValueError if name is invalid or path traversal is detected.
    """
    validate_run_name(name)
    path = (WORKSPACE_DIR / f"{name}.json").resolve()
    workspace_resolved = WORKSPACE_DIR.resolve()
    if not str(path).startswith(str(workspace_resolved)):
        raise ValueError("Path traversal detected: file path escapes workspace directory")
    return path


def workspace_file(path: str) -> Path:
    """Resolve a file the caller names for upload: relative to the workspace,
    or absolute but inside it. Symlinks are resolved first, so one pointing
    out of the workspace is refused too.

    The MCP may serve over HTTP from a shared host, where reading any path the
    model names would hand it the server's own files (its .env among them) to
    upload into a run. Raises ValueError outside the workspace, or when the
    file is missing.
    """
    workspace = WORKSPACE_DIR.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = WORKSPACE_DIR / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError(
            f"'{path}' is outside the MCP workspace ({workspace}). Copy the file into "
            f"the workspace first, or pass its content as text or content_base64."
        )
    if not resolved.is_file():
        raise ValueError(f"No such file in the workspace: {resolved}")
    return resolved


def run_filepath(name: str) -> Path:
    """Like safe_run_filepath but also creates the workspace directory if needed."""
    path = safe_run_filepath(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# Over HTTP the server keeps no workspace: a tool that reads or edits a run's
# structure is given it, fetched from formr with the caller's token, for the
# one call (server._run_structure). Unset (stdio), the files are used.
MEMORY_STRUCTURES: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "formr_memory_structures", default=None)


def load_structure(name: str) -> dict:
    """Load a run structure from the workspace, or the one fetched for this call.

    Raises FileNotFoundError if the file does not exist.
    """
    store = MEMORY_STRUCTURES.get()
    if store is not None:
        validate_run_name(name)
        if name not in store:
            raise FileNotFoundError(f"Run '{name}' was not fetched for this call.")
        return copy.deepcopy(store[name])
    filepath = safe_run_filepath(name)
    if not filepath.exists():
        raise FileNotFoundError(
            f"No local file for run '{name}' at {filepath}. "
            f"Call get_run_structure_to_file(\"{name}\") first."
        )
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def save_structure(name: str, structure: dict) -> str:
    """Save a run structure to the workspace, creating a backup first.

    Returns a summary string like 'Saved 5 units to .formr/name.json'.
    For a structure fetched for this call (MEMORY_STRUCTURES), it is kept
    there for the tool to upload if it changed (server._run_structure).
    """
    store = MEMORY_STRUCTURES.get()
    if store is not None:
        validate_run_name(name)
        store[name] = structure
        return f"The run's structure now has {len(structure.get('units', []))} units."
    path = safe_run_filepath(name)
    bak_path = path.with_suffix(".json.bak")
    if path.exists():
        shutil.copy2(str(path), str(bak_path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)
    units = len(structure.get("units", []))
    return f"Saved {units} units to {path}"