import sys
import os
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as server_mod
from server import VALID_SETTINGS, _normalize_survey_choices
from formr_mcp.utils import run_filepath, validate_new_run_name, validate_run_name


class TestValidSettings:
    def test_known_settings_complete(self):
        expected = {
            "title", "description", "footer_text", "public_blurb",
            "privacy", "tos", "header_image_path", "custom_css", "custom_js",
            "custom_r", "cron_active", "expiresOn",
            "expire_cookie_value", "expire_cookie_unit", "public", "locked",
        }
        assert VALID_SETTINGS == expected


class TestSettingsValidation:
    def test_rejects_unknown_settings(self):
        unknown = {"foo"} - VALID_SETTINGS
        assert unknown == {"foo"}

    def test_rejects_mix_of_known_and_unknown(self):
        settings = {"title": "Hello", "invalid_key": 1}
        unknown = set(settings) - VALID_SETTINGS
        assert unknown == {"invalid_key"}

    def test_all_known_settings_pass(self):
        settings = {"title": "Test", "locked": 0}
        unknown = set(settings) - VALID_SETTINGS
        assert unknown == set()

    def test_use_material_design_one_rejected(self):
        with pytest.raises(ValueError, match="must be 0 or omitted"):
            asyncio.run(
                server_mod.update_run_settings("demo", {"use_material_design": 1}, ctx=MagicMock())
            )

    def test_use_material_design_zero_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")
        mock_client = AsyncMock()
        mock_client.get_run.return_value = {"name": "demo", "settings": {}}
        mock_client.patch_run.return_value = None
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        result = asyncio.run(
            server_mod.update_run_settings("demo", {"use_material_design": 0}, ctx=MagicMock())
        )
        assert result["name"] == "demo"

    def test_use_material_design_is_not_a_known_setting(self):
        assert "use_material_design" not in VALID_SETTINGS


class TestRunFilepath:
    def test_derives_path_from_name(self):
        path = run_filepath("my-run")
        assert path.name == "my-run.json"
        assert str(path).endswith(".formr/my-run.json")

    def test_creates_workspace_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")
        path = server_mod.run_filepath("test-run")
        assert (tmp_path / "ws").is_dir()
        assert path.name == "test-run.json"

    def test_bak_path(self):
        path = run_filepath("my-run")
        bak = path.with_suffix(".json.bak")
        assert bak.name == "my-run.json.bak"

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid run name"):
            run_filepath("../../etc/passwd")

    def test_rejects_short_name(self):
        with pytest.raises(ValueError, match="Invalid run name"):
            run_filepath("ab")


class TestRunNameParityWithFormr:
    # formr's rule for a new run (Run::NAME_PATTERN): /^[a-zA-Z][a-zA-Z0-9-]{2,44}$/D;
    # a run named under older rules may keep underscores (Run::linkedName).
    # The MCP must accept everything formr accepts (not be stricter).
    def test_accepts_uppercase_and_mixed(self):
        for ok in ("My-Run", "ABC", "MixedCase123", "a-B-3"):
            validate_run_name(ok)  # must not raise
            validate_new_run_name(ok)

    def test_accepts_max_length_45(self):
        validate_run_name("A" + "a" * 44)  # 45 chars — survey_runs.name holds 45
        validate_new_run_name("A" + "a" * 44)

    def test_rejects_too_long_46(self):
        with pytest.raises(ValueError):
            validate_run_name("A" + "a" * 45)

    def test_rejects_leading_digit_and_specials(self):
        for bad in ("1run", "-run", "run.name", "ru", "abc\n"):
            with pytest.raises(ValueError):
                validate_run_name(bad)

    def test_an_old_run_keeps_its_underscores_but_a_new_one_has_none(self):
        validate_run_name("old_study")  # a run named before 2018: the tools reach it
        with pytest.raises(ValueError, match="no underscores"):
            validate_new_run_name("old_study")

    def test_accepts_uppercase(self, tmp_path, monkeypatch):
        # formr allows uppercase (RunResource.php: [a-zA-Z]); the MCP must not be stricter.
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")
        path = run_filepath("My-Run")
        assert path.name == "My-Run.json"


