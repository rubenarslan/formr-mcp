"""Tests for editing.py — add_run_unit, remove_run_unit, duplicate_run_units,
shift_run_positions, renormalize_positions."""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formr_mcp.editing import (
    add_run_unit,
    duplicate_run_units,
    remove_run_unit,
    renormalize_positions,
    shift_run_positions,
)
from formr_mcp.utils import WORKSPACE_DIR, load_structure


@pytest.fixture
def run_file(tmp_path, monkeypatch):
    """Create a temporary .formr/ directory and a test run file."""
    workspace = tmp_path / ".formr"
    workspace.mkdir()
    monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", workspace)
    return workspace


def _write_run(workspace, units, name="test-run"):
    """Write a run structure to the workspace."""
    structure = {"name": name, "units": units, "settings": {}, "files": []}
    path = workspace / f"{name}.json"
    with open(path, "w") as f:
        json.dump(structure, f, indent=2)
    return path


def _read_run(workspace, name="test-run"):
    """Read a run structure from the workspace."""
    path = workspace / f"{name}.json"
    with open(path) as f:
        return json.load(f)


class TestAddRunUnit:
    def test_add_survey(self, run_file):
        _write_run(run_file, [])
        add_run_unit("test-run", "Survey", 10, description="baseline survey")
        structure = _read_run(run_file)
        assert len(structure["units"]) == 1
        assert structure["units"][0]["type"] == "Survey"
        assert structure["units"][0]["position"] == 10

    def test_add_skip_forward(self, run_file):
        _write_run(run_file, [])
        add_run_unit("test-run", "SkipForward", 20, description="eligibility check",
                     condition="screening$eligible==1", if_true=40)
        structure = _read_run(run_file)
        unit = structure["units"][0]
        assert unit["type"] == "SkipForward"
        assert unit["condition"] == "screening$eligible==1"
        assert unit["if_true"] == 40
        assert unit["automatically_jump"] == 1  # default for SkipForward

    def test_add_email(self, run_file):
        _write_run(run_file, [])
        add_run_unit("test-run", "Email", 30, description="welcome email",
                     subject="Welcome!", body="Hello {{login_link}}", account_id=1,
                     cron_only=1)
        structure = _read_run(run_file)
        unit = structure["units"][0]
        assert unit["type"] == "Email"
        assert unit["subject"] == "Welcome!"
        assert unit["account_id"] == 1
        assert unit["cron_only"] == 1

    def test_add_endpage(self, run_file):
        _write_run(run_file, [])
        add_run_unit("test-run", "Endpage", 50, body="Thank you!")
        structure = _read_run(run_file)
        unit = structure["units"][0]
        assert unit["type"] == "Endpage"
        assert unit["body"] == "Thank you!"

    def test_shift_mode(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
        ])
        add_run_unit("test-run", "Survey", 10, description="inserted", insert_mode="shift")
        structure = _read_run(run_file)
        assert len(structure["units"]) == 2
        positions = sorted(u["position"] for u in structure["units"])
        assert positions == [10, 20]  # original 10 shifted to 20

    def test_overwrite_mode(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "original"},
        ])
        add_run_unit("test-run", "Survey", 10, description="replacement", insert_mode="overwrite")
        structure = _read_run(run_file)
        assert len(structure["units"]) == 1
        assert structure["units"][0]["description"] == "replacement"

    def test_add_with_pause(self, run_file):
        _write_run(run_file, [])
        add_run_unit("test-run", "Pause", 20, description="wait until 9AM",
                     wait_minutes=600, relative_to="library(lubridate)\nhour(now()) >= 9",
                     body="Please wait")
        structure = _read_run(run_file)
        unit = structure["units"][0]
        assert unit["type"] == "Pause"
        assert unit["wait_minutes"] == 600

    def test_add_shift_updates_if_true(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "screening"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 30,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 30, "description": "consent"},
        ])
        add_run_unit("test-run", "Survey", 20, description="inserted")
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["if_true"] == 40  # was 30, shifted by 10

    def test_add_shift_updates_wait_body(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "Wait", "position": 20, "body": 30},
            {"type": "Survey", "position": 30, "description": "s2"},
        ])
        add_run_unit("test-run", "Survey", 20, description="inserted")
        structure = _read_run(run_file)
        wait = next(u for u in structure["units"] if u["type"] == "Wait")
        assert wait["body"] == 40  # was 30, shifted by 10

    def test_add_no_shift_preserves_if_true(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "screening"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 30,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 30, "description": "consent"},
        ])
        add_run_unit("test-run", "Survey", 50, description="appended")
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["if_true"] == 30  # no shift, unchanged

    def test_unknown_unit_type_raises(self, run_file):
        _write_run(run_file, [])
        with pytest.raises(ValueError, match="Unknown unit type"):
            add_run_unit("test-run", "InvalidType", 10)

    def test_missing_branch_condition_raises(self, run_file):
        _write_run(run_file, [])
        with pytest.raises(ValueError, match="condition"):
            add_run_unit("test-run", "SkipForward", 10, if_true=30)

    def test_invalid_run_name_raises(self, run_file):
        with pytest.raises(ValueError, match="Invalid run name"):
            add_run_unit("../../etc", "Survey", 10)

    def test_file_not_found_raises(self, run_file):
        with pytest.raises(FileNotFoundError, match="No local file"):
            add_run_unit("nonexistent-run", "Survey", 10)


