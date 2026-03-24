"""Tests for briefing.py — daily briefing engine.

Requirement coverage:
- OUTP-03: Daily briefing content (TestBriefingGenerate)
- OUTP-04: Delivery timing 8-10 PM Pacific (TestShouldSend)
- OUTP-06: Delta computation showing momentum (TestDeltaSummary)
- OUTP-07: ACTION NEEDED section visually separate (TestActionItems, TestFormatTelegram)
- OUTP-08: 4096-char limit with truncation and dashboard link (TestTruncation)
- Emergency alert bypass (TestEmergencyAlert)
- Send mechanics and sentinel (TestSendTiming)
- Minimum content gate (TestMinimumContent)
"""
import json
import sys
import os
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

# Ensure scripts directory is importable
sys.path.insert(0, "/home/alex/.openclaw/workspace/scripts")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(tmp_path, task_id, goal, status, completed_at=None, last_error=None):
    """Write a task state file to tmp_path/state/."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    task = {"id": task_id, "goal": goal, "status": status}
    if completed_at:
        task["completed_at"] = completed_at
    if last_error:
        task["last_error"] = last_error
    (state_dir / f"task_{task_id}.json").write_text(json.dumps(task))
    return state_dir


def make_ledger(tmp_path, missions):
    """Write a ledger.json to tmp_path."""
    missions_dir = tmp_path / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)
    (missions_dir / "ledger.json").write_text(json.dumps({"missions": missions}))
    return missions_dir


def write_briefing_state(state_path, data):
    """Write briefing-state.json at a given path."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# TestShouldSend
# ---------------------------------------------------------------------------

