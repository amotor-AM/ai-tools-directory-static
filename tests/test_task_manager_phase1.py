"""Tests for task_manager.py Phase 1 extensions.

Covers: TASK-03 (mission linkage), TASK-04 (duplicate detection), HEAL-08 (checkpointing)
Also covers: requires_gpu field (SC-4), quality_gate_status initialization, backward compat.
"""
import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

TM_SCRIPT = "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py"

# Use a temporary state directory to avoid polluting live tasks
@pytest.fixture(autouse=True)
def temp_state_dir(tmp_path):
    """Redirect task_manager.py to a temp directory for isolated tests."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    os.environ["ARIA_TASK_DIR"] = str(state_dir)
    yield state_dir
    del os.environ["ARIA_TASK_DIR"]


def run_tm(*args):
    """Run task_manager.py with given args, return CompletedProcess."""
    return subprocess.run(
        [sys.executable, TM_SCRIPT] + list(args),
        capture_output=True, text=True, timeout=15
    )


def get_latest_task(state_dir):
    """Load the most recently created task JSON from the state dir."""
    files = sorted(state_dir.glob("task_*.json"), key=lambda f: f.stat().st_mtime)
    assert files, "No task files found"
    with open(files[-1]) as f:
        return json.load(f)


def get_task_by_id(state_dir, task_id):
    """Load a specific task JSON by its ID."""
    path = state_dir / f"task_{task_id}.json"
    with open(path) as f:
        return json.load(f)


# --- TASK-03: mission_id linkage ---

class TestMissionLinkage:
    def test_create_with_mission_id(self, temp_state_dir):
        r = run_tm("create", "--goal", "test mission link", "--mission-id", "m_abc123")
        assert r.returncode == 0
        assert "CREATED" in r.stdout
        task = get_latest_task(temp_state_dir)
        assert task["mission_id"] == "m_abc123"

    def test_create_without_mission_id(self, temp_state_dir):
        r = run_tm("create", "--goal", "test no mission")
        assert r.returncode == 0
        task = get_latest_task(temp_state_dir)
        assert task["mission_id"] is None

    def test_mission_id_appears_in_show(self, temp_state_dir):
        run_tm("create", "--goal", "test show mission", "--mission-id", "m_show")
        task = get_latest_task(temp_state_dir)
        r = run_tm("show", task["id"])
        assert r.returncode == 0
        output = json.loads(r.stdout)
        assert output["mission_id"] == "m_show"

    def test_mission_id_in_json_list(self, temp_state_dir):
        run_tm("create", "--goal", "list mission test", "--mission-id", "m_list_test")
        r = run_tm("list", "--all", "--json")
        assert r.returncode == 0
        tasks = json.loads(r.stdout)
        matching = [t for t in tasks if t.get("mission_id") == "m_list_test"]
        assert len(matching) == 1


# --- TASK-04: duplicate detection ---

class TestDuplicateDetection:
    def test_duplicate_rejected_with_exit_code_2(self, temp_state_dir):
        r1 = run_tm("create", "--goal", "unique goal for dedup test")
        assert r1.returncode == 0

        r2 = run_tm("create", "--goal", "unique goal for dedup test")
        assert r2.returncode == 2
        assert "DUPLICATE_REJECTED" in r2.stdout

    def test_same_goal_different_case_not_duplicate(self, temp_state_dir):
        """Exact string match only — case differences are not duplicates."""
        run_tm("create", "--goal", "Deploy API")
        r = run_tm("create", "--goal", "deploy api")
        assert r.returncode == 0  # Different case = not duplicate

    def test_completed_task_not_counted_as_duplicate(self, temp_state_dir):
        """DONE tasks should not block new creation."""
        r1 = run_tm("create", "--goal", "completable task")
        assert r1.returncode == 0
        task = get_latest_task(temp_state_dir)
        run_tm("complete", task["id"], "--summary", "done")

        r2 = run_tm("create", "--goal", "completable task")
        assert r2.returncode == 0  # Not a duplicate — original is DONE

    def test_cancelled_task_not_counted_as_duplicate(self, temp_state_dir):
        r1 = run_tm("create", "--goal", "cancellable task")
        task = get_latest_task(temp_state_dir)
        run_tm("cancel", task["id"], "--reason", "test")

        r2 = run_tm("create", "--goal", "cancellable task")
        assert r2.returncode == 0  # Not a duplicate — original is CANCELLED

    def test_triple_create_still_rejected(self, temp_state_dir):
        """Third create with same goal still returns exit code 2."""
        run_tm("create", "--goal", "persistent goal")
        run_tm("create", "--goal", "persistent goal")  # exits 2
        r = run_tm("create", "--goal", "persistent goal")
        assert r.returncode == 2


# --- HEAL-08: per-step checkpointing ---

class TestCheckpointing:
    def test_checkpoint_saves_step_and_data(self, temp_state_dir):
        run_tm("create", "--goal", "checkpoint test task")
        task = get_latest_task(temp_state_dir)

        r = run_tm("checkpoint", task["id"], "--step", "downloaded_page_3",
                   "--data", '{"page": 3, "url": "http://example.com"}')
        assert r.returncode == 0
        assert "CHECKPOINT_SAVED" in r.stdout

        task = get_latest_task(temp_state_dir)
        assert task["checkpoint"]["last_checkpoint_step"] == "downloaded_page_3"
        assert task["checkpoint"]["checkpoint_data"] == {"page": 3, "url": "http://example.com"}
        assert task["checkpoint"]["checkpointed_at"] is not None

    def test_checkpoint_overwrites_previous(self, temp_state_dir):
        run_tm("create", "--goal", "overwrite checkpoint test")
        task = get_latest_task(temp_state_dir)

        run_tm("checkpoint", task["id"], "--step", "step_1", "--data", '{"x": 1}')
        run_tm("checkpoint", task["id"], "--step", "step_2", "--data", '{"x": 2}')

        task = get_latest_task(temp_state_dir)
        assert task["checkpoint"]["last_checkpoint_step"] == "step_2"
        assert task["checkpoint"]["checkpoint_data"] == {"x": 2}

    def test_checkpoint_default_data_empty(self, temp_state_dir):
        run_tm("create", "--goal", "empty data checkpoint")
        task = get_latest_task(temp_state_dir)

        # --data defaults to "{}" in the argparse definition
        r = run_tm("checkpoint", task["id"], "--step", "simple_step")
        assert r.returncode == 0

        task = get_latest_task(temp_state_dir)
        assert task["checkpoint"]["checkpoint_data"] == {}

    def test_initial_checkpoint_is_null(self, temp_state_dir):
        run_tm("create", "--goal", "initial checkpoint check")
        task = get_latest_task(temp_state_dir)
        assert task["checkpoint"]["last_checkpoint_step"] is None
        assert task["checkpoint"]["checkpoint_data"] == {}
        assert task["checkpoint"]["checkpointed_at"] is None

    def test_checkpoint_step_in_show_output(self, temp_state_dir):
        run_tm("create", "--goal", "show checkpoint test")
        task = get_latest_task(temp_state_dir)
        run_tm("checkpoint", task["id"], "--step", "step_visible", "--data", '{}')

        r = run_tm("show", task["id"])
        assert r.returncode == 0
        output = json.loads(r.stdout)
        assert output["checkpoint"]["last_checkpoint_step"] == "step_visible"


# --- requires_gpu field ---

class TestRequiresGpu:
    def test_default_requires_gpu_false(self, temp_state_dir):
        run_tm("create", "--goal", "non-gpu task")
        task = get_latest_task(temp_state_dir)
        assert task["requires_gpu"] is False

    def test_requires_gpu_flag_sets_true(self, temp_state_dir):
        run_tm("create", "--goal", "gpu task", "--requires-gpu")
        task = get_latest_task(temp_state_dir)
        assert task["requires_gpu"] is True

    def test_requires_gpu_persists_in_show(self, temp_state_dir):
        run_tm("create", "--goal", "gpu show test", "--requires-gpu")
        task = get_latest_task(temp_state_dir)
        r = run_tm("show", task["id"])
        output = json.loads(r.stdout)
        assert output["requires_gpu"] is True


# --- quality_gate_status field ---

class TestQualityGateStatus:
    def test_initial_quality_gate_status_null(self, temp_state_dir):
        run_tm("create", "--goal", "gate test")
        task = get_latest_task(temp_state_dir)
        assert task["quality_gate_status"] is None

    def test_quality_gate_status_in_show(self, temp_state_dir):
        run_tm("create", "--goal", "gate show test")
        task = get_latest_task(temp_state_dir)
        r = run_tm("show", task["id"])
        output = json.loads(r.stdout)
        assert "quality_gate_status" in output
        assert output["quality_gate_status"] is None


# --- Backward compatibility ---

class TestBackwardCompatibility:
    def test_old_task_file_loads_via_list(self, temp_state_dir):
        """Simulate an old task file without new fields — list should not crash."""
        old_task = {
            "id": "old12345",
            "goal": "Legacy task from before Phase 1",
            "status": "RUNNING",
            "priority": 3,
            "created_at": "2026-03-01T00:00:00+00:00",
            "updated_at": "2026-03-01T00:00:00+00:00",
            "last_heartbeat": None,
            "attempts": 0,
            "max_attempts": 15,
            "steps_completed": [],
            "current_step": None,
            "last_error": None,
            "last_error_at": None,
            "retry_strategy": None,
            "notes": [],
            "context": {},
            "escalation": {
                "escalated": False,
                "escalated_at": None,
                "claude_code_session_id": None,
                "handoff_brief": None,
                "resolution": None
            },
            "error_history": [],
            "tags": [],
            "source": "heartbeat",
            "deadline": None,
            "consecutive_step_errors": 0,
            "blocked_heartbeats": 0,
            "step_started_at": None,
        }
        with open(temp_state_dir / "task_old12345.json", "w") as f:
            json.dump(old_task, f)

        r = run_tm("list", "--all", "--json")
        assert r.returncode == 0
        tasks = json.loads(r.stdout)
        assert any(t["id"] == "old12345" for t in tasks)

    def test_old_task_file_loads_via_show(self, temp_state_dir):
        old_task = {
            "id": "old67890",
            "goal": "Another legacy task",
            "status": "DONE",
            "priority": 3,
            "created_at": "2026-03-01T00:00:00+00:00",
            "updated_at": "2026-03-01T00:00:00+00:00",
            "last_heartbeat": None,
            "attempts": 1,
            "max_attempts": 15,
            "steps_completed": [
                {"step": "did something", "completed_at": "2026-03-01T01:00:00+00:00"}
            ],
            "current_step": None,
            "last_error": None,
            "last_error_at": None,
            "retry_strategy": None,
            "notes": [],
            "context": {},
            "escalation": {
                "escalated": False,
                "escalated_at": None,
                "claude_code_session_id": None,
                "handoff_brief": None,
                "resolution": None
            },
            "error_history": [],
            "tags": [],
            "source": "heartbeat",
            "deadline": None,
            "consecutive_step_errors": 0,
            "blocked_heartbeats": 0,
            "step_started_at": None,
        }
        with open(temp_state_dir / "task_old67890.json", "w") as f:
            json.dump(old_task, f)

        r = run_tm("show", "old67890")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["id"] == "old67890"

    def test_old_task_new_task_coexist_in_list(self, temp_state_dir):
        """Old and new task files can coexist without crashing list."""
        old_task = {
            "id": "coexist1",
            "goal": "Old task for coexistence test",
            "status": "RUNNING",
            "priority": 3,
            "created_at": "2026-03-01T00:00:00+00:00",
            "updated_at": "2026-03-01T00:00:00+00:00",
            "last_heartbeat": None,
            "attempts": 0,
            "max_attempts": 15,
            "steps_completed": [],
            "current_step": None,
            "last_error": None,
            "last_error_at": None,
            "retry_strategy": None,
            "notes": [],
            "context": {},
            "escalation": {
                "escalated": False, "escalated_at": None,
                "claude_code_session_id": None, "handoff_brief": None, "resolution": None
            },
            "error_history": [],
            "tags": [],
            "source": "heartbeat",
            "deadline": None,
            "consecutive_step_errors": 0,
            "blocked_heartbeats": 0,
            "step_started_at": None,
        }
        with open(temp_state_dir / "task_coexist1.json", "w") as f:
            json.dump(old_task, f)

        # Create a new task with all Phase 1 fields
        run_tm("create", "--goal", "New task for coexistence test", "--mission-id", "m_coexist")

        r = run_tm("list", "--all", "--json")
        assert r.returncode == 0
        tasks = json.loads(r.stdout)
        ids = [t["id"] for t in tasks]
        assert "coexist1" in ids
        assert any(t.get("mission_id") == "m_coexist" for t in tasks)