class TestRemoveRunUnit:
    def test_remove_unit(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Endpage", "position": 20, "body": "end"},
        ])
        result = remove_run_unit("test-run", 20)
        assert "Removed Endpage" in result
        structure = _read_run(run_file)
        assert len(structure["units"]) == 1
        assert structure["units"][0]["position"] == 10

    def test_remove_with_compact(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Survey", "position": 20, "description": "second"},
            {"type": "Endpage", "position": 30, "body": "end"},
        ])
        remove_run_unit("test-run", 20, compact=True)
        structure = _read_run(run_file)
        assert len(structure["units"]) == 2
        positions = sorted(u["position"] for u in structure["units"])
        # 10 stays, 30 compacted to 29
        assert positions == [10, 29]

    def test_remove_nonexistent_position_raises(self, run_file):
        _write_run(run_file, [{"type": "Survey", "position": 10, "description": "first"}])
        with pytest.raises(ValueError, match="No unit found at position 99"):
            remove_run_unit("test-run", 99)

    def test_remove_compact_updates_if_true(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "Survey", "position": 20, "description": "remove me"},
            {"type": "SkipForward", "position": 30, "condition": "x==1", "if_true": 40,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 40, "description": "target"},
        ])
        remove_run_unit("test-run", 20, compact=True)
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["position"] == 29  # was 30, compacted
        assert skip["if_true"] == 39  # was 40, compacted

    def test_remove_compact_updates_wait_body(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "Survey", "position": 20, "description": "remove me"},
            {"type": "Wait", "position": 30, "body": 40},
            {"type": "Survey", "position": 40, "description": "s2"},
        ])
        remove_run_unit("test-run", 20, compact=True)
        structure = _read_run(run_file)
        wait = next(u for u in structure["units"] if u["type"] == "Wait")
        assert wait["position"] == 29
        assert wait["body"] == 39

    def test_remove_flags_dangling_reference(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 30,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 30, "description": "target"},
        ])
        result = remove_run_unit("test-run", 30)
        assert "WARNING" in result
        assert "if_true" in result

    def test_remove_no_compact_no_reference_change(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 40,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 30, "description": "remove me"},
            {"type": "Survey", "position": 40, "description": "target"},
        ])
        remove_run_unit("test-run", 30, compact=False)
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["if_true"] == 40  # unchanged


