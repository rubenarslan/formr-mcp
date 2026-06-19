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
from formr_mcp.utils import run_filepath, validate_run_name


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
    # formr's rule (RunResource.php): /^[a-zA-Z][a-zA-Z0-9-]{2,255}$/
    # The MCP must accept everything formr accepts (not be stricter).
    def test_accepts_uppercase_and_mixed(self):
        for ok in ("My-Run", "ABC", "MixedCase123", "a-B-3"):
            validate_run_name(ok)  # must not raise

    def test_accepts_max_length_256(self):
        validate_run_name("A" + "a" * 255)  # 256 chars — formr's max

    def test_rejects_too_long_257(self):
        with pytest.raises(ValueError):
            validate_run_name("A" + "a" * 256)  # 257 chars

    def test_rejects_leading_digit_and_specials(self):
        for bad in ("1run", "-run", "run_name", "run.name", "ru"):
            with pytest.raises(ValueError):
                validate_run_name(bad)

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