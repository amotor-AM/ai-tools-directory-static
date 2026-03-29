"""TDD test suite for supervisor.py — Phase 4 Plans 01 and 02.

Covers:
  SUPV-01: Mission Ledger (outer loop) — check-missions
  SUPV-02: Execution Ledger (inner loop) — validate-task
  SUPV-03: Stall-triggered adapt — _trigger_adapt, check-missions stall logic
  SUPV-05: Sub-agent assignment — assign_task via manage.py route + spawn
  SUPV-06: Outer-loop replan — [ADAPT] marker, stall reset after adapt
  SUPV-08: Pre-execution hooks — pre-check
  SUPV-09: Audit log — _audit(), audit-log subcommand

Requirement coverage:
  TestMissionLedger   → SUPV-01
  TestExecutionLedger → SUPV-02
  TestStallReplan     → SUPV-03
  TestAgentAssignment → SUPV-05
  TestOuterLoopReplan → SUPV-06
  TestPreExecHooks    → SUPV-08
  TestManageMissionId → (manage.py backward compat)
  TestAuditLog        → SUPV-09

Testing strategy:
  - TestMissionLedger and TestAuditLog use subprocess (run_supervisor) since they
    need real file I/O without mocking subprocess calls.
  - TestExecutionLedger uses direct module imports so subprocess.run can be mocked
    in-process (patching the subprocess calls to task_manager.py and heal.py).
"""

import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

# Add scripts/ to path so supervisor can be imported directly
SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUPERVISOR_PATH = Path(__file__).resolve().parent.parent / "scripts" / "supervisor.py"
TM_PATH = "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py"
HEAL_PATH = "/home/alex/.openclaw/workspace/scripts/heal.py"


def write_ledger(mission_dir: Path, missions: list) -> None:
    """Write a ledger.json to the missions directory."""
    ledger = {"missions": missions, "updated_at": datetime.now(timezone.utc).isoformat()}
    (mission_dir / "ledger.json").write_text(json.dumps(ledger))


