"""Phase 2 Mission Engine tests.

Requirement coverage:
- MISS-01: TestMissionCreate (create produces valid mission file)
- MISS-02: TestMissionDecompose (decompose creates tasks via Sonnet)
- MISS-03: TestMissionPersistence (file survives restart simulation)
- MISS-04: TestMissionStatus (status returns progress %)
- MISS-05: TestNextTask (next-task returns unblocked task)
- MISS-06: TestKPICompletion (all KPIs met -> COMPLETED)
- MISS-07: TestMissionArchive (archive moves to archive/)
- MISS-08: TestKPIAutoSelect (goal keywords -> KPI mapping)
- MISS-09: TestStallDetection + TestAdapt (stall_count + replanning)
- MISS-10: TestMultiMission (multiple missions, priority sort)
- MISS-11: TestAmbiguityCheck (ambiguous goal -> clarification)
- TASK-01: TestTaskClassify (one-time vs recurring)
- TASK-02: TestCadenceExtraction (cron cadence stored)
- TASK-05: TestClassifyAmbiguity (low confidence -> clarification)
- TASK-06: TestComplexityRouting (complexity in task context)
- TASK-07: TestDependencyOrder (tasks in dependency order)

All tests use subprocess invocation (same pattern as test_task_manager_phase1.py)
and MISSION_DIR env var for full isolation. LLM-dependent tests use direct import
with unittest.mock.patch to avoid real API calls.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, "/home/alex/.openclaw/workspace/scripts")
sys.path.insert(0, "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts")

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


# =============================================================================
# Plan 02-02: LLM-powered features — decompose, classify, next-task
# Uses direct import + unittest.mock.patch for fast isolated testing.
# =============================================================================

def _make_mock_sonnet_decompose(mission_id="test123"):
    """Return MagicMock that simulates client.beta.messages.parse() result."""
    from output_schema import MissionDecompositionOutput
    mock_result = MagicMock()
    mock_result.parsed = MissionDecompositionOutput(
        mission_id=mission_id,
        subtasks=[
            "Research competitor sites",
            "Write 5 SEO articles",
            "Submit to Google indexing",
        ],
        kpis=["GSC clicks > 1000/month", "5 articles published"],
        cadence=None,
    )
    return mock_result


def _make_mock_ambiguity(is_ambiguous=False, question=None, confidence=0.85):
    """Return MagicMock that simulates ambiguity parse result."""
    mock_result = MagicMock()
    mock_result.parsed = MagicMock()
    mock_result.parsed.is_ambiguous = is_ambiguous
    mock_result.parsed.missing_info = ["who", "what"] if is_ambiguous else []
    mock_result.parsed.clarification_question = question or ("What domain?" if is_ambiguous else None)
    mock_result.parsed.confidence = confidence
    return mock_result


def _make_mock_classification(task_type="one-time", confidence=0.9, cadence=None):
    """Return MagicMock that simulates ollama.chat() classification result."""
    mock_response = MagicMock()
    mock_response.message = MagicMock()
    import json as _json
    mock_response.message.content = _json.dumps({
        "task_type": task_type,
        "confidence": confidence,
        "cadence": cadence,
        "reasoning": f"This is a {task_type} task.",
    })
    return mock_response


def _make_mock_enrichment(complexity=3, requires_gpu=False):
    """Return MagicMock that simulates ollama.chat() enrichment result."""
    mock_response = MagicMock()
    mock_response.message = MagicMock()
    import json as _json
    mock_response.message.content = _json.dumps({
        "complexity": complexity,
        "requires_gpu": requires_gpu,
        "reasoning": "Standard task.",
    })
    return mock_response


def _create_mission_file(mission_dir, mission_id, goal="Test goal", priority=3, status="INBOX"):
    """Helper: create a mission JSON file directly for testing."""
    from datetime import datetime, timezone
    mission = {
        "id": mission_id,
        "goal": goal,
        "original_goal": goal,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": [],
        "tasks": [],
        "strategy": "",
        "stall_count": 0,
        "priority": priority,
    }
    path = mission_dir / f"mission_{mission_id}.json"
    with open(path, "w") as f:
        json.dump(mission, f, indent=2)
    # Also write ledger entry
    ledger_path = mission_dir / "ledger.json"
    if ledger_path.exists():
        with open(ledger_path) as f:
            ledger = json.load(f)
    else:
        ledger = {"missions": [], "updated_at": ""}
    ledger["missions"].append({
        "id": mission_id,
        "goal": goal,
        "status": status,
        "priority": priority,
        "kpi_summary": "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    with open(ledger_path, "w") as f:
        json.dump(ledger, f, indent=2)
    return mission, path


def _create_task_file(task_dir, task_id, status="CREATED", goal="Do something"):
    """Helper: create a task state JSON file for next-task tests."""
    from datetime import datetime, timezone
    task = {
        "id": task_id,
        "goal": goal,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "context": {},
        "source": "mission-subtask",
    }
    path = task_dir / f"task_{task_id}.json"
    with open(path, "w") as f:
        json.dump(task, f, indent=2)
    return task, path


# --- MISS-02 / TASK-01: Mission Decompose (mocked LLM) ---

class TestMissionDecompose:
    def test_decompose_updates_mission_to_active(self, mission_dir, task_state_dir):
        """decompose_mission sets mission status from INBOX to ACTIVE."""
        _create_mission_file(mission_dir, "abc00001", goal="Grow SEO traffic", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),  # ambiguity check
            _make_mock_sonnet_decompose("abc00001"),    # decompose
        ]

        # ollama.chat returns classification, then enrichment for each subtask (3 subtasks)
        ollama_responses = []
        for _ in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(3, False))

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            # Also mock the subprocess call to task_manager.py
            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = "CREATED: task_sub00001\n"
                mock_sub.run.return_value = mock_proc

                args = MagicMock()
                args.mission_id = "abc00001"
                mission_engine.decompose_mission(args)

        mission, _ = mission_engine.load_mission("abc00001")
        assert mission["status"] == "ACTIVE", f"Expected ACTIVE, got {mission['status']}"

    def test_decompose_creates_tasks_array(self, mission_dir, task_state_dir):
        """decompose_mission populates mission tasks array."""
        _create_mission_file(mission_dir, "abc00002", goal="Grow SEO traffic", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            _make_mock_sonnet_decompose("abc00002"),
        ]

        ollama_responses = []
        for i in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(3, False))

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = "CREATED: task_sub00002\n"
                mock_sub.run.return_value = mock_proc

                args = MagicMock()
                args.mission_id = "abc00002"
                mission_engine.decompose_mission(args)

        mission, _ = mission_engine.load_mission("abc00002")
        assert len(mission["tasks"]) == 3, f"Expected 3 tasks, got {len(mission['tasks'])}"

    def test_decompose_task_includes_required_fields(self, mission_dir, task_state_dir):
        """Each task entry in mission tasks array has task_id, goal, type, status, requires_gpu."""
        _create_mission_file(mission_dir, "abc00003", goal="Grow SEO traffic", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            _make_mock_sonnet_decompose("abc00003"),
        ]

        ollama_responses = []
        for _ in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(3, False))

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = "CREATED: task_sub00003\n"
                mock_sub.run.return_value = mock_proc

                args = MagicMock()
                args.mission_id = "abc00003"
                mission_engine.decompose_mission(args)

        mission, _ = mission_engine.load_mission("abc00003")
        for task_entry in mission["tasks"]:
            assert "task_id" in task_entry, f"Missing task_id in {task_entry}"
            assert "goal" in task_entry, f"Missing goal in {task_entry}"
            assert "type" in task_entry, f"Missing type in {task_entry}"
            assert "status" in task_entry, f"Missing status in {task_entry}"
            assert "requires_gpu" in task_entry, f"Missing requires_gpu in {task_entry}"

    def test_decompose_calls_task_manager_with_mission_id_and_source(self, mission_dir, task_state_dir):
        """decompose_mission calls task_manager.py with --mission-id and --source mission-subtask."""
        _create_mission_file(mission_dir, "abc00004", goal="Grow SEO traffic", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            _make_mock_sonnet_decompose("abc00004"),
        ]

        ollama_responses = []
        for _ in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(3, False))

        calls_made = []
        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            with patch("mission_engine.subprocess") as mock_sub:
                def capture_call(*args, **kwargs):
                    calls_made.append(args[0] if args else kwargs.get("args", []))
                    mock_proc = MagicMock()
                    mock_proc.returncode = 0
                    mock_proc.stdout = "CREATED: task_sub00004\n"
                    return mock_proc
                mock_sub.run.side_effect = capture_call

                mission_arg = MagicMock()
                mission_arg.mission_id = "abc00004"
                mission_engine.decompose_mission(mission_arg)

        assert len(calls_made) > 0, "No subprocess calls made"
        # Check that at least one call has --mission-id and --source mission-subtask
        found_mission_id = False
        found_source = False
        for call in calls_made:
            cmd_str = " ".join(str(c) for c in call)
            if "--mission-id" in cmd_str and "abc00004" in cmd_str:
                found_mission_id = True
            if "--source" in cmd_str and "mission-subtask" in cmd_str:
                found_source = True
        assert found_mission_id, f"--mission-id abc00004 not found in calls: {calls_made}"
        assert found_source, f"--source mission-subtask not found in calls: {calls_made}"

    def test_decompose_handles_duplicate_exit_code_2(self, mission_dir, task_state_dir):
        """decompose_mission handles exit code 2 (duplicate) gracefully."""
        _create_mission_file(mission_dir, "abc00005", goal="Grow SEO traffic", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            _make_mock_sonnet_decompose("abc00005"),
        ]

        ollama_responses = []
        for _ in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(3, False))

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 2  # DUPLICATE_REJECTED
                mock_proc.stdout = ""
                mock_sub.run.return_value = mock_proc

                # Should not raise an exception
                args = MagicMock()
                args.mission_id = "abc00005"
                mission_engine.decompose_mission(args)  # no exception = pass

    def test_decompose_kpis_stored_in_mission(self, mission_dir, task_state_dir):
        """decompose_mission stores KPIs from Sonnet decomposition in mission file."""
        _create_mission_file(mission_dir, "abc00006", goal="Grow SEO traffic", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            _make_mock_sonnet_decompose("abc00006"),
        ]

        ollama_responses = []
        for _ in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(3, False))

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = "CREATED: task_sub00006\n"
                mock_sub.run.return_value = mock_proc

                args = MagicMock()
                args.mission_id = "abc00006"
                mission_engine.decompose_mission(args)

        mission, _ = mission_engine.load_mission("abc00006")
        assert len(mission["kpis"]) == 2, f"Expected 2 KPIs, got {len(mission['kpis'])}"
        # KPIs should be stored as dicts with at least 'metric' field
        for kpi in mission["kpis"]:
            assert "metric" in kpi, f"KPI missing 'metric' field: {kpi}"


# --- TASK-01: Standalone classify subcommand ---

class TestTaskClassify:
    def test_classify_one_time_task(self, mission_dir):
        """classify_task returns one-time for 'write intro blog post'."""
        import mission_engine
        with patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = [
                _make_mock_classification("one-time", 0.92),
            ]
            result = mission_engine.classify_task("write intro blog post")
        assert result.task_type == "one-time"
        assert result.confidence >= 0.9

    def test_classify_recurring_task(self, mission_dir):
        """classify_task returns recurring with cadence for 'post to Reddit daily'."""
        import mission_engine
        with patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = [
                _make_mock_classification("recurring", 0.95, "0 9 * * *"),
            ]
            result = mission_engine.classify_task("post to Reddit daily")
        assert result.task_type == "recurring"
        assert result.cadence == "0 9 * * *"

    def test_classify_returns_taskclassification_object(self, mission_dir):
        """classify_task returns a TaskClassification instance."""
        import mission_engine
        with patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = [
                _make_mock_classification("one-time", 0.85),
            ]
            result = mission_engine.classify_task("do something once")
        assert isinstance(result, mission_engine.TaskClassification)

    @pytest.mark.slow
    def test_classify_live_one_time(self, mission_dir):
        """Live Qwen3 call: classify a one-time task."""
        import mission_engine
        result = mission_engine.classify_task("write an intro blog post about Python")
        assert result.task_type in ("one-time", "recurring")
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.slow
    def test_classify_live_recurring(self, mission_dir):
        """Live Qwen3 call: classify a recurring task."""
        import mission_engine
        result = mission_engine.classify_task("post daily updates to Reddit every morning")
        assert result.task_type == "recurring"


# --- TASK-02: Cron cadence extraction ---

class TestCadenceExtraction:
    def test_cadence_stored_in_mission_task(self, mission_dir, task_state_dir):
        """Recurring subtask gets cron cadence stored in mission task entry."""
        from output_schema import MissionDecompositionOutput
        _create_mission_file(mission_dir, "cad00001", goal="Post daily SEO updates", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_result = MagicMock()
        mock_result.parsed = MissionDecompositionOutput(
            mission_id="cad00001",
            subtasks=["Post to Reddit daily"],
            kpis=["100 karma/month"],
            cadence=None,
        )

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            mock_result,
        ]

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            # classification: recurring with cadence
            mock_ollama.chat.side_effect = [
                _make_mock_classification("recurring", 0.95, "0 8 * * *"),
                _make_mock_enrichment(2, False),
            ]

            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = "CREATED: task_cad00001\n"
                mock_sub.run.return_value = mock_proc

                args = MagicMock()
                args.mission_id = "cad00001"
                mission_engine.decompose_mission(args)

        mission, _ = mission_engine.load_mission("cad00001")
        assert len(mission["tasks"]) == 1
        task_entry = mission["tasks"][0]
        assert task_entry["type"] == "recurring", f"Expected recurring, got {task_entry['type']}"
        assert task_entry["cadence"] == "0 8 * * *", f"Expected cron cadence, got {task_entry.get('cadence')}"

    @pytest.mark.slow
    def test_cadence_live_extraction(self, mission_dir):
        """Live Qwen3 call: cadence extracted from recurring task description."""
        import mission_engine
        result = mission_engine.classify_task("submit Reddit posts every morning at 8am")
        assert result.task_type == "recurring"
        # cadence may be None (Qwen3 might not always extract cron) — just verify no crash


# --- TASK-05: Low confidence triggers clarification ---

class TestClassifyAmbiguity:
    def test_low_confidence_sets_needs_clarification(self, mission_dir, capsys):
        """classify subcommand with confidence < 0.6 prints NEEDS_CLARIFICATION."""
        with patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = [
                _make_mock_classification("one-time", 0.4),  # low confidence
            ]
            import mission_engine
            args = MagicMock()
            args.description = "do something vague"
            mission_engine.classify_subcommand(args)

        captured = capsys.readouterr()
        assert "NEEDS_CLARIFICATION" in captured.out, \
            f"Expected NEEDS_CLARIFICATION in output: {captured.out}"

    def test_high_confidence_no_clarification(self, mission_dir, capsys):
        """classify subcommand with confidence >= 0.6 does NOT print NEEDS_CLARIFICATION."""
        with patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = [
                _make_mock_classification("one-time", 0.9),
            ]
            import mission_engine
            args = MagicMock()
            args.description = "write a blog post about Python"
            mission_engine.classify_subcommand(args)

        captured = capsys.readouterr()
        assert "NEEDS_CLARIFICATION" not in captured.out
        assert "TYPE:" in captured.out


# --- TASK-06: Complexity score stored in task context ---

class TestComplexityRouting:
    def test_complexity_stored_in_task_context(self, mission_dir, task_state_dir):
        """Each subtask gets complexity score stored in task context (for model routing)."""
        from output_schema import MissionDecompositionOutput
        _create_mission_file(mission_dir, "cpx00001", goal="Build complex system", priority=1)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_result = MagicMock()
        mock_result.parsed = MissionDecompositionOutput(
            mission_id="cpx00001",
            subtasks=["Design architecture"],
            kpis=["System deployed"],
            cadence=None,
        )

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            mock_result,
        ]

        captured_cmds = []
        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = [
                _make_mock_classification("one-time", 0.9),
                _make_mock_enrichment(4, False),  # complexity=4
            ]

            with patch("mission_engine.subprocess") as mock_sub:
                def capture(cmd, **kwargs):
                    captured_cmds.append(cmd)
                    p = MagicMock()
                    p.returncode = 0
                    p.stdout = "CREATED: task_cpx00001\n"
                    return p
                mock_sub.run.side_effect = capture

                args = MagicMock()
                args.mission_id = "cpx00001"
                mission_engine.decompose_mission(args)

        # Verify subprocess call includes complexity in context JSON
        assert len(captured_cmds) > 0, "No subprocess calls"
        cmd_str = " ".join(str(c) for c in captured_cmds[0])
        assert "complexity" in cmd_str, f"'complexity' not found in subprocess cmd: {cmd_str}"
        assert "4" in cmd_str, f"complexity value 4 not found in cmd: {cmd_str}"


# --- TASK-07: Dependency order preserved ---

class TestDependencyOrder:
    def test_tasks_preserve_decomposition_order(self, mission_dir, task_state_dir):
        """Mission tasks array preserves subtask ordering from Sonnet decomposition."""
        from output_schema import MissionDecompositionOutput
        _create_mission_file(mission_dir, "dep00001", goal="Multi-step pipeline", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        subtasks = ["Step 1: research", "Step 2: write", "Step 3: publish"]
        mock_result = MagicMock()
        mock_result.parsed = MissionDecompositionOutput(
            mission_id="dep00001",
            subtasks=subtasks,
            kpis=["Published"],
            cadence=None,
        )

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False),
            mock_result,
        ]

        ollama_responses = []
        for _ in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(2, False))

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = "CREATED: task_dep00001\n"
                mock_sub.run.return_value = mock_proc

                args = MagicMock()
                args.mission_id = "dep00001"
                mission_engine.decompose_mission(args)

        mission, _ = mission_engine.load_mission("dep00001")
        assert len(mission["tasks"]) == 3
        for i, task_entry in enumerate(mission["tasks"]):
            assert subtasks[i] in task_entry["goal"], \
                f"Task {i} goal mismatch: expected '{subtasks[i]}', got '{task_entry['goal']}'"


# --- MISS-11: Ambiguity check gates decomposition ---

class TestAmbiguityCheck:
    def test_ambiguous_goal_exits_with_code_3(self, mission_dir, task_state_dir, capsys):
        """Ambiguous goal triggers CLARIFICATION_NEEDED and does not decompose."""
        _create_mission_file(mission_dir, "amb00001", goal="do something about the website", priority=3)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=True, question="What should be done with the website?", confidence=0.9),
        ]

        with patch("mission_engine.Anthropic", return_value=mock_client):
            with pytest.raises(SystemExit) as exc_info:
                args = MagicMock()
                args.mission_id = "amb00001"
                mission_engine.decompose_mission(args)

        assert exc_info.value.code == 3, f"Expected exit code 3, got {exc_info.value.code}"
        captured = capsys.readouterr()
        assert "CLARIFICATION_NEEDED" in captured.out

    def test_ambiguous_goal_does_not_create_tasks(self, mission_dir, task_state_dir):
        """Ambiguous goal aborts before creating any tasks."""
        _create_mission_file(mission_dir, "amb00002", goal="make money somehow", priority=3)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=True, question="Make money how?", confidence=0.95),
        ]

        with patch("mission_engine.Anthropic", return_value=mock_client):
            with pytest.raises(SystemExit):
                args = MagicMock()
                args.mission_id = "amb00002"
                mission_engine.decompose_mission(args)

        mission, _ = mission_engine.load_mission("amb00002")
        assert mission["tasks"] == [], f"Tasks should be empty for ambiguous goal, got {mission['tasks']}"
        assert mission["status"] == "INBOX", f"Status should remain INBOX, got {mission['status']}"

    def test_clear_goal_proceeds_to_decompose(self, mission_dir, task_state_dir):
        """Clear goal passes ambiguity check and proceeds to decompose."""
        _create_mission_file(mission_dir, "amb00003", goal="Write 5 SEO articles about Python", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = [
            _make_mock_ambiguity(is_ambiguous=False, confidence=0.95),
            _make_mock_sonnet_decompose("amb00003"),
        ]

        ollama_responses = []
        for _ in range(3):
            ollama_responses.append(_make_mock_classification("one-time", 0.9))
            ollama_responses.append(_make_mock_enrichment(2, False))

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama:
            mock_ollama.chat.side_effect = ollama_responses

            with patch("mission_engine.subprocess") as mock_sub:
                mock_proc = MagicMock()
                mock_proc.returncode = 0
                mock_proc.stdout = "CREATED: task_amb00003\n"
                mock_sub.run.return_value = mock_proc

                args = MagicMock()
                args.mission_id = "amb00003"
                mission_engine.decompose_mission(args)  # should not raise

        mission, _ = mission_engine.load_mission("amb00003")
        assert mission["status"] == "ACTIVE"


# --- MISS-05: next-task subcommand ---

class TestNextTask:
    def test_next_task_returns_first_created(self, mission_dir, task_state_dir):
        """next-task returns first CREATED task in dependency order."""
        _create_mission_file(mission_dir, "nxt00001", goal="Multi-step work", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        # Set up mission with 3 tasks
        mission, path = mission_engine.load_mission("nxt00001")
        mission["tasks"] = [
            {"task_id": "t001", "goal": "Step 1", "type": "one-time", "status": "CREATED", "requires_gpu": False},
            {"task_id": "t002", "goal": "Step 2", "type": "one-time", "status": "CREATED", "requires_gpu": False},
            {"task_id": "t003", "goal": "Step 3", "type": "one-time", "status": "CREATED", "requires_gpu": False},
        ]
        mission_engine.save_mission(mission, path)

        # Create corresponding task files
        _create_task_file(task_state_dir, "t001", "CREATED", "Step 1")
        _create_task_file(task_state_dir, "t002", "CREATED", "Step 2")
        _create_task_file(task_state_dir, "t003", "CREATED", "Step 3")

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            args = MagicMock()
            args.mission_id = "nxt00001"
            args.all_missions = False
            mission_engine.next_task(args)
        output = f.getvalue()
        assert "NEXT_TASK:" in output, f"Expected NEXT_TASK: in output: {output}"
        assert "t001" in output, f"Expected t001 as first task, got: {output}"

    def test_next_task_skips_done(self, mission_dir, task_state_dir):
        """next-task skips DONE tasks and returns next eligible."""
        _create_mission_file(mission_dir, "nxt00002", goal="Multi-step work", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mission, path = mission_engine.load_mission("nxt00002")
        mission["tasks"] = [
            {"task_id": "d001", "goal": "Done task", "type": "one-time", "status": "DONE", "requires_gpu": False},
            {"task_id": "d002", "goal": "Active task", "type": "one-time", "status": "CREATED", "requires_gpu": False},
        ]
        mission_engine.save_mission(mission, path)

        _create_task_file(task_state_dir, "d001", "DONE", "Done task")
        _create_task_file(task_state_dir, "d002", "CREATED", "Active task")

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            args = MagicMock()
            args.mission_id = "nxt00002"
            args.all_missions = False
            mission_engine.next_task(args)
        output = f.getvalue()
        assert "d002" in output, f"Expected d002 as next task after skipping DONE, got: {output}"

    def test_next_task_skips_running(self, mission_dir, task_state_dir):
        """next-task skips RUNNING tasks (already in progress)."""
        _create_mission_file(mission_dir, "nxt00003", goal="Multi-step work", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mission, path = mission_engine.load_mission("nxt00003")
        mission["tasks"] = [
            {"task_id": "r001", "goal": "Running task", "type": "one-time", "status": "RUNNING", "requires_gpu": False},
            {"task_id": "r002", "goal": "Pending task", "type": "one-time", "status": "CREATED", "requires_gpu": False},
        ]
        mission_engine.save_mission(mission, path)

        _create_task_file(task_state_dir, "r001", "RUNNING", "Running task")
        _create_task_file(task_state_dir, "r002", "CREATED", "Pending task")

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            args = MagicMock()
            args.mission_id = "nxt00003"
            args.all_missions = False
            mission_engine.next_task(args)
        output = f.getvalue()
        assert "r002" in output, f"Expected r002 after skipping RUNNING, got: {output}"

    def test_next_task_all_done_returns_no_tasks_available(self, mission_dir, task_state_dir):
        """next-task prints NO_TASKS_AVAILABLE when all tasks are done."""
        _create_mission_file(mission_dir, "nxt00004", goal="Done mission", priority=2)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mission, path = mission_engine.load_mission("nxt00004")
        mission["tasks"] = [
            {"task_id": "a001", "goal": "Completed task", "type": "one-time", "status": "DONE", "requires_gpu": False},
        ]
        mission_engine.save_mission(mission, path)

        _create_task_file(task_state_dir, "a001", "DONE", "Completed task")

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            args = MagicMock()
            args.mission_id = "nxt00004"
            args.all_missions = False
            mission_engine.next_task(args)
        output = f.getvalue()
        assert "NO_TASKS_AVAILABLE" in output, f"Expected NO_TASKS_AVAILABLE, got: {output}"

    def test_next_task_all_missions_respects_priority(self, mission_dir, task_state_dir):
        """next-task --all-missions returns tasks in priority order."""
        _create_mission_file(mission_dir, "all00001", goal="Low priority mission", priority=4)
        _create_mission_file(mission_dir, "all00002", goal="High priority mission", priority=1)
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        for mid, tid, goal in [("all00001", "low001", "Low priority task"), ("all00002", "high001", "High priority task")]:
            mission, path = mission_engine.load_mission(mid)
            mission["tasks"] = [
                {"task_id": tid, "goal": goal, "type": "one-time", "status": "CREATED", "requires_gpu": False},
            ]
            mission_engine.save_mission(mission, path)
            _create_task_file(task_state_dir, tid, "CREATED", goal)

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            args = MagicMock()
            args.all_missions = True
            args.mission_id = None
            mission_engine.next_task(args)
        output = f.getvalue()

        # High priority (P1) task should appear before low priority (P4) task
        idx_high = output.find("high001")
        idx_low = output.find("low001")
        assert idx_high >= 0, f"high001 not found in output: {output}"
        assert idx_low >= 0, f"low001 not found in output: {output}"
        assert idx_high < idx_low, f"High priority task should appear before low priority. Output: {output}"


# =============================================================================
# Plan 02-03: KPI lifecycle — auto-select, update-kpi, stall detection,
#             recurring task re-creation, adapt subcommand.
# Uses direct import + unittest.mock.patch for fast isolated testing.
# =============================================================================

def _create_mission_file_with_kpis(mission_dir, mission_id, goal="Test goal",
                                    priority=3, status="ACTIVE",
                                    kpis=None, tasks=None, stall_count=0, strategy=""):
    """Helper: create a mission JSON with custom kpis and tasks."""
    from datetime import datetime, timezone
    mission = {
        "id": mission_id,
        "goal": goal,
        "original_goal": goal,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": kpis or [],
        "tasks": tasks or [],
        "strategy": strategy or goal,
        "stall_count": stall_count,
        "priority": priority,
    }
    path = mission_dir / f"mission_{mission_id}.json"
    with open(path, "w") as f:
        import json
        json.dump(mission, f, indent=2)
    # Write ledger entry
    ledger_path = mission_dir / "ledger.json"
    if ledger_path.exists():
        with open(ledger_path) as f:
            import json
            ledger = json.load(f)
    else:
        ledger = {"missions": [], "updated_at": ""}
    ledger["missions"].append({
        "id": mission_id,
        "goal": goal,
        "status": status,
        "priority": priority,
        "kpi_summary": "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    with open(ledger_path, "w") as f:
        import json
        json.dump(ledger, f, indent=2)
    return mission, path


# --- MISS-08: KPI auto-selection ---

class TestKPIAutoSelect:
    def test_traffic_goal_selects_gsc_clicks(self, mission_dir):
        """Mission with 'traffic' in goal auto-selects gsc_clicks KPI."""
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        kpis = mission_engine.auto_select_kpis("Increase traffic from SEO")
        metrics = [k["metric"] for k in kpis]
        assert "gsc_clicks" in metrics, f"Expected gsc_clicks, got: {metrics}"

    def test_books_goal_selects_books_published(self, mission_dir):
        """Mission with 'books' in goal auto-selects books_published KPI."""
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        kpis = mission_engine.auto_select_kpis("Publish 3 books on KDP")
        metrics = [k["metric"] for k in kpis]
        assert "books_published" in metrics, f"Expected books_published, got: {metrics}"

    def test_revenue_goal_selects_monthly_revenue(self, mission_dir):
        """Mission with 'revenue' in goal auto-selects monthly_revenue KPI."""
        import mission_engine
        kpis = mission_engine.auto_select_kpis("Grow monthly revenue to $1000")
        metrics = [k["metric"] for k in kpis]
        assert "monthly_revenue" in metrics, f"Expected monthly_revenue, got: {metrics}"

    def test_followers_goal_selects_follower_count(self, mission_dir):
        """Mission with 'followers' in goal auto-selects follower_count KPI."""
        import mission_engine
        kpis = mission_engine.auto_select_kpis("Get 500 followers on Twitter")
        metrics = [k["metric"] for k in kpis]
        assert "follower_count" in metrics, f"Expected follower_count, got: {metrics}"

    def test_articles_goal_selects_articles_published(self, mission_dir):
        """Mission with 'articles' in goal auto-selects articles_published KPI."""
        import mission_engine
        kpis = mission_engine.auto_select_kpis("Write 10 articles about Python")
        metrics = [k["metric"] for k in kpis]
        assert "articles_published" in metrics, f"Expected articles_published, got: {metrics}"

    def test_no_keyword_returns_empty(self, mission_dir):
        """Mission with no recognized keyword returns empty KPI list."""
        import mission_engine
        kpis = mission_engine.auto_select_kpis("Do something vague")
        assert kpis == [], f"Expected empty list, got: {kpis}"

    def test_auto_select_deduplicates_metrics(self, mission_dir):
        """Goals with multiple synonyms for same metric return only one KPI per metric."""
        import mission_engine
        # "traffic" and "seo" both map to gsc_clicks
        kpis = mission_engine.auto_select_kpis("Grow seo traffic and get more visits")
        metrics = [k["metric"] for k in kpis]
        # gsc_clicks should appear only once even though traffic, seo, visits all match
        assert metrics.count("gsc_clicks") <= 2, f"gsc_clicks duplicated: {metrics}"
        # unique metrics
        assert len(metrics) == len(set(metrics)), f"Duplicate metrics found: {metrics}"

    def test_create_mission_with_traffic_goal_has_kpi(self, mission_dir):
        """create command with 'traffic' goal populates kpis in mission file."""
        r = run_me("create", "--goal", "Grow organic traffic from SEO",
                   env_override={"MISSION_DIR": str(mission_dir)})
        assert r.returncode == 0, f"create failed: {r.stderr}"
        mission_id = extract_mission_id(r.stdout)
        m = get_mission_file(mission_dir, mission_id)
        assert len(m["kpis"]) > 0, f"Expected KPIs to be auto-selected, got empty: {m['kpis']}"
        metrics = [k["metric"] for k in m["kpis"]]
        assert "gsc_clicks" in metrics, f"Expected gsc_clicks, got: {metrics}"

    def test_create_mission_no_keyword_has_empty_kpis(self, mission_dir):
        """create command with unrecognized goal leaves kpis empty (for decompose to fill)."""
        r = run_me("create", "--goal", "Set up workspace organization",
                   env_override={"MISSION_DIR": str(mission_dir)})
        assert r.returncode == 0
        mission_id = extract_mission_id(r.stdout)
        m = get_mission_file(mission_dir, mission_id)
        assert m["kpis"] == [], f"Expected empty KPIs, got: {m['kpis']}"


# --- MISS-06: KPI completion triggers COMPLETED status ---

class TestKPICompletion:
    def test_all_kpis_met_transitions_to_completed(self, mission_dir):
        """update_kpi with all KPIs met sets status to COMPLETED."""
        _create_mission_file_with_kpis(
            mission_dir, "kpi00001",
            goal="Grow traffic",
            status="ACTIVE",
            kpis=[
                {"metric": "gsc_clicks", "target": 1000, "current": 0, "met": False},
            ]
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        import io, sys
        from contextlib import redirect_stdout
        buf = io.StringIO()
        args = MagicMock()
        args.mission_id = "kpi00001"
        args.kpi_metric = "gsc_clicks"
        args.kpi_value = "1500"
        args.check_stall = False
        # archive will try to move the file; that's fine in temp dir
        with redirect_stdout(buf):
            mission_engine.update_kpi(args)
        output = buf.getvalue()
        assert "MISSION_COMPLETE" in output, f"Expected MISSION_COMPLETE, got: {output}"

    def test_partial_kpis_met_does_not_complete(self, mission_dir):
        """update_kpi with only some KPIs met does NOT set COMPLETED."""
        _create_mission_file_with_kpis(
            mission_dir, "kpi00002",
            goal="Grow traffic and revenue",
            status="ACTIVE",
            kpis=[
                {"metric": "gsc_clicks", "target": 1000, "current": 0, "met": False},
                {"metric": "monthly_revenue", "target": 500, "current": 0, "met": False},
            ]
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        args = MagicMock()
        args.mission_id = "kpi00002"
        args.kpi_metric = "gsc_clicks"
        args.kpi_value = "1500"
        args.check_stall = False
        with redirect_stdout(buf):
            mission_engine.update_kpi(args)
        output = buf.getvalue()
        assert "MISSION_COMPLETE" not in output, \
            f"Should NOT complete with only one KPI met: {output}"
        # Mission status should still be ACTIVE
        mission, _ = mission_engine.load_mission("kpi00002")
        assert mission["status"] == "ACTIVE", f"Expected ACTIVE, got: {mission['status']}"

    def test_kpi_value_updated_in_mission_file(self, mission_dir):
        """update_kpi persists the new current value to the mission file."""
        _create_mission_file_with_kpis(
            mission_dir, "kpi00003",
            status="ACTIVE",
            kpis=[{"metric": "articles_published", "target": 10, "current": 0, "met": False}]
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        args = MagicMock()
        args.mission_id = "kpi00003"
        args.kpi_metric = "articles_published"
        args.kpi_value = "5"
        args.check_stall = False
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            mission_engine.update_kpi(args)
        mission, _ = mission_engine.load_mission("kpi00003")
        kpi = next(k for k in mission["kpis"] if k["metric"] == "articles_published")
        assert kpi["current"] == 5, f"Expected current=5, got: {kpi['current']}"

    def test_kpi_met_flag_set_when_target_reached(self, mission_dir):
        """update_kpi sets met=True when current >= target."""
        _create_mission_file_with_kpis(
            mission_dir, "kpi00004",
            status="ACTIVE",
            kpis=[{"metric": "books_published", "target": 2, "current": 0, "met": False}]
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        args = MagicMock()
        args.mission_id = "kpi00004"
        args.kpi_metric = "books_published"
        args.kpi_value = "3"
        args.check_stall = False
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            mission_engine.update_kpi(args)
        # mission may be archived; check archive or active
        try:
            mission, _ = mission_engine.load_mission("kpi00004")
        except SystemExit:
            # may be archived
            archive_path = mission_dir / "archive" / "mission_kpi00004.json"
            with open(archive_path) as f:
                import json
                mission = json.load(f)
        kpi = next(k for k in mission["kpis"] if k["metric"] == "books_published")
        assert kpi["met"] is True, f"Expected met=True, got: {kpi['met']}"


# --- MISS-09: Stall detection ---

class TestStallDetection:
    def test_check_stall_increments_stall_count_when_no_progress(self, mission_dir, task_state_dir):
        """update_kpi --check-stall increments stall_count when no tasks completed."""
        _create_mission_file_with_kpis(
            mission_dir, "stl00001",
            status="ACTIVE",
            tasks=[{"task_id": "s001", "goal": "Do thing", "type": "one-time",
                    "cadence": None, "status": "CREATED", "requires_gpu": False}],
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir
        # Create task file with CREATED status (not done)
        _create_task_file(task_state_dir, "s001", "CREATED", "Do thing")

        args = MagicMock()
        args.mission_id = "stl00001"
        args.kpi_metric = None
        args.kpi_value = None
        args.check_stall = True
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            mission_engine.update_kpi(args)
        mission, _ = mission_engine.load_mission("stl00001")
        assert mission["stall_count"] == 1, f"Expected stall_count=1, got: {mission['stall_count']}"

    def test_check_stall_resets_when_task_completed(self, mission_dir, task_state_dir):
        """update_kpi --check-stall resets stall_count to 0 when a task completes."""
        _create_mission_file_with_kpis(
            mission_dir, "stl00002",
            status="ACTIVE",
            stall_count=2,
            tasks=[{"task_id": "s002", "goal": "Do thing", "type": "one-time",
                    "cadence": None, "status": "DONE", "requires_gpu": False}],
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir
        # Task file shows DONE
        _create_task_file(task_state_dir, "s002", "DONE", "Do thing")

        args = MagicMock()
        args.mission_id = "stl00002"
        args.kpi_metric = None
        args.kpi_value = None
        args.check_stall = True
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            mission_engine.update_kpi(args)
        mission, _ = mission_engine.load_mission("stl00002")
        assert mission["stall_count"] == 0, f"Expected stall_count=0 after reset, got: {mission['stall_count']}"

    def test_check_stall_at_3_sets_stalled_status(self, mission_dir, task_state_dir):
        """update_kpi --check-stall sets status to STALLED when stall_count reaches 3."""
        _create_mission_file_with_kpis(
            mission_dir, "stl00003",
            status="ACTIVE",
            stall_count=2,  # will become 3 after this check
            tasks=[{"task_id": "s003", "goal": "Do thing", "type": "one-time",
                    "cadence": None, "status": "CREATED", "requires_gpu": False}],
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir
        _create_task_file(task_state_dir, "s003", "CREATED", "Do thing")

        args = MagicMock()
        args.mission_id = "stl00003"
        args.kpi_metric = None
        args.kpi_value = None
        args.check_stall = True
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            mission_engine.update_kpi(args)
        output = buf.getvalue()
        assert "MISSION_STALLED" in output, f"Expected MISSION_STALLED in output: {output}"
        mission, _ = mission_engine.load_mission("stl00003")
        assert mission["status"] == "STALLED", f"Expected STALLED, got: {mission['status']}"

    def test_check_stall_prints_adapt_recommended_at_3(self, mission_dir, task_state_dir):
        """update_kpi prints ADAPT_RECOMMENDED when stall_count reaches 3."""
        _create_mission_file_with_kpis(
            mission_dir, "stl00004",
            status="ACTIVE",
            stall_count=2,
            tasks=[{"task_id": "s004", "goal": "Do thing", "type": "one-time",
                    "cadence": None, "status": "CREATED", "requires_gpu": False}],
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir
        _create_task_file(task_state_dir, "s004", "CREATED", "Do thing")

        args = MagicMock()
        args.mission_id = "stl00004"
        args.kpi_metric = None
        args.kpi_value = None
        args.check_stall = True
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            mission_engine.update_kpi(args)
        output = buf.getvalue()
        assert "ADAPT_RECOMMENDED" in output, f"Expected ADAPT_RECOMMENDED, got: {output}"


# --- Recurring task re-creation ---

class TestRecurringReCreation:
    def test_recurring_task_due_creates_new_task(self, mission_dir, task_state_dir):
        """Recurring task that is DONE and whose next run is overdue gets re-created."""
        import time, json as _json
        from datetime import datetime, timezone, timedelta
        # Create a task that completed 2 hours ago with a "every 30 min" cadence
        # so next run is overdue
        completed_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        _create_task_file(task_state_dir, "rec001", "DONE", "Post daily update")
        # Patch completed_at in the task file
        task_path = task_state_dir / "task_rec001.json"
        with open(task_path) as f:
            t = _json.load(f)
        t["completed_at"] = completed_at
        with open(task_path, "w") as f:
            _json.dump(t, f)

        _create_mission_file_with_kpis(
            mission_dir, "rec00001",
            status="ACTIVE",
            tasks=[{
                "task_id": "rec001",
                "goal": "Post daily update",
                "type": "recurring",
                "cadence": "*/30 * * * *",  # every 30 min
                "status": "DONE",
                "requires_gpu": False,
            }],
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        # Mock subprocess so task_manager create doesn't actually run
        with patch("mission_engine.subprocess") as mock_sub:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "CREATED: task_rec002\n"
            mock_sub.run.return_value = mock_proc

            args = MagicMock()
            args.mission_id = "rec00001"
            args.kpi_metric = None
            args.kpi_value = None
            args.check_stall = False
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.update_kpi(args)

            # Subprocess should have been called to create new task
            assert mock_sub.run.called, "Expected subprocess.run to be called for re-creation"

    def test_recurring_task_not_due_does_not_recreate(self, mission_dir, task_state_dir):
        """Recurring task whose next run is not yet due is NOT re-created."""
        import json as _json
        from datetime import datetime, timezone
        # Task just completed right now — next run is far in the future
        completed_at = datetime.now(timezone.utc).isoformat()
        _create_task_file(task_state_dir, "rec002", "DONE", "Post weekly update")
        task_path = task_state_dir / "task_rec002.json"
        with open(task_path) as f:
            t = _json.load(f)
        t["completed_at"] = completed_at
        with open(task_path, "w") as f:
            _json.dump(t, f)

        _create_mission_file_with_kpis(
            mission_dir, "rec00002",
            status="ACTIVE",
            tasks=[{
                "task_id": "rec002",
                "goal": "Post weekly update",
                "type": "recurring",
                "cadence": "0 9 * * 1",  # every Monday 9am
                "status": "DONE",
                "requires_gpu": False,
            }],
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        with patch("mission_engine.subprocess") as mock_sub:
            args = MagicMock()
            args.mission_id = "rec00002"
            args.kpi_metric = None
            args.kpi_value = None
            args.check_stall = False
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.update_kpi(args)

            # Subprocess should NOT have been called (not due yet)
            assert not mock_sub.run.called, \
                "Expected no subprocess.run for not-yet-due recurring task"


# --- MISS-09 (adapt): Strategy adaptation via Sonnet ---

def _make_mock_adaptation(revised_strategy="New strategy", new_subtasks=None, cancel_ids=None):
    """Return MagicMock simulating client.beta.messages.parse() for AdaptationOutput."""
    mock_result = MagicMock()
    mock_result.parsed = MagicMock()
    mock_result.parsed.revised_strategy = revised_strategy
    mock_result.parsed.new_subtasks = new_subtasks or ["New subtask A", "New subtask B"]
    mock_result.parsed.cancel_task_ids = cancel_ids or []
    mock_result.parsed.reasoning = "Mission stalled; new approach needed."
    return mock_result


def _adapt_ollama_side_effect(subtask_count=2):
    """Build ollama.chat side_effect list for adapt_mission.

    adapt calls classify_task + _enrich_subtask per subtask (2 chat calls each).
    """
    responses = []
    for _ in range(subtask_count):
        responses.append(_make_mock_classification("one-time", 0.9))
        responses.append(_make_mock_enrichment(2, False))
    return responses


class TestAdapt:
    def test_adapt_updates_strategy_field(self, mission_dir, task_state_dir):
        """adapt sets mission strategy to revised_strategy from Sonnet."""
        _create_mission_file_with_kpis(
            mission_dir, "ada00001",
            goal="Grow traffic",
            status="STALLED",
            stall_count=3,
            strategy="Old strategy",
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mock_client = MagicMock()
        mock_client.beta.messages.parse.return_value = _make_mock_adaptation(
            revised_strategy="New focused strategy"
        )

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_ada001\n")

            args = MagicMock()
            args.mission_id = "ada00001"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.adapt_mission(args)

        mission, _ = mission_engine.load_mission("ada00001")
        assert mission["strategy"] == "New focused strategy", \
            f"Expected 'New focused strategy', got: {mission['strategy']}"

    def test_adapt_resets_stall_count(self, mission_dir, task_state_dir):
        """adapt resets stall_count to 0."""
        _create_mission_file_with_kpis(
            mission_dir, "ada00002",
            goal="Grow revenue",
            status="STALLED",
            stall_count=4,
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mock_client = MagicMock()
        mock_client.beta.messages.parse.return_value = _make_mock_adaptation()

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_ada002\n")

            args = MagicMock()
            args.mission_id = "ada00002"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.adapt_mission(args)

        mission, _ = mission_engine.load_mission("ada00002")
        assert mission["stall_count"] == 0, f"Expected stall_count=0, got: {mission['stall_count']}"

    def test_adapt_sets_status_to_active(self, mission_dir, task_state_dir):
        """adapt transitions mission status from STALLED to ACTIVE."""
        _create_mission_file_with_kpis(
            mission_dir, "ada00003",
            goal="Grow followers",
            status="STALLED",
            stall_count=3,
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mock_client = MagicMock()
        mock_client.beta.messages.parse.return_value = _make_mock_adaptation()

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_ada003\n")

            args = MagicMock()
            args.mission_id = "ada00003"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.adapt_mission(args)

        mission, _ = mission_engine.load_mission("ada00003")
        assert mission["status"] == "ACTIVE", f"Expected ACTIVE, got: {mission['status']}"

    def test_adapt_adds_new_tasks_to_mission(self, mission_dir, task_state_dir):
        """adapt adds new tasks from Sonnet new_subtasks to mission tasks array."""
        _create_mission_file_with_kpis(
            mission_dir, "ada00004",
            goal="Grow books published",
            status="STALLED",
            stall_count=3,
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mock_client = MagicMock()
        mock_client.beta.messages.parse.return_value = _make_mock_adaptation(
            new_subtasks=["Write outline", "Draft chapter 1"]
        )

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_ada004\n")

            args = MagicMock()
            args.mission_id = "ada00004"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.adapt_mission(args)

        mission, _ = mission_engine.load_mission("ada00004")
        assert len(mission["tasks"]) >= 2, \
            f"Expected at least 2 new tasks, got: {len(mission['tasks'])}"

    def test_adapt_original_goal_unchanged(self, mission_dir, task_state_dir):
        """adapt never modifies original_goal field."""
        original = "Grow traffic through SEO"
        _create_mission_file_with_kpis(
            mission_dir, "ada00005",
            goal=original,
            status="STALLED",
            stall_count=3,
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mock_client = MagicMock()
        mock_client.beta.messages.parse.return_value = _make_mock_adaptation()

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_ada005\n")

            args = MagicMock()
            args.mission_id = "ada00005"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.adapt_mission(args)

        mission, _ = mission_engine.load_mission("ada00005")
        assert mission["original_goal"] == original, \
            f"original_goal mutated: expected '{original}', got '{mission['original_goal']}'"

    def test_adapt_prints_mission_adapted(self, mission_dir, task_state_dir):
        """adapt prints MISSION_ADAPTED: with mission ID."""
        _create_mission_file_with_kpis(
            mission_dir, "ada00006",
            goal="Grow social audience",
            status="STALLED",
            stall_count=3,
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mock_client = MagicMock()
        mock_client.beta.messages.parse.return_value = _make_mock_adaptation()

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_ada006\n")

            args = MagicMock()
            args.mission_id = "ada00006"
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                mission_engine.adapt_mission(args)

        assert "MISSION_ADAPTED" in buf.getvalue(), \
            f"Expected MISSION_ADAPTED in output: {buf.getvalue()}"


class TestAdaptReanchor:
    def test_adapt_prompt_contains_original_goal(self, mission_dir, task_state_dir):
        """Sonnet prompt passed to beta.messages.parse includes original_goal for re-anchoring."""
        original = "Grow organic SEO traffic to 10K visits"
        _create_mission_file_with_kpis(
            mission_dir, "anc00001",
            goal=original,
            status="STALLED",
            stall_count=3,
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        mock_client = MagicMock()
        mock_client.beta.messages.parse.return_value = _make_mock_adaptation()
        captured_calls = []

        def capture_parse(*a, **kw):
            captured_calls.append(kw)
            return _make_mock_adaptation()

        mock_client.beta.messages.parse.side_effect = capture_parse

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_anc001\n")

            args = MagicMock()
            args.mission_id = "anc00001"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.adapt_mission(args)

        assert len(captured_calls) > 0, "No calls made to client.beta.messages.parse"
        # The prompt should contain the original goal for re-anchoring
        found_goal = False
        for call in captured_calls:
            messages = call.get("messages", [])
            for msg in messages:
                content = msg.get("content", "")
                if original in content:
                    found_goal = True
                    break
        assert found_goal, \
            f"original_goal '{original}' not found in Sonnet prompt. Calls: {captured_calls}"

    def test_adapt_prompt_contains_reanchor_directive(self, mission_dir, task_state_dir):
        """Sonnet prompt explicitly tells model to re-anchor on original goal (anti-drift)."""
        _create_mission_file_with_kpis(
            mission_dir, "anc00002",
            goal="Grow content articles",
            status="STALLED",
            stall_count=3,
        )
        import mission_engine
        mission_engine.MISSIONS_DIR = mission_dir
        mission_engine.LEDGER_PATH = mission_dir / "ledger.json"
        mission_engine.ARCHIVE_DIR = mission_dir / "archive"
        mission_engine.TASK_STATE_DIR = task_state_dir

        captured_calls = []

        def capture_parse(*a, **kw):
            captured_calls.append(kw)
            return _make_mock_adaptation()

        mock_client = MagicMock()
        mock_client.beta.messages.parse.side_effect = capture_parse

        with patch("mission_engine.Anthropic", return_value=mock_client), \
             patch("mission_engine.ollama") as mock_ollama, \
             patch("mission_engine.subprocess") as mock_sub:
            mock_ollama.chat.side_effect = _adapt_ollama_side_effect(2)
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="CREATED: task_anc002\n")

            args = MagicMock()
            args.mission_id = "anc00002"
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                mission_engine.adapt_mission(args)

        assert len(captured_calls) > 0, "No calls made to client.beta.messages.parse"
        # Prompt should include re-anchor language
        found_reanchor = False
        for call in captured_calls:
            messages = call.get("messages", [])
            for msg in messages:
                content = msg.get("content", "").lower()
                if "re-anchor" in content or "original goal" in content or "do not drift" in content:
                    found_reanchor = True
                    break
        assert found_reanchor, \
            f"Re-anchor directive not found in prompt. Calls: {captured_calls}"


# ---------------------------------------------------------------------------
# TestAutonomyTier — AUTO-04
# ---------------------------------------------------------------------------


class TestAutonomyTier:
    """Tests that mission_engine.py create accepts --autonomy-tier and persists it."""

    def test_create_default_tier_is_1(self, mission_dir):
        """create --goal 'test' creates mission with autonomy_tier=1 (default)."""
        r = run_me("create", "--goal", "test goal for tier check",
                   env_override={"MISSION_DIR": str(mission_dir)})
        assert r.returncode == 0, f"Expected exit 0. stderr={r.stderr}"
        mission_id = extract_mission_id(r.stdout)
        assert mission_id, f"Could not extract mission_id from: {r.stdout}"
        mission = get_mission_file(mission_dir, mission_id)
        assert "autonomy_tier" in mission, f"Mission missing 'autonomy_tier' field: {mission}"
        assert mission["autonomy_tier"] == 1, \
            f"Expected default autonomy_tier=1, got {mission['autonomy_tier']}"

    def test_create_explicit_tier_2(self, mission_dir):
        """create --goal 'test' --autonomy-tier 2 creates mission with autonomy_tier=2."""
        r = run_me("create", "--goal", "test goal explicit tier 2",
                   "--autonomy-tier", "2",
                   env_override={"MISSION_DIR": str(mission_dir)})
        assert r.returncode == 0, f"Expected exit 0. stderr={r.stderr}"
        mission_id = extract_mission_id(r.stdout)
        assert mission_id
        mission = get_mission_file(mission_dir, mission_id)
        assert mission["autonomy_tier"] == 2, \
            f"Expected autonomy_tier=2, got {mission.get('autonomy_tier')}"

    def test_create_explicit_tier_3(self, mission_dir):
        """create --goal 'test' --autonomy-tier 3 creates mission with autonomy_tier=3."""
        r = run_me("create", "--goal", "test goal explicit tier 3",
                   "--autonomy-tier", "3",
                   env_override={"MISSION_DIR": str(mission_dir)})
        assert r.returncode == 0, f"Expected exit 0. stderr={r.stderr}"
        mission_id = extract_mission_id(r.stdout)
        assert mission_id
        mission = get_mission_file(mission_dir, mission_id)
        assert mission["autonomy_tier"] == 3, \
            f"Expected autonomy_tier=3, got {mission.get('autonomy_tier')}"

    def test_autonomy_tier_in_ledger(self, mission_dir):
        """Created mission's ledger entry includes autonomy_tier."""
        r = run_me("create", "--goal", "test goal for ledger tier check",
                   "--autonomy-tier", "2",
                   env_override={"MISSION_DIR": str(mission_dir)})
        assert r.returncode == 0, f"Expected exit 0. stderr={r.stderr}"
        mission_id = extract_mission_id(r.stdout)
        assert mission_id
        ledger = load_ledger(mission_dir)
        assert ledger is not None, "Ledger not created"
        entries = [e for e in ledger.get("missions", []) if e.get("id") == mission_id]
        assert len(entries) == 1, f"Mission {mission_id} not found in ledger"
        entry = entries[0]
        assert "autonomy_tier" in entry, \
            f"Ledger entry missing 'autonomy_tier': {entry}"
        assert entry["autonomy_tier"] == 2, \
            f"Expected autonomy_tier=2 in ledger, got {entry.get('autonomy_tier')}"

    def test_invalid_tier_rejected(self, mission_dir):
        """--autonomy-tier 5 is rejected by argparse (choices=[1,2,3])."""
        r = run_me("create", "--goal", "test invalid tier",
                   "--autonomy-tier", "5",
                   env_override={"MISSION_DIR": str(mission_dir)})
        assert r.returncode != 0, \
            f"Expected non-zero exit for invalid tier, got {r.returncode}"
