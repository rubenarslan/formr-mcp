"""Coverage checks for the form_v2 documentation topic and the item-type docs.

These assert on *content*, not prose style: if the Form unit's identity model,
the showif/value rules, or the new item types stop being documented, the MCP
would start handing agents advice that silently breaks v2 runs.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formr_mcp import documentation as doc
from formr_mcp.validation import FORM_V2_ONLY_ITEM_TYPES, KNOWN_ITEM_TYPES


class TestFormV2Topic:
    def test_topic_registered(self):
        assert "form-v2" in [t["name"] for t in doc.get_topics()]

    def test_form_unit_identity_documented(self):
        body = doc.get_documentation("form-v2")
        assert "form_study_id" in body
        assert "rendering_mode" in body
        # The Survey-vs-Form distinction is the thing people get wrong.
        assert "survey_run_units.unit_id" in body

    def test_export_round_trip_warning_present(self):
        """The export drops a Form's study_id; uploading a fetched structure
        unchanged detaches the study. This warning must not disappear."""
        body = doc.get_documentation("form-v2")
        assert "study_id" in body
        names = [s["name"] for s in doc.get_sections("form-v2")]
        assert any("round-trip" in n for n in names)

    def test_showif_rules(self):
        section = doc.get_documentation(
            "form-v2", "showif-in-v2-is-javascript-and-only-javascript"
        )
        assert "invalid" in section  # r(...) in showif is rejected
        assert "hidden" in section  # the bridge
        # An undefined showif preserves the server's decision, not "show".
        assert "preserve" in section.lower()

    def test_value_needs_r_wrap(self):
        section = doc.get_documentation("form-v2", "value-in-v2-wrap-server-side-r-in-r")
        assert "r(" in section
        assert "sticky" in section

    def test_per_study_settings_documented(self):
        section = doc.get_documentation("form-v2", "per-study-v2-settings")
        for col in ("rendering_mode", "offline_mode", "allow_previous", "layout", "language"):
            assert col in section
        assert "solo" in section
        # FormStrings ships en + de only.
        assert "`de`" in section or "de-AT" in section

    def test_offline_queue_documented(self):
        section = doc.get_documentation("form-v2", "offline-queue-and-background-sync")
        assert "IndexedDB" in section
        assert "Background Sync" in section
        assert "iOS Safari" in section

    def test_compat_scanner_documented(self):
        body = doc.get_documentation("form-v2")
        assert "bin/form_v2_compat_scan.php" in body


class TestNewItemTypeDocs:
    @pytest.mark.parametrize("item_type", ["rank", "visual_analog_scale", "bot_check"])
    def test_item_type_appears_in_item_types_topic(self, item_type):
        body = doc.get_documentation("item-types")
        assert f"`{item_type}`" in body

    def test_form_v2_only_types_flagged_in_docs(self):
        body = doc.get_documentation("item-types")
        for t in FORM_V2_ONLY_ITEM_TYPES:
            assert f"`{t}`" in body
        assert "form_v2-only" in body

    def test_item_types_topic_title_counts_the_classes(self):
        title = next(t["title"] for t in doc.get_topics() if t["name"] == "item-types")
        # KNOWN_ITEM_TYPES carries one alias (datetime_local) over the PHP classes.
        assert "59" in title
        assert len(KNOWN_ITEM_TYPES) == 60

    def test_rank_storage_format_documented(self):
        """Agents need to know rank stores keys joined by ', ' so they write
        the right R to parse it."""
        body = doc.get_documentation("item-types")
        assert "strsplit" in body

    def test_form_unit_listed_for_add_run_unit(self):
        body = doc.get_documentation("editing-tools")
        assert "**Form**" in body
