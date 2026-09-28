import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formr_mcp.validation import (
    FORM_V2_ONLY_ITEM_TYPES,
    KNOWN_ITEM_TYPES,
    KNOWN_TYPES,
    SURVEY_UNIT_TYPES,
    get_unit_type_schemas,
    validate_structure,
    validate_survey_data,
)


class TestSurveyDataSettings:
    def _make_survey_unit(self, name, items, settings=None):
        unit = {
            "type": "Survey",
            "position": 10,
            "description": name,
            "special": "",
            "survey_data": {
                "name": name,
                "items": items,
            },
        }
        if settings is not None:
            unit["survey_data"]["settings"] = settings
        return unit

    def test_use_paging_nonzero_rejected(self):
        items = [{"type": "note", "name": "n1", "label": "Hi", "optional": 1}]
        unit = self._make_survey_unit("demo", items, {"use_paging": 1})
        errors = validate_structure({"units": [unit]})
        paging_errors = [e for e in errors if "use_paging" in e]
        assert len(paging_errors) == 1
        assert "must be 0 or omitted" in paging_errors[0]

    def test_use_paging_zero_accepted(self):
        items = [{"type": "note", "name": "n1", "label": "Hi", "optional": 1}]
        unit = self._make_survey_unit("demo", items, {"use_paging": 0})
        errors = validate_structure({"units": [unit]})
        paging_errors = [e for e in errors if "use_paging" in e]
        assert len(paging_errors) == 0

    def test_use_paging_absent_accepted(self):
        items = [{"type": "note", "name": "n1", "label": "Hi", "optional": 1}]
        unit = self._make_survey_unit("demo", items)
        errors = validate_structure({"units": [unit]})
        paging_errors = [e for e in errors if "use_paging" in e]
        assert len(paging_errors) == 0

    def test_page_items_rejected(self):
        items = [{"type": "note", "name": "n1", "label": "Hi", "optional": 1}]
        unit = self._make_survey_unit("demo", items, {"page_items": ["item1"]})
        errors = validate_structure({"units": [unit]})
        pi_errors = [e for e in errors if "page_items" in e]
        assert len(pi_errors) == 1
        assert "not supported" in pi_errors[0]

    def test_both_forbidden_settings_rejected(self):
        items = [{"type": "note", "name": "n1", "label": "Hi", "optional": 1}]
        unit = self._make_survey_unit("demo", items, {"use_paging": 1, "page_items": ["n1"]})
        errors = validate_structure({"units": [unit]})
        assert len(errors) >= 2

    def test_valid_settings_accepted(self):
        items = [{"type": "note", "name": "n1", "label": "Hi", "optional": 1}]
        unit = self._make_survey_unit("demo", items, {
            "add_percentage_points": 1,
            "enable_instant_validation": 0,
            "unlinked": 0,
        })
        errors = validate_structure({"units": [unit]})
        forbidden_errors = [e for e in errors if "use_paging" in e or "page_items" in e]
        assert len(forbidden_errors) == 0

    def test_use_paging_string_value_rejected(self):
        items = [{"type": "note", "name": "n1", "label": "Hi", "optional": 1}]
        unit = self._make_survey_unit("demo", items, {"use_paging": "1"})
        errors = validate_structure({"units": [unit]})
        paging_errors = [e for e in errors if "use_paging" in e]
        assert len(paging_errors) == 1

class TestImportableTypes:
    """formr's importer only creates RunUnitFactory::SupportedUnits; anything
    else used to fail server-side with an opaque unit-count mismatch."""

    @pytest.mark.parametrize("utype,hint", [("Branch", "SkipForward"), ("Privacy", "run setting")])
    def test_unimportable_type_is_refused_with_a_hint(self, utype, hint):
        units = [
            {"type": "Survey", "position": 10, "survey_data": {"name": "s", "items": [{"type": "note", "name": "n", "label": "x"}]}},
            {"type": utype, "position": 20, "condition": "TRUE", "if_true": 10},
        ]
        errors = validate_structure({"units": units})
        assert len(errors) == 1
        assert f"'{utype}' at index 1" in errors[0] and hint in errors[0]

    def test_unit_type_schemas_do_not_offer_branch_or_privacy(self):
        from formr_mcp.validation import get_unit_type_schemas
        schemas = get_unit_type_schemas()
        assert "Branch" not in schemas and "Privacy" not in schemas