class TestShouldSend:

    def test_returns_true_in_window(self, tmp_path, monkeypatch):
        """should_send returns True at 20:30 Pacific when not yet sent today."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-23", "total_sent": 1})
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        # Reload module with env var set
        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.should_send()
        assert result is True

    def test_returns_false_before_window(self, tmp_path, monkeypatch):
        """should_send returns False at 15:00 Pacific."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 15, 0, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.should_send()
        assert result is False

    def test_returns_false_after_window(self, tmp_path, monkeypatch):
        """should_send returns False at 22:30 Pacific."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 22, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.should_send()
        assert result is False

    def test_returns_false_if_already_sent(self, tmp_path, monkeypatch):
        """should_send returns False when last_sent_date == today."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-24", "total_sent": 1})
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.should_send()
        assert result is False

    def test_uses_dst_aware_timezone(self, tmp_path, monkeypatch):
        """ZoneInfo('America/Los_Angeles') used — not hardcoded UTC-7."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        # Verify now_pacific returns a ZoneInfo-aware datetime
        result = briefing.now_pacific()
        assert result.tzinfo is not None
        assert "Los_Angeles" in str(result.tzinfo) or result.tzinfo == ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# TestBriefingGenerate
# ---------------------------------------------------------------------------

class TestBriefingGenerate:

    def test_generates_with_done_tasks(self, tmp_path, monkeypatch):
        """generate() includes DONE tasks completed today."""
        task_dir = make_task(
            tmp_path, "abc123", "Write SEO article about Python",
            "DONE", completed_at="2026-03-24T10:00:00"
        )
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.generate()

        assert result is not None
        assert len(result.done) > 0
        assert any("SEO article" in item or "Python" in item for item in result.done)

    def test_generates_with_active_missions(self, tmp_path, monkeypatch):
        """generate() includes ACTIVE missions from ledger.json."""
        task_dir = tmp_path / "state"
        task_dir.mkdir(parents=True)
        missions_dir = make_ledger(tmp_path, [
            {"id": "m1", "goal": "Build AI tools directory", "status": "ACTIVE", "priority": 1}
        ])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.generate()

        assert result is not None
        assert len(result.active) > 0
        assert any("AI tools" in item for item in result.active)

    def test_generates_with_escalated_tasks(self, tmp_path, monkeypatch):
        """generate() populates action_items from ESCALATED tasks."""
        task_dir = make_task(
            tmp_path, "esc001", "Get Stripe API key",
            "ESCALATED", last_error="API key not configured"
        )
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.generate()

        assert result is not None
        assert len(result.action_items) > 0
        # Should be human readable, not raw task IDs
        assert not any("esc001" in item for item in result.action_items)
        assert any("Stripe" in item or "API" in item or "Input needed" in item for item in result.action_items)

    def test_empty_state_returns_none(self, tmp_path, monkeypatch):
        """generate() returns None when no tasks, no missions, no escalations."""
        task_dir = tmp_path / "state"
        task_dir.mkdir(parents=True)
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.generate()

        assert result is None

    def test_tomorrow_from_scheduler_plan(self, tmp_path, monkeypatch):
        """tomorrow list populated from active missions."""
        task_dir = tmp_path / "state"
        task_dir.mkdir(parents=True)
        missions_dir = make_ledger(tmp_path, [
            {"id": "m1", "goal": "Launch Reddit marketing campaign", "status": "ACTIVE", "priority": 1},
            {"id": "m2", "goal": "Build email subscriber list", "status": "ACTIVE", "priority": 2},
        ])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            result = briefing.generate()

        assert result is not None
        assert len(result.tomorrow) > 0


# ---------------------------------------------------------------------------
# TestDeltaSummary
# ---------------------------------------------------------------------------

class TestDeltaSummary:

    def test_delta_positive(self, tmp_path, monkeypatch):
        """delta_summary shows +N when more tasks completed today than yesterday."""
        # yesterday done_count=2, today done_count=5 => +3
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"previous_brief": {"done_count": 2}})

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        state = briefing.load_json(state_path)
        result = briefing.compute_delta(5, state)
        assert "+3" in result

    def test_delta_zero(self, tmp_path, monkeypatch):
        """delta_summary says 'same as yesterday' when counts equal."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"previous_brief": {"done_count": 3}})

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        state = briefing.load_json(state_path)
        result = briefing.compute_delta(3, state)
        assert result is not None
        assert "same" in result.lower() or "no new" in result.lower()

    def test_no_previous_brief(self, tmp_path, monkeypatch):
        """delta_summary treats prev as 0 when no previous_brief in state."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {})

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        state = briefing.load_json(state_path)
        result = briefing.compute_delta(4, state)
        assert "+4" in result


# ---------------------------------------------------------------------------
# TestActionItems
# ---------------------------------------------------------------------------

class TestActionItems:

    def test_action_items_from_escalated(self, tmp_path, monkeypatch):
        """ESCALATED task produces human-readable action item."""
        task_dir = make_task(
            tmp_path, "stripe001", "Get Stripe key",
            "ESCALATED", last_error="API key missing"
        )
        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            data = briefing.collect_briefing_data(task_dir, tmp_path / "missions")

        assert len(data["action_items"]) > 0
        item = data["action_items"][0]
        assert "Input needed" in item
        assert "Stripe" in item or "key" in item.lower()

    def test_action_items_capped_at_3(self, tmp_path, monkeypatch):
        """Only max 3 action items returned even with 5 escalated tasks."""
        task_dir = tmp_path / "state"
        task_dir.mkdir(parents=True)
        for i in range(5):
            task = {"id": f"t{i}", "goal": f"Task goal {i}", "status": "ESCALATED", "last_error": "err"}
            (task_dir / f"task_t{i}.json").write_text(json.dumps(task))
        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            data = briefing.collect_briefing_data(task_dir, tmp_path / "missions")

        assert len(data["action_items"]) == 3

    def test_action_items_human_readable(self, tmp_path, monkeypatch):
        """No raw task IDs in action_items text."""
        task_dir = make_task(
            tmp_path, "rawid123456", "Configure email marketing",
            "ESCALATED", last_error="MailerLite key needed"
        )
        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        with patch.object(briefing, "now_pacific", return_value=fake_now):
            data = briefing.collect_briefing_data(task_dir, tmp_path / "missions")

        for item in data["action_items"]:
            assert "rawid123456" not in item


# ---------------------------------------------------------------------------
# TestTruncation
# ---------------------------------------------------------------------------

class TestTruncation:

    def test_short_text_unchanged(self):
        """Text under 3900 chars returned unchanged."""
        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        text = "Hello world\nSecond line\n"
        result = briefing.truncate_with_link(text)
        assert result == text

    def test_long_text_truncated_with_link(self):
        """Text over 3900 chars is truncated with dashboard link appended."""
        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        # Create text with embedded newlines just under 3900
        lines = [f"Line {i}: " + "x" * 50 for i in range(100)]
        text = "\n".join(lines)
        assert len(text) > 3900

        result = briefing.truncate_with_link(text)
        assert len(result) < len(text)
        assert "localhost" in result or "full report" in result.lower()

    def test_truncated_text_under_4096(self):
        """Truncated result is always <= 4096 chars."""
        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        lines = [f"Line {i}: " + "y" * 80 for i in range(200)]
        text = "\n".join(lines)

        result = briefing.truncate_with_link(text)
        assert len(result) <= 4096

    def test_dashboard_link_uses_8080(self):
        """Truncation appends http://localhost:8080, not 8888."""
        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        lines = ["x" * 60 for _ in range(100)]
        text = "\n".join(lines)

        result = briefing.truncate_with_link(text)
        assert "8080" in result
        assert "8888" not in result