class TestGetRunStructureToFile:
    def test_creates_file_and_writes_structure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")

        mock_client = AsyncMock()
        mock_client.get_run_structure.return_value = {
            "units": [{"type": "Survey", "position": 10}]
        }
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)

        result = asyncio.run(
            server_mod.get_run_structure_to_file("demo", ctx=MagicMock())
        )
        assert "wrote" in result
        filepath = tmp_path / "ws" / "demo.json"
        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert len(data["units"]) == 1

    def test_backs_up_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")

        filepath = server_mod.run_filepath("demo")
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text('{"old": true}')

        mock_client = AsyncMock()
        mock_client.get_run_structure.return_value = {"units": []}
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)

        asyncio.run(
            server_mod.get_run_structure_to_file("demo", ctx=MagicMock())
        )

        bak = filepath.with_suffix(".json.bak")
        assert bak.exists()
        assert json.loads(bak.read_text()) == {"old": True}

    def test_no_backup_when_no_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")

        mock_client = AsyncMock()
        mock_client.get_run_structure.return_value = {"units": []}
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)

        asyncio.run(
            server_mod.get_run_structure_to_file("new-run", ctx=MagicMock())
        )

        bak = server_mod.run_filepath("new-run").with_suffix(".json.bak")
        assert not bak.exists()


class TestUpdateRunStructureFromFile:
    def test_raises_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")

        with pytest.raises(FileNotFoundError, match="No local file"):
            asyncio.run(
                server_mod.update_run_structure_from_file("nonexistent", ctx=MagicMock())
            )

    def test_removes_bak_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")

        filepath = server_mod.run_filepath("demo")
        filepath.parent.mkdir(parents=True, exist_ok=True)

        structure = {
            "units": [{"type": "Survey", "position": 10, "survey_data": {"name": "s", "items": [
                {"type": "note", "name": "n1", "label": "Welcome", "optional": 1}
            ]}}]
        }
        filepath.write_text(json.dumps(structure))

        bak = filepath.with_suffix(".json.bak")
        bak.write_text('{"old": true}')

        mock_client = AsyncMock()
        mock_client.put_run_structure.return_value = None
        mock_client.get_run_structure.return_value = structure
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)

        result = asyncio.run(
            server_mod.update_run_structure_from_file("demo", ctx=MagicMock())
        )
        assert "successfully updated" in result
        assert not bak.exists()

    def test_the_file_takes_the_ids_formr_gave_new_units(self, tmp_path, monkeypatch):
        # uploaded again without them, the units made last time would be new again
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")
        filepath = server_mod.run_filepath("demo")
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(json.dumps({"units": [{"type": "Pause", "position": 10, "wait_minutes": 5}]}))
        stored = {"units": [{"type": "Pause", "position": 10, "wait_minutes": 5, "run_unit_id": 41}]}
        mock_client = AsyncMock()
        mock_client.put_run_structure.return_value = {"units_imported": 1, "kept": 0, "moved": 0, "created": 1,
                                                      "removed": 0, "participants_moved": 0, "steps_ended": 0}
        mock_client.get_run_structure.return_value = stored
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)

        result = asyncio.run(server_mod.update_run_structure_from_file("demo", ctx=MagicMock()))
        assert "created 1" in result
        assert json.loads(filepath.read_text())["units"][0]["run_unit_id"] == 41
        assert mock_client.put_run_structure.await_args.kwargs == {"confirm_removal": False}

    def test_validation_error_preserves_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("formr_mcp.utils.WORKSPACE_DIR", tmp_path / "ws")

        filepath = server_mod.run_filepath("bad-run")
        filepath.parent.mkdir(parents=True, exist_ok=True)

        bad_structure = {"units": [{"type": "Survey", "position": 10, "survey_data": {"name": "s", "items": []}, "condition": "x", "if_true": "not_int"}]}
        filepath.write_text(json.dumps(bad_structure))

        bak = filepath.with_suffix(".json.bak")
        bak.write_text('{"old": true}')

        with pytest.raises(ValueError, match="Structure validation failed"):
            asyncio.run(
                server_mod.update_run_structure_from_file("bad-run", ctx=MagicMock())
            )

        assert filepath.exists()
        assert bak.exists()


