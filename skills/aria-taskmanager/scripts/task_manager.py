#!/usr/bin/env python3
"""Task Manager for Aria — Persistent structured task state with retry/escalation logic.

Aria uses this to track tasks across heartbeats with full context preservation.
Each task has a durable JSON state file that survives context resets.

Usage:
  # Create a new task
  task_manager.py create --goal "Deploy pricing endpoint" --context '{"repo": "apex-repricer"}'

  # List active tasks
  task_manager.py list

  # Show task details (resumption context)
  task_manager.py show <task_id>

  # Record a completed step
  task_manager.py step <task_id> "cloned repo and ran tests"

  # Set what the current step is
  task_manager.py set-step <task_id> "docker push to registry"

  # Record an error on current step
  task_manager.py error <task_id> "UNAUTHORIZED 401 — credentials expired"

  # Record a retry attempt with strategy
  task_manager.py retry <task_id> --strategy "refreshing docker credentials via keychain"

  # Escalate to Claude Code
  task_manager.py escalate <task_id>

  # Mark task complete
  task_manager.py complete <task_id> --summary "Deployed successfully"

  # Get resumption prompt for a task (for heartbeat injection)
  task_manager.py resume <task_id>

  # Get resumption prompts for ALL active tasks
  task_manager.py resume-all

  # Archive completed tasks older than N days
  task_manager.py archive --days 7

  # Add a note/observation to a task
  task_manager.py note <task_id> "Found that the issue is related to expired OAuth token"

  # Set task priority (1=critical, 2=high, 3=normal, 4=low)
  task_manager.py priority <task_id> 1
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add file locking for concurrent access
from file_lock import FileLock

STATE_DIR = Path(os.environ.get(
    "ARIA_TASK_DIR",
    "/home/alex/.openclaw/workspace/memory/tasks/state"
))
ARCHIVE_DIR = STATE_DIR / "archived"

# Task status flow: CREATED -> RUNNING -> BLOCKED -> RETRYING -> ESCALATED -> DELEGATED -> DONE
VALID_STATUSES = {"CREATED", "RUNNING", "BLOCKED", "RETRYING", "ESCALATED", "DELEGATED", "DONE", "CANCELLED"}
ACTIVE_STATUSES = {"CREATED", "RUNNING", "BLOCKED", "RETRYING", "ESCALATED", "DELEGATED"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def short_id():
    return uuid.uuid4().hex[:8]


def load_task(task_id):
    """Load a task by ID (supports partial ID matching)."""
    matches = []
    for f in STATE_DIR.glob("task_*.json"):
        if task_id in f.stem:
            matches.append(f)

    if not matches:
        print(f"ERROR: No task found matching '{task_id}'", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"ERROR: Ambiguous ID '{task_id}', matches: {[m.stem for m in matches]}", file=sys.stderr)
        sys.exit(1)

    # Use file locking for safe concurrent reads/writes
    with FileLock(matches[0], timeout=10.0) as lock:
        with open(matches[0]) as f:
            return json.load(f), matches[0]


def save_task(task, path=None, use_lock=True):
    """Save task to its state file with optional locking."""
    if path is None:
        path = STATE_DIR / f"task_{task['id']}.json"

    task["updated_at"] = now_iso()

    if use_lock:
        with FileLock(path, timeout=10.0) as lock:
            with open(path, "w") as f:
                json.dump(task, f, indent=2)
    else:
        with open(path, "w") as f:
            json.dump(task, f, indent=2)

    return path


WORKING_DIR = Path("/home/alex/.openclaw/workspace/memory/working")


def _write_working_memory(task):
    """Write task context to disk so it survives context compaction."""
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKING_DIR / f"task_{task['id']}.md"
    steps_str = "\n".join(
        f"  {i+1}. {s['step']}"
        for i, s in enumerate(task["steps_completed"])
    ) or "  (none)"

    content = f"""# Working Memory — task_{task['id']}
**Goal:** {task['goal']}
**Status:** {task['status']} | P{task.get('priority', 3)} | Attempts: {task['attempts']}/{task['max_attempts']}
**Current step:** {task.get('current_step') or '(none)'}
**Last error:** {task.get('last_error') or '(none)'}
**Consecutive errors on step:** {task.get('consecutive_step_errors', 0)}
**Blocked heartbeats:** {task.get('blocked_heartbeats', 0)}/3

