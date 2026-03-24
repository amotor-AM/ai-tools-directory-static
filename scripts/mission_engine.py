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

  # Decompose a mission into tasks via LLM (Sonnet + Qwen3)
  mission_engine.py decompose --mission-id <id>

  # Classify a task description as one-time or recurring
  mission_engine.py classify --description "post to Reddit daily"

  # Get next task for a mission (dependency order)
  mission_engine.py next-task --mission-id <id>
  mission_engine.py next-task --all-missions
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from croniter import croniter as Croniter

from pydantic import BaseModel, Field

# file_lock.py lives in aria-taskmanager/scripts — add both paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts")

from file_lock import FileLock  # noqa: E402

# LLM client imports — imported at module level so tests can patch them.
# These are optional at import time (no network calls on import).
try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None  # type: ignore

try:
    import ollama
except ImportError:
    ollama = None  # type: ignore

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

# Task state directory for next-task (reads task files written by task_manager.py)
TASK_STATE_DIR = Path(os.environ.get(
    "ARIA_TASK_DIR",
    "/home/alex/.openclaw/workspace/memory/tasks/state"
))

# Path to task_manager.py — resolved at runtime for subprocess calls
TM_PATH = Path("/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py")

# ---------------------------------------------------------------------------
# KPI auto-selection mapping — keyword → KPI dict
# Deduplicated by metric name during auto_select_kpis()
# ---------------------------------------------------------------------------

KPI_MAP = {
    "traffic":   {"metric": "gsc_clicks",          "target": "TBD", "current": 0, "met": False},
    "seo":       {"metric": "gsc_clicks",          "target": "TBD", "current": 0, "met": False},
    "visits":    {"metric": "monthly_visits",      "target": "TBD", "current": 0, "met": False},
    "followers": {"metric": "follower_count",      "target": "TBD", "current": 0, "met": False},
    "social":    {"metric": "follower_count",      "target": "TBD", "current": 0, "met": False},
    "audience":  {"metric": "follower_count",      "target": "TBD", "current": 0, "met": False},
    "revenue":   {"metric": "monthly_revenue",     "target": "TBD", "current": 0, "met": False},
    "sales":     {"metric": "monthly_revenue",     "target": "TBD", "current": 0, "met": False},
    "income":    {"metric": "monthly_revenue",     "target": "TBD", "current": 0, "met": False},
    "articles":  {"metric": "articles_published",  "target": "TBD", "current": 0, "met": False},
    "content":   {"metric": "articles_published",  "target": "TBD", "current": 0, "met": False},
    "posts":     {"metric": "articles_published",  "target": "TBD", "current": 0, "met": False},
    "books":     {"metric": "books_published",     "target": "TBD", "current": 0, "met": False},
    "publish":   {"metric": "books_published",     "target": "TBD", "current": 0, "met": False},
    "launch":    {"metric": "books_published",     "target": "TBD", "current": 0, "met": False},
}

# Mission status values
VALID_STATUSES = {"INBOX", "ACTIVE", "ADAPTING", "STALLED", "COMPLETED"}
ACTIVE_STATUSES = {"INBOX", "ACTIVE", "ADAPTING", "STALLED"}

# Task statuses considered "done" for progress calculation
DONE_TASK_STATUSES = {"DONE", "COMPLETED", "CANCELLED"}

# Task statuses considered "in progress" (skip for next-task selection)
IN_PROGRESS_STATUSES = {"RUNNING", "DELEGATED", "ESCALATED"}

# Task statuses eligible for next-task selection
ELIGIBLE_STATUSES = {"CREATED", "BLOCKED", "RETRYING"}


# ---------------------------------------------------------------------------
# Pydantic models for LLM-structured outputs
# ---------------------------------------------------------------------------

class TaskClassification(BaseModel):
    """Classification of a task as one-time or recurring."""
    task_type: Literal["one-time", "recurring"]
    confidence: float = Field(ge=0.0, le=1.0)
    cadence: Optional[str] = None  # cron expression if recurring
    reasoning: str