def write_task(task_state_dir: Path, task_id: str, mission_id: str = "m_test",
               status: str = "DONE", goal: str = "Test task",
               task_type: str = "task_complete") -> Path:
    """Write a fake task JSON file to the task state directory."""
    task = {
        "id": task_id,
        "goal": goal,
        "mission_id": mission_id,
        "status": status,
        "quality_gate_status": "PENDING",
        "context": {"task_type": task_type},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = task_state_dir / f"task_{task_id}.json"
    path.write_text(json.dumps(task))
    return path


def run_supervisor(args: list, env: dict = None) -> subprocess.CompletedProcess:
    """Run supervisor.py as subprocess with optional env overrides."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(SUPERVISOR_PATH)] + args,
        capture_output=True,
        text=True,
        env=full_env,
    )


# ---------------------------------------------------------------------------
# TestMissionLedger — SUPV-01
# ---------------------------------------------------------------------------

class TestMissionLedger:
    """Tests for check-missions subcommand (outer loop, SUPV-01)."""

    def test_check_missions_empty_ledger(self, mission_dir, task_state_dir, tmp_path):
        """Empty ledger returns empty output and exit code 0."""
        write_ledger(mission_dir, [])
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        result = run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_state_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )
        assert result.returncode == 0
        # No mission lines in output
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        assert len(lines) == 0 or all("|" not in l for l in lines)

    def test_check_missions_progress(self, mission_dir, task_state_dir, tmp_path):
        """Mission with new DONE tasks resets stall_count to 0 in meta sidecar."""
        mission_id = "m_progress_001"
        write_ledger(mission_dir, [
            {"id": mission_id, "goal": "Grow Reddit karma", "status": "ACTIVE", "priority": 2}
        ])
        # Pre-seed meta with stall_count=2 and last_done_count=1
        meta_dir = mission_dir / "meta"
        meta_dir.mkdir()
        meta_path = meta_dir / f"mission_{mission_id}_meta.json"
        meta_path.write_text(json.dumps({"last_done_count": 1, "stall_count": 2}))
        # Write 2 DONE tasks for this mission
        write_task(task_state_dir, "task_a1b2", mission_id=mission_id, status="DONE")
        write_task(task_state_dir, "task_c3d4", mission_id=mission_id, status="DONE")

        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        result = run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_state_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )
        assert result.returncode == 0
        meta = json.loads(meta_path.read_text())
        assert meta.get("stall_count", 0) == 0, f"stall_count should be 0, got {meta.get('stall_count')}"
        assert meta.get("last_done_count") == 2

    def test_check_missions_no_progress(self, mission_dir, task_state_dir, tmp_path):
        """Mission with no new DONE tasks increments stall_count by 1."""
        mission_id = "m_stall_001"
        write_ledger(mission_dir, [
            {"id": mission_id, "goal": "Increase site traffic", "status": "ACTIVE", "priority": 2}
        ])
        # Pre-seed meta with last_done_count matching current state (1 done task already)
        meta_dir = mission_dir / "meta"
        meta_dir.mkdir()
        meta_path = meta_dir / f"mission_{mission_id}_meta.json"
        meta_path.write_text(json.dumps({"last_done_count": 1, "stall_count": 1}))
        # Write 1 DONE task — same as last_done_count, no progress
        write_task(task_state_dir, "task_e5f6", mission_id=mission_id, status="DONE")

        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        result = run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_state_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )
        assert result.returncode == 0
        meta = json.loads(meta_path.read_text())
        assert meta.get("stall_count") == 2, f"stall_count should be 2, got {meta.get('stall_count')}"

    def test_check_missions_terse_output(self, mission_dir, task_state_dir, tmp_path):
        """Output is one line per active mission with id, goal (truncated 40 chars), status, stall_count, done_tasks."""
        mission_id = "m_terse_001"
        long_goal = "A" * 60  # 60 chars, will be truncated to 40
        write_ledger(mission_dir, [
            {"id": mission_id, "goal": long_goal, "status": "ACTIVE", "priority": 2}
        ])
        write_task(task_state_dir, "task_g7h8", mission_id=mission_id, status="DONE")

        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        result = run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_state_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )
        assert result.returncode == 0
        output = result.stdout
        # Should contain mission id
        assert mission_id in output
        # Should contain status
        assert "ACTIVE" in output
        # Goal should be truncated to 40 chars
        assert long_goal not in output  # full 60-char goal should not appear
        assert long_goal[:40] in output  # truncated goal should appear
        # Should have stall: and done: indicators
        assert "stall:" in output
        assert "done:" in output

    def test_check_missions_skips_completed(self, mission_dir, task_state_dir, tmp_path):
        """COMPLETED and ARCHIVED missions are not checked (not in output)."""
        write_ledger(mission_dir, [
            {"id": "m_done_001", "goal": "Done mission", "status": "COMPLETED", "priority": 2},
            {"id": "m_arch_001", "goal": "Archived mission", "status": "ARCHIVED", "priority": 2},
            {"id": "m_active_001", "goal": "Active mission", "status": "ACTIVE", "priority": 2},
        ])
        write_task(task_state_dir, "task_i9j0", mission_id="m_active_001", status="DONE")

        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        result = run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_state_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )
        assert result.returncode == 0
        output = result.stdout
        assert "m_done_001" not in output
        assert "m_arch_001" not in output
        assert "m_active_001" in output


# ---------------------------------------------------------------------------
# TestExecutionLedger — SUPV-02
#
# Uses direct module import (not subprocess) so subprocess.run can be mocked
# in-process to prevent actual task_manager.py and heal.py calls.
# ---------------------------------------------------------------------------

def _import_supervisor():
    """Import supervisor module fresh (respects current env vars)."""
    import importlib
    import supervisor as sup_module
    # Reload so module-level env vars (EXEC_DIR, AUDIT_LOG_PATH) re-read from environ
    importlib.reload(sup_module)
    return sup_module


class TestExecutionLedger:
    """Tests for validate-task subcommand (inner loop, SUPV-02)."""

    def _run_validate(self, task_state_dir, tmp_path, task_id, result_json):
        """Helper: run validate_task() directly with env isolation."""
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir(exist_ok=True)

        env_patch = {
            "ARIA_TASK_DIR": str(task_state_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        old_env = {}
        for k, v in env_patch.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v

        try:
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                exit_code = sup.validate_task(task_id, result_json)
                return exit_code, exec_dir, audit_log, mock_run
        finally:
            for k, old_v in old_env.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

    def test_validate_task_pass(self, task_state_dir, tmp_path):
        """Valid result JSON writes exec_ file with passed=True and calls task_manager.py complete."""
        task_id = "task_pass_001"
        write_task(task_state_dir, task_id, task_type="task_complete")
        result_json = json.dumps({
            "status": "success",
            "summary": "Task completed successfully",
        })
        exit_code, exec_dir, audit_log, mock_run = self._run_validate(
            task_state_dir, tmp_path, task_id, result_json
        )
        assert exit_code == 0
        exec_file = exec_dir / f"exec_{task_id}.json"
        assert exec_file.exists(), f"exec_{task_id}.json not found in {exec_dir}"
        gate_result = json.loads(exec_file.read_text())
        assert gate_result["passed"] is True
        # subprocess.run called with task_manager.py complete
        assert mock_run.called
        call_args = mock_run.call_args_list[0][0][0]
        assert "complete" in call_args

    def test_validate_task_fail(self, task_state_dir, tmp_path):
        """Invalid result JSON writes exec_ file with passed=False and issues list, calls heal.py."""
        task_id = "task_fail_001"
        write_task(task_state_dir, task_id, task_type="article_published")
        # Missing required fields for ArticlePublishedOutput (url, word_count, title)
        result_json = json.dumps({"bad_field": "missing required fields"})
        exit_code, exec_dir, audit_log, mock_run = self._run_validate(
            task_state_dir, tmp_path, task_id, result_json
        )
        assert exit_code == 1
        exec_file = exec_dir / f"exec_{task_id}.json"
        assert exec_file.exists()
        gate_result = json.loads(exec_file.read_text())
        assert gate_result["passed"] is False
        assert len(gate_result["issues"]) > 0
        # subprocess.run called with heal.py attempt
        assert mock_run.called
        call_args = mock_run.call_args_list[0][0][0]
        assert "attempt" in call_args

    def test_validate_task_unknown_type(self, task_state_dir, tmp_path):
        """Missing context.task_type falls back to TaskCompleteOutput schema."""
        task_id = "task_unknown_001"
        task = {
            "id": task_id,
            "goal": "Some generic task",
            "mission_id": "m_test",
            "status": "DONE",
            "quality_gate_status": "PENDING",
            "context": {},  # no task_type key
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        (task_state_dir / f"task_{task_id}.json").write_text(json.dumps(task))
        result_json = json.dumps({"status": "success", "summary": "Done"})
        exit_code, exec_dir, audit_log, mock_run = self._run_validate(
            task_state_dir, tmp_path, task_id, result_json
        )
        assert exit_code == 0
        exec_file = exec_dir / f"exec_{task_id}.json"
        assert exec_file.exists()
        gate_result = json.loads(exec_file.read_text())
        assert gate_result["passed"] is True
        assert gate_result["task_type"] == "task_complete"

    def test_validate_task_infer_type(self, task_state_dir, tmp_path):
        """Task with goal containing 'article' infers article_published type."""
        task_id = "task_infer_001"
        task = {
            "id": task_id,
            "goal": "Publish SEO article about Python testing",
            "mission_id": "m_test",
            "status": "DONE",
            "quality_gate_status": "PENDING",
            "context": {},  # no task_type
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        (task_state_dir / f"task_{task_id}.json").write_text(json.dumps(task))
        result_json = json.dumps({
            "url": "https://example.com/article",
            "word_count": 1500,
            "title": "Python Testing Guide",
        })
        exit_code, exec_dir, audit_log, mock_run = self._run_validate(
            task_state_dir, tmp_path, task_id, result_json
        )
        assert exit_code == 0
        exec_file = exec_dir / f"exec_{task_id}.json"
        gate_result = json.loads(exec_file.read_text())
        assert gate_result["passed"] is True
        assert gate_result["task_type"] == "article_published"

    def test_exec_ledger_file_exists(self, task_state_dir, tmp_path):
        """exec_<task_id>.json is created in tasks/execution/ with QualityGateResult fields."""
        task_id = "task_ledger_001"
        write_task(task_state_dir, task_id, task_type="task_complete")
        result_json = json.dumps({"status": "success", "summary": "All done"})
        exit_code, exec_dir, audit_log, mock_run = self._run_validate(
            task_state_dir, tmp_path, task_id, result_json
        )
        exec_file = exec_dir / f"exec_{task_id}.json"
        assert exec_file.exists()
        data = json.loads(exec_file.read_text())
        assert "passed" in data
        assert "score" in data
        assert "issues" in data
        assert "task_type" in data
        assert "validated_at" in data
        datetime.fromisoformat(data["validated_at"])

    def test_quality_gate_status_updated(self, task_state_dir, tmp_path):
        """Task JSON quality_gate_status field is set to PASS or FAIL after validation."""
        task_id = "task_gate_001"
        task_path = write_task(task_state_dir, task_id, task_type="task_complete")
        result_json = json.dumps({"status": "success", "summary": "Completed"})
        exit_code, exec_dir, audit_log, mock_run = self._run_validate(
            task_state_dir, tmp_path, task_id, result_json
        )
        task_data = json.loads(task_path.read_text())
        assert task_data["quality_gate_status"] in ("PASS", "FAIL")
        assert task_data["quality_gate_status"] == "PASS"


# ---------------------------------------------------------------------------
# TestAuditLog — SUPV-09
# ---------------------------------------------------------------------------

class TestAuditLog:
    """Tests for _audit() function and audit-log subcommand (SUPV-09)."""

    def test_audit_appends_jsonl(self, tmp_path):
        """_audit() appends one JSON line to supervisor.jsonl."""
        import importlib
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()

        # Call _audit via CLI side-effect: run check-missions on empty ledger
        mission_dir = tmp_path / "missions"
        mission_dir.mkdir()
        task_dir = tmp_path / "tasks"
        task_dir.mkdir()
        write_ledger(mission_dir, [])

        run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )

        assert audit_log.exists(), "supervisor.jsonl should be created by check-missions"
        lines = audit_log.read_text().strip().splitlines()
        assert len(lines) >= 1

    def test_audit_record_structure(self, tmp_path):
        """Each audit record has ts (ISO), op (string), data (dict)."""
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        mission_dir = tmp_path / "missions"
        mission_dir.mkdir()
        task_dir = tmp_path / "tasks"
        task_dir.mkdir()
        write_ledger(mission_dir, [])

        run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )

        lines = audit_log.read_text().strip().splitlines()
        record = json.loads(lines[0])
        assert "ts" in record, "audit record missing 'ts' field"
        assert "op" in record, "audit record missing 'op' field"
        assert "data" in record, "audit record missing 'data' field"
        assert isinstance(record["data"], dict)
        # ts should be parseable ISO
        datetime.fromisoformat(record["ts"])

    def test_audit_multiple_appends(self, tmp_path):
        """3 check-missions calls produce 3 audit lines (one per call)."""
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        mission_dir = tmp_path / "missions"
        mission_dir.mkdir()
        task_dir = tmp_path / "tasks"
        task_dir.mkdir()
        write_ledger(mission_dir, [])

        env = {
            "MISSION_DIR": str(mission_dir),
            "ARIA_TASK_DIR": str(task_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        for _ in range(3):
            run_supervisor(["check-missions"], env=env)

        lines = [l for l in audit_log.read_text().strip().splitlines() if l.strip()]
        assert len(lines) == 3, f"Expected 3 audit lines, got {len(lines)}"

    def test_audit_log_subcommand(self, tmp_path):
        """audit-log CLI prints last N records from file."""
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        # Write 5 records manually
        records = []
        for i in range(5):
            rec = {"ts": datetime.now(timezone.utc).isoformat(), "op": f"test_op_{i}", "data": {"i": i}}
            records.append(json.dumps(rec))
        audit_log.write_text("\n".join(records) + "\n")

        result = run_supervisor(
            ["audit-log", "--tail", "3"],
            env={"SUPERVISOR_AUDIT_LOG": str(audit_log)},
        )
        assert result.returncode == 0
        output = result.stdout
        # Should have 3 records (last 3)
        assert "test_op_2" in output or "test_op_3" in output or "test_op_4" in output
        # First record should NOT appear if we asked for last 3
        assert "test_op_0" not in output

    def test_check_missions_writes_audit(self, mission_dir, task_state_dir, tmp_path):
        """check-missions produces audit record with op='check_missions'."""
        write_ledger(mission_dir, [])
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()

        run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_state_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )

        lines = audit_log.read_text().strip().splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        ops = [r["op"] for r in records]
        assert "check_missions" in ops, f"Expected 'check_missions' op in audit, got: {ops}"

    def test_validate_task_writes_audit(self, task_state_dir, tmp_path):
        """validate-task produces audit record with op='validate_task'."""
        task_id = "task_audit_001"
        write_task(task_state_dir, task_id, task_type="task_complete")
        result_json = json.dumps({"status": "success", "summary": "Done"})
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()

        # Use direct module call (in-process) so env var patches take effect
        env_patch = {
            "ARIA_TASK_DIR": str(task_state_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        old_env = {}
        for k, v in env_patch.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v

        try:
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                sup.validate_task(task_id, result_json)
        finally:
            for k, old_v in old_env.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

        lines = audit_log.read_text().strip().splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        ops = [r["op"] for r in records]
        assert "validate_task" in ops, f"Expected 'validate_task' op in audit, got: {ops}"


# ---------------------------------------------------------------------------
# TestStallReplan — SUPV-03
#
# Uses direct module import so subprocess.run can be mocked in-process to
# capture calls to mission_engine.py adapt without actually running it.
# ---------------------------------------------------------------------------

def _env_context(env_patch: dict):
    """Context manager: temporarily set env vars, restore on exit."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        old = {}
        for k, v in env_patch.items():
            old[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            yield
        finally:
            for k, old_v in old.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

    return _ctx()


class TestStallReplan:
    """Tests for stall-triggered adapt in supervisor.py (SUPV-03)."""

    def _setup_mission(self, mission_dir, task_state_dir, mission_id, stall_count,
                       last_done_count, done_task_count=None):
        """Create ledger + meta sidecar + tasks for a given mission state."""
        write_ledger(mission_dir, [
            {"id": mission_id, "goal": "Grow audience", "status": "ACTIVE", "priority": 2}
        ])
        meta_dir = mission_dir / "meta"
        meta_dir.mkdir(exist_ok=True)
        meta_path = meta_dir / f"mission_{mission_id}_meta.json"
        meta_path.write_text(json.dumps({
            "last_done_count": last_done_count,
            "stall_count": stall_count,
        }))
        if done_task_count is not None:
            for i in range(done_task_count):
                write_task(task_state_dir, f"task_{mission_id}_{i}", mission_id=mission_id, status="DONE")
        return meta_path

    def test_stall_triggers_adapt(self, mission_dir, task_state_dir, tmp_path):
        """stall_count=2 + no progress → stall_count=3 >= STALL_THRESHOLD → adapt called."""
        mission_id = "m_stall_adapt_001"
        meta_path = self._setup_mission(
            mission_dir, task_state_dir, mission_id,
            stall_count=2, last_done_count=1, done_task_count=1  # no new done tasks
        )
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSION_DIR": str(mission_dir),
            "ARIA_TASK_DIR": str(task_state_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                sup.check_missions()
        # subprocess.run should have been called with mission_engine.py adapt
        calls = mock_run.call_args_list
        adapt_calls = [c for c in calls if "adapt" in str(c)]
        assert len(adapt_calls) >= 1, f"Expected adapt call, got: {calls}"
        adapt_cmd = adapt_calls[0][0][0]
        assert "adapt" in adapt_cmd
        assert "--mission-id" in adapt_cmd
        assert mission_id in adapt_cmd

    def test_stall_below_threshold_no_adapt(self, mission_dir, task_state_dir, tmp_path):
        """stall_count=1 + no progress → stall_count=2 < STALL_THRESHOLD(3) → adapt NOT called."""
        mission_id = "m_stall_below_001"
        self._setup_mission(
            mission_dir, task_state_dir, mission_id,
            stall_count=1, last_done_count=1, done_task_count=1  # no new done tasks
        )
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSION_DIR": str(mission_dir),
            "ARIA_TASK_DIR": str(task_state_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                sup.check_missions()
        adapt_calls = [c for c in mock_run.call_args_list if "adapt" in str(c)]
        assert len(adapt_calls) == 0, f"Expected no adapt call at stall_count=2, got: {adapt_calls}"

    def test_progress_resets_stall(self, mission_dir, task_state_dir, tmp_path):
        """stall_count=2 + new DONE tasks found → stall_count reset to 0, adapt NOT called."""
        mission_id = "m_progress_reset_001"
        meta_path = self._setup_mission(
            mission_dir, task_state_dir, mission_id,
            stall_count=2, last_done_count=1, done_task_count=3  # 3 > 1, so progress
        )
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSION_DIR": str(mission_dir),
            "ARIA_TASK_DIR": str(task_state_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                sup.check_missions()
        # stall_count should be 0 in meta
        meta = json.loads(meta_path.read_text())
        assert meta["stall_count"] == 0, f"Expected stall_count=0 after progress, got {meta['stall_count']}"
        # adapt should NOT be called
        adapt_calls = [c for c in mock_run.call_args_list if "adapt" in str(c)]
        assert len(adapt_calls) == 0, f"Expected no adapt call on progress, got: {adapt_calls}"

    def test_adapt_audit_record(self, mission_dir, task_state_dir, tmp_path):
        """When adapt is triggered, audit record with op='trigger_adapt' is written."""
        mission_id = "m_adapt_audit_001"
        self._setup_mission(
            mission_dir, task_state_dir, mission_id,
            stall_count=2, last_done_count=1, done_task_count=1  # triggers adapt
        )
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSION_DIR": str(mission_dir),
            "ARIA_TASK_DIR": str(task_state_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                sup.check_missions()
        # Audit should contain trigger_adapt record
        lines = audit_log.read_text().strip().splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        ops = [r["op"] for r in records]
        assert "trigger_adapt" in ops, f"Expected 'trigger_adapt' in audit ops, got: {ops}"


# ---------------------------------------------------------------------------
# TestOuterLoopReplan — SUPV-06
# ---------------------------------------------------------------------------

class TestOuterLoopReplan:
    """Tests for outer-loop replan markers and stall reset after adapt (SUPV-06)."""

    def test_outer_loop_flags_stalled_mission(self, mission_dir, task_state_dir, tmp_path):
        """check-missions output includes '[ADAPT]' for missions at stall threshold."""
        mission_id = "m_adapt_flag_001"
        write_ledger(mission_dir, [
            {"id": mission_id, "goal": "Build email list", "status": "ACTIVE", "priority": 2}
        ])
        meta_dir = mission_dir / "meta"
        meta_dir.mkdir()
        (meta_dir / f"mission_{mission_id}_meta.json").write_text(
            json.dumps({"last_done_count": 2, "stall_count": 2})
        )
        # 2 done tasks, same as last_done_count → no progress → stall_count becomes 3
        write_task(task_state_dir, "task_flag_a", mission_id=mission_id, status="DONE")
        write_task(task_state_dir, "task_flag_b", mission_id=mission_id, status="DONE")

        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        # Run via subprocess so we can capture stdout (check-missions is sync/file-based)
        result = run_supervisor(
            ["check-missions"],
            env={
                "MISSION_DIR": str(mission_dir),
                "ARIA_TASK_DIR": str(task_state_dir),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
                "SUPERVISOR_EXEC_DIR": str(exec_dir),
            },
        )
        assert result.returncode == 0
        assert "[ADAPT]" in result.stdout, f"Expected '[ADAPT]' in output: {result.stdout!r}"

    def test_stall_resets_after_adapt(self, mission_dir, task_state_dir, tmp_path):
        """After _trigger_adapt fires, stall_count in meta sidecar is reset to 0."""
        mission_id = "m_stall_reset_001"
        write_ledger(mission_dir, [
            {"id": mission_id, "goal": "Publish 10 articles", "status": "ACTIVE", "priority": 2}
        ])
        meta_dir = mission_dir / "meta"
        meta_dir.mkdir()
        meta_path = meta_dir / f"mission_{mission_id}_meta.json"
        meta_path.write_text(json.dumps({"last_done_count": 1, "stall_count": 2}))
        write_task(task_state_dir, "task_reset_a", mission_id=mission_id, status="DONE")
        # 1 done task, same as last_done_count → no progress → stall_count becomes 3 → adapt fires

        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSION_DIR": str(mission_dir),
            "ARIA_TASK_DIR": str(task_state_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                sup.check_missions()
        # After adapt, stall_count should be reset to 0
        meta = json.loads(meta_path.read_text())
        assert meta["stall_count"] == 0, (
            f"Expected stall_count=0 after adapt reset, got {meta['stall_count']}"
        )


# ---------------------------------------------------------------------------
# TestAgentAssignment — SUPV-05
# ---------------------------------------------------------------------------

MANAGE_PATH = "/home/alex/.openclaw/workspace/agents/manage.py"


class TestAgentAssignment:
    """Tests for assign_task() in supervisor.py routing tasks to sub-agents (SUPV-05)."""

    def test_assign_task_calls_route(self, tmp_path):
        """assign_task() calls manage.py route with the task description."""
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            # Mock subprocess.run: route returns agent slug, spawn succeeds
            def fake_run(cmd, *args, **kwargs):
                mock = MagicMock()
                mock.returncode = 0
                if "route" in cmd:
                    mock.stdout = "  agent: contentagent\n"
                else:
                    mock.stdout = "SPAWN_READY:\n  agent: contentagent\n"
                return mock

            with patch("subprocess.run", side_effect=fake_run) as mock_run:
                sup.assign_task("write an article about SEO", mission_id="m_test_001")

        route_calls = [c for c in mock_run.call_args_list if "route" in str(c)]
        assert len(route_calls) >= 1, f"Expected route call, got: {mock_run.call_args_list}"
        route_cmd = route_calls[0][0][0]
        assert "route" in route_cmd
        assert "write an article about SEO" in route_cmd

    def test_assign_task_calls_spawn(self, tmp_path):
        """assign_task() calls manage.py spawn with --task and --mission-id after routing."""
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()

            def fake_run(cmd, *args, **kwargs):
                mock = MagicMock()
                mock.returncode = 0
                if "route" in cmd:
                    mock.stdout = "  agent: seoagent\n"
                else:
                    mock.stdout = "SPAWN_READY:\n  agent: seoagent\n"
                return mock

            with patch("subprocess.run", side_effect=fake_run) as mock_run:
                sup.assign_task("optimize site SEO", mission_id="m_seo_001")

        spawn_calls = [c for c in mock_run.call_args_list if "spawn" in str(c)]
        assert len(spawn_calls) >= 1, f"Expected spawn call, got: {mock_run.call_args_list}"
        spawn_cmd = spawn_calls[0][0][0]
        assert "spawn" in spawn_cmd
        assert "--task" in spawn_cmd
        assert "--mission-id" in spawn_cmd
        assert "m_seo_001" in spawn_cmd

    def test_assign_task_audit(self, tmp_path):
        """assign_task() writes audit record with op='assign_task', agent slug, mission_id."""
        audit_log = tmp_path / "supervisor.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()

            def fake_run(cmd, *args, **kwargs):
                mock = MagicMock()
                mock.returncode = 0
                if "route" in cmd:
                    mock.stdout = "  agent: researchagent\n"
                else:
                    mock.stdout = "SPAWN_READY:\n"
                return mock

            with patch("subprocess.run", side_effect=fake_run):
                sup.assign_task("research competitor backlinks", mission_id="m_res_001")

        lines = audit_log.read_text().strip().splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        assign_records = [r for r in records if r.get("op") == "assign_task"]
        assert len(assign_records) >= 1, f"Expected assign_task audit record, got ops: {[r['op'] for r in records]}"
        rec = assign_records[0]["data"]
        assert "agent" in rec
        assert "mission_id" in rec
        assert rec["mission_id"] == "m_res_001"

    def test_manage_spawn_accepts_mission_id(self, tmp_path):
        """manage.py spawn <slug> --task <desc> --mission-id <id> does not error."""
        result = subprocess.run(
            [sys.executable, MANAGE_PATH, "spawn", "contentagent",
             "--task", "write test article",
             "--mission-id", "m_test_abc123"],
            capture_output=True,
            text=True,
        )
        # Should not error with 'unrecognized arguments'
        assert "unrecognized arguments" not in result.stderr, (
            f"manage.py spawn does not accept --mission-id: {result.stderr}"
        )
        assert "error" not in result.stderr.lower() or result.returncode == 0 or "not found" in result.stdout.lower(), (
            f"Unexpected error: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestManageMissionId — manage.py backward compat
# ---------------------------------------------------------------------------

class TestManageMissionId:
    """Tests that manage.py spawn --mission-id is backward compatible (optional)."""

    def test_spawn_without_mission_id(self):
        """manage.py spawn still works without --mission-id (backward compatible)."""
        result = subprocess.run(
            [sys.executable, MANAGE_PATH, "spawn", "contentagent",
             "--task", "write article without mission"],
            capture_output=True,
            text=True,
        )
        # Should not error on missing optional --mission-id
        assert "unrecognized arguments" not in result.stderr
        assert "--mission-id" not in result.stderr

    def test_spawn_with_mission_id(self):
        """manage.py spawn --mission-id includes mission_id in the task record."""
        mission_id = "m_bc_test_xyz"
        result = subprocess.run(
            [sys.executable, MANAGE_PATH, "spawn", "contentagent",
             "--task", "write article with mission",
             "--mission-id", mission_id],
            capture_output=True,
            text=True,
        )
        # Should not crash. If the agent exists, output includes SPAWN_READY.
        # If not found, that's OK too — we just need --mission-id to be accepted.
        assert "unrecognized arguments" not in result.stderr, (
            f"--mission-id not accepted: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestPreExecHooks — SUPV-08
#
# Uses a mix of direct module import (for function-level tests) and subprocess
# (for CLI exit-code tests). SUPERVISOR_BLOCKLIST_PATH env var isolates the
# blocklist.json path; SUPERVISOR_AUDIT_LOG isolates audit output.
# ---------------------------------------------------------------------------


def _write_blocklist(path, patterns):
    """Write a blocklist.json to path with the given patterns list."""
    import json
    blocklist = {"version": 1, "patterns": patterns}
    path.write_text(json.dumps(blocklist))


def _import_supervisor_fresh():
    """Import (or reload) supervisor module so module-level env vars re-read from environ."""
    import importlib
    import supervisor as sup_module
    importlib.reload(sup_module)
    return sup_module


class TestPreExecHooks:
    """Tests for pre-check subcommand and pre_check() function (SUPV-08)."""

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _run_pre_check(self, action, blocklist_path, audit_log, dry_run=False):
        """Run supervisor.py pre-check via subprocess."""
        args = ["pre-check", "--action", action]
        if dry_run:
            args.append("--dry-run")
        return run_supervisor(
            args,
            env={
                "SUPERVISOR_BLOCKLIST_PATH": str(blocklist_path),
                "SUPERVISOR_AUDIT_LOG": str(audit_log),
            },
        )

    def _call_pre_check(self, action, blocklist_path, audit_log, dry_run=False):
        """Call pre_check() function directly with env isolation. Returns (allowed, reason)."""
        env_patch = {
            "SUPERVISOR_BLOCKLIST_PATH": str(blocklist_path),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
        }
        old_env = {}
        for k, v in env_patch.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            sup = _import_supervisor_fresh()
            return sup.pre_check(action, dry_run=dry_run)
        finally:
            for k, old_v in old_env.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

    def _make_standard_blocklist(self, tmp_path):
        """Create a standard blocklist.json in tmp_path with 4 initial patterns."""
        bl_path = tmp_path / "blocklist.json"
        _write_blocklist(bl_path, [
            {"pattern": "delete.*all", "reason": "mass deletion blocked", "severity": "critical"},
            {"pattern": "email.*blast", "reason": "bulk email blocked without review", "severity": "critical"},
            {"pattern": "payment|purchase|buy|charge", "reason": "financial action requires Alex approval", "severity": "critical"},
            {"pattern": "email.*[5-9][0-9][0-9]|email.*[0-9]{4,}", "reason": "email blast >500 blocked", "severity": "high"},
        ])
        return bl_path

    # -----------------------------------------------------------------------
    # Function-level tests (direct import)
    # -----------------------------------------------------------------------

    def test_pre_check_allows_safe_action(self, tmp_path):
        """Safe action returns (True, '') — not matched by any blocklist pattern."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("write article about SEO tips", bl, audit_log)
        assert allowed is True
        assert reason == ""

    def test_pre_check_blocks_mass_delete(self, tmp_path):
        """'delete all user data from database' matches delete.*all pattern."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("delete all user data from database", bl, audit_log)
        assert allowed is False
        assert reason == "mass deletion blocked"

    def test_pre_check_blocks_payment(self, tmp_path):
        """'purchase domain example.com' matches payment pattern."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("purchase domain example.com", bl, audit_log)
        assert allowed is False
        assert reason == "financial action requires Alex approval"

    def test_pre_check_blocks_email_blast(self, tmp_path):
        """'email blast 1000 subscribers' matches email.*blast or email.*[0-9]{4,} pattern."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("email blast 1000 subscribers", bl, audit_log)
        assert allowed is False
        assert reason in ("bulk email blocked without review", "email blast >500 blocked")

    def test_pre_check_blocks_bulk_email(self, tmp_path):
        """'send email to 750 contacts' matches email.*[5-9][0-9][0-9] pattern."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("send email to 750 contacts", bl, audit_log)
        assert allowed is False
        assert reason == "email blast >500 blocked"

    def test_pre_check_case_insensitive(self, tmp_path):
        """'DELETE ALL files' is blocked — matching is case-insensitive."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("DELETE ALL files", bl, audit_log)
        assert allowed is False
        assert reason == "mass deletion blocked"

    def test_pre_check_audit_allowed(self, tmp_path):
        """Allowed action produces audit record with result='ALLOWED'."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        self._call_pre_check("write a blog post about Python", bl, audit_log)
        assert audit_log.exists(), "audit log should be created"
        lines = [l for l in audit_log.read_text().strip().splitlines() if l.strip()]
        records = [json.loads(l) for l in lines]
        pre_check_records = [r for r in records if r.get("op") == "pre_check"]
        assert len(pre_check_records) >= 1
        assert any(r["data"].get("result") == "ALLOWED" for r in pre_check_records)

    def test_pre_check_audit_blocked(self, tmp_path):
        """Blocked action produces audit record with result='BLOCKED' and reason."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        self._call_pre_check("delete all production records", bl, audit_log)
        lines = [l for l in audit_log.read_text().strip().splitlines() if l.strip()]
        records = [json.loads(l) for l in lines]
        blocked_records = [
            r for r in records
            if r.get("op") == "pre_check" and r["data"].get("result") == "BLOCKED"
        ]
        assert len(blocked_records) >= 1
        assert blocked_records[0]["data"].get("reason") == "mass deletion blocked"

    def test_pre_check_empty_blocklist(self, tmp_path):
        """Empty patterns list allows all actions."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist(bl, [])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("delete all user data", bl, audit_log)
        assert allowed is True
        assert reason == ""

    def test_pre_check_dry_run(self, tmp_path):
        """dry_run=True shows matched patterns but returns (True, '') — does not block."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("delete all records", bl, audit_log, dry_run=True)
        assert allowed is True, "dry-run should not block even if pattern matches"
        assert reason == ""

    def test_blocklist_file_missing(self, tmp_path):
        """Missing blocklist.json allows all actions (fail-open) and writes warning to audit."""
        non_existent = tmp_path / "does_not_exist.json"
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check("delete all data", non_existent, audit_log)
        assert allowed is True
        assert audit_log.exists(), "audit log should be created even on missing blocklist"

    def test_blocklist_file_structure(self):
        """blocklist.json has version int and patterns array with pattern/reason/severity per entry."""
        bl_path = Path("/home/alex/.openclaw/workspace/memory/guardrails/blocklist.json")
        assert bl_path.exists(), f"blocklist.json not found at {bl_path}"
        data = json.loads(bl_path.read_text())
        assert "version" in data, "blocklist.json missing 'version' key"
        assert isinstance(data["version"], int)
        assert "patterns" in data, "blocklist.json missing 'patterns' key"
        assert isinstance(data["patterns"], list)
        assert len(data["patterns"]) >= 4, f"Expected at least 4 patterns, got {len(data['patterns'])}"
        for entry in data["patterns"]:
            assert "pattern" in entry, f"Entry missing 'pattern': {entry}"
            assert "reason" in entry, f"Entry missing 'reason': {entry}"
            assert "severity" in entry, f"Entry missing 'severity': {entry}"

    # -----------------------------------------------------------------------
    # CLI exit-code tests (subprocess)
    # -----------------------------------------------------------------------

    def test_pre_check_cli_allows_safe_action(self, tmp_path):
        """CLI: safe action exits 0 and prints ALLOWED."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        result = self._run_pre_check("write an article about Python", bl, audit_log)
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "ALLOWED" in result.stdout

    def test_pre_check_cli_blocks_mass_delete(self, tmp_path):
        """CLI: 'delete all user data' exits 1 and prints BLOCKED."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        result = self._run_pre_check("delete all user data", bl, audit_log)
        assert result.returncode == 1, f"Expected exit 1, got {result.returncode}"
        assert "BLOCKED" in result.stdout

    def test_pre_check_cli_dry_run_exits_0(self, tmp_path):
        """CLI: --dry-run on a blocked action exits 0 (does not block)."""
        bl = self._make_standard_blocklist(tmp_path)
        audit_log = tmp_path / "supervisor.jsonl"
        result = self._run_pre_check("delete all records", bl, audit_log, dry_run=True)
        assert result.returncode == 0, (
            f"Expected exit 0 for dry-run, got {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# TestAssignTaskPreCheck — AUTO-02
# ---------------------------------------------------------------------------


def _write_blocklist_v2(path, patterns, safe_actions=None):
    """Write a v2 blocklist.json to path."""
    blocklist = {
        "version": 2,
        "safe_actions": safe_actions or [],
        "patterns": patterns,
    }
    path.write_text(json.dumps(blocklist))


class TestAssignTaskPreCheck:
    """Tests that assign_task() calls pre_check() before dispatching (AUTO-02)."""

    def _call_assign_task(self, task_desc, tmp_path, blocklist_path=None, mission_id=None):
        """Call assign_task() directly with env isolation."""
        audit_log = tmp_path / "supervisor.jsonl"
        actions_log = tmp_path / "actions.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir(exist_ok=True)
        env_patch = {
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        if blocklist_path:
            env_patch["SUPERVISOR_BLOCKLIST_PATH"] = str(blocklist_path)
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                def fake_run(cmd, *args, **kwargs):
                    m = MagicMock()
                    m.returncode = 0
                    if "route" in str(cmd):
                        m.stdout = "  agent: contentagent\n"
                    else:
                        m.stdout = "SPAWN_READY:\n"
                    return m
                mock_run.side_effect = fake_run
                result = sup.assign_task(task_desc, mission_id=mission_id)
        return result, mock_run

    def test_assign_task_blocked_purchase(self, tmp_path):
        """assign_task('purchase a domain') returns 'BLOCKED' and does not call manage.py spawn."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "payment|purchase|buy|charge",
             "reason": "financial action requires Alex approval",
             "severity": "critical"},
        ])
        result, mock_run = self._call_assign_task("purchase a domain", tmp_path, blocklist_path=bl)
        assert result == "BLOCKED", f"Expected BLOCKED, got {result!r}"
        spawn_calls = [c for c in mock_run.call_args_list if "spawn" in str(c)]
        assert len(spawn_calls) == 0, f"Blocked task should not reach spawn; spawn_calls={spawn_calls}"

    def test_assign_task_blocked_audit_record(self, tmp_path):
        """Blocked assign_task writes audit record with op='assign_task_blocked'."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "delete.*all", "reason": "mass deletion blocked", "severity": "critical"},
        ])
        audit_log = tmp_path / "supervisor.jsonl"
        actions_log = tmp_path / "actions.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir(exist_ok=True)
        env_patch = {
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_BLOCKLIST_PATH": str(bl),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run"):
                sup.assign_task("delete all user records")
        records = [json.loads(l) for l in audit_log.read_text().strip().splitlines() if l.strip()]
        blocked_recs = [r for r in records if r.get("op") == "assign_task_blocked"]
        assert len(blocked_recs) >= 1, f"Expected assign_task_blocked audit record. ops={[r['op'] for r in records]}"

    def test_assign_task_allowed_proceeds(self, tmp_path):
        """assign_task('write SEO article') proceeds to route+spawn."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "delete.*all", "reason": "mass deletion blocked", "severity": "critical"},
        ])
        result, mock_run = self._call_assign_task("write SEO article", tmp_path, blocklist_path=bl)
        assert result != "BLOCKED", f"Safe task should not be blocked"
        spawn_calls = [c for c in mock_run.call_args_list if "spawn" in str(c)]
        assert len(spawn_calls) >= 1, f"Safe task should reach spawn; calls={mock_run.call_args_list}"


# ---------------------------------------------------------------------------
# TestContextAwareRisk — AUTO-08
# ---------------------------------------------------------------------------


class TestContextAwareRisk:
    """Tests context-aware risk scoring via context_rules in blocklist (AUTO-08)."""

    def _write_context_blocklist(self, path):
        """Write a blocklist with context_rules on post.*reddit."""
        blocklist = {
            "version": 2,
            "safe_actions": [],
            "patterns": [
                {
                    "pattern": "post.*reddit|submit.*reddit",
                    "reason": "Reddit activity — check account context",
                    "severity": "medium",
                    "context_rules": [
                        {"if_context_contains": "test_account|warmup",
                         "override_severity": "low"},
                        {"if_context_contains": "main_account|primary",
                         "override_severity": "high"},
                    ],
                }
            ],
        }
        path.write_text(json.dumps(blocklist))

    def _call_pre_check_ctx(self, action, blocklist_path, audit_log, context=None):
        """Call pre_check() with context param via env isolation."""
        env_patch = {
            "SUPERVISOR_BLOCKLIST_PATH": str(blocklist_path),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
        }
        old_env = {}
        for k, v in env_patch.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            sup = _import_supervisor_fresh()
            return sup.pre_check(action, context=context)
        finally:
            for k, old_v in old_env.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

    def test_context_override_low_allows(self, tmp_path):
        """context={'account_type': 'test_account'} overrides medium->low, returns True."""
        bl = tmp_path / "blocklist.json"
        self._write_context_blocklist(bl)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_ctx(
            "post to reddit", bl, audit_log,
            context={"account_type": "test_account"}
        )
        assert allowed is True, f"Expected allowed=True for test_account context, got {allowed!r}, reason={reason!r}"

    def test_context_override_high_blocks(self, tmp_path):
        """context={'account_type': 'main_account'} overrides medium->high, returns False."""
        bl = tmp_path / "blocklist.json"
        self._write_context_blocklist(bl)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_ctx(
            "post to reddit", bl, audit_log,
            context={"account_type": "main_account"}
        )
        assert allowed is False, f"Expected allowed=False for main_account context, got {allowed!r}"
        assert reason, f"Expected non-empty reason for blocked action"

    def test_context_none_uses_default_severity(self, tmp_path):
        """No context (None) uses pattern's default severity (medium=log+proceed)."""
        bl = tmp_path / "blocklist.json"
        self._write_context_blocklist(bl)
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_ctx("post to reddit", bl, audit_log, context=None)
        # medium severity = log+proceed = allowed
        assert allowed is True, f"Expected medium to allow with no context, got {allowed!r}"


# ---------------------------------------------------------------------------
# TestSafeActions — AUTO-06
# ---------------------------------------------------------------------------


class TestSafeActions:
    """Tests that safe_actions list bypasses pattern matching entirely (AUTO-06)."""

    def _call_pre_check_safe(self, action, blocklist_path, audit_log):
        env_patch = {
            "SUPERVISOR_BLOCKLIST_PATH": str(blocklist_path),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
        }
        old_env = {}
        for k, v in env_patch.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            sup = _import_supervisor_fresh()
            return sup.pre_check(action)
        finally:
            for k, old_v in old_env.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

    def test_write_file_safe_action_bypasses_block(self, tmp_path):
        """'write file to /tmp/test.txt' matches safe_actions, bypasses all patterns."""
        bl = tmp_path / "blocklist.json"
        # Even with a pattern that would match, safe_actions should short-circuit
        _write_blocklist_v2(bl, [
            {"pattern": "write.*file", "reason": "should not reach this", "severity": "critical"},
        ], safe_actions=["write.*file|create.*file|save.*file"])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_safe("write file to /tmp/test.txt", bl, audit_log)
        assert allowed is True, f"Expected safe_action to bypass block, got {allowed!r}, reason={reason!r}"

    def test_run_script_safe_action(self, tmp_path):
        """'run script check_links.py' matches run.*script safe_action."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "delete.*all", "reason": "mass deletion blocked", "severity": "critical"},
        ], safe_actions=["run.*script|execute.*script|python3"])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_safe("run script check_links.py", bl, audit_log)
        assert allowed is True, f"Expected run script to be a safe action"

    def test_safe_action_audit_record(self, tmp_path):
        """Safe action match writes audit record with result='SAFE_ACTION'."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [], safe_actions=["write.*file"])
        audit_log = tmp_path / "supervisor.jsonl"
        self._call_pre_check_safe("write file to /tmp/output.txt", bl, audit_log)
        records = [json.loads(l) for l in audit_log.read_text().strip().splitlines() if l.strip()]
        safe_recs = [r for r in records if r.get("op") == "pre_check"
                     and r.get("data", {}).get("result") == "SAFE_ACTION"]
        assert len(safe_recs) >= 1, f"Expected SAFE_ACTION audit record. records={records}"

    def test_no_safe_actions_does_not_bypass(self, tmp_path):
        """Empty safe_actions list does not bypass pattern matching."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "delete.*all", "reason": "mass deletion blocked", "severity": "critical"},
        ], safe_actions=[])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_safe("delete all records", bl, audit_log)
        assert allowed is False, f"Expected pattern block without safe_actions bypass"


# ---------------------------------------------------------------------------
# TestSeverityMapping — AUTO-03
# ---------------------------------------------------------------------------


class TestSeverityMapping:
    """Tests that critical/high block, medium logs+proceeds, low allows."""

    def _call_pre_check_sev(self, action, blocklist_path, audit_log):
        env_patch = {
            "SUPERVISOR_BLOCKLIST_PATH": str(blocklist_path),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
        }
        old_env = {}
        for k, v in env_patch.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            sup = _import_supervisor_fresh()
            return sup.pre_check(action)
        finally:
            for k, old_v in old_env.items():
                if old_v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old_v

    def test_critical_blocks(self, tmp_path):
        """severity='critical' blocks the action."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "dangerous.*action", "reason": "critical block", "severity": "critical"},
        ])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_sev("do dangerous action", bl, audit_log)
        assert allowed is False

    def test_high_blocks(self, tmp_path):
        """severity='high' blocks the action."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "risky.*action", "reason": "high block", "severity": "high"},
        ])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_sev("do risky action", bl, audit_log)
        assert allowed is False

    def test_medium_log_proceed(self, tmp_path):
        """severity='medium' allows the action but writes MEDIUM_PROCEED audit record."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "medium.*action", "reason": "medium log", "severity": "medium"},
        ])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_sev("do medium action", bl, audit_log)
        assert allowed is True, f"Expected medium to allow, got {allowed!r}"
        records = [json.loads(l) for l in audit_log.read_text().strip().splitlines() if l.strip()]
        medium_recs = [r for r in records if r.get("data", {}).get("result") == "MEDIUM_PROCEED"]
        assert len(medium_recs) >= 1, f"Expected MEDIUM_PROCEED audit record. records={records}"

    def test_low_allows_silently(self, tmp_path):
        """severity='low' allows the action silently (no BLOCKED or MEDIUM_PROCEED)."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [
            {"pattern": "low.*action", "reason": "low allow", "severity": "low"},
        ])
        audit_log = tmp_path / "supervisor.jsonl"
        allowed, reason = self._call_pre_check_sev("do low action", bl, audit_log)
        assert allowed is True, f"Expected low to allow, got {allowed!r}"
        if audit_log.exists():
            records = [json.loads(l) for l in audit_log.read_text().strip().splitlines() if l.strip()]
            blocked = [r for r in records if r.get("data", {}).get("result") == "BLOCKED"]
            assert len(blocked) == 0, f"Low severity should not produce BLOCKED records"


# ---------------------------------------------------------------------------
# TestAutonomyTierEnforcement — AUTO-04
# ---------------------------------------------------------------------------


class TestAutonomyTierEnforcement:
    """Tests tier 1/2/3 enforcement in assign_task() (AUTO-04)."""

    def _write_mission(self, missions_dir, mission_id, autonomy_tier=1):
        """Write a mission JSON file with given autonomy_tier."""
        missions_dir.mkdir(parents=True, exist_ok=True)
        mission = {
            "id": mission_id,
            "goal": "Test mission",
            "status": "ACTIVE",
            "autonomy_tier": autonomy_tier,
        }
        (missions_dir / f"mission_{mission_id}.json").write_text(json.dumps(mission))

    def test_tier3_returns_tier3_alert(self, tmp_path):
        """assign_task with tier-3 mission returns 'TIER3_ALERT' and does not spawn."""
        missions_dir = tmp_path / "missions"
        self._write_mission(missions_dir, "M-tier3test", autonomy_tier=3)
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [])  # No blocking patterns
        audit_log = tmp_path / "supervisor.jsonl"
        actions_log = tmp_path / "actions.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSIONS_DIR": str(missions_dir),
            "MISSION_DIR": str(missions_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_BLOCKLIST_PATH": str(bl),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="  agent: contentagent\n")
                result = sup.assign_task("write article", mission_id="M-tier3test")
        assert result == "TIER3_ALERT", f"Expected TIER3_ALERT, got {result!r}"
        # Verify spawn was not called
        spawn_calls = [c for c in mock_run.call_args_list if "spawn" in str(c)]
        assert len(spawn_calls) == 0, f"Tier-3 should not spawn; calls={mock_run.call_args_list}"

    def test_tier3_writes_audit_record(self, tmp_path):
        """Tier-3 assign_task writes audit record with op='tier3_alert'."""
        missions_dir = tmp_path / "missions"
        self._write_mission(missions_dir, "M-tier3audit", autonomy_tier=3)
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [])
        audit_log = tmp_path / "supervisor.jsonl"
        actions_log = tmp_path / "actions.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSIONS_DIR": str(missions_dir),
            "MISSION_DIR": str(missions_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_BLOCKLIST_PATH": str(bl),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="")
                sup.assign_task("write article", mission_id="M-tier3audit")
        records = [json.loads(l) for l in audit_log.read_text().strip().splitlines() if l.strip()]
        tier3_recs = [r for r in records if r.get("op") == "tier3_alert"]
        assert len(tier3_recs) >= 1, f"Expected tier3_alert audit record. ops={[r['op'] for r in records]}"

    def test_tier2_proceeds_with_extra_audit(self, tmp_path):
        """Tier-2 assign_task spawns normally but writes 'tier2_dispatch' audit record."""
        missions_dir = tmp_path / "missions"
        self._write_mission(missions_dir, "M-tier2test", autonomy_tier=2)
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [])
        audit_log = tmp_path / "supervisor.jsonl"
        actions_log = tmp_path / "actions.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSIONS_DIR": str(missions_dir),
            "MISSION_DIR": str(missions_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_BLOCKLIST_PATH": str(bl),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                def fake_run(cmd, *args, **kwargs):
                    m = MagicMock()
                    m.returncode = 0
                    if "route" in str(cmd):
                        m.stdout = "  agent: contentagent\n"
                    else:
                        m.stdout = "SPAWN_READY:\n"
                    return m
                mock_run.side_effect = fake_run
                result = sup.assign_task("write article", mission_id="M-tier2test")
        assert result != "TIER3_ALERT", f"Tier-2 should not return TIER3_ALERT"
        assert result != "BLOCKED", f"Tier-2 should not be blocked"
        records = [json.loads(l) for l in audit_log.read_text().strip().splitlines() if l.strip()]
        tier2_recs = [r for r in records if r.get("op") == "tier2_dispatch"]
        assert len(tier2_recs) >= 1, f"Expected tier2_dispatch record. ops={[r['op'] for r in records]}"

    def test_tier1_proceeds_normally(self, tmp_path):
        """Tier-1 assign_task spawns normally without extra tier audit records."""
        missions_dir = tmp_path / "missions"
        self._write_mission(missions_dir, "M-tier1test", autonomy_tier=1)
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [])
        audit_log = tmp_path / "supervisor.jsonl"
        actions_log = tmp_path / "actions.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "MISSIONS_DIR": str(missions_dir),
            "MISSION_DIR": str(missions_dir),
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_BLOCKLIST_PATH": str(bl),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                def fake_run(cmd, *args, **kwargs):
                    m = MagicMock()
                    m.returncode = 0
                    if "route" in str(cmd):
                        m.stdout = "  agent: contentagent\n"
                    else:
                        m.stdout = "SPAWN_READY:\n"
                    return m
                mock_run.side_effect = fake_run
                result = sup.assign_task("write article", mission_id="M-tier1test")
        assert result != "TIER3_ALERT"
        assert result != "BLOCKED"
        spawn_calls = [c for c in mock_run.call_args_list if "spawn" in str(c)]
        assert len(spawn_calls) >= 1, f"Tier-1 should spawn normally; calls={mock_run.call_args_list}"

    def test_no_mission_id_skips_tier_check(self, tmp_path):
        """assign_task with no mission_id proceeds without tier enforcement."""
        bl = tmp_path / "blocklist.json"
        _write_blocklist_v2(bl, [])
        audit_log = tmp_path / "supervisor.jsonl"
        actions_log = tmp_path / "actions.jsonl"
        exec_dir = tmp_path / "execution"
        exec_dir.mkdir()
        env_patch = {
            "SUPERVISOR_AUDIT_LOG": str(audit_log),
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_BLOCKLIST_PATH": str(bl),
            "SUPERVISOR_EXEC_DIR": str(exec_dir),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            with patch("subprocess.run") as mock_run:
                def fake_run(cmd, *args, **kwargs):
                    m = MagicMock()
                    m.returncode = 0
                    if "route" in str(cmd):
                        m.stdout = "  agent: contentagent\n"
                    else:
                        m.stdout = "SPAWN_READY:\n"
                    return m
                mock_run.side_effect = fake_run
                result = sup.assign_task("write article")  # no mission_id
        assert result != "TIER3_ALERT"
        assert result != "BLOCKED"
        spawn_calls = [c for c in mock_run.call_args_list if "spawn" in str(c)]
        assert len(spawn_calls) >= 1, f"No mission_id should spawn normally"


# ---------------------------------------------------------------------------
# TestLogAction — AUTO-05
# ---------------------------------------------------------------------------


class TestLogAction:
    """Tests that log_action() writes to actions.jsonl (AUTO-05)."""

    def test_log_action_writes_record(self, tmp_path):
        """log_action() appends a JSONL record with ts, op, data fields."""
        actions_log = tmp_path / "actions.jsonl"
        env_patch = {
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_AUDIT_LOG": str(tmp_path / "supervisor.jsonl"),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            sup.log_action("assign_task", {"task": "test task", "agent": "seoagent"})
        assert actions_log.exists(), "actions.jsonl should be created by log_action()"
        records = [json.loads(l) for l in actions_log.read_text().strip().splitlines() if l.strip()]
        assert len(records) == 1, f"Expected 1 record, got {len(records)}"
        rec = records[0]
        assert "ts" in rec, f"Record missing 'ts' field: {rec}"
        assert rec["op"] == "assign_task", f"Expected op='assign_task', got {rec['op']!r}"
        assert rec["data"]["task"] == "test task"
        assert rec["data"]["agent"] == "seoagent"

    def test_log_action_appends_multiple(self, tmp_path):
        """Multiple log_action() calls append multiple records."""
        actions_log = tmp_path / "actions.jsonl"
        env_patch = {
            "ACTIONS_AUDIT_LOG": str(actions_log),
            "SUPERVISOR_AUDIT_LOG": str(tmp_path / "supervisor.jsonl"),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            sup.log_action("op1", {"x": 1})
            sup.log_action("op2", {"x": 2})
            sup.log_action("op3", {"x": 3})
        records = [json.loads(l) for l in actions_log.read_text().strip().splitlines() if l.strip()]
        assert len(records) == 3
        assert [r["op"] for r in records] == ["op1", "op2", "op3"]

    def test_log_action_env_var_override(self, tmp_path):
        """ACTIONS_AUDIT_LOG env var overrides default path."""
        custom_log = tmp_path / "custom_actions.jsonl"
        env_patch = {
            "ACTIONS_AUDIT_LOG": str(custom_log),
            "SUPERVISOR_AUDIT_LOG": str(tmp_path / "supervisor.jsonl"),
        }
        with _env_context(env_patch):
            sup = _import_supervisor()
            sup.log_action("test_op", {"info": "custom path test"})
        assert custom_log.exists(), f"Custom log path should be used: {custom_log}"
        default_log = Path("/home/alex/.openclaw/workspace/memory/audit/actions.jsonl")
        # Only check default isn't written if it doesn't already exist from prod
        records = [json.loads(l) for l in custom_log.read_text().strip().splitlines() if l.strip()]
        assert len(records) == 1
        assert records[0]["op"] == "test_op"