## Completed Steps
{steps_str}

## What to do next
{'BLOCKED — move to next task, will retry next heartbeat' if task['status'] == 'BLOCKED' else 'Continue from current step'}
"""
    with open(path, "w") as f:
        f.write(content)


def _check_duplicate(goal: str):
    """Return existing task_id if an identical active task exists, else None."""
    for f in STATE_DIR.glob("task_*.json"):
        try:
            with open(f) as fh:
                t = json.load(fh)
            if t.get("status") in ACTIVE_STATUSES and t.get("goal") == goal:
                return t["id"]
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return None


def create_task(args):
    """Create a new task with structured state."""
    existing = _check_duplicate(args.goal)
    if existing:
        print(f"DUPLICATE_REJECTED: Identical active task already exists: task_{existing}")
        print(f"  Goal: {args.goal}")
        sys.exit(2)

    task_id = short_id()
    task = {
        "id": task_id,
        "goal": args.goal,
        "status": "CREATED",
        "priority": args.priority or 3,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_heartbeat": None,
        "attempts": 0,
        "max_attempts": args.max_attempts or 15,
        "steps_completed": [],
        "current_step": args.first_step or None,
        "last_error": None,
        "last_error_at": None,
        "retry_strategy": None,
        "notes": [],
        "context": json.loads(args.context) if args.context else {},
        "escalation": {
            "escalated": False,
            "escalated_at": None,
            "claude_code_session_id": None,
            "handoff_brief": None,
            "resolution": None
        },
        "error_history": [],
        "tags": args.tags.split(",") if args.tags else [],
        "source": args.source or "heartbeat",
        "deadline": args.deadline or None,
        "consecutive_step_errors": 0,
        "blocked_heartbeats": 0,
        "step_started_at": None,
        "completed_at": None,
        "resolution": None,
        "mission_id": getattr(args, "mission_id", None),
        "quality_gate_status": None,
        "requires_gpu": getattr(args, "requires_gpu", False),
        "checkpoint": {
            "last_checkpoint_step": None,
            "checkpoint_data": {},
            "checkpointed_at": None,
        },
    }

    path = save_task(task)
    print(f"CREATED: task_{task_id}")
    print(f"  Goal: {args.goal}")
    print(f"  File: {path}")
    print(json.dumps(task, indent=2))


def list_tasks(args):
    """List all tasks, optionally filtered by status."""
    tasks = []
    for f in sorted(STATE_DIR.glob("task_*.json")):
        with open(f) as fh:
            t = json.load(fh)
            if args.status and t["status"] != args.status.upper():
                continue
            if args.active_only and t["status"] not in ACTIVE_STATUSES:
                continue
            tasks.append(t)

    # Sort by priority then created_at
    tasks.sort(key=lambda t: (t.get("priority", 3), t["created_at"]))

    if args.json:
        print(json.dumps(tasks, indent=2))
        return

    if not tasks:
        print("No tasks found.")
        return

    print(f"{'ID':<12} {'STATUS':<12} {'PRI':<5} {'ATTEMPTS':<10} {'GOAL'}")
    print("-" * 80)
    for t in tasks:
        attempts_str = f"{t['attempts']}/{t['max_attempts']}"
        goal_short = t["goal"][:45] + "..." if len(t["goal"]) > 45 else t["goal"]
        print(f"task_{t['id']:<6} {t['status']:<12} P{t.get('priority', 3):<4} {attempts_str:<10} {goal_short}")
        if t.get("current_step"):
            print(f"             -> {t['current_step']}")
        if t.get("last_error"):
            err_short = t["last_error"][:60] + "..." if len(t["last_error"]) > 60 else t["last_error"]
            print(f"             !! {err_short}")


def show_task(args):
    """Show full task details."""
    task, path = load_task(args.task_id)
    print(json.dumps(task, indent=2))


def step_complete(args):
    """Record a completed step. Resets all error/block counters."""
    task, path = load_task(args.task_id)
    step_desc = " ".join(args.description)

    task["steps_completed"].append({
        "step": step_desc,
        "completed_at": now_iso()
    })
    task["status"] = "RUNNING"
    task["attempts"] = 0  # Reset attempts on successful step
    task["last_error"] = None
    task["retry_strategy"] = None
    task["consecutive_step_errors"] = 0  # Reset circuit breaker
    task["blocked_heartbeats"] = 0  # Reset escalation counter
    task["step_started_at"] = None

    save_task(task, path)
    _write_working_memory(task)
    print(f"STEP_DONE: {step_desc}")
    print(f"  Steps completed: {len(task['steps_completed'])}")


def set_step(args):
    """Set what the current step is. Resets error counters."""
    task, path = load_task(args.task_id)
    step_desc = " ".join(args.description)

    task["current_step"] = step_desc
    task["status"] = "RUNNING"
    task["consecutive_step_errors"] = 0  # Reset on new step
    task["step_started_at"] = now_iso()  # Track step timing

    save_task(task, path)
    _write_working_memory(task)
    print(f"STEP_SET: {step_desc}")


def record_error(args):
    """Record an error on the current step. Auto-blocks after 2 consecutive errors."""
    task, path = load_task(args.task_id)
    error_msg = " ".join(args.message)

    # Circuit breaker: track consecutive errors on the same step
    task["consecutive_step_errors"] = task.get("consecutive_step_errors", 0) + 1
    task["attempts"] += 1
    task["last_error"] = error_msg
    task["last_error_at"] = now_iso()
    task["status"] = "BLOCKED"
    task["error_history"].append({
        "error": error_msg,
        "step": task.get("current_step"),
        "attempt": task["attempts"],
        "at": now_iso()
    })

    save_task(task, path)

    consec = task["consecutive_step_errors"]
    if consec >= 5:
        # AUTO-BLOCK: Five consecutive failures on same step — you really tried
        print(f"AUTO_BLOCKED: task_{task['id']} — same step failed {consec} times")
        print(f"  Error: {error_msg}")
        print(f"  Step: {(task.get('current_step') or '?')[:60]}")
        print(f"  ACTION: Move to the next task. Will retry next heartbeat with fresh approach.")
        _write_working_memory(task)
    elif task["attempts"] >= task["max_attempts"]:
        print(f"AUTO_BLOCKED: task_{task['id']} — max attempts ({task['max_attempts']}) reached")
        print(f"  Error: {error_msg}")
        print(f"  ACTION: Move to next task. Will auto-escalate after 8 blocked heartbeats.")
    else:
        print(f"ERROR_RECORDED: {error_msg}")
        print(f"  Consecutive errors on step: {consec}/5 (auto-blocks at 5)")
        print(f"  Total attempts: {task['attempts']}/{task['max_attempts']}")
        if consec >= 3:
            print(f"  ACTION: 3 failures on this step. Try a COMPLETELY different approach. Spawn Claude Code if needed. Search the web for solutions.")
        elif consec >= 2:
            print(f"  ACTION: Step failing repeatedly. Don't repeat the same approach. Change tools, method, or strategy entirely.")
        else:
            print(f"  ACTION: Try a different approach. Think about WHY it failed before trying again.")


def record_retry(args):
    """Record a retry attempt with strategy. Resets consecutive errors (new approach)."""
    task, path = load_task(args.task_id)

    task["retry_strategy"] = args.strategy
    task["status"] = "RETRYING"
    task["last_heartbeat"] = now_iso()
    task["consecutive_step_errors"] = 0  # New approach = reset consecutive counter

    save_task(task, path)
    print(f"RETRY: Attempt {task['attempts']}/{task['max_attempts']}")
    print(f"  Strategy: {args.strategy}")

    if task["attempts"] >= task["max_attempts"]:
        print(f"  WARNING: Max attempts reached. Next failure will auto-block.")


def escalate_task(args):
    """Escalate a task to Claude Code."""
    task, path = load_task(args.task_id)

    # Build handoff brief
    steps_done = "\n".join(
        f"  {i+1}. {s['step']} (at {s['completed_at']})"
        for i, s in enumerate(task["steps_completed"])
    ) or "  (none)"

    error_hist = "\n".join(
        f"  - [{e['at']}] Attempt {e['attempt']}: {e['error']}"
        for e in task["error_history"][-5:]  # Last 5 errors
    ) or "  (none)"

    brief = f"""TASK HANDOFF FROM ARIA TO CLAUDE CODE