class AdaptationOutput(BaseModel):
    """Sonnet-generated strategy revision for a stalled mission."""
    revised_strategy: str
    new_subtasks: list[str]
    cancel_task_ids: list[str] = Field(default_factory=list)
    reasoning: str


class AmbiguityCheck(BaseModel):
    """Assessment of whether a goal is too ambiguous to act on."""
    is_ambiguous: bool
    missing_info: list[str] = Field(default_factory=list)
    clarification_question: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)


class SubtaskEnrichment(BaseModel):
    """Complexity and GPU requirement assessment for a subtask."""
    complexity: int = Field(ge=1, le=5)  # 1=trivial, 5=very complex
    requires_gpu: bool = False
    reasoning: str


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
# KPI auto-selection
# ---------------------------------------------------------------------------

def auto_select_kpis(goal: str) -> list:
    """Auto-select KPIs based on keyword matching in the goal string.

    Deduplicates by metric name so that synonyms (e.g. 'traffic' + 'seo')
    only produce one entry per metric.

    Returns an empty list if no keyword matches — decompose will populate KPIs.
    """
    goal_lower = goal.lower()
    seen_metrics: set = set()
    kpis = []
    for keyword, kpi_template in KPI_MAP.items():
        if keyword in goal_lower and kpi_template["metric"] not in seen_metrics:
            seen_metrics.add(kpi_template["metric"])
            kpis.append(dict(kpi_template))  # fresh copy so mutations don't affect KPI_MAP
    return kpis


# ---------------------------------------------------------------------------
# Mission meta file helpers (sidecar — keeps schema-strict mission JSON clean)
# ---------------------------------------------------------------------------

def _meta_dir() -> Path:
    """Return the meta directory for sidecar files, creating if needed."""
    meta = MISSIONS_DIR / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    return meta


def _load_meta(mission_id: str) -> dict:
    """Load sidecar meta file for a mission. Returns default if not found."""
    path = _meta_dir() / f"mission_{mission_id}_meta.json"
    if not path.exists():
        return {"last_done_count": 0}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_done_count": 0}


def _save_meta(mission_id: str, meta: dict) -> None:
    """Save sidecar meta file for a mission atomically."""
    meta_dir = _meta_dir()
    path = meta_dir / f"mission_{mission_id}_meta.json"
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(meta, f, indent=2)
    os.rename(tmp_path, path)


# ---------------------------------------------------------------------------
# LLM helper functions
# ---------------------------------------------------------------------------

def classify_task(description: str) -> TaskClassification:
    """Classify a task description as one-time or recurring using Qwen3.

    Args:
        description: Natural language task description.

    Returns:
        TaskClassification with task_type, confidence, cadence, reasoning.
    """
    prompt = (
        f"Classify this task as 'one-time' or 'recurring'. "
        f"A recurring task is done on a regular schedule (daily, weekly, etc.) and needs a cron expression. "
        f"A one-time task is done once and then complete.\n\n"
        f"Task: {description}\n\n"
        f"Return JSON matching the schema exactly."
    )

    response = ollama.chat(
        model="huihui_ai/qwen3-abliterated:14b",
        format=TaskClassification.model_json_schema(),
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )

    return TaskClassification.model_validate_json(response.message.content)