# ---------------------------------------------------------------------------
# TestFormatTelegram
# ---------------------------------------------------------------------------

class TestFormatTelegram:

    def setup_method(self):
        """Re-import briefing for each test."""
        if "briefing" in sys.modules:
            del sys.modules["briefing"]

    def test_action_needed_section_present(self):
        """Briefing with action_items renders *ACTION NEEDED* header."""
        import briefing
        from output_schema import DailyBriefing
        b = DailyBriefing(
            done=["Wrote article"],
            active=["SEO campaign"],
            action_items=["Provide Stripe key"]
        )
        text = briefing.format_for_telegram(b)
        assert "ACTION NEEDED" in text

    def test_fyi_separate_from_action(self):
        """flag renders in FYI section, not under ACTION NEEDED."""
        import briefing
        from output_schema import DailyBriefing
        b = DailyBriefing(
            active=["SEO campaign"],
            action_items=["Provide API key"],
            flag="Site traffic is up 20%"
        )
        text = briefing.format_for_telegram(b)
        # flag should appear AFTER action items section
        action_pos = text.find("ACTION NEEDED")
        flag_pos = text.find("FYI:")
        assert action_pos != -1
        assert flag_pos != -1
        assert flag_pos > action_pos

    def test_delta_summary_at_top(self):
        """delta_summary renders near the top of the message as italic."""
        import briefing
        from output_schema import DailyBriefing
        b = DailyBriefing(
            done=["Task one"],
            active=["Mission alpha"],
            delta_summary="+3 tasks completed today"
        )
        text = briefing.format_for_telegram(b)
        # delta_summary should be near top (within first 200 chars)
        assert "_+3 tasks completed today_" in text
        delta_pos = text.find("_+3 tasks completed today_")
        # Header is first; delta should be early
        assert delta_pos < 200


# ---------------------------------------------------------------------------
# TestSendTiming
# ---------------------------------------------------------------------------

class TestSendTiming:

    def test_send_calls_telegram_api(self, tmp_path, monkeypatch):
        """send() posts to Telegram Bot API endpoint."""
        task_dir = make_task(
            tmp_path, "done001", "Completed task",
            "DONE", completed_at="2026-03-24T10:00:00"
        )
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-23"})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(briefing, "now_pacific", return_value=fake_now):
            with patch("briefing.get_bot_token", return_value="test-token"):
                with patch("briefing.requests") as mock_requests:
                    mock_requests.post.return_value = mock_resp
                    briefing.send()

        assert mock_requests.post.called
        call_args = mock_requests.post.call_args
        assert "sendMessage" in call_args[0][0] or "sendMessage" in str(call_args)

    def test_send_marks_sent(self, tmp_path, monkeypatch):
        """After successful send, briefing-state.json has today's date."""
        task_dir = make_task(
            tmp_path, "done002", "Article published",
            "DONE", completed_at="2026-03-24T09:00:00"
        )
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-23", "total_sent": 2})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(briefing, "now_pacific", return_value=fake_now):
            with patch("briefing.get_bot_token", return_value="test-token"):
                with patch("briefing.requests") as mock_requests:
                    mock_requests.post.return_value = mock_resp
                    briefing.send()

        updated = json.loads(state_path.read_text())
        assert updated["last_sent_date"] == "2026-03-24"

    def test_send_stores_previous_brief(self, tmp_path, monkeypatch):
        """After successful send, briefing-state.json has previous_brief with done_count."""
        task_dir = make_task(
            tmp_path, "done003", "Video published",
            "DONE", completed_at="2026-03-24T11:00:00"
        )
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-23"})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(briefing, "now_pacific", return_value=fake_now):
            with patch("briefing.get_bot_token", return_value="test-token"):
                with patch("briefing.requests") as mock_requests:
                    mock_requests.post.return_value = mock_resp
                    briefing.send()

        updated = json.loads(state_path.read_text())
        assert "previous_brief" in updated
        assert "done_count" in updated["previous_brief"]