class TestChoiceLabels:
    def _survey(self, choices):
        return {"units": [{"type": "Survey", "position": 10, "survey_data": {"name": "s", "items": [
            {"type": "mc", "name": "q1", "label": "Pick", "choices": choices},
        ]}}]}

    def test_object_shaped_choice_is_refused(self):
        errors = validate_structure(self._survey({"1": {"label": "a", "label_parsed": None}, "2": "b"}))
        assert any("choice '1' must be a plain label string" in e for e in errors)
        assert not any("choice '2'" in e for e in errors)

    @pytest.mark.parametrize("choices", [{"1": "yes", "2": "no"}, {"1": 1, "2": 2.5}, ["a", "b"], {"1": "a", "2": None}])
    def test_plain_labels_and_null_pass(self, choices):
        assert not any("plain label" in e for e in validate_structure(self._survey(choices)))


class TestFormV2ItemTypes:
    """Item types added on the form_v2 branch (application/Model/Item/*.php)."""

    def _survey_unit(self, items, unit_type="Survey"):
        return {
            "type": unit_type,
            "position": 10,
            "description": "demo",
            "survey_data": {"name": "demo", "items": items},
        }

    @pytest.mark.parametrize("item_type", ["rank", "visual_analog_scale", "bot_check", "time"])
    def test_new_item_types_are_known(self, item_type):
        assert item_type in KNOWN_ITEM_TYPES

    def test_rank_accepted_with_choices(self):
        items = [{"type": "rank", "name": "prefs", "label": "Rank these",
                  "choices": {"1": "A", "2": "B", "3": "C"}}]
        errors = validate_structure({"units": [self._survey_unit(items)]})
        assert errors == []

    def test_rank_without_choices_rejected(self):
        items = [{"type": "rank", "name": "prefs", "label": "Rank these"}]
        errors = validate_structure({"units": [self._survey_unit(items)]})
        assert any("prefs" in e and "choices" in e for e in errors)

    def test_visual_analog_scale_requires_choices(self):
        ok = [{"type": "visual_analog_scale", "name": "vas", "label": "How much?",
               "choices": {"1": "not at all", "2": "very much"}}]
        assert validate_structure({"units": [self._survey_unit(ok)]}) == []
        bad = [{"type": "visual_analog_scale", "name": "vas", "label": "How much?"}]
        assert any("vas" in e for e in validate_structure({"units": [self._survey_unit(bad)]}))

    def test_bot_check_choices_are_optional_not_forbidden(self):
        """BotCheck_Item blanks its choices before parent::validate(), so
        choice1..3 are accepted as widget copy — and omitting them is fine too."""
        without = [{"type": "bot_check", "name": "human", "label": "Quick check"}]
        assert validate_structure({"units": [self._survey_unit(without)]}) == []
        with_copy = [{"type": "bot_check", "name": "human", "label": "Quick check",
                      "choices": {"1": "I am human", "2": "Confirmed", "3": "Checking…"}}]
        assert validate_structure({"units": [self._survey_unit(with_copy)]}) == []

    def test_datetime_local_both_spellings_known(self):
        for t in ("datetime-local", "datetime_local"):
            items = [{"type": t, "name": "when", "label": "When?"}]
            assert validate_structure({"units": [self._survey_unit(items)]}) == []

    def test_form_v2_only_set(self):
        assert FORM_V2_ONLY_ITEM_TYPES == {"rank", "bot_check"}
        assert FORM_V2_ONLY_ITEM_TYPES <= KNOWN_ITEM_TYPES

    def test_unknown_item_type_still_rejected(self):
        items = [{"type": "definitely_not_an_item", "name": "x", "label": "x"}]
        errors = validate_structure({"units": [self._survey_unit(items)]})
        assert any("unknown item type" in e for e in errors)


class TestFormUnit:
    def test_form_is_a_known_unit_type(self):
        assert "Form" in KNOWN_TYPES
        assert SURVEY_UNIT_TYPES == {"Survey", "Form"}

    def test_form_unit_accepted(self):
        errors = validate_structure({"units": [
            {"type": "Form", "position": 10, "description": "diary", "study_id": 291},
            {"type": "Endpage", "position": 20, "body": "done"},
        ]})
        assert errors == []

    def test_form_schema_documents_study_id(self):
        schemas = get_unit_type_schemas()
        assert "Form" in schemas
        assert "study_id" in schemas["Form"]["optional"]
        assert "rendering_mode" in schemas["Form"]["description"]

    def test_form_survey_data_is_validated_like_survey(self):
        errors = validate_structure({"units": [{
            "type": "Form", "position": 10,
            "survey_data": {"name": "demo", "items": [
                {"type": "mc", "name": "m1", "label": "pick"},  # missing choices
            ]},
        }]})
        assert any("Form unit at position 10" in e for e in errors)
