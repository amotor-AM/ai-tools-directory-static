"""Tests for output_schema.py — Pydantic v2 output schemas for all Aria task types.

Covers: SUPV-04 (schema validation), SUPV-07 (quality gate enforcement), OUTP-01 (human message limits)
"""
import sys
import pytest

sys.path.insert(0, "/home/alex/.openclaw/workspace/scripts")

from output_schema import (
    TaskCompleteOutput,
    ArticlePublishedOutput,
    BookUploadedOutput,
    MissionDecompositionOutput,
    QualityGateResult,
    HumanMessage,
    DailyBriefing,
    OUTPUT_SCHEMAS,
)
from pydantic import ValidationError


# --- SUPV-04: Valid schema instances ---

class TestArticlePublishedOutput:
    def test_valid_article(self):
        a = ArticlePublishedOutput(url="http://example.com/post", word_count=600, title="Test Article")
        assert a.url == "http://example.com/post"
        assert a.word_count == 600
        assert a.title == "Test Article"
        assert a.canonical is None

    def test_negative_word_count_rejected(self):
        with pytest.raises(ValidationError):
            ArticlePublishedOutput(url="http://x.com", word_count=-1, title="T")

    def test_zero_word_count_rejected(self):
        with pytest.raises(ValidationError):
            ArticlePublishedOutput(url="http://x.com", word_count=0, title="T")

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            ArticlePublishedOutput(url="http://x.com")


class TestBookUploadedOutput:
    def test_valid_book(self):
        b = BookUploadedOutput(platform="kdp", asin_or_id="B0EXAMPLE", title="My Book")
        assert b.platform == "kdp"

    def test_invalid_platform_rejected(self):
        with pytest.raises(ValidationError):
            BookUploadedOutput(platform="amazon", asin_or_id="X", title="T")

    def test_all_valid_platforms(self):
        for platform in ("kdp", "d2d", "google_play", "publishdrive"):
            b = BookUploadedOutput(platform=platform, asin_or_id="ID123", title="Book")
            assert b.platform == platform


class TestMissionDecompositionOutput:
    def test_valid_decomposition(self):
        m = MissionDecompositionOutput(
            mission_id="m1", subtasks=["task a", "task b"], kpis=["metric 1"]
        )
        assert len(m.subtasks) == 2
        assert m.cadence is None

    def test_with_cadence(self):
        m = MissionDecompositionOutput(
            mission_id="m2", subtasks=["task x"], kpis=["kpi y"], cadence="0 9 * * 1"
        )
        assert m.cadence == "0 9 * * 1"

    def test_missing_mission_id_rejected(self):
        with pytest.raises(ValidationError):
            MissionDecompositionOutput(subtasks=["t1"], kpis=["k1"])


class TestTaskCompleteOutput:
    def test_valid_success(self):
        t = TaskCompleteOutput(status="success", summary="Done")
        assert t.status == "success"

    def test_valid_error(self):
        t = TaskCompleteOutput(status="error", summary="Failed", error_detail="Timeout")
        assert t.status == "error"
        assert t.error_detail == "Timeout"

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            TaskCompleteOutput(status="maybe", summary="Hmm")

    def test_missing_status_rejected(self):
        with pytest.raises(ValidationError):
            TaskCompleteOutput(summary="No status")


# --- SUPV-07: Quality gate enforcement ---

class TestQualityGateResult:
    def test_valid_gate(self):
        q = QualityGateResult(passed=True, score=0.95, task_type="article_published", validated_at="2026-01-01T00:00:00Z")
        assert q.passed is True
        assert q.score == 0.95

    def test_score_too_high_rejected(self):
        with pytest.raises(ValidationError):
            QualityGateResult(passed=True, score=1.5, task_type="x", validated_at="2026-01-01T00:00:00Z")

    def test_score_negative_rejected(self):
        with pytest.raises(ValidationError):
            QualityGateResult(passed=False, score=-0.1, task_type="x", validated_at="2026-01-01T00:00:00Z")

    def test_score_boundary_zero(self):
        q = QualityGateResult(passed=False, score=0.0, task_type="x", validated_at="2026-01-01T00:00:00Z")
        assert q.score == 0.0

    def test_score_boundary_one(self):
        q = QualityGateResult(passed=True, score=1.0, task_type="x", validated_at="2026-01-01T00:00:00Z")
        assert q.score == 1.0

    def test_issues_default_empty(self):
        q = QualityGateResult(passed=True, score=0.8, task_type="book_uploaded", validated_at="2026-01-01T00:00:00Z")
        assert q.issues == []

    def test_issues_can_be_populated(self):
        q = QualityGateResult(passed=False, score=0.4, task_type="x", validated_at="2026-01-01T00:00:00Z", issues=["Too short", "No canonical"])
        assert len(q.issues) == 2