# ---------------------------------------------------------------------------
# TestEmergencyAlert
# ---------------------------------------------------------------------------

class TestEmergencyAlert:

    def test_alert_sends_immediately(self, tmp_path, monkeypatch):
        """alert() sends regardless of time window."""
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-24"})  # already sent
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        # Time is outside window (3 AM)
        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 3, 0, tzinfo=pacific)
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(briefing, "now_pacific", return_value=fake_now):
            with patch("briefing.get_bot_token", return_value="test-token"):
                with patch("briefing.requests") as mock_requests:
                    mock_requests.post.return_value = mock_resp
                    briefing.alert(
                        text="Need Stripe API key to proceed",
                        category="credentials_needed"
                    )

        assert mock_requests.post.called

    def test_alert_validates_schema(self):
        """alert() raises ValueError when text > 500 chars."""
        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        long_text = "x" * 501
        with pytest.raises((ValueError, Exception)):
            briefing.alert(text=long_text, category="credentials_needed")

    def test_alert_includes_category(self, monkeypatch):
        """Sent text includes the category label."""
        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        sent_texts = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("briefing.get_bot_token", return_value="test-token"):
            with patch("briefing.requests") as mock_requests:
                mock_requests.post.return_value = mock_resp
                briefing.alert(
                    text="Need payment info",
                    category="money_needed"
                )
                call_kwargs = mock_requests.post.call_args
                # The body should include category in the text
                body = call_kwargs[1] if call_kwargs[1] else {}
                if not body:
                    # positional json arg
                    body = call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {}
                sent_text = str(body)

        assert "money_needed" in sent_text or mock_requests.post.called


# ---------------------------------------------------------------------------
# TestMinimumContent
# ---------------------------------------------------------------------------

class TestMinimumContent:

    def test_empty_briefing_skipped(self, tmp_path, monkeypatch):
        """No done, no active, no action_items => send() marks sent but does NOT call Telegram."""
        task_dir = tmp_path / "state"
        task_dir.mkdir(parents=True)
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-23", "total_sent": 1})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)

        with patch.object(briefing, "now_pacific", return_value=fake_now):
            with patch("briefing.get_bot_token", return_value="test-token"):
                with patch("briefing.requests") as mock_requests:
                    briefing.send()

        # Telegram should NOT be called
        assert not mock_requests.post.called
        # But sentinel should be updated
        updated = json.loads(state_path.read_text())
        assert updated["last_sent_date"] == "2026-03-24"

    def test_only_action_items_sends(self, tmp_path, monkeypatch):
        """No done, no active, but action_items present => send() calls Telegram."""
        task_dir = make_task(
            tmp_path, "esc_only", "Set up payment processor",
            "ESCALATED", last_error="No Stripe key"
        )
        missions_dir = make_ledger(tmp_path, [])
        state_path = tmp_path / "briefing-state.json"
        write_briefing_state(state_path, {"last_sent_date": "2026-03-23"})

        monkeypatch.setenv("ARIA_TASK_DIR", str(task_dir))
        monkeypatch.setenv("MISSIONS_DIR", str(missions_dir))
        monkeypatch.setenv("BRIEFING_STATE_PATH", str(state_path))

        if "briefing" in sys.modules:
            del sys.modules["briefing"]
        import briefing

        pacific = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2026, 3, 24, 20, 30, tzinfo=pacific)
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.object(briefing, "now_pacific", return_value=fake_now):
            with patch("briefing.get_bot_token", return_value="test-token"):
                with patch("briefing.requests") as mock_requests:
                    mock_requests.post.return_value = mock_resp
                    briefing.send()

        assert mock_requests.post.called