def check_ambiguity(goal: str) -> AmbiguityCheck:
    """Check if a goal is too ambiguous to act on using Sonnet.

    Args:
        goal: High-level goal string.

    Returns:
        AmbiguityCheck with is_ambiguous, missing_info, clarification_question, confidence.
    """
    client = Anthropic()

    prompt = (
        "Evaluate if this goal is clear enough to act on. "
        "A goal is ambiguous ONLY if 2+ fundamental parameters (who/what/how/where) are genuinely "
        "undefined and cannot be reasonably inferred. "
        "Goals like 'grow SEO traffic' or 'write 5 articles' are CLEAR — Aria knows her domains. "
        "Return is_ambiguous=true ONLY for goals like 'do something about the website' or 'make money somehow'.\n\n"
        f"Goal: {goal}\n\n"
        "Return JSON matching the schema exactly."
    )

    result = client.beta.messages.parse(
        model="claude-sonnet-4-5",
        max_tokens=512,
        output_format=AmbiguityCheck,
        messages=[{"role": "user", "content": prompt}],
    )

    return result.parsed


def _enrich_subtask(subtask: str) -> SubtaskEnrichment:
    """Get complexity score and GPU requirement for a subtask using Qwen3."""
    prompt = (
        f"Assess the complexity (1=trivial, 5=very complex) and whether this task requires GPU resources "
        f"(e.g., image generation, video processing, model training).\n\n"
        f"Task: {subtask}\n\n"
        f"Return JSON matching the schema exactly."
    )

    response = ollama.chat(
        model="huihui_ai/qwen3-abliterated:14b",
        format=SubtaskEnrichment.model_json_schema(),
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )

    return SubtaskEnrichment.model_validate_json(response.message.content)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def create_mission(args):
    """Create a new mission from a high-level goal."""
    _ensure_dirs()

    mission_id = short_id()
    path = MISSIONS_DIR / f"mission_{mission_id}.json"

    auto_kpis = auto_select_kpis(args.goal)

    mission = {
        "id": mission_id,
        "goal": args.goal,
        "original_goal": args.goal,   # IMMUTABLE — never modify
        "status": "INBOX",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "kpis": auto_kpis,
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


def decompose_mission(args):
    """Decompose a mission into ordered subtasks via Sonnet + Qwen3 two-step pattern.

    Step 0: Check goal ambiguity via Sonnet AmbiguityCheck.
    Step 1: Call Sonnet to decompose goal -> MissionDecompositionOutput.
    Step 2: For each subtask, classify (Qwen3) and enrich (Qwen3).
    Step 3: Create task_manager.py entries via subprocess.
    Step 4: Update mission JSON with tasks, kpis, status=ACTIVE.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from output_schema import MissionDecompositionOutput

    mission, path = load_mission(args.mission_id)

    if mission["status"] != "INBOX":
        print(f"ERROR: Mission '{args.mission_id}' is not in INBOX status (current: {mission['status']})",
              file=sys.stderr)
        sys.exit(1)

    client = Anthropic()  # module-level import

    # --- Step 0: Ambiguity check ---
    ambiguity_result = client.beta.messages.parse(
        model="claude-sonnet-4-5",
        max_tokens=512,
        output_format=AmbiguityCheck,
        messages=[{
            "role": "user",
            "content": (
                "Evaluate if this goal is clear enough to act on. "
                "A goal is ambiguous ONLY if 2+ fundamental parameters (who/what/how/where) are genuinely "
                "undefined and cannot be reasonably inferred. "
                "Goals like 'grow SEO traffic' or 'write 5 articles' are CLEAR — Aria knows her domains. "
                "Return is_ambiguous=true ONLY for goals like 'do something about the website' or 'make money somehow'.\n\n"
                f"Goal: {mission['goal']}\n\n"
                "Return JSON matching the schema exactly."
            ),
        }],
    )
    ambiguity = ambiguity_result.parsed

    if ambiguity.is_ambiguous and ambiguity.confidence > 0.7:
        question = ambiguity.clarification_question or "Please clarify the goal."
        print(f"CLARIFICATION_NEEDED: {question}")
        sys.exit(3)

    # --- Step 1: Sonnet decomposition ---
    decompose_result = client.beta.messages.parse(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        output_format=MissionDecompositionOutput,
        messages=[{
            "role": "user",
            "content": (
                f"Decompose this mission into ordered subtasks. Return subtasks in dependency order "
                f"(earlier subtasks must be done before later ones). Each subtask should be a clear, "
                f"actionable goal statement. Include 2-5 measurable KPIs.\n\n"
                f"Mission ID: {mission['id']}\n"
                f"Goal: {mission['goal']}\n\n"
                f"Return JSON matching the schema exactly."
            ),
        }],
    )
    decomposition = decompose_result.parsed

    # --- Step 2: Classify and enrich each subtask ---
    task_entries = []
    for subtask_str in decomposition.subtasks:
        classification = classify_task(subtask_str)
        enrichment = _enrich_subtask(subtask_str)

        # Build subprocess command for task_manager.py
        context_obj = {
            "complexity": enrichment.complexity,
            "mission_goal": mission["original_goal"],
        }
        cmd = [
            sys.executable,
            str(TM_PATH),
            "create",
            "--goal", subtask_str,
            "--mission-id", args.mission_id,
            "--source", "mission-subtask",
            "--priority", str(mission.get("priority", 3)),
            "--context", json.dumps(context_obj),
        ]
        if enrichment.requires_gpu:
            cmd.append("--requires-gpu")

        proc = subprocess.run(cmd, capture_output=True, text=True)

        if proc.returncode == 2:
            # DUPLICATE_REJECTED — task already exists, note and continue
            print(f"  NOTE: Subtask already exists (duplicate): {subtask_str[:60]}")
            task_id = None
            # Try to extract task ID from stdout if available
            for line in proc.stdout.splitlines():
                if line.startswith("CREATED:"):
                    token = line.split(":", 1)[1].strip()
                    task_id = token.split("_", 1)[1] if "_" in token else token
                    break
        elif proc.returncode != 0:
            print(f"  WARNING: task_manager.py failed (exit {proc.returncode}): {proc.stderr.strip()}")
            task_id = None
        else:
            # Extract task ID from "CREATED: task_<id>" line
            task_id = None
            for line in proc.stdout.splitlines():
                if line.startswith("CREATED:"):
                    token = line.split(":", 1)[1].strip()
                    task_id = token.split("_", 1)[1] if "_" in token else token
                    break

        task_entries.append({
            "task_id": task_id or short_id(),
            "goal": subtask_str,
            "type": classification.task_type,
            "cadence": classification.cadence,
            "status": "CREATED",
            "requires_gpu": enrichment.requires_gpu,
        })

    # --- Step 3: Update mission JSON ---
    mission["tasks"] = task_entries
    mission["kpis"] = [
        {"metric": kpi, "target": "TBD", "current": 0, "met": False}
        for kpi in decomposition.kpis
    ]
    mission["strategy"] = mission.get("goal", "")
    mission["status"] = "ACTIVE"
    mission["updated_at"] = now_iso()

    save_mission(mission, path)
    update_ledger(args.mission_id, mission["goal"], "ACTIVE", mission.get("priority", 3))

    print(f"MISSION_DECOMPOSED: mission_{args.mission_id}")
    print(f"  Tasks: {len(task_entries)}")
    print(f"  KPIs: {len(mission['kpis'])}")


def classify_subcommand(args):
    """Standalone classify subcommand — classify a task description.

    Prints TYPE, CONFIDENCE, CADENCE, REASONING. If confidence < 0.6,
    prints NEEDS_CLARIFICATION instead.
    """
    classification = classify_task(args.description)

    if classification.confidence < 0.6:
        print(
            f"NEEDS_CLARIFICATION: Low confidence ({classification.confidence:.2f}). "
            f"What did you mean by: {args.description}?"
        )
    else:
        print(f"TYPE: {classification.task_type}")
        print(f"CONFIDENCE: {classification.confidence:.2f}")
        print(f"CADENCE: {classification.cadence or 'N/A'}")
        print(f"REASONING: {classification.reasoning}")


def next_task(args):
    """Return the next eligible task for a mission (or all active missions).

    Tasks are returned in dependency order (the order they were added to the
    mission tasks array, which matches the Sonnet decomposition order).

    Skips tasks that are DONE, COMPLETED, CANCELLED (finished) or
    RUNNING, DELEGATED, ESCALATED (already in progress).

    With --all-missions: iterates all active missions by priority (ascending),
    returning one eligible task per mission, capped at 3 total.
    """
    if getattr(args, "all_missions", False):
        _next_task_all_missions()
        return

    if not getattr(args, "mission_id", None):
        print("ERROR: --mission-id or --all-missions required", file=sys.stderr)
        sys.exit(1)

    _next_task_for_mission(args.mission_id)


def _next_task_for_mission(mission_id: str) -> bool:
    """Find and print the next eligible task for a single mission.

    Returns True if a task was found, False if none available.
    """
    mission, _ = load_mission(mission_id)
    tasks = mission.get("tasks", [])

    for task_entry in tasks:
        task_id = task_entry.get("task_id")
        if not task_id:
            continue

        # Read task file from task_manager.py state dir to get live status
        task_file = TASK_STATE_DIR / f"task_{task_id}.json"
        if not task_file.exists():
            # Task file not found — treat as CREATED (not yet recorded by task_manager)
            task_status = "CREATED"
        else:
            try:
                with open(task_file) as f:
                    task_data = json.load(f)
                task_status = task_data.get("status", "CREATED")
            except (json.JSONDecodeError, OSError):
                task_status = "CREATED"

        # Skip finished tasks
        if task_status in DONE_TASK_STATUSES:
            continue

        # Skip tasks already in progress
        if task_status in IN_PROGRESS_STATUSES:
            continue

        # Eligible — return this task
        context_str = ""
        if task_file.exists():
            try:
                with open(task_file) as f:
                    task_data = json.load(f)
                ctx = task_data.get("context", {})
                context_str = f"complexity={ctx.get('complexity', '?')}, mission_goal={mission.get('goal', '')[:60]}"
            except (json.JSONDecodeError, OSError):
                context_str = f"mission_goal={mission.get('goal', '')[:60]}"
        else:
            context_str = f"mission_goal={mission.get('goal', '')[:60]}"

        print(f"NEXT_TASK: task_{task_id}")
        print(f"  Goal: {task_entry.get('goal', '')}")
        print(f"  Type: {task_entry.get('type', 'one-time')}")
        print(f"  Context: {context_str}")
        return True

    print(f"NO_TASKS_AVAILABLE: All tasks complete or in progress for mission_{mission_id}")
    return False


def _next_task_all_missions():
    """Find next eligible tasks across all active missions, ordered by priority.

    Returns up to 3 tasks (one per mission, highest priority first).
    """
    ledger = load_ledger()
    active = [e for e in ledger["missions"] if e.get("status") in ACTIVE_STATUSES]
    active.sort(key=lambda e: e.get("priority", 3))

    if not active:
        print("NO_ACTIVE_MISSIONS")
        return

    found = 0
    for entry in active:
        if found >= 3:
            break
        if _next_task_for_mission(entry["id"]):
            found += 1


# ---------------------------------------------------------------------------
# Recurring task re-creation
# ---------------------------------------------------------------------------

def check_recurring_tasks(mission: dict) -> None:
    """Check all recurring tasks; re-create via task_manager.py if cadence is due.

    Called at the end of update_kpi after every update cycle.
    """
    for task_entry in mission.get("tasks", []):
        if task_entry.get("type") != "recurring" or not task_entry.get("cadence"):
            continue

        task_id = task_entry.get("task_id")
        if not task_id:
            continue

        task_file = TASK_STATE_DIR / f"task_{task_id}.json"
        if not task_file.exists():
            continue

        try:
            with open(task_file) as f:
                task_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if task_data.get("status") != "DONE":
            continue

        completed_at = task_data.get("completed_at")
        if not completed_at:
            continue

        try:
            completed_ts = datetime.fromisoformat(completed_at).timestamp()
        except (ValueError, AttributeError):
            continue

        try:
            c = Croniter(task_entry["cadence"], completed_ts)
            if c.get_next() <= time.time():
                # Re-create the recurring task via task_manager.py
                cmd = [
                    sys.executable,
                    str(TM_PATH),
                    "create",
                    "--goal", task_entry["goal"],
                    "--mission-id", mission["id"],
                    "--source", "mission-subtask",
                    "--priority", str(mission.get("priority", 3)),
                ]
                if task_entry.get("requires_gpu"):
                    cmd.append("--requires-gpu")
                subprocess.run(cmd, capture_output=True, text=True)
        except Exception:
            # croniter errors (bad cron string, etc.) are non-fatal
            pass


# ---------------------------------------------------------------------------
# KPI update and lifecycle management (Plan 03)
# ---------------------------------------------------------------------------

def update_kpi(args):
    """Update KPI values for a mission and check for completion or stall.

    Args:
        --mission-id: Required. Mission ID to update.
        --kpi-metric: Optional. Metric name to update.
        --kpi-value:  Optional. New current value for the metric.
        --check-stall: Flag. Run stall detection based on task progress.
    """
    mission, path = load_mission(args.mission_id)

    # --- Update specific KPI value ---
    if getattr(args, "kpi_metric", None) and getattr(args, "kpi_value", None) is not None:
        metric_name = args.kpi_metric
        try:
            new_value = float(args.kpi_value)
        except (ValueError, TypeError):
            new_value = args.kpi_value

        for kpi in mission.get("kpis", []):
            if kpi["metric"] == metric_name:
                kpi["current"] = new_value
                # Mark as met if numeric target is reached
                target = kpi.get("target")
                if isinstance(target, (int, float)) and isinstance(new_value, (int, float)):
                    if new_value >= target:
                        kpi["met"] = True
                break

    # --- Check if ALL KPIs are met => COMPLETED ---
    kpis = mission.get("kpis", [])
    if kpis and all(k.get("met") for k in kpis):
        mission["status"] = "COMPLETED"
        mission["updated_at"] = now_iso()
        save_mission(mission, path)
        update_ledger(args.mission_id, mission["goal"], "COMPLETED", mission.get("priority", 3))
        # Archive the mission
        archive_args = type("A", (), {"mission_id": args.mission_id})()
        archive_mission(archive_args)
        print(f"MISSION_COMPLETE: mission_{args.mission_id} — all KPIs met")
        return

    # --- Stall detection ---
    if getattr(args, "check_stall", False):
        meta = _load_meta(args.mission_id)
        last_done_count = meta.get("last_done_count", 0)

        # Count tasks with DONE status from live task files
        current_done_count = 0
        for task_entry in mission.get("tasks", []):
            task_id = task_entry.get("task_id")
            if not task_id:
                continue
            task_file = TASK_STATE_DIR / f"task_{task_id}.json"
            if task_file.exists():
                try:
                    with open(task_file) as f:
                        task_data = json.load(f)
                    if task_data.get("status") in DONE_TASK_STATUSES:
                        current_done_count += 1
                except (json.JSONDecodeError, OSError):
                    pass

        if current_done_count > last_done_count:
            # Progress was made — reset stall counter
            mission["stall_count"] = 0
            meta["last_done_count"] = current_done_count
        else:
            # No new completions — increment stall
            mission["stall_count"] = mission.get("stall_count", 0) + 1

        if mission["stall_count"] >= 3:
            mission["status"] = "STALLED"
            print(f"MISSION_STALLED: mission_{args.mission_id} — "
                  f"{mission['stall_count']} heartbeats with no progress")
            print(f"ADAPT_RECOMMENDED: Run 'mission_engine.py adapt --mission-id {args.mission_id}'")

        _save_meta(args.mission_id, meta)

    # --- Recurring task re-creation ---
    check_recurring_tasks(mission)

    # --- Persist updated mission ---
    mission["updated_at"] = now_iso()
    save_mission(mission, path)

    # Build kpi_summary for ledger
    kpi_summary = "; ".join(
        f"{k['metric']}={k.get('current', 0)}/{k.get('target', 'TBD')}"
        for k in kpis
    ) if kpis else ""

    update_ledger(args.mission_id, mission["goal"], mission["status"],
                  mission.get("priority", 3), kpi_summary)


# ---------------------------------------------------------------------------
# Mission adaptation (Plan 03 — stall recovery via Sonnet replanning)
# ---------------------------------------------------------------------------

def _check_reanchor(mission: dict) -> str:
    """Build re-anchor language for the adapt prompt (anti-drift measure)."""
    original_goal = mission.get("original_goal", mission.get("goal", ""))
    return (
        f"IMPORTANT: Re-anchor on the original goal: '{original_goal}'. "
        "Do not drift toward proxy metrics. "
        "All new subtasks must directly advance the original goal."
    )


def adapt_mission(args):
    """Generate a revised task strategy via Sonnet when a mission is stalled.

    Steps:
    1. Load mission and build context (goal, strategy, completed/stalled tasks, KPIs).
    2. Call Sonnet with re-anchor directive.
    3. Cancel listed stalled tasks (annotate in mission tasks array).
    4. Create new subtasks via task_manager.py classify + create flow.
    5. Reset stall_count to 0, set status to ACTIVE, save.

    Args:
        --mission-id: Required. Mission ID to adapt.
    """
    mission, path = load_mission(args.mission_id)
    client = Anthropic()

    original_goal = mission.get("original_goal", mission.get("goal", ""))

    # --- Build context snapshot ---
    tasks = mission.get("tasks", [])
    completed_tasks = []
    stalled_tasks = []
    for task_entry in tasks:
        task_id = task_entry.get("task_id")
        task_file = TASK_STATE_DIR / f"task_{task_id}.json" if task_id else None
        status = "UNKNOWN"
        if task_file and task_file.exists():
            try:
                with open(task_file) as f:
                    td = json.load(f)
                status = td.get("status", "UNKNOWN")
            except (json.JSONDecodeError, OSError):
                pass
        if status in DONE_TASK_STATUSES:
            completed_tasks.append(task_entry.get("goal", ""))
        elif status not in IN_PROGRESS_STATUSES:
            stalled_tasks.append(task_entry.get("goal", ""))

    kpis = mission.get("kpis", [])
    kpi_summary = "; ".join(
        f"{k['metric']}={k.get('current', 0)}/{k.get('target', 'TBD')}"
        for k in kpis
    ) if kpis else "No KPIs tracked"

    reanchor = _check_reanchor(mission)

    prompt = (
        f"This mission has stalled for {mission.get('stall_count', 3)} heartbeats with no progress.\n\n"
        f"Original goal: {original_goal}\n"
        f"Current strategy: {mission.get('strategy', 'None')}\n"
        f"Completed tasks: {completed_tasks or ['None']}\n"
        f"Stalled/pending tasks: {stalled_tasks or ['None']}\n"
        f"Current KPI progress: {kpi_summary}\n\n"
        f"{reanchor}\n\n"
        "Generate a revised strategy with new subtasks to make progress. "
        "For each stalled task that is no longer relevant, include its task_id in cancel_task_ids. "
        "Return JSON matching the schema exactly."
    )

    result = client.beta.messages.parse(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        output_format=AdaptationOutput,
        messages=[{"role": "user", "content": prompt}],
    )
    adaptation = result.parsed

    # --- Cancel stalled tasks listed by Sonnet ---
    for cancel_id in adaptation.cancel_task_ids:
        for task_entry in mission["tasks"]:
            if task_entry.get("task_id") == cancel_id:
                task_entry["status"] = "CANCELLED"

    # --- Create new tasks from new_subtasks ---
    new_task_entries = []
    for subtask_str in adaptation.new_subtasks:
        classification = classify_task(subtask_str)
        enrichment = _enrich_subtask(subtask_str)

        context_obj = {
            "complexity": enrichment.complexity,
            "mission_goal": original_goal,
        }
        cmd = [
            sys.executable,
            str(TM_PATH),
            "create",
            "--goal", subtask_str,
            "--mission-id", args.mission_id,
            "--source", "mission-subtask",
            "--priority", str(mission.get("priority", 3)),
            "--context", json.dumps(context_obj),
        ]
        if enrichment.requires_gpu:
            cmd.append("--requires-gpu")

        proc = subprocess.run(cmd, capture_output=True, text=True)

        task_id = None
        if proc.returncode in (0, 2):
            for line in proc.stdout.splitlines():
                if line.startswith("CREATED:"):
                    token = line.split(":", 1)[1].strip()
                    task_id = token.split("_", 1)[1] if "_" in token else token
                    break

        new_task_entries.append({
            "task_id": task_id or short_id(),
            "goal": subtask_str,
            "type": classification.task_type,
            "cadence": classification.cadence,
            "status": "CREATED",
            "requires_gpu": enrichment.requires_gpu,
        })

    # --- Update mission ---
    mission["tasks"] = mission["tasks"] + new_task_entries
    mission["strategy"] = adaptation.revised_strategy
    mission["stall_count"] = 0
    mission["status"] = "ACTIVE"
    mission["updated_at"] = now_iso()

    save_mission(mission, path)
    update_ledger(args.mission_id, mission["goal"], "ACTIVE", mission.get("priority", 3))

    print(f"MISSION_ADAPTED: mission_{args.mission_id}")
    print(f"  New tasks: {len(new_task_entries)}")
    print(f"  Cancelled: {len(adaptation.cancel_task_ids)}")
    print(f"  Strategy: {adaptation.revised_strategy[:80]}")


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

    # --- decompose ---
    p_decompose = sub.add_parser("decompose", help="Decompose mission into tasks via LLM (Sonnet + Qwen3)")
    p_decompose.add_argument("--mission-id", required=True, help="Mission ID to decompose")

    # --- classify ---
    p_classify = sub.add_parser("classify", help="Classify a task description as one-time or recurring")
    p_classify.add_argument("--description", required=True, help="Task description to classify")

    # --- update-kpi ---
    p_kpi = sub.add_parser("update-kpi", help="Update KPI values for a mission and check lifecycle")
    p_kpi.add_argument("--mission-id", required=True, help="Mission ID to update")
    p_kpi.add_argument("--kpi-metric", help="Metric name to update (e.g. gsc_clicks)")
    p_kpi.add_argument("--kpi-value", help="New current value for the metric")
    p_kpi.add_argument("--check-stall", action="store_true",
                       help="Run stall detection based on task progress")

    # --- adapt ---
    p_adapt = sub.add_parser("adapt", help="Adapt mission strategy when stalled (calls Sonnet)")
    p_adapt.add_argument("--mission-id", required=True, help="Mission ID to adapt")

    # --- next-task ---
    p_next = sub.add_parser("next-task", help="Get next eligible task for a mission (dependency order)")
    next_group = p_next.add_mutually_exclusive_group()
    next_group.add_argument("--mission-id", help="Mission ID to get next task for")
    next_group.add_argument("--all-missions", action="store_true",
                            help="Get next task across all active missions (up to 3, by priority)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    def _not_implemented(args):
        print("NOT_IMPLEMENTED")
        sys.exit(1)

    commands = {
        "create": create_mission,
        "status": mission_status,
        "archive": archive_mission,
        "decompose": decompose_mission,
        "classify": classify_subcommand,
        "update-kpi": update_kpi,
        "adapt": adapt_mission,
        "next-task": next_task,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
