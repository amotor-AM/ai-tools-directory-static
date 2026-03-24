#!/usr/bin/env python3
"""Mission Engine for Aria — Persistent goal tracking with decomposition and lifecycle management.

Manages high-level missions from intake through completion. Each mission is a persistent
goal that spans multiple heartbeats, tracked in a file-based state store.

Usage:
  # Create a new mission
  mission_engine.py create --goal "Grow Reddit karma to 500 points" --priority 2

  # Check status of a specific mission
  mission_engine.py status --mission-id <id>

  # List all active missions sorted by priority
  mission_engine.py status --all

  # Brief status for heartbeat injection (max 5 lines)
  mission_engine.py status --active-brief

  # Archive a completed mission
  mission_engine.py archive --mission-id <id>

  # Stub subcommands (implemented in Plans 02-04)
  mission_engine.py decompose --mission-id <id>
  mission_engine.py classify --mission-id <id>
  mission_engine.py update-kpi --mission-id <id>
  mission_engine.py adapt --mission-id <id>
  mission_engine.py next-task --mission-id <id>
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# file_lock.py lives in aria-taskmanager/scripts — add both paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts")

from file_lock import FileLock  # noqa: E402

# ---------------------------------------------------------------------------
# Directory configuration — MISSION_DIR env var enables test isolation
# ---------------------------------------------------------------------------

MISSIONS_DIR = Path(os.environ.get(
    "MISSION_DIR",
    "/home/alex/.openclaw/workspace/memory/missions"
))
LEDGER_PATH = MISSIONS_DIR / "ledger.json"
ARCHIVE_DIR = MISSIONS_DIR / "archive"
OUTCOMES_PATH = MISSIONS_DIR / "archive" / "outcomes.jsonl"

# Mission status values
VALID_STATUSES = {"INBOX", "ACTIVE", "ADAPTING", "STALLED", "COMPLETED"}
ACTIVE_STATUSES = {"INBOX", "ACTIVE", "ADAPTING", "STALLED"}

# Task statuses considered "done" for progress calculation
DONE_TASK_STATUSES = {"DONE", "COMPLETED", "CANCELLED"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def short_id() -> str:
    """Return an 8-character hex ID."""
    return uuid.uuid4().hex[:8]


def _ensure_dirs():
    """Ensure mission directories exist (called before first write)."""
    MISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def save_mission(mission: dict, path: Path) -> None:
    """Atomically write mission dict to path using FileLock + os.rename."""
    tmp_path = path.with_suffix(".json.tmp")
    with FileLock(path, timeout=10.0):
        with open(tmp_path, "w") as f:
            json.dump(mission, f, indent=2)
        os.rename(tmp_path, path)


def load_mission(mission_id: str) -> tuple:
    """Load mission file by ID. Returns (mission_dict, path).

    Raises SystemExit(1) if not found.
    """
    path = MISSIONS_DIR / f"mission_{mission_id}.json"
    if not path.exists():
        # Also check archive
        archived = ARCHIVE_DIR / f"mission_{mission_id}.json"
        if archived.exists():
            path = archived
        else:
            print(f"ERROR: Mission '{mission_id}' not found", file=sys.stderr)
            sys.exit(1)

    with FileLock(path, timeout=10.0):
        with open(path) as f:
            return json.load(f), path


def load_ledger() -> dict:
    """Load ledger.json. Returns empty structure if not found."""
    if not LEDGER_PATH.exists():
        return {"missions": [], "updated_at": ""}
    try:
        with FileLock(LEDGER_PATH, timeout=10.0):
            with open(LEDGER_PATH) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"missions": [], "updated_at": ""}


def update_ledger(mission_id: str, goal: str, status: str, priority: int, kpi_summary: str = "") -> None:
    """Upsert a mission entry in ledger.json. Writes atomically."""
    _ensure_dirs()
    ledger = load_ledger()

    entry = {
        "id": mission_id,
        "goal": goal,
        "status": status,
        "priority": priority,
        "kpi_summary": kpi_summary,
        "updated_at": now_iso(),
    }

    # Upsert: replace existing entry or append new
    updated = False
    for i, e in enumerate(ledger["missions"]):
        if e["id"] == mission_id:
            ledger["missions"][i] = entry
            updated = True
            break
    if not updated:
        ledger["missions"].append(entry)

    ledger["updated_at"] = now_iso()

    # Atomic write
    tmp_path = LEDGER_PATH.with_suffix(".json.tmp")
    with FileLock(LEDGER_PATH, timeout=10.0):
        with open(tmp_path, "w") as f:
            json.dump(ledger, f, indent=2)
        os.rename(tmp_path, LEDGER_PATH)


def remove_from_ledger(mission_id: str) -> None:
    """Remove a mission entry from ledger.json. Writes atomically."""
    ledger = load_ledger()
    ledger["missions"] = [e for e in ledger["missions"] if e["id"] != mission_id]
    ledger["updated_at"] = now_iso()

    tmp_path = LEDGER_PATH.with_suffix(".json.tmp")
    with FileLock(LEDGER_PATH, timeout=10.0):
        with open(tmp_path, "w") as f:
            json.dump(ledger, f, indent=2)
        os.rename(tmp_path, LEDGER_PATH)


def _compute_progress(mission: dict) -> int:
    """Compute mission progress as percentage of completed tasks."""
    tasks = mission.get("tasks", [])
    if not tasks:
        return 0
    done = sum(1 for t in tasks if t.get("status", "") in DONE_TASK_STATUSES)
    return int(done / len(tasks) * 100)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def create_mission(args):
    """Create a new mission from a high-level goal."""
    _ensure_dirs()

    mission_id = short_id()
    path = MISSIONS_DIR / f"mission_{mission_id}.json"

    mission = {
        "id": mission_id,
        "goal": args.goal,
        "original_goal": args.goal,   # IMMUTABLE — never modify
        "status": "INBOX",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "kpis": [],
        "tasks": [],
        "strategy": "",
        "stall_count": 0,
        "priority": args.priority if args.priority is not None else 3,
    }

    save_mission(mission, path)
    update_ledger(mission_id, args.goal, "INBOX", mission["priority"])

    print(f"MISSION_CREATED: mission_{mission_id}")
    print(f"  Goal: {args.goal}")
    print(f"  File: {path}")


def mission_status(args):
    """Print mission status — by ID, all, or active-brief for heartbeat."""
    if getattr(args, "active_brief", False):
        # Heartbeat-mode: one line per active mission, max 5
        ledger = load_ledger()
        active = [e for e in ledger["missions"] if e.get("status") in ACTIVE_STATUSES]
        active.sort(key=lambda e: e.get("priority", 3))
        if not active:
            print("NO_ACTIVE_MISSIONS")
            return
        for entry in active[:5]:
            # Load full mission for progress
            m_path = MISSIONS_DIR / f"mission_{entry['id']}.json"
            progress = 0
            if m_path.exists():
                try:
                    with open(m_path) as f:
                        m = json.load(f)
                    progress = _compute_progress(m)
                except (json.JSONDecodeError, OSError):
                    pass
            goal_short = entry["goal"][:60]
            print(f"[P{entry.get('priority', 3)}] {goal_short} — {entry.get('status', 'INBOX')} ({progress}%)")
        return

    if getattr(args, "all", False):
        # List all missions sorted by priority
        ledger = load_ledger()
        missions = sorted(ledger["missions"], key=lambda e: e.get("priority", 3))
        if not missions:
            print("NO_MISSIONS_FOUND")
            return
        print(f"{'PRI':<5} {'STATUS':<12} {'ID':<10} GOAL")
        print("-" * 80)
        for entry in missions:
            goal_short = entry["goal"][:55] + "..." if len(entry["goal"]) > 55 else entry["goal"]
            print(f"P{entry.get('priority', 3):<4} {entry.get('status', 'INBOX'):<12} {entry['id']:<10} {goal_short}")
        return

    if getattr(args, "mission_id", None):
        mission, _ = load_mission(args.mission_id)
        progress = _compute_progress(mission)
        tasks = mission.get("tasks", [])
        done = sum(1 for t in tasks if t.get("status", "") in DONE_TASK_STATUSES)
        blocked = [t for t in tasks if t.get("status", "") == "BLOCKED"]

        print(f"MISSION: {mission['id']}")
        print(f"  Goal:     {mission['goal']}")
        print(f"  Status:   {mission['status']}")
        print(f"  Priority: P{mission.get('priority', 3)}")
        print(f"  Progress: {progress}% ({done}/{len(tasks)} tasks done)")
        if blocked:
            print(f"  Blocked tasks: {len(blocked)}")
            for t in blocked:
                print(f"    - {t.get('task_id', '?')}: {t.get('goal', '')[:60]}")
        if mission.get("kpis"):
            print(f"  KPIs: {len(mission['kpis'])} tracked")
        print(f"  Created:  {mission.get('created_at', '')}")
        print(f"  Updated:  {mission.get('updated_at', '')}")
        return

    # No flag given — print help
    print("Usage: mission_engine.py status --mission-id <id> | --all | --active-brief", file=sys.stderr)
    sys.exit(1)


def archive_mission(args):
    """Archive a completed mission: move to archive/, write outcomes.jsonl, remove from ledger."""
    _ensure_dirs()
    mission, src_path = load_mission(args.mission_id)

    # Mark completed
    if mission["status"] != "COMPLETED":
        mission["status"] = "COMPLETED"
    mission["updated_at"] = now_iso()

    dest_path = ARCHIVE_DIR / f"mission_{args.mission_id}.json"

    # Write to archive location first
    save_mission(mission, dest_path)

    # Remove from active missions directory
    if src_path != dest_path and src_path.exists():
        src_path.unlink()

    # Append outcome record to outcomes.jsonl
    outcome = {
        "mission_id": mission["id"],
        "goal": mission["original_goal"],
        "status": mission["status"],
        "archived_at": now_iso(),
        "task_count": len(mission.get("tasks", [])),
        "kpis": mission.get("kpis", []),
    }
    with open(OUTCOMES_PATH, "a") as f:
        f.write(json.dumps(outcome) + "\n")

    # Remove from ledger
    remove_from_ledger(args.mission_id)

    print(f"MISSION_ARCHIVED: mission_{args.mission_id}")
    print(f"  Goal: {mission['original_goal']}")
    print(f"  File: {dest_path}")


# ---------------------------------------------------------------------------
# Stub subcommands (implemented in Plans 02-04)
# ---------------------------------------------------------------------------

def _not_implemented(args):
    print("NOT_IMPLEMENTED")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Argparse setup
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Mission Engine — manage persistent high-level goals for Aria"
    )
    sub = parser.add_subparsers(dest="command")

    # --- create ---
    p_create = sub.add_parser("create", help="Create a new mission from a high-level goal")
    p_create.add_argument("--goal", required=True, help="High-level goal statement")
    p_create.add_argument("--priority", type=int, default=None,
                          help="Priority 1-4 (1=critical, 4=low). Default: 3")

    # --- status ---
    p_status = sub.add_parser("status", help="Show mission status")
    status_group = p_status.add_mutually_exclusive_group()
    status_group.add_argument("--mission-id", help="Show status for a specific mission")
    status_group.add_argument("--all", action="store_true", help="Show all missions sorted by priority")
    status_group.add_argument("--active-brief", action="store_true",
                              help="One-line per active mission (max 5) for heartbeat injection")

    # --- archive ---
    p_archive = sub.add_parser("archive", help="Archive a completed mission")
    p_archive.add_argument("--mission-id", required=True, help="Mission ID to archive")

    # --- stubs (Plans 02-04) ---
    p_decompose = sub.add_parser("decompose", help="[Plan 02] Decompose mission into tasks via LLM")
    p_decompose.add_argument("--mission-id", required=True)

    p_classify = sub.add_parser("classify", help="[Plan 02] Classify tasks as one-time or recurring")
    p_classify.add_argument("--mission-id", required=True)

    p_kpi = sub.add_parser("update-kpi", help="[Plan 03] Update KPI values for a mission")
    p_kpi.add_argument("--mission-id", required=True)

    p_adapt = sub.add_parser("adapt", help="[Plan 04] Adapt mission strategy when stalled")
    p_adapt.add_argument("--mission-id", required=True)

    p_next = sub.add_parser("next-task", help="[Plan 04] Get next task for a mission")
    p_next.add_argument("--mission-id", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "create": create_mission,
        "status": mission_status,
        "archive": archive_mission,
        "decompose": _not_implemented,
        "classify": _not_implemented,
        "update-kpi": _not_implemented,
        "adapt": _not_implemented,
        "next-task": _not_implemented,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
