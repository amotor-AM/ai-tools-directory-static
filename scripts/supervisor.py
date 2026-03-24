#!/usr/bin/env python3
"""Aria Dual-Loop Supervisor — Mission health tracking and task validation.

Outer loop (check-missions, SUPV-01):
  Reads missions/ledger.json, counts DONE tasks per mission, updates stall_count
  in meta sidecar. Called once per heartbeat before task selection.

Inner loop (validate-task, SUPV-02):
  Validates sub-agent result JSON against OUTPUT_SCHEMAS, writes exec_ ledger entry,
  gates task completion (task_manager.py complete) or triggers healing (heal.py attempt).

Audit log (audit-log, SUPV-09):
  Append-only JSONL decision trail for every supervisor operation.

Usage:
  supervisor.py check-missions
  supervisor.py validate-task --task-id <id> --result '<json>'
  supervisor.py audit-log [--tail N]

Environment overrides (for testing):
  SUPERVISOR_AUDIT_LOG  — override AUDIT_LOG_PATH
  SUPERVISOR_EXEC_DIR   — override EXEC_DIR
  MISSION_DIR           — override missions directory (reuses mission_engine pattern)
  ARIA_TASK_DIR         — override task state directory (reuses task_manager pattern)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add scripts/ dir to path so output_schema.py can be imported (leaf-node dependency)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from output_schema import OUTPUT_SCHEMAS, QualityGateResult, TaskCompleteOutput  # noqa: E402

# ---------------------------------------------------------------------------
# Paths — all override-able for test isolation
# ---------------------------------------------------------------------------

# Outer loop — missions
MISSIONS_DIR = Path(os.environ.get(
    "MISSION_DIR",
    "/home/alex/.openclaw/workspace/memory/missions"
))

# Inner loop — task state (written by task_manager.py)
TASK_STATE_DIR = Path(os.environ.get(
    "ARIA_TASK_DIR",
    "/home/alex/.openclaw/workspace/memory/tasks/state"
))

# Execution ledger directory — exec_<task_id>.json files
EXEC_DIR = Path(os.environ.get(
    "SUPERVISOR_EXEC_DIR",
    "/home/alex/.openclaw/workspace/memory/tasks/execution"
))

# Audit log — append-only JSONL
AUDIT_LOG_PATH = Path(os.environ.get(
    "SUPERVISOR_AUDIT_LOG",
    "/home/alex/.openclaw/workspace/memory/audit/supervisor.jsonl"
))

# Downstream script paths
TM_PATH = "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py"
HEAL_PATH = "/home/alex/.openclaw/workspace/scripts/heal.py"

# Mission statuses to check in outer loop
ACTIVE_STATUSES = {"ACTIVE", "ADAPTING", "STALLED"}

# Task statuses that count as "DONE" for stall detection
DONE_TASK_STATUSES = {"DONE", "COMPLETED"}


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _audit(op: str, data: dict) -> None:
    """Append one JSONL record to the audit log.

    Uses POSIX O_APPEND atomicity — safe for <4096-byte records without FileLock.
    Creates parent directories if they don't exist.
    """
    # Re-read AUDIT_LOG_PATH at call time so env var overrides take effect
    audit_path = Path(os.environ.get("SUPERVISOR_AUDIT_LOG", str(AUDIT_LOG_PATH)))
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "op": op,
        "data": data,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(audit_path, "a") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Execution ledger writer
# ---------------------------------------------------------------------------

def _write_exec_ledger(task_id: str, gate_result: QualityGateResult) -> None:
    """Atomically write QualityGateResult to exec_<task_id>.json.

    Uses tmp + os.rename atomic write pattern (consistent with circuit_breaker.py).
    """
    exec_dir = Path(os.environ.get("SUPERVISOR_EXEC_DIR", str(EXEC_DIR)))
    exec_dir.mkdir(parents=True, exist_ok=True)
    target = exec_dir / f"exec_{task_id}.json"
    tmp_path = target.with_suffix(".json.tmp")
    data = gate_result.model_dump()
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.rename(tmp_path, target)


# ---------------------------------------------------------------------------
# Meta sidecar helpers (parallel to mission_engine._load_meta / _save_meta)
# ---------------------------------------------------------------------------

def _meta_dir() -> Path:
    """Return the meta directory, creating it if needed."""
    missions_dir = Path(os.environ.get("MISSION_DIR", str(MISSIONS_DIR)))
    meta = missions_dir / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    return meta


def _load_meta(mission_id: str) -> dict:
    """Load meta sidecar for mission. Returns default if not found."""
    path = _meta_dir() / f"mission_{mission_id}_meta.json"
    if not path.exists():
        return {"last_done_count": 0, "stall_count": 0}
    try:
        with open(path) as f:
            data = json.load(f)
        # Ensure stall_count key exists
        if "stall_count" not in data:
            data["stall_count"] = 0
        return data
    except (json.JSONDecodeError, OSError):
        return {"last_done_count": 0, "stall_count": 0}


def _save_meta(mission_id: str, meta: dict) -> None:
    """Atomically save meta sidecar for mission."""
    meta_dir = _meta_dir()
    path = meta_dir / f"mission_{mission_id}_meta.json"
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(meta, f, indent=2)
    os.rename(tmp_path, path)


# ---------------------------------------------------------------------------
# Outer loop: check-missions (SUPV-01)
# ---------------------------------------------------------------------------

def check_missions() -> None:
    """Read ledger.json, count DONE tasks per active mission, update stall_count.

    Prints terse summary: one line per active mission.
    Appends audit record with op='check_missions'.
    """
    missions_dir = Path(os.environ.get("MISSION_DIR", str(MISSIONS_DIR)))
    ledger_path = missions_dir / "ledger.json"

    # Load ledger
    if not ledger_path.exists():
        _audit("check_missions", {"missions_checked": 0, "note": "ledger not found"})
        return

    try:
        with open(ledger_path) as f:
            ledger = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: cannot read ledger.json: {e}", file=sys.stderr)
        _audit("check_missions", {"error": str(e)})
        sys.exit(1)

    missions = ledger.get("missions", [])
    active_missions = [m for m in missions if m.get("status") in ACTIVE_STATUSES]

    # Count DONE tasks in ARIA_TASK_DIR for each active mission
    task_state_dir = Path(os.environ.get("ARIA_TASK_DIR", str(TASK_STATE_DIR)))

    missions_checked = 0
    for mission in active_missions:
        mission_id = mission.get("id", "")
        goal = mission.get("goal", "")
        status = mission.get("status", "ACTIVE")

        # Count DONE tasks for this mission by scanning task state files
        done_count = 0
        if task_state_dir.exists():
            for task_file in task_state_dir.glob("task_*.json"):
                try:
                    with open(task_file) as f:
                        task = json.load(f)
                    if (task.get("mission_id") == mission_id and
                            task.get("status") in DONE_TASK_STATUSES):
                        done_count += 1
                except (json.JSONDecodeError, OSError):
                    continue

        # Load meta sidecar
        meta = _load_meta(mission_id)
        last_done_count = meta.get("last_done_count", 0)
        stall_count = meta.get("stall_count", 0)

        # Stall detection: progress resets stall_count, no progress increments it
        if done_count > last_done_count:
            stall_count = 0
            meta["last_done_count"] = done_count
        else:
            stall_count += 1

        meta["stall_count"] = stall_count
        _save_meta(mission_id, meta)

        # Terse output: one line per mission
        goal_truncated = goal[:40]
        print(f"{mission_id} | {goal_truncated} | {status} | stall:{stall_count} | done:{done_count}")
        missions_checked += 1

    _audit("check_missions", {"missions_checked": missions_checked})


# ---------------------------------------------------------------------------
# Inner loop: validate-task (SUPV-02)
# ---------------------------------------------------------------------------

def validate_task(task_id: str, result_json: str) -> int:
    """Validate sub-agent result JSON against OUTPUT_SCHEMAS.

    Writes exec_<task_id>.json execution ledger entry.
    On PASS: calls task_manager.py complete.
    On FAIL: calls heal.py attempt --auto.
    Updates task JSON quality_gate_status.
    Appends audit record with op='validate_task'.

    Returns 0 on pass, 1 on fail.
    """
    import subprocess

    task_state_dir = Path(os.environ.get("ARIA_TASK_DIR", str(TASK_STATE_DIR)))
    task_file = task_state_dir / f"task_{task_id}.json"

    # Load task JSON
    if not task_file.exists():
        print(f"ERROR: task file not found: {task_file}", file=sys.stderr)
        _audit("validate_task", {"task_id": task_id, "error": "task file not found"})
        sys.exit(1)

    try:
        with open(task_file) as f:
            task = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: cannot read task file: {e}", file=sys.stderr)
        _audit("validate_task", {"task_id": task_id, "error": str(e)})
        sys.exit(1)

    # Determine task_type
    task_type = task.get("context", {}).get("task_type", "")
    if not task_type:
        # Infer from goal
        goal_lower = task.get("goal", "").lower()
        if "article" in goal_lower:
            task_type = "article_published"
        elif "book" in goal_lower:
            task_type = "book_uploaded"
        else:
            task_type = "task_complete"

    # Look up schema class
    schema_class = OUTPUT_SCHEMAS.get(task_type, TaskCompleteOutput)

    # Parse and validate result
    issues = []
    passed = False
    score = 0.0
    summary_for_tm = ""

    try:
        result_data = json.loads(result_json)
    except json.JSONDecodeError as e:
        issues.append(f"Invalid JSON: {e}")
        result_data = None

    if result_data is not None:
        try:
            validated = schema_class.model_validate(result_data)
            passed = True
            score = 1.0
            # Extract summary for task_manager.py complete call
            if hasattr(validated, "summary"):
                summary_for_tm = str(validated.summary)[:200]
            else:
                summary_for_tm = f"Validated {task_type}"
        except Exception as e:
            # Pydantic ValidationError — collect issues
            issues_raw = str(e)
            issues = [issues_raw[:500]]  # cap issue length
            passed = False
            score = 0.0

    # Build QualityGateResult
    gate_result = QualityGateResult(
        passed=passed,
        score=score,
        issues=issues,
        task_type=task_type,
        validated_at=datetime.now(timezone.utc).isoformat(),
    )

    # Write execution ledger
    _write_exec_ledger(task_id, gate_result)

    # Update task JSON quality_gate_status
    task["quality_gate_status"] = "PASS" if passed else "FAIL"
    tmp_task = task_file.with_suffix(".json.tmp")
    with open(tmp_task, "w") as f:
        json.dump(task, f, indent=2)
    os.rename(tmp_task, task_file)

    # Call downstream scripts
    if passed:
        subprocess.run(
            [sys.executable, TM_PATH, "complete", task_id, "--summary", summary_for_tm],
            check=False,
        )
    else:
        subprocess.run(
            [sys.executable, HEAL_PATH, "attempt", "--task-id", task_id, "--auto"],
            check=False,
        )

    # Audit
    _audit("validate_task", {
        "task_id": task_id,
        "task_type": task_type,
        "result": "PASS" if passed else "FAIL",
        "issues": issues,
    })

    return 0 if passed else 1


# ---------------------------------------------------------------------------
# audit-log subcommand (SUPV-09)
# ---------------------------------------------------------------------------

def audit_log_cmd(tail: int = 20) -> None:
    """Print last N records from the audit log."""
    audit_path = Path(os.environ.get("SUPERVISOR_AUDIT_LOG", str(AUDIT_LOG_PATH)))

    if not audit_path.exists():
        print("(no audit log found)")
        return

    try:
        lines = audit_path.read_text().splitlines()
    except OSError as e:
        print(f"ERROR: cannot read audit log: {e}", file=sys.stderr)
        sys.exit(1)

    # Filter empty lines
    lines = [l for l in lines if l.strip()]
    last_n = lines[-tail:] if tail > 0 else lines

    for line in last_n:
        try:
            record = json.loads(line)
            print(json.dumps(record, indent=2))
        except json.JSONDecodeError:
            print(line)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aria dual-loop supervisor: mission health and task validation."
    )
    subparsers = parser.add_subparsers(dest="command")

    # check-missions
    subparsers.add_parser("check-missions", help="Check active missions for stalls (outer loop)")

    # validate-task
    vt_parser = subparsers.add_parser("validate-task", help="Validate task result against schema (inner loop)")
    vt_parser.add_argument("--task-id", required=True, help="Task ID to validate")
    vt_parser.add_argument("--result", required=True, help="Result JSON string")

    # audit-log
    al_parser = subparsers.add_parser("audit-log", help="Print last N audit records")
    al_parser.add_argument("--tail", type=int, default=20, help="Number of records to show (default: 20)")

    args = parser.parse_args()

    if args.command == "check-missions":
        check_missions()
    elif args.command == "validate-task":
        exit_code = validate_task(args.task_id, args.result)
        sys.exit(exit_code)
    elif args.command == "audit-log":
        audit_log_cmd(tail=args.tail)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