# --- OUTP-01: Human-facing message limits ---

class TestHumanMessage:
    def test_valid_message(self):
        h = HumanMessage(text="Hello Alex")
        assert h.urgency == "none"
        assert h.action_required is False

    def test_exact_limit_accepted(self):
        h = HumanMessage(text="x" * 4096)
        assert len(h.text) == 4096

    def test_over_limit_rejected(self):
        with pytest.raises(ValidationError):
            HumanMessage(text="x" * 4097)

    def test_emergency_urgency(self):
        h = HumanMessage(text="Critical issue", urgency="emergency", action_required=True)
        assert h.urgency == "emergency"

    def test_briefing_urgency(self):
        h = HumanMessage(text="Daily summary", urgency="briefing")
        assert h.urgency == "briefing"

    def test_invalid_urgency_rejected(self):
        with pytest.raises(ValidationError):
            HumanMessage(text="Hi", urgency="high")

    def test_action_required_default_false(self):
        h = HumanMessage(text="Info only")
        assert h.action_required is False


class TestDailyBriefing:
    def test_valid_briefing(self):
        b = DailyBriefing(
            done=["Published article on SEO"],
            active=["Writing book chapter 3"],
            tomorrow=["Research keywords"],
        )
        assert len(b.done) == 1

    def test_too_many_done_items_rejected(self):
        with pytest.raises(ValidationError):
            DailyBriefing(done=["a", "b", "c", "d", "e", "f"])

    def test_five_done_items_accepted(self):
        b = DailyBriefing(done=["a", "b", "c", "d", "e"])
        assert len(b.done) == 5

    def test_too_many_active_items_rejected(self):
        with pytest.raises(ValidationError):
            DailyBriefing(active=["a", "b", "c", "d"])

    def test_three_active_items_accepted(self):
        b = DailyBriefing(active=["a", "b", "c"])
        assert len(b.active) == 3

    def test_too_many_tomorrow_items_rejected(self):
        with pytest.raises(ValidationError):
            DailyBriefing(tomorrow=["a", "b", "c", "d"])

    def test_item_over_60_chars_rejected(self):
        with pytest.raises(ValidationError):
            DailyBriefing(done=["x" * 61])

    def test_item_at_60_chars_accepted(self):
        b = DailyBriefing(done=["x" * 60])
        assert len(b.done[0]) == 60

    def test_flag_over_100_chars_rejected(self):
        with pytest.raises(ValidationError):
            DailyBriefing(flag="x" * 101)

    def test_flag_at_100_chars_accepted(self):
        b = DailyBriefing(flag="x" * 100)
        assert len(b.flag) == 100

    def test_empty_briefing_accepted(self):
        b = DailyBriefing()
        assert b.done == []
        assert b.flag is None


# --- Schema registry ---

class TestOutputSchemas:
    def test_registry_has_four_entries(self):
        assert len(OUTPUT_SCHEMAS) == 4

    def test_registry_keys(self):
        expected = {"article_published", "book_uploaded", "mission_decomposed", "task_complete"}
        assert set(OUTPUT_SCHEMAS.keys()) == expected

    def test_registry_values_are_classes(self):
        for key, cls in OUTPUT_SCHEMAS.items():
            assert hasattr(cls, "model_validate"), f"{key} is not a Pydantic model"

    def test_registry_article_published_maps_to_correct_class(self):
        cls = OUTPUT_SCHEMAS["article_published"]
        assert cls is ArticlePublishedOutput

    def test_registry_task_complete_maps_to_correct_class(self):
        cls = OUTPUT_SCHEMAS["task_complete"]
        assert cls is TaskCompleteOutput