=====================================

GOAL: {task['goal']}

PRIORITY: P{task.get('priority', 3)}

COMPLETED STEPS:
{steps_done}

CURRENT STEP (FAILING): {task.get('current_step', 'unknown')}

LAST ERROR: {task.get('last_error', 'unknown')}

ERROR HISTORY (last 5):
{error_hist}

ATTEMPTS MADE: {task['attempts']}

RETRY STRATEGY TRIED: {task.get('retry_strategy', 'none')}

CONTEXT:
{json.dumps(task.get('context', {}), indent=2)}

NOTES:
{chr(10).join(f'  - {n["note"]}' for n in task.get("notes", [])) or "  (none)"}

INSTRUCTIONS:
Please complete this task. When done, output a structured summary
of what you did, what worked, and what the resolution was.
Focus on the failing step: "{task.get('current_step', 'unknown')}"
"""

    task["status"] = "ESCALATED"
    task["escalation"]["escalated"] = True
    task["escalation"]["escalated_at"] = now_iso()
    task["escalation"]["handoff_brief"] = brief

    save_task(task, path)

    # Write the brief to a file for easy pickup by Claude Code spawn
    brief_path = STATE_DIR / f"handoff_{task['id']}.md"
    with open(brief_path, "w") as f:
        f.write(brief)

    print(f"ESCALATED: task_{task['id']}")
    print(f"  Handoff brief: {brief_path}")
    print(f"\nTo spawn Claude Code with this brief:")
    print(f'  exec: {{ "command": "/home/alex/.openclaw/workspace/skills/aria-dev/scripts/spawn-dev-session.sh {brief_path}", "host": "gateway", "timeout": 7200 }}')
    print(f"\nAfter Claude Code completes, run:")
    print(f"  task_manager.py complete {task['id']} --summary 'Claude Code resolution...'")


def set_delegated(args):
    """Mark task as delegated to Claude Code with session ID."""
    task, path = load_task(args.task_id)
    task["status"] = "DELEGATED"
    task["escalation"]["claude_code_session_id"] = args.session_id
    save_task(task, path)
    print(f"DELEGATED: task_{task['id']} -> session {args.session_id}")


def complete_task(args):
    """Mark a task as complete. Fires event chains for follow-up actions."""
    task, path = load_task(args.task_id)
    task["status"] = "DONE"
    task["completed_at"] = now_iso()
    if args.summary:
        task["resolution"] = " ".join(args.summary)
    if task["escalation"]["escalated"]:
        task["escalation"]["resolution"] = task.get("resolution")

    save_task(task, path)
    print(f"COMPLETED: task_{task['id']}")
    print(f"  Goal: {task['goal']}")
    if task.get("resolution"):
        print(f"  Resolution: {task['resolution']}")

    # DO NOT notify Alex on task completion.
    # Alex only wants daily briefs. No individual task notifications.
    # Completions are visible on the dashboard at http://localhost:8888

    # Fire event chains for automatic follow-up actions
    event_chains = Path("/home/alex/.openclaw/workspace/scripts/event_chains.py")
    if event_chains.exists():
        tags = ",".join(task.get("tags", []))
        goal_escaped = task["goal"].replace('"', '\\"')
        cmd = f'python3 {event_chains} fire --task-id {task["id"]} --goal "{goal_escaped}" --tags "{tags}"'
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            if result.stdout.strip():
                print(f"\n  EVENT_CHAINS:")
                for line in result.stdout.strip().split("\n"):
                    print(f"  {line}")
        except (subprocess.TimeoutExpired, Exception):
            pass

    # Auto-record outcome for learning
    outcome_tracker = Path("/home/alex/.openclaw/workspace/scripts/outcome_tracker.py")
    if outcome_tracker.exists():
        # Calculate duration from created_at to now
        try:
            created = datetime.fromisoformat(task["created_at"])
            duration = int((datetime.now(timezone.utc) - created).total_seconds() / 60)
        except (ValueError, TypeError):
            duration = 0
        # Infer task type from goal keywords
        goal_lower = task["goal"].lower()
        task_type = "general"
        for keyword, ttype in [("article", "article"), ("book", "book"), ("publish", "publish"),
                                ("seo", "seo"), ("reddit", "reddit"), ("browser", "browser"),
                                ("deploy", "deploy"), ("fix", "fix"), ("social", "social"),
                                ("outreach", "outreach"), ("account", "account")]:
            if keyword in goal_lower:
                task_type = ttype
                break
        goal_escaped = task["goal"][:60].replace('"', '\\"')
        cmd = (f'python3 {outcome_tracker} record --task-type {task_type} '
               f'--outcome success --duration {duration} --notes "{goal_escaped}"')
        try:
            subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        except (subprocess.TimeoutExpired, Exception):
            pass

    # Clean up working memory file
    wm_file = WORKING_DIR / f"task_{task['id']}.md"
    if wm_file.exists():
        wm_file.unlink()


def cancel_task(args):
    """Cancel a task."""
    task, path = load_task(args.task_id)
    task["status"] = "CANCELLED"
    task["cancelled_at"] = now_iso()
    task["cancel_reason"] = " ".join(args.reason) if args.reason else None
    save_task(task, path)
    print(f"CANCELLED: task_{task['id']}")


def resume_task(args):
    """Generate a resumption prompt for a single task."""
    task, path = load_task(args.task_id)
    print(_build_resume_prompt(task))


def resume_all(args):
    """Generate resumption prompts for all active tasks, sorted by priority."""
    tasks = []
    for f in sorted(STATE_DIR.glob("task_*.json")):
        with open(f) as fh:
            t = json.load(fh)
            if t["status"] in ACTIVE_STATUSES:
                tasks.append(t)

    if not tasks:
        print("NO_ACTIVE_TASKS")
        return

    tasks.sort(key=lambda t: (t.get("priority", 3), t["created_at"]))

    print(f"ACTIVE_TASKS: {len(tasks)}")
    print("=" * 70)
    for t in tasks:
        print(_build_resume_prompt(t))
        print("-" * 70)


def _build_resume_prompt(task):
    """Build a focused resumption prompt with full context."""
    steps_str = "\n".join(
        f"    {i+1}. {s['step']}"
        for i, s in enumerate(task["steps_completed"])
    ) or "    (none yet)"

    notes_str = "\n".join(
        f"    - {n['note']}"
        for n in task.get("notes", [])
    ) or ""

    prompt = f"""TASK RESUMPTION — task_{task['id']} [P{task.get('priority', 3)}] [{task['status']}]

  Goal: {task['goal']}
  Current step: {task.get('current_step') or '(determine next step)'}
  Attempts on current step: {task['attempts']}/{task['max_attempts']}

  Completed steps:
{steps_str}
"""

    if task.get("last_error"):
        prompt += f"""
  Last error: {task['last_error']}
  Error at: {task.get('last_error_at', 'unknown')}
  Retry strategy: {task.get('retry_strategy') or '(none yet)'}