class TestDuplicateRunUnits:
    def test_duplicate_single_unit(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "baseline"},
        ])
        result = duplicate_run_units("test-run", [10], 100)
        assert "Duplicated 1 unit" in result
        structure = _read_run(run_file)
        assert len(structure["units"]) == 2
        assert structure["units"][1]["position"] == 100
        assert "copy" in structure["units"][1]["description"]

    def test_a_unit_copied_ahead_of_itself_keeps_the_order(self, run_file):
        _write_run(run_file, [{"type": "Pause", "position": p, "description": f"u{p}"} for p in (10, 20, 25, 30, 40)])
        duplicate_run_units("test-run", [25], 20)
        units = _read_run(run_file)["units"]
        assert [u["description"] for u in units] == ["u10", "copy: u25", "u20", "u25", "u30", "u40"]
        assert len({u["position"] for u in units}) == len(units), "no two at one position"

    def test_a_copy_has_no_run_unit_id(self, run_file):
        # formr keeps the unit a run_unit_id names: a copy that kept it would be the original
        _write_run(run_file, [{"type": "Pause", "position": 10, "description": "wait", "run_unit_id": 55}])
        duplicate_run_units("test-run", [10], 100)
        original, copy = _read_run(run_file)["units"]
        assert original["run_unit_id"] == 55
        assert "run_unit_id" not in copy

    def test_duplicate_multiple_units(self, run_file):
        _write_run(run_file, [
            {"type": "Pause", "position": 30, "description": "wait", "wait_minutes": 90},
            {"type": "Email", "position": 40, "description": "reminder", "cron_only": 1},
            {"type": "Survey", "position": 45, "description": "esm"},
        ])
        duplicate_run_units("test-run", [30, 40, 45], 100)
        structure = _read_run(run_file)
        assert len(structure["units"]) == 6
        new_positions = sorted(u["position"] for u in structure["units"] if u["position"] >= 100)
        assert new_positions == [100, 110, 120]

    def test_shift_existing(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "baseline"},
            {"type": "Endpage", "position": 100, "body": "end"},
        ])
        # Duplicate baseline at position 10, targeting pos 100
        # This should shift the Endpage at 100 up to make room
        duplicate_run_units("test-run", [10], 100, shift_existing=True)
        structure = _read_run(run_file)
        # Original at 10, copy at 100, original Endpage shifted up by 10
        positions = sorted(u["position"] for u in structure["units"])
        assert len(structure["units"]) == 3

    def test_no_shift_no_conflict_raises(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "baseline"},
            {"type": "Endpage", "position": 20, "body": "end"},
        ])
        with pytest.raises(ValueError, match="already occupied"):
            duplicate_run_units("test-run", [10], 20, shift_existing=False)

    def test_empty_from_positions_raises(self, run_file):
        _write_run(run_file, [])
        with pytest.raises(ValueError, match="from_positions is empty"):
            duplicate_run_units("test-run", [], 100)

    def test_missing_source_position_raises(self, run_file):
        _write_run(run_file, [{"type": "Survey", "position": 10, "description": "s"}])
        with pytest.raises(ValueError, match="No unit found at position 99"):
            duplicate_run_units("test-run", [99], 100)

    def test_duplicate_remaps_internal_if_true(self, run_file):
        """If_true pointing within the copied block gets remapped to new positions."""
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "esm"},
            {"type": "SkipForward", "position": 20, "condition": "x>3", "if_true": 10,
             "automatically_jump": 1, "automatically_go_on": 1, "description": "loop"},
        ])
        result = duplicate_run_units("test-run", [10, 20], 100)
        structure = _read_run(run_file)
        copies = [u for u in structure["units"] if u["position"] >= 100]
        skip_copy = next(u for u in copies if u["type"] == "SkipForward")
        assert skip_copy["position"] == 110
        assert skip_copy["if_true"] == 100  # remapped from 10 → 100

    def test_duplicate_preserves_external_if_true(self, run_file):
        """If_true pointing outside the copied block is left unchanged."""
        _write_run(run_file, [
            {"type": "Endpage", "position": 10, "body": "ineligible"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 10,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 30, "description": "main"},
        ])
        duplicate_run_units("test-run", [20], 100)
        structure = _read_run(run_file)
        skip_copy = next(u for u in structure["units"] if u["position"] == 100)
        assert skip_copy["if_true"] == 10  # external, unchanged

    def test_duplicate_shift_updates_existing_if_true(self, run_file):
        """When existing units are shifted, if_true in those units is updated."""
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 30,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 30, "description": "target"},
        ])
        # Duplicate to pos 10, which shifts the SkipForward and target
        duplicate_run_units("test-run", [10], 10, shift_existing=True)
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        # Skip was at 20, shifted to 30; if_true was 30, should now be 40
        assert skip["position"] == 30
        assert skip["if_true"] == 40

    def test_duplicate_remaps_wait_body(self, run_file):
        """Wait body pointing within copied block gets remapped."""
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "esm"},
            {"type": "Wait", "position": 20, "body": 10},
        ])
        duplicate_run_units("test-run", [10, 20], 100)
        structure = _read_run(run_file)
        wait_copy = next(u for u in structure["units"] if u["type"] == "Wait" and u["position"] == 110)
        assert wait_copy["body"] == 100  # remapped from 10 → 100


