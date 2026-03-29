"""Tests for mission JSON schema validation.

Covers: SC-3 (sample validates against schema), mission state machine states,
required fields enforcement, task structure within missions.
"""
import json
import copy
from pathlib import Path

import pytest
import jsonschema

SCHEMA_DIR = Path("/home/alex/.openclaw/workspace/memory/missions/schema")
SCHEMA_PATH = SCHEMA_DIR / "mission_schema.json"
SAMPLE_PATH = SCHEMA_DIR / "mission_sample.json"


@pytest.fixture
def schema():
    with open(SCHEMA_PATH) as f:
        return json.load(f)


@pytest.fixture
def sample():
    with open(SAMPLE_PATH) as f:
        return json.load(f)


class TestSampleValidation:
    def test_sample_validates_against_schema(self, schema, sample):
        """SC-3: The sample mission file validates without errors."""
        jsonschema.validate(sample, schema)

    def test_schema_is_draft_07(self, schema):
        assert schema.get("$schema") == "http://json-schema.org/draft-07/schema#"

    def test_schema_has_title(self, schema):
        assert "title" in schema
        assert schema["title"] == "Aria Mission"

    def test_schema_enforces_no_additional_properties(self, schema, sample):
        """additionalProperties: false means unknown fields at root are rejected."""
        doc = copy.deepcopy(sample)
        doc["unexpected_field"] = "should fail"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


class TestRequiredFields:
    @pytest.mark.parametrize("field", ["id", "goal", "original_goal", "status", "created_at", "kpis", "tasks"])
    def test_missing_required_field_fails(self, schema, sample, field):
        bad = copy.deepcopy(sample)
        del bad[field]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)

    def test_all_required_fields_present_in_sample(self, sample):
        required = ["id", "goal", "original_goal", "status", "created_at", "kpis", "tasks"]
        for field in required:
            assert field in sample, f"Required field '{field}' missing from sample"


class TestStatusEnum:
    @pytest.mark.parametrize("status", ["INBOX", "ACTIVE", "ADAPTING", "STALLED", "COMPLETED"])
    def test_valid_status_accepted(self, schema, sample, status):
        doc = copy.deepcopy(sample)
        doc["status"] = status
        jsonschema.validate(doc, schema)

    def test_invalid_status_rejected(self, schema, sample):
        doc = copy.deepcopy(sample)
        doc["status"] = "INVALID"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_lowercase_status_rejected(self, schema, sample):
        doc = copy.deepcopy(sample)
        doc["status"] = "active"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)


class TestTaskStructure:
    def test_task_requires_task_id(self, schema, sample):
        doc = copy.deepcopy(sample)
        del doc["tasks"][0]["task_id"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_task_requires_goal(self, schema, sample):
        doc = copy.deepcopy(sample)
        del doc["tasks"][0]["goal"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_task_requires_type(self, schema, sample):
        doc = copy.deepcopy(sample)
        del doc["tasks"][0]["type"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_task_requires_status(self, schema, sample):
        doc = copy.deepcopy(sample)
        del doc["tasks"][0]["status"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_task_type_enum_one_time(self, schema, sample):
        doc = copy.deepcopy(sample)
        doc["tasks"][0]["type"] = "one-time"
        jsonschema.validate(doc, schema)

    def test_task_type_enum_recurring(self, schema, sample):
        doc = copy.deepcopy(sample)
        doc["tasks"][0]["type"] = "recurring"
        jsonschema.validate(doc, schema)

    def test_task_type_invalid_rejected(self, schema, sample):
        doc = copy.deepcopy(sample)
        doc["tasks"][0]["type"] = "invalid-type"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_valid_task_types_in_sample(self, schema, sample):
        """Both one-time and recurring are valid."""
        types_found = {t["type"] for t in sample["tasks"]}
        assert "one-time" in types_found or "recurring" in types_found

    def test_requires_gpu_field_accepted(self, schema, sample):
        doc = copy.deepcopy(sample)
        doc["tasks"][0]["requires_gpu"] = True
        jsonschema.validate(doc, schema)

    def test_empty_tasks_array_accepted(self, schema, sample):
        doc = copy.deepcopy(sample)
        doc["tasks"] = []
        jsonschema.validate(doc, schema)


class TestKPIStructure:
    def test_kpi_requires_metric(self, schema, sample):
        doc = copy.deepcopy(sample)
        del doc["kpis"][0]["metric"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_kpi_requires_target(self, schema, sample):
        doc = copy.deepcopy(sample)
        del doc["kpis"][0]["target"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_kpi_requires_current(self, schema, sample):
        doc = copy.deepcopy(sample)
        del doc["kpis"][0]["current"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    def test_sample_has_kpis(self, sample):
        assert len(sample["kpis"]) > 0

    def test_kpi_met_field_optional(self, schema, sample):
        doc = copy.deepcopy(sample)
        # Remove optional 'met' field — should still validate
        if "met" in doc["kpis"][0]:
            del doc["kpis"][0]["met"]
        jsonschema.validate(doc, schema)


class TestSampleContent:
    def test_sample_has_immutable_original_goal(self, sample):
        assert "original_goal" in sample
        assert sample["original_goal"] == sample["goal"]

    def test_sample_has_gpu_task(self, sample):
        gpu_tasks = [t for t in sample["tasks"] if t.get("requires_gpu")]
        assert len(gpu_tasks) >= 1

    def test_sample_has_recurring_task(self, sample):
        recurring = [t for t in sample["tasks"] if t["type"] == "recurring"]
        assert len(recurring) >= 1
        assert recurring[0].get("cadence") is not None

    def test_sample_has_active_status(self, sample):
        assert sample["status"] == "ACTIVE"

    def test_sample_id_is_string(self, sample):
        assert isinstance(sample["id"], str)
        assert len(sample["id"]) > 0

    def test_sample_priority_within_bounds(self, sample):
        priority = sample.get("priority")
        if priority is not None:
            assert 1 <= priority <= 4