"""

    if task.get("context"):
        prompt += f"""
  Context: {json.dumps(task['context'])}
"""

    if notes_str:
        prompt += f"""
  Notes:
{notes_str}
"""

    if task["status"] == "ESCALATED":
        prompt += """
  STATUS: ESCALATED to Claude Code. Spawn a dev session with the handoff brief.
"""
    elif task["status"] == "DELEGATED":
        sid = task["escalation"].get("claude_code_session_id", "unknown")
        prompt += f"""
  STATUS: DELEGATED to Claude Code session {sid}. Poll for completion.
"""
    elif task["status"] == "BLOCKED" and task["attempts"] >= task["max_attempts"]:
        prompt += """
  STATUS: BLOCKED and max attempts reached. DO NOT GIVE UP. You have options:
  1. Spawn Claude Code (Opus) to investigate with fresh eyes
  2. Search the web for solutions: web_search for the specific error
  3. Try a COMPLETELY different tool or approach (different library, API, manual workflow)
  4. Break the problem into smaller sub-steps and solve each one
  Only escalate to Alex if you LITERALLY need his credentials or money.
"""
    elif task["status"] in ("BLOCKED", "RETRYING"):
        blocked_hb = task.get("blocked_heartbeats", 0)
        prompt += f"""
  ACTION: Continue working on this. You have {task['max_attempts'] - task['attempts']} attempts remaining.
  Blocked heartbeats: {blocked_hb}/8 (auto-escalates at 8).
  DO NOT ask Alex for help. DO NOT give up. Think creatively:
  - Why did it fail? What's the ROOT CAUSE?
  - Is there a completely different approach?
  - Can you search the web for a solution?
  - Should you spawn Claude Code (Opus) to help investigate?
  - Can you break this into smaller steps?