class TestShiftRunPositions:
    def test_shift_up(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Survey", "position": 20, "description": "second"},
            {"type": "Endpage", "position": 30, "body": "end"},
        ])
        result = shift_run_positions("test-run", 20, 10)
        assert "Shifted 2 unit(s)" in result
        structure = _read_run(run_file)
        positions = {u["position"] for u in structure["units"]}
        assert positions == {10, 30, 40}

    def test_shift_down(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Survey", "position": 20, "description": "second"},
            {"type": "Endpage", "position": 30, "body": "end"},
        ])
        result = shift_run_positions("test-run", 20, -5)
        assert "Shifted 2 unit(s)" in result
        structure = _read_run(run_file)
        positions = {u["position"] for u in structure["units"]}
        assert positions == {10, 15, 25}

    def test_zero_delta(self, run_file):
        _write_run(run_file, [{"type": "Survey", "position": 10, "description": "s"}])
        result = shift_run_positions("test-run", 10, 0)
        assert "No change" in result

    def test_shift_updates_if_true(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "screening"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 40,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Endpage", "position": 30, "body": "not eligible"},
            {"type": "Survey", "position": 40, "description": "consent"},
        ])
        shift_run_positions("test-run", 30, 5)
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["if_true"] == 45  # was 40, shifted by 5

    def test_shift_preserves_unaffected_if_true(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "screening"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 10,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 30, "description": "consent"},
        ])
        shift_run_positions("test-run", 30, 5)
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["if_true"] == 10  # below from_position, not shifted

    def test_negative_shift_updates_if_true(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "SkipForward", "position": 20, "condition": "x==1", "if_true": 40,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 40, "description": "s2"},
        ])
        shift_run_positions("test-run", 20, -10)
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["position"] == 10
        assert skip["if_true"] == 30

    def test_shift_updates_wait_body(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "Wait", "position": 20, "body": 40},
            {"type": "Survey", "position": 40, "description": "s2"},
        ])
        shift_run_positions("test-run", 20, 5)
        structure = _read_run(run_file)
        wait = next(u for u in structure["units"] if u["type"] == "Wait")
        assert wait["position"] == 25
        assert wait["body"] == 45

    def test_shift_updates_wait_body_string(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "Wait", "position": 20, "body": "40"},
            {"type": "Survey", "position": 40, "description": "s2"},
        ])
        shift_run_positions("test-run", 40, 5)
        structure = _read_run(run_file)
        wait = next(u for u in structure["units"] if u["type"] == "Wait")
        assert wait["body"] == 45

    def test_shift_does_not_touch_pause_body(self, run_file):
        _write_run(run_file, [
            {"type": "Pause", "position": 10, "body": "Some display text"},
            {"type": "Survey", "position": 20, "description": "s1"},
        ])
        shift_run_positions("test-run", 10, 5)
        structure = _read_run(run_file)
        pause = next(u for u in structure["units"] if u["type"] == "Pause")
        assert pause["body"] == "Some display text"  # unchanged

    def test_shift_updates_skip_backward(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "loop body"},
            {"type": "SkipBackward", "position": 30, "condition": "x<5", "if_true": 10},
        ])
        shift_run_positions("test-run", 10, 5)
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipBackward")
        assert skip["if_true"] == 15