class TestInlineStructureTools:
    def test_get_run_structure_returns_json_text(self, monkeypatch):
        mock_client = AsyncMock()
        mock_client.get_run_structure.return_value = {
            "units": [{"type": "Survey", "position": 10}]
        }
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        out = asyncio.run(server_mod.get_run_structure("demo", ctx=MagicMock()))
        assert isinstance(out, str)
        parsed = json.loads(out)
        assert parsed["units"][0]["position"] == 10

    def test_update_run_structure_uploads(self, monkeypatch):
        structure = {"units": [{"type": "Survey", "position": 10, "survey_data": {
            "name": "s", "items": [
                {"type": "note", "name": "n1", "label": "Hi", "optional": 1}
            ]}}]}
        mock_client = AsyncMock()
        mock_client.put_run_structure.return_value = None
        mock_client.get_run_structure.return_value = structure
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        out = asyncio.run(
            server_mod.update_run_structure("demo", json.dumps(structure), ctx=MagicMock())
        )
        assert "successfully updated" in out
        mock_client.put_run_structure.assert_awaited_once()

    def test_update_run_structure_rejects_bad_json(self, monkeypatch):
        monkeypatch.setattr(server_mod, "_client", lambda ctx: AsyncMock())
        with pytest.raises(ValueError, match="not valid JSON"):
            asyncio.run(server_mod.update_run_structure("demo", "{not json", ctx=MagicMock()))

    def test_update_run_structure_requires_units(self, monkeypatch):
        monkeypatch.setattr(server_mod, "_client", lambda ctx: AsyncMock())
        with pytest.raises(ValueError, match="units"):
            asyncio.run(server_mod.update_run_structure("demo", '{"foo": 1}', ctx=MagicMock()))

    def test_update_run_structure_validation_error(self, monkeypatch):
        bad = {"units": [{"type": "Survey", "position": 10,
                          "survey_data": {"name": "s", "items": []},
                          "condition": "x", "if_true": "not_int"}]}
        monkeypatch.setattr(server_mod, "_client", lambda ctx: AsyncMock())
        with pytest.raises(ValueError, match="validation failed"):
            asyncio.run(server_mod.update_run_structure("demo", json.dumps(bad), ctx=MagicMock()))

class TestVerifyUpload:
    """formr can answer 200 for an import it rolled back; the upload tools
    compare what was sent with what is stored."""

    def test_matching_structure_passes_with_page_endpage_and_string_positions(self):
        sent = {"units": [{"type": "Survey", "position": "10"}, {"type": "Page", "position": 20}]}
        stored = {"units": [{"type": "Endpage", "position": 20}, {"type": "Survey", "position": 10}]}
        server_mod._verify_upload("r", sent, stored)

    def test_rolled_back_import_is_reported(self):
        sent = {"units": [{"type": "Survey", "position": 10}, {"type": "SkipForward", "position": 20}]}
        stored = {"units": [{"type": "Survey", "position": 10}, {"type": "Pause", "position": 20}]}
        with pytest.raises(ValueError, match=r"Missing \(position, type\): \[\(20, 'SkipForward'\)\]"):
            server_mod._verify_upload("r", sent, stored)


