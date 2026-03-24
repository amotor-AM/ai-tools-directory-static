"""TDD test suite for supervisor.py — Phase 4 Plan 01.

Covers:
  SUPV-01: Mission Ledger (outer loop) — check-missions
  SUPV-02: Execution Ledger (inner loop) — validate-task
  SUPV-09: Audit log — _audit(), audit-log subcommand

Requirement coverage:
  TestMissionLedger   → SUPV-01
  TestExecutionLedger → SUPV-02
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