class TestRenormalizePositions:
    def test_basic_renumbering(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Endpage", "position": 20, "body": "end"},
        ])
        result = renormalize_positions("test-run")
        assert "2 unit(s)" in result
        structure = _read_run(run_file)
        positions = sorted(u["position"] for u in structure["units"])
        assert positions == [10, 20]

    def test_messy_positions_cleaned_up(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Survey", "position": 19, "description": "second"},
            {"type": "Survey", "position": 29, "description": "third"},
            {"type": "Endpage", "position": 39, "body": "end"},
        ])
        renormalize_positions("test-run")
        structure = _read_run(run_file)
        positions = sorted(u["position"] for u in structure["units"])
        assert positions == [10, 20, 30, 40]

    def test_remap_if_true_references(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "screening"},
            {"type": "SkipForward", "position": 19, "condition": "x==1", "if_true": 39,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Endpage", "position": 29, "body": "not eligible"},
            {"type": "Survey", "position": 39, "description": "consent"},
        ])
        renormalize_positions("test-run")
        structure = _read_run(run_file)
        positions = sorted(u["position"] for u in structure["units"])
        assert positions == [10, 20, 30, 40]
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["position"] == 20
        assert skip["if_true"] == 40

    def test_remap_wait_body(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "s1"},
            {"type": "Wait", "position": 19, "body": 29},
            {"type": "Survey", "position": 29, "description": "s2"},
        ])
        renormalize_positions("test-run")
        structure = _read_run(run_file)
        wait = next(u for u in structure["units"] if u["type"] == "Wait")
        assert wait["position"] == 20
        assert wait["body"] == 30

    def test_remap_skip_backward(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "loop body"},
            {"type": "SkipBackward", "position": 29, "condition": "x<5", "if_true": 10},
        ])
        renormalize_positions("test-run")
        structure = _read_run(run_file)
        skip = next(u for u in structure["units"] if u["type"] == "SkipBackward")
        assert skip["position"] == 20
        assert skip["if_true"] == 10

    def test_already_clean_positions(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Endpage", "position": 20, "body": "end"},
        ])
        result = renormalize_positions("test-run")
        assert "No position changes needed" in result

    def test_custom_spacing(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 5, "description": "first"},
            {"type": "Endpage", "position": 8, "body": "end"},
        ])
        renormalize_positions("test-run", spacing=5)
        structure = _read_run(run_file)
        positions = sorted(u["position"] for u in structure["units"])
        assert positions == [5, 10]

    def test_empty_run(self, run_file):
        _write_run(run_file, [])
        result = renormalize_positions("test-run")
        assert "No units" in result

    def test_invalid_spacing_raises(self, run_file):
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
        ])
        with pytest.raises(ValueError, match="spacing must be >= 1"):
            renormalize_positions("test-run", spacing=0)

    def test_compact_then_renormalize(self, run_file):
        """Integration: compact removal leaves messy positions, renormalize cleans them."""
        _write_run(run_file, [
            {"type": "Survey", "position": 10, "description": "first"},
            {"type": "Survey", "position": 20, "description": "remove me"},
            {"type": "SkipForward", "position": 30, "condition": "x==1", "if_true": 40,
             "automatically_jump": 1, "automatically_go_on": 1},
            {"type": "Survey", "position": 40, "description": "target"},
        ])
        remove_run_unit("test-run", 20, compact=True)
        structure = _read_run(run_file)
        positions = sorted(u["position"] for u in structure["units"])
        assert positions == [10, 29, 39]

        renormalize_positions("test-run")
        structure = _read_run(run_file)
        positions = sorted(u["position"] for u in structure["units"])
        assert positions == [10, 20, 30]
        skip = next(u for u in structure["units"] if u["type"] == "SkipForward")
        assert skip["if_true"] == 30


class TestWorkspaceDir:
    def test_error_message_includes_absolute_path(self, run_file):
        with pytest.raises(FileNotFoundError, match=r"/.+\.json"):
            load_structure("does-not-exist")

    def test_workspace_dir_env_override(self, tmp_path, monkeypatch):
        alt = tmp_path / "alt-workspace"
        alt.mkdir()
        monkeypatch.setenv("FORMR_WORKSPACE_DIR", str(alt))
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", alt)
        (alt / "test-run.json").write_text(
            '{"name":"test-run","units":[],"settings":{}}', encoding="utf-8"
        )
        result = load_structure("test-run")
        assert result["name"] == "test-run"