class TestStoredUnitTypes:
    def test_a_study_placed_as_form_may_be_stored_as_survey(self):
        # formr names a study's unit by the study's engine: a Form unit for a v1 study is a Survey
        sent = {"units": [{"type": "Form", "position": 15, "study_id": 30}, {"type": "Page", "position": 20}]}
        stored = {"units": [{"type": "Survey", "position": 15}, {"type": "Endpage", "position": 20}]}
        server_mod._verify_upload("demo", sent, stored)

    def test_survey_names_take_underscores(self):
        from formr_mcp.utils import validate_survey_name
        validate_survey_name("daily_diary")
        with pytest.raises(ValueError):
            validate_survey_name("daily-diary")


class TestRound4:
    def test_get_run_structure_is_compact_json(self, monkeypatch):
        mock_client = AsyncMock()
        mock_client.get_run_structure.return_value = {"units": [{"type": "Survey", "position": 10, "description": "Ä"}]}
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        out = asyncio.run(server_mod.get_run_structure("demo", ctx=MagicMock()))
        assert out == '{"units":[{"type":"Survey","position":10,"description":"Ä"}]}'

    def test_an_upload_without_version_says_formr_did_not_check_it(self, monkeypatch):
        sent = {"units": [{"type": "Pause", "position": 10, "description": "wait", "body": "Please wait",
                           "wait_minutes": "5", "run_unit_id": 1},
                          {"type": "Endpage", "position": 20, "description": "", "body": "Thanks", "run_unit_id": 2}]}
        mock_client = AsyncMock()
        mock_client.put_run_structure.return_value = {"kept": 2, "moved": 0, "created": 0, "removed": 0,
                                                      "participants_moved": 0, "steps_ended": 0}
        mock_client.get_run_structure.return_value = dict(sent, version="v2")
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        out = asyncio.run(server_mod.update_run_structure("demo", json.dumps(sent), ctx=MagicMock()))
        assert "successfully updated" in out and 'no "version"' in out
        out = asyncio.run(server_mod.update_run_structure("demo", json.dumps(dict(sent, version="v1")), ctx=MagicMock()))
        assert "version" not in out
        mock_client.get_run_structure.return_value = sent  # formr before v2.0.0-rc13: no version at all
        out = asyncio.run(server_mod.update_run_structure("demo", json.dumps(sent), ctx=MagicMock()))
        assert "version" not in out

    def test_tools_that_overwrite_a_live_run_are_destructive(self):
        tools = {t.name: t for t in asyncio.run(server_mod.mcp.list_tools())}
        for name in ("update_run_structure", "update_run_structure_from_file", "upload_run_file",
                     "update_run_settings", "delete_run", "delete_run_file", "test_session_action"):
            assert tools[name].annotations.destructiveHint is True, name
        for name in ("create_run", "create_test_sessions", "add_run_unit"):
            assert tools[name].annotations.destructiveHint is False, name

    def test_a_symlink_to_a_sibling_directory_is_outside_the_workspace(self, tmp_path, monkeypatch):
        from formr_mcp import utils
        workspace = tmp_path / ".formr"
        other = tmp_path / ".formr-other"
        workspace.mkdir()
        other.mkdir()
        (workspace / "sym-run.json").symlink_to(other / "victim.json")
        monkeypatch.setattr(utils, "WORKSPACE_DIR", workspace)
        with pytest.raises(ValueError, match="escapes"):
            utils.safe_run_filepath("sym-run")

    def test_a_unit_type_that_is_no_string_is_a_validation_error(self, monkeypatch):
        from formr_mcp.validation import validate_structure
        assert validate_structure({"units": [{"type": ["Survey"], "position": 10}]}) == ["Unit at index 0 is missing 'type'"]
        mock_client = AsyncMock()
        monkeypatch.setattr(server_mod, "_client", lambda ctx: mock_client)
        with pytest.raises(ValueError, match="missing 'type'"):
            asyncio.run(server_mod.update_run_structure(
                "demo", json.dumps({"units": [{"type": ["Survey"], "position": 10, "survey_data": {}}]}), ctx=MagicMock()))
        mock_client.put_run_structure.assert_not_awaited()
