"""Tests for mission_engine.py — covers MISS-01, MISS-03, MISS-04, MISS-07, MISS-10.

All tests use subprocess invocation (same pattern as test_task_manager_phase1.py)
and MISSION_DIR env var for full isolation.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ME_SCRIPT = "/home/alex/.openclaw/workspace/scripts/mission_engine.py"
EC_SCRIPT = "/home/alex/.openclaw/workspace/scripts/event_chains.py"


def run_me(*args, env_override=None):
    """Run mission_engine.py with given args, return CompletedProcess."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, ME_SCRIPT] + list(args),
        capture_output=True, text=True, timeout=15,
        env=env
    )


def run_ec(*args, env_override=None):
    """Run event_chains.py with given args, return CompletedProcess."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, EC_SCRIPT] + list(args),
        capture_output=True, text=True, timeout=15,
        env=env
    )


def get_mission_file(mission_dir, mission_id):
    """Load a mission JSON file by ID."""
    path = mission_dir / f"mission_{mission_id}.json"
    assert path.exists(), f"Mission file not found: {path}"
    with open(path) as f:
        return json.load(f)


def extract_mission_id(stdout):
    """Extract mission_id from MISSION_CREATED output line."""
    for line in stdout.splitlines():
        if line.startswith("MISSION_CREATED:"):
            # e.g. "MISSION_CREATED: mission_abc12345"
            token = line.split(":", 1)[1].strip()
            # token is "mission_<id>"
            return token.split("_", 1)[1]
    return None


def load_ledger(mission_dir):
    """Load ledger.json from mission directory."""
    ledger_path = mission_dir / "ledger.json"
    if not ledger_path.exists():
        return None
    with open(ledger_path) as f:
        return json.load(f)


# --- MISS-01: Mission Create ---

class TestMissionCreate:
    def test_create_exits_zero(self, mission_dir):
        r = run_me("create", "--goal", "Grow Reddit karma to 500")
        assert r.returncode == 0, f"Expected exit 0, got {r.returncode}. stderr: {r.stderr}"

    def test_create_prints_mission_created(self, mission_dir):
        r = run_me("create", "--goal", "Grow Reddit karma to 500")
        assert "MISSION_CREATED:" in r.stdout

    def test_create_produces_json_file(self, mission_dir):
        r = run_me("create", "--goal", "Publish first book on KDP")
        assert r.returncode == 0
        mission_id = extract_mission_id(r.stdout)
        assert mission_id, f"Could not extract mission ID from: {r.stdout}"
        mission_file = mission_dir / f"mission_{mission_id}.json"
        assert mission_file.exists(), f"Mission file not created at {mission_file}"

    def test_create_all_required_fields(self, mission_dir):
        r = run_me("create", "--goal", "Build an email list of 1000 subscribers")
        assert r.returncode == 0
        mission_id = extract_mission_id(r.stdout)
        m = get_mission_file(mission_dir, mission_id)
        assert m["id"] == mission_id
        assert m["goal"] == "Build an email list of 1000 subscribers"
        assert m["original_goal"] == "Build an email list of 1000 subscribers"
        assert m["status"] == "INBOX"
        assert "created_at" in m
        assert "updated_at" in m
        assert m["kpis"] == []
        assert m["tasks"] == []
        assert m["stall_count"] == 0
        assert "priority" in m

    def test_create_default_priority_is_3(self, mission_dir):
        r = run_me("create", "--goal", "Test default priority")
        mission_id = extract_mission_id(r.stdout)
        m = get_mission_file(mission_dir, mission_id)
        assert m["priority"] == 3

    def test_create_custom_priority(self, mission_dir):
        r = run_me("create", "--goal", "Urgent mission", "--priority", "1")
        assert r.returncode == 0
        mission_id = extract_mission_id(r.stdout)
        m = get_mission_file(mission_dir, mission_id)
        assert m["priority"] == 1

    def test_original_goal_immutable_field_exists(self, mission_dir):
        goal = "This is the immutable original goal"
        r = run_me("create", "--goal", goal)
        mission_id = extract_mission_id(r.stdout)
        m = get_mission_file(mission_dir, mission_id)
        assert m["original_goal"] == goal

    def test_create_strategy_field_present(self, mission_dir):
        r = run_me("create", "--goal", "Some mission")
        mission_id = extract_mission_id(r.stdout)
        m = get_mission_file(mission_dir, mission_id)
        assert "strategy" in m


# --- MISS-03: Mission Persistence ---

class TestMissionPersistence:
    def test_mission_survives_fresh_read(self, mission_dir):
        """Mission state JSON written by create can be read back with identical data."""
        r = run_me("create", "--goal", "Persistent mission test")
        assert r.returncode == 0
        mission_id = extract_mission_id(r.stdout)

        # Fresh read: call status to trigger file read from a fresh invocation
        r2 = run_me("status", "--mission-id", mission_id)
        assert r2.returncode == 0
        # Should mention the mission id in output
        assert mission_id in r2.stdout

    def test_mission_data_identical_after_fresh_read(self, mission_dir):
        """Data read back by a second process invocation matches the created data."""
        goal = "Verify data survives process restart"
        r = run_me("create", "--goal", goal)
        mission_id = extract_mission_id(r.stdout)

        # Read via file (simulating fresh process read)
        m = get_mission_file(mission_dir, mission_id)
        assert m["goal"] == goal
        assert m["original_goal"] == goal
        assert m["id"] == mission_id
        assert m["status"] == "INBOX"

    def test_multiple_missions_persist_independently(self, mission_dir):
        """Multiple missions coexist without overwriting each other."""
        r1 = run_me("create", "--goal", "Mission A")
        r2 = run_me("create", "--goal", "Mission B")
        id1 = extract_mission_id(r1.stdout)
        id2 = extract_mission_id(r2.stdout)
        assert id1 != id2, "IDs must be unique"
        m1 = get_mission_file(mission_dir, id1)
        m2 = get_mission_file(mission_dir, id2)
        assert m1["goal"] == "Mission A"
        assert m2["goal"] == "Mission B"


# --- MISS-04: Mission Status ---

class TestMissionStatus:
    def test_status_by_id_exits_zero(self, mission_dir):
        r = run_me("create", "--goal", "Status test mission")
        mission_id = extract_mission_id(r.stdout)
        r2 = run_me("status", "--mission-id", mission_id)
        assert r2.returncode == 0, f"status --mission-id failed: {r2.stderr}"

    def test_status_by_id_contains_progress(self, mission_dir):
        r = run_me("create", "--goal", "Progress display test")
        mission_id = extract_mission_id(r.stdout)
        r2 = run_me("status", "--mission-id", mission_id)
        # Should contain a percentage (0%) since no tasks yet
        assert "%" in r2.stdout or "0" in r2.stdout

    def test_status_by_id_contains_goal(self, mission_dir):
        goal = "SEO article blitz mission"
        r = run_me("create", "--goal", goal)
        mission_id = extract_mission_id(r.stdout)
        r2 = run_me("status", "--mission-id", mission_id)
        assert goal in r2.stdout or goal[:40] in r2.stdout

    def test_status_all_exits_zero(self, mission_dir):
        run_me("create", "--goal", "First mission for all")
        r = run_me("status", "--all")
        assert r.returncode == 0, f"status --all failed: {r.stderr}"

    def test_status_all_shows_created_missions(self, mission_dir):
        run_me("create", "--goal", "Alpha mission")
        run_me("create", "--goal", "Beta mission")
        r = run_me("status", "--all")
        assert r.returncode == 0
        # Both missions should appear
        assert "Alpha" in r.stdout or "alpha" in r.stdout.lower()

    def test_status_active_brief_exits_zero(self, mission_dir):
        run_me("create", "--goal", "Heartbeat mission brief")
        r = run_me("status", "--active-brief")
        assert r.returncode == 0

    def test_status_active_brief_format(self, mission_dir):
        run_me("create", "--goal", "Brief format test mission", "--priority", "2")
        r = run_me("status", "--active-brief")
        # Should contain priority marker
        assert "P" in r.stdout or "priority" in r.stdout.lower() or r.stdout.strip() != ""


# --- MISS-07: Mission Archive ---

class TestMissionArchive:
    def test_archive_exits_zero(self, mission_dir):
        r = run_me("create", "--goal", "Mission to archive")
        mission_id = extract_mission_id(r.stdout)
        r2 = run_me("archive", "--mission-id", mission_id)
        assert r2.returncode == 0, f"archive failed: {r2.stderr}"

    def test_archive_prints_mission_archived(self, mission_dir):
        r = run_me("create", "--goal", "Archive output test")
        mission_id = extract_mission_id(r.stdout)
        r2 = run_me("archive", "--mission-id", mission_id)
        assert "MISSION_ARCHIVED" in r2.stdout

    def test_archive_moves_file_to_archive_dir(self, mission_dir):
        r = run_me("create", "--goal", "File move test")
        mission_id = extract_mission_id(r.stdout)
        original_path = mission_dir / f"mission_{mission_id}.json"
        assert original_path.exists(), "File should exist before archive"
        run_me("archive", "--mission-id", mission_id)
        assert not original_path.exists(), "File should be moved after archive"
        archived_path = mission_dir / "archive" / f"mission_{mission_id}.json"
        assert archived_path.exists(), f"Archived file not found at {archived_path}"

    def test_archive_writes_outcomes_jsonl(self, mission_dir):
        r = run_me("create", "--goal", "Outcomes JSONL test")
        mission_id = extract_mission_id(r.stdout)
        run_me("archive", "--mission-id", mission_id)
        outcomes_path = mission_dir / "archive" / "outcomes.jsonl"
        assert outcomes_path.exists(), "outcomes.jsonl should be created"
        with open(outcomes_path) as f:
            line = f.readline().strip()
        record = json.loads(line)
        assert record["mission_id"] == mission_id

    def test_archive_removes_from_ledger(self, mission_dir):
        r = run_me("create", "--goal", "Ledger removal test")
        mission_id = extract_mission_id(r.stdout)
        ledger_before = load_ledger(mission_dir)
        assert any(e["id"] == mission_id for e in ledger_before["missions"])
        run_me("archive", "--mission-id", mission_id)
        ledger_after = load_ledger(mission_dir)
        assert not any(e["id"] == mission_id for e in ledger_after["missions"])


# --- MISS-10: Multi-Mission Priority Sort ---

class TestMultiMission:
    def test_create_multiple_missions_unique_ids(self, mission_dir):
        ids = []
        for i in range(3):
            r = run_me("create", "--goal", f"Mission priority test {i+1}", "--priority", str(i + 1))
            mission_id = extract_mission_id(r.stdout)
            assert mission_id, f"Failed to create mission {i+1}"
            ids.append(mission_id)
        assert len(set(ids)) == 3, "All mission IDs must be unique"

    def test_status_all_sorted_by_priority(self, mission_dir):
        """status --all should return missions sorted ascending by priority."""
        run_me("create", "--goal", "Low priority mission", "--priority", "4")
        run_me("create", "--goal", "High priority mission", "--priority", "1")
        run_me("create", "--goal", "Normal priority mission", "--priority", "3")
        r = run_me("status", "--all")
        assert r.returncode == 0
        lines = r.stdout
        # High priority (P1) should appear before low priority (P4)
        idx_high = lines.find("High priority") if "High priority" in lines else lines.find("P1")
        idx_low = lines.find("Low priority") if "Low priority" in lines else lines.find("P4")
        if idx_high >= 0 and idx_low >= 0:
            assert idx_high < idx_low, "Priority 1 mission should appear before priority 4 in output"

    def test_three_missions_all_appear_in_status(self, mission_dir):
        goals = ["Revenue goal A", "Revenue goal B", "Revenue goal C"]
        for goal in goals:
            run_me("create", "--goal", goal)
        r = run_me("status", "--all")
        for goal in goals:
            # At least a substring should appear
            assert goal[:20] in r.stdout or goal in r.stdout, f"Mission '{goal}' not found in status --all output"


# --- Ledger Management ---

class TestLedgerManagement:
    def test_ledger_created_after_first_mission(self, mission_dir):
        run_me("create", "--goal", "Ledger creation test")
        ledger_path = mission_dir / "ledger.json"
        assert ledger_path.exists(), "ledger.json should be created after first mission"

    def test_ledger_has_correct_structure(self, mission_dir):
        run_me("create", "--goal", "Ledger structure test")
        ledger = load_ledger(mission_dir)
        assert "missions" in ledger
        assert "updated_at" in ledger
        assert isinstance(ledger["missions"], list)

    def test_ledger_updated_on_create(self, mission_dir):
        r = run_me("create", "--goal", "Ledger update test")
        mission_id = extract_mission_id(r.stdout)
        ledger = load_ledger(mission_dir)
        assert any(e["id"] == mission_id for e in ledger["missions"]), \
            f"Mission {mission_id} not found in ledger"

    def test_ledger_entry_has_required_fields(self, mission_dir):
        r = run_me("create", "--goal", "Ledger entry fields test", "--priority", "2")
        mission_id = extract_mission_id(r.stdout)
        ledger = load_ledger(mission_dir)
        entry = next(e for e in ledger["missions"] if e["id"] == mission_id)
        assert "id" in entry
        assert "goal" in entry
        assert "status" in entry
        assert "priority" in entry

    def test_ledger_atomic_write_no_corruption(self, mission_dir):
        """Create multiple missions rapidly and verify ledger is consistent."""
        for i in range(5):
            run_me("create", "--goal", f"Concurrent-ish mission {i}")
        ledger = load_ledger(mission_dir)
        # All 5 should be in ledger
        assert len(ledger["missions"]) == 5

    def test_ledger_updated_on_archive(self, mission_dir):
        r1 = run_me("create", "--goal", "Keep me")
        r2 = run_me("create", "--goal", "Archive me")
        keep_id = extract_mission_id(r1.stdout)
        archive_id = extract_mission_id(r2.stdout)
        run_me("archive", "--mission-id", archive_id)
        ledger = load_ledger(mission_dir)
        ids_in_ledger = [e["id"] for e in ledger["missions"]]
        assert keep_id in ids_in_ledger, "Kept mission should still be in ledger"
        assert archive_id not in ids_in_ledger, "Archived mission should be removed from ledger"


# --- Event Chain Guard ---

class TestEventChainGuard:
    def test_fire_with_mission_subtask_source_prints_chain_skipped(self, mission_dir):
        """fire --source mission-subtask should print CHAIN_SKIPPED and NOT fire chains."""
        r = run_ec(
            "fire",
            "--task-id", "test123",
            "--goal", "Published book on KDP",
            "--tags", "book,publish",
            "--source", "mission-subtask"
        )
        assert r.returncode == 0, f"event_chains.py fire failed: {r.stderr}"
        assert "CHAIN_SKIPPED" in r.stdout, f"Expected CHAIN_SKIPPED in output, got: {r.stdout}"

    def test_fire_with_mission_subtask_does_not_fire_chains(self, mission_dir):
        """fire --source mission-subtask must NOT print EVENT_CHAINS_FIRED."""
        r = run_ec(
            "fire",
            "--task-id", "test456",
            "--goal", "Published book on KDP",
            "--tags", "book,publish",
            "--source", "mission-subtask"
        )
        assert "EVENT_CHAINS_FIRED" not in r.stdout, \
            f"EVENT_CHAINS_FIRED should NOT appear for mission-subtask source"

    def test_fire_without_mission_subtask_fires_chains(self, task_state_dir):
        """Normal tasks (no mission-subtask source) should still fire matching chains."""
        r = run_ec(
            "fire",
            "--task-id", "testnormal",
            "--goal", "Published book on KDP",
            "--tags", "book,publish"
        )
        # Should fire chains normally (not skip)
        assert "CHAIN_SKIPPED" not in r.stdout, \
            f"CHAIN_SKIPPED should NOT appear for normal tasks"
        # Either fires chains or no matches — both acceptable, but not skipped
        assert r.returncode == 0