"""
    else:
        prompt += """
  ACTION: Continue from where you left off. Do not start over. Push through obstacles.
"""

    return prompt


def add_note(args):
    """Add a note/observation to a task."""
    task, path = load_task(args.task_id)
    note_text = " ".join(args.note)
    task.setdefault("notes", []).append({
        "note": note_text,
        "at": now_iso()
    })
    save_task(task, path)
    print(f"NOTE_ADDED: {note_text}")


def set_priority(args):
    """Set task priority."""
    task, path = load_task(args.task_id)
    task["priority"] = int(args.level)
    save_task(task, path)
    print(f"PRIORITY_SET: P{args.level} for task_{task['id']}")


def save_checkpoint(args):
    """Save checkpoint state for a task (HEAL-08: per-step resume)."""
    task, path = load_task(args.task_id)
    checkpoint_data = json.loads(args.data) if args.data else {}
    task["checkpoint"] = {
        "last_checkpoint_step": args.step,
        "checkpoint_data": checkpoint_data,
        "checkpointed_at": now_iso(),
    }
    save_task(task, path)
    print(f"CHECKPOINT_SAVED: task_{task['id']} at step '{args.step}'")


def archive_tasks(args):
    """Archive completed tasks older than N days."""
    ARCHIVE_DIR.mkdir(exist_ok=True)
    days = args.days or 7
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    archived = 0

    for f in STATE_DIR.glob("task_*.json"):
        with open(f) as fh:
            t = json.load(fh)
        if t["status"] in ("DONE", "CANCELLED"):
            completed_at = t.get("completed_at") or t.get("cancelled_at") or t.get("updated_at")
            if completed_at:
                try:
                    ts = datetime.fromisoformat(completed_at).timestamp()
                    if ts < cutoff:
                        dest = ARCHIVE_DIR / f.name
                        f.rename(dest)
                        archived += 1
                except (ValueError, TypeError):
                    pass

    print(f"ARCHIVED: {archived} tasks (older than {days} days)")


def heartbeat_check(args):
    """Quick heartbeat status check with circuit breaker enforcement."""
    tasks = []
    task_files = {}  # Track file paths for saving
    for f in sorted(STATE_DIR.glob("task_*.json")):
        with open(f) as fh:
            t = json.load(fh)
            if t["status"] in ACTIVE_STATUSES:
                tasks.append(t)
                task_files[t["id"]] = f

    if not tasks:
        print("HEARTBEAT_STATUS: No active tasks.")
        return

    tasks.sort(key=lambda t: (t.get("priority", 3), t["created_at"]))

    # --- Circuit Breaker: Auto-escalate tasks blocked for 8+ heartbeats (4 hours) ---
    # At 4 heartbeats: suggest spawning Claude Code for help
    # At 8 heartbeats: auto-escalate to Alex as absolute last resort
    auto_escalated = []
    for t in tasks:
        if t["status"] == "BLOCKED":
            t["blocked_heartbeats"] = t.get("blocked_heartbeats", 0) + 1
            save_task(t, task_files.get(t["id"]))
            if t["blocked_heartbeats"] == 4:
                # Intermediate escalation: suggest Claude Code
                goal_short = t['goal'][:60]
                print(f"\n  SUGGEST_CLAUDE_CODE: task_{t['id'][:8]} blocked for 4 heartbeats: {goal_short}")
                print(f"    ACTION: Spawn Claude Code to investigate this. Don't wait for Alex.")
            if t["blocked_heartbeats"] >= 8:
                t["status"] = "ESCALATED"
                t["escalation"]["escalated"] = True
                t["escalation"]["escalated_at"] = now_iso()
                save_task(t, task_files.get(t["id"]))
                auto_escalated.append(t)

    needs_escalation = [t for t in tasks if t["attempts"] >= t["max_attempts"] and t["status"] != "ESCALATED" and t not in auto_escalated]
    blocked = [t for t in tasks if t["status"] == "BLOCKED" and t not in needs_escalation]
    delegated = [t for t in tasks if t["status"] == "DELEGATED"]
    running = [t for t in tasks if t["status"] in ("RUNNING", "CREATED", "RETRYING")]

    print(f"HEARTBEAT_STATUS: {len(tasks)} active tasks")

    # Auto-escalated tasks — DO NOT message Alex. Just log it and move on.
    # Spawn Claude Code to investigate instead.
    if auto_escalated:
        print(f"\n  AUTO_ESCALATED ({len(auto_escalated)}) — blocked for 8+ heartbeats:")
        for t in auto_escalated:
            goal_short = t['goal'][:60]
            last_err = (t.get('last_error') or '?')[:50]
            print(f"    task_{t['id']}: {goal_short}")
            print(f"      Error: {last_err}")
            print(f"    ACTION: Spawn Claude Code to investigate. DO NOT message Alex.")

    if needs_escalation:
        print(f"\n  ESCALATE NOW ({len(needs_escalation)}):")
        for t in needs_escalation:
            print(f"    task_{t['id']}: {t['goal'][:50]} (P{t.get('priority', 3)}, {t['attempts']} failed attempts)")

    if blocked:
        print(f"\n  BLOCKED ({len(blocked)}):")
        for t in blocked:
            last_err = (t.get("last_error") or "?")[:40]
            hb = t.get("blocked_heartbeats", 0)
            print(f"    task_{t['id']}: {t['goal'][:50]} — {last_err} (blocked {hb}/3 heartbeats)")

    if delegated:
        print(f"\n  DELEGATED TO CLAUDE CODE ({len(delegated)}):")
        for t in delegated:
            sid = t.get("escalation", {}).get("claude_code_session_id") or "?"
            print(f"    task_{t['id']}: {t['goal'][:50]} — session: {sid}")

    # Detect stale tasks — RUNNING for >24h with no recent step update
    stale = []
    for t in running:
        updated = t.get("updated_at") or t.get("created_at", "")
        try:
            updated_dt = datetime.fromisoformat(updated)
            hours_since = (datetime.now(timezone.utc) - updated_dt).total_seconds() / 3600
            if hours_since > 24:
                stale.append((t, hours_since))
        except (ValueError, TypeError):
            pass

    # Detect steps running too long (>30 min since step_started_at)
    long_running = []
    for t in running:
        started = t.get("step_started_at")
        if started:
            try:
                started_dt = datetime.fromisoformat(started)
                mins_since = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60
                if mins_since > 30:
                    long_running.append((t, mins_since))
            except (ValueError, TypeError):
                pass

    if stale:
        print(f"\n  STALE — NO PROGRESS >24h ({len(stale)}):")
        for t, hours in stale:
            print(f"    task_{t['id']}: {t['goal'][:50]} — {hours:.0f}h since last update")
        print("    ACTION: Cancel or re-activate these. They are wasting context.")

    if long_running:
        print(f"\n  LONG-RUNNING STEPS ({len(long_running)}):")
        for t, mins in long_running:
            step = (t.get("current_step") or "?")[:40]
            print(f"    task_{t['id']}: step \"{step}\" running for {mins:.0f}min")
        print("    ACTION: These steps may be stuck. Consider moving on.")

    if running:
        print(f"\n  IN PROGRESS ({len(running)}):")
        for t in running:
            step = (t.get("current_step") or "?")[:40]
            print(f"    task_{t['id']}: {t['goal'][:50]} — step: {step}")

    # Auto-archive completed tasks older than 3 days
    cutoff = datetime.now(timezone.utc).timestamp() - (3 * 86400)
    auto_archived = 0
    ARCHIVE_DIR.mkdir(exist_ok=True)
    for f in STATE_DIR.glob("task_*.json"):
        try:
            with open(f) as fh:
                task_data = json.load(fh)
            if task_data["status"] in ("DONE", "CANCELLED"):
                done_at = task_data.get("completed_at") or task_data.get("cancelled_at") or task_data.get("updated_at", "")
                ts = datetime.fromisoformat(done_at).timestamp()
                if ts < cutoff:
                    f.rename(ARCHIVE_DIR / f.name)
                    auto_archived += 1
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
    if auto_archived:
        print(f"\n  AUTO-ARCHIVED: {auto_archived} completed tasks (>3 days old)")

    # Check for due follow-up tasks (event chain system)
    event_chains = Path("/home/alex/.openclaw/workspace/scripts/event_chains.py")
    if event_chains.exists():
        try:
            result = subprocess.run(
                f"python3 {event_chains} check-followups",
                shell=True, capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip() and "NO_PENDING" not in result.stdout:
                print(f"\n  FOLLOW-UPS:")
                for line in result.stdout.strip().split("\n"):
                    print(f"    {line}")
        except (subprocess.TimeoutExpired, Exception):
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Aria Task Manager — Persistent structured task state"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # create
    p = subparsers.add_parser("create", help="Create a new task")
    p.add_argument("--goal", "-g", required=True, help="Task goal description")
    p.add_argument("--context", "-c", help="JSON context object")
    p.add_argument("--first-step", help="First step to work on")
    p.add_argument("--priority", "-p", type=int, help="Priority 1-4 (1=critical)")
    p.add_argument("--max-attempts", type=int, help="Max retry attempts before escalation (default: 5)")
    p.add_argument("--tags", help="Comma-separated tags")
    p.add_argument("--source", help="Where this task came from (heartbeat, alex, self)")
    p.add_argument("--deadline", help="ISO deadline")
    p.add_argument("--mission-id", help="Parent mission ID (if task belongs to a mission)")
    p.add_argument("--requires-gpu", action="store_true", default=False,
                   help="Flag this task as requiring GPU resources")

    # list
    p = subparsers.add_parser("list", help="List tasks")
    p.add_argument("--status", help="Filter by status")
    p.add_argument("--active-only", "-a", action="store_true", default=True, help="Only show active tasks")
    p.add_argument("--all", dest="active_only", action="store_false", help="Show all tasks including completed")
    p.add_argument("--json", action="store_true", help="Output as JSON")

    # show (also aliased as "get")
    p = subparsers.add_parser("show", help="Show task details")
    p.add_argument("task_id", help="Task ID (full or partial)")
    p2 = subparsers.add_parser("get", help="Show task details (alias for show)")
    p2.add_argument("task_id", help="Task ID (full or partial)")

    # step
    p = subparsers.add_parser("step", help="Record a completed step")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("description", nargs="+", help="Step description")

    # set-step
    p = subparsers.add_parser("set-step", help="Set the current step")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("description", nargs="+", help="Step description")

    # error
    p = subparsers.add_parser("error", help="Record an error")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("message", nargs="+", help="Error message")

    # retry
    p = subparsers.add_parser("retry", help="Record a retry attempt")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("--strategy", "-s", required=True, help="Retry strategy description")

    # escalate
    p = subparsers.add_parser("escalate", help="Escalate to Claude Code")
    p.add_argument("task_id", help="Task ID")

    # delegated
    p = subparsers.add_parser("delegated", help="Mark task as delegated with session ID")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("session_id", help="Claude Code session ID")

    # complete
    p = subparsers.add_parser("complete", help="Mark task as complete")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("--summary", nargs="+", help="Completion summary")

    # cancel
    p = subparsers.add_parser("cancel", help="Cancel a task")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("--reason", nargs="+", help="Cancellation reason")

    # resume
    p = subparsers.add_parser("resume", help="Get resumption prompt for a task")
    p.add_argument("task_id", help="Task ID")

    # resume-all
    subparsers.add_parser("resume-all", help="Get resumption prompts for all active tasks")

    # note
    p = subparsers.add_parser("note", help="Add a note to a task")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("note", nargs="+", help="Note text")

    # priority
    p = subparsers.add_parser("priority", help="Set task priority")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("level", help="Priority level (1=critical, 2=high, 3=normal, 4=low)")

    # archive
    p = subparsers.add_parser("archive", help="Archive old completed tasks")
    p.add_argument("--days", type=int, default=7, help="Archive tasks older than N days")

    # checkpoint
    p = subparsers.add_parser("checkpoint", help="Save checkpoint for task resumption (HEAL-08)")
    p.add_argument("task_id", help="Task ID")
    p.add_argument("--step", "-s", required=True, help="Checkpoint step name")
    p.add_argument("--data", "-d", default="{}", help="JSON checkpoint data")

    # heartbeat
    subparsers.add_parser("heartbeat", help="Quick heartbeat status check")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    commands = {
        "create": create_task,
        "list": list_tasks,
        "show": show_task,
        "get": show_task,
        "step": step_complete,
        "set-step": set_step,
        "error": record_error,
        "retry": record_retry,
        "escalate": escalate_task,
        "delegated": set_delegated,
        "complete": complete_task,
        "cancel": cancel_task,
        "resume": resume_task,
        "resume-all": resume_all,
        "note": add_note,
        "priority": set_priority,
        "archive": archive_tasks,
        "checkpoint": save_checkpoint,
        "heartbeat": heartbeat_check,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
