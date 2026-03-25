#!/usr/bin/env python3
"""Aria self-healing coordinator — 4-tier + skip recovery stack.

Recovery tiers:
  Tier 1  — Transient failure: exponential backoff retry via tenacity.
  Tier 2  — Approach failure: switch to an alternative strategy.
  Tier 3  — Capability / model fallback: walk Sonnet->Qwen3->GLM, then delegate.
  Tier 4  — Escalate to Claude Code via task_manager.py escalate (no Telegram).
  Tier 5  — Skip: cancel task and log failure (never contacts Alex).

Exit codes (per research Pattern 1):
  0  — success / recovery dispatched
  1  — failed, try next tier
  2  — all tiers exhausted
  3  — circuit breaker open, do not attempt

Usage (CLI):
  python3 heal.py attempt --task-id <id> [--tier 1|2|3|4|5] [--auto]
  python3 heal.py classify --error "rate limit exceeded"
  python3 heal.py status --task-id <id>
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TM_PATH = "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py"
OUTCOME_TRACKER = "/home/alex/.openclaw/workspace/scripts/outcome_tracker.py"
MANAGE_PATH = "/home/alex/.openclaw/workspace/agents/manage.py"
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Rollback registry — append-only JSONL (AUTO-09)
ROLLBACK_REGISTRY = Path(os.environ.get(
    "ROLLBACK_REGISTRY_PATH",
    "/home/alex/.openclaw/workspace/memory/audit/rollback_registry.jsonl"
))

# Known reversible action types and their rollback command templates
REVERSIBLE_ACTION_TYPES = {
    "vercel_deploy": "vercel rollback --yes",
    "wordpress_post": "python3 {scripts_dir}/wordpress.py delete --post-id {post_id}",
    "wordpress_publish": "python3 {scripts_dir}/wordpress.py unpublish --post-id {post_id}",
}

# ---------------------------------------------------------------------------
# Circuit breaker imports (lazy path setup for portability)
# ---------------------------------------------------------------------------

# Add scripts/ to sys.path so circuit_breaker can be imported directly.
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from circuit_breaker import is_open, record_failure  # noqa: E402

# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

ERROR_CLASSES: dict[str, list[str]] = {
    "transient": [
        "rate limit", "429", "503", "connection", "timeout", "network",
        "temporary", "overloaded", "too many requests", "socket",
    ],
    "approach": [
        "not found", "404", "dom", "selector", "captcha", "auth",
        "permission denied", "forbidden", "element", "locator",
    ],
    "capability": [
        "not implemented", "cannot", "context window", "token limit",
        "unsupported", "out of memory", "cuda", "model",
    ],
    "permanent": [
        "invalid", "schema", "json decode", "syntax error",
        "type error", "assertion", "validation",
    ],
}


def classify_error(error_str: str) -> str:
    """Classify an error string into one of: transient, approach, capability, permanent, unknown.

    Checks in priority order: transient first (so rate-limit/network errors don't fall through
    to approach or permanent), then approach, capability, permanent.  Returns "unknown" if no
    pattern matches.
    """
    lower = error_str.lower()
    for class_name in ("transient", "approach", "capability", "permanent"):
        for pattern in ERROR_CLASSES[class_name]:
            if pattern in lower:
                return class_name
    return "unknown"


# ---------------------------------------------------------------------------
# Alternative strategy mapping
# ---------------------------------------------------------------------------

ALTERNATIVES: dict[str, str] = {
    "rate_limit":    "wait 60s then retry with reduced request frequency",
    "captcha":       "switch to stealth_browser.py with CapSolver",
    "auth":          "refresh credentials from keychain then retry",
    "timeout":       "increase timeout to 120s or use streaming variant",
    "dom_not_found": "use browse.py visual mode instead of Playwright direct",
    "api_error":     "switch to browser automation as fallback",
    "not_found":     "verify URL/endpoint exists, try alternative path",
    "permission":    "check API key scope, refresh token",
}

# Maps substrings in error message to ALTERNATIVES keys
_ALT_PATTERN_MAP: list[tuple[str, str]] = [
    ("captcha",        "captcha"),
    ("rate limit",     "rate_limit"),
    ("429",            "rate_limit"),
    ("auth",           "auth"),
    ("permission",     "permission"),
    ("forbidden",      "permission"),
    ("timeout",        "timeout"),
    # DOM/selector patterns before generic "not found" so "element not found"
    # matches dom_not_found rather than falling through to not_found
    ("element",        "dom_not_found"),
    ("dom",            "dom_not_found"),
    ("selector",       "dom_not_found"),
    ("locator",        "dom_not_found"),
    ("not found",      "not_found"),
    ("404",            "not_found"),
    ("api",            "api_error"),
    ("connection",     "api_error"),
]


def get_alternative(error_str: str) -> str:
    """Return a concrete alternative strategy for the given error string.

    Matches error_str against known patterns in order.  Falls back to a
    generic hint when no pattern matches.
    """
    lower = error_str.lower()
    for pattern, alt_key in _ALT_PATTERN_MAP:
        if pattern in lower:
            return ALTERNATIVES[alt_key]
    return "retry with modified parameters and increased verbosity"


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


def select_tier(task: dict) -> int:
    """Determine which recovery tier to use for the given task.

    Logic (from research Pattern 2):
      - capability error  → tier 3 immediately (model swap needed)
      - transient + few errors → tier 1 (exponential backoff)
      - approach or many errors → tier 2 (different strategy)
      - exhausted (blocked heartbeats or max attempts) → tier 4
      - anything else → tier 3 (model fallback)
    """
    last_error = task.get("last_error", "") or ""
    error_class = classify_error(last_error)

    if error_class == "capability":
        return 3

    consec = task.get("consecutive_step_errors", 0)
    blocked = task.get("blocked_heartbeats", 0)
    attempts = task.get("attempts", 0)
    max_attempts = task.get("max_attempts", 15)

    if blocked >= 4 or attempts >= max_attempts:
        return 4

    if error_class == "transient" and consec <= 2:
        return 1

    if consec <= 3 or error_class == "approach":
        return 2

    return 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_task(task_id: str) -> dict:
    """Load task state from task_manager.py show."""
    result = subprocess.run(
        ["python3", TM_PATH, "show", task_id],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=60), reraise=True)
def _do_retry_subprocess(task_id: str, strategy: str) -> None:
    """Subprocess retry with tenacity exponential backoff.

    Wraps the subprocess call to task_manager.py retry in tenacity's @retry
    decorator.  Because subprocess.run() does not raise by default, we pass
    check=True so that a non-zero exit code raises CalledProcessError, which
    tenacity then catches and retries.
    """
    subprocess.run(
        ["python3", TM_PATH, "retry", task_id, "--strategy", strategy],
        check=True,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Outcome recording helper  (HEAL-07) — defined early so all tiers can call it
# ---------------------------------------------------------------------------


def _record_outcome(task: dict, tier: int, action: str, success: bool) -> None:
    """Record a recovery attempt to outcome_tracker.py (HEAL-07).

    approach format: heal_tier{N}_{action}
    task_type inferred from context.task_type or goal keywords.
    """
    task_type = (task.get("context") or {}).get("task_type", "unknown")
    if task_type == "unknown":
        goal = (task.get("goal") or "").lower()
        if "article" in goal:
            task_type = "article"
        elif "video" in goal:
            task_type = "video"
        elif "book" in goal:
            task_type = "book"
        else:
            task_type = "general"

    approach = f"heal_tier{tier}_{action}"
    outcome = "success" if success else "failure"
    last_error = (task.get("last_error") or "")[:80]
    task_id = task.get("id", "?")

    subprocess.run(
        [
            "python3", OUTCOME_TRACKER, "record",
            "--task-type", task_type,
            "--outcome", outcome,
            "--approach", approach,
            "--notes", f"Task {task_id}: {last_error}",
        ],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Tier 1 — Exponential backoff retry
# ---------------------------------------------------------------------------


def tier1_retry(task_id: str, task: dict) -> int:
    """Tier 1: exponential backoff retry with a fresh strategy.

    Determines a retry strategy from classify_error + get_alternative.
    If the proposed strategy is identical to the task's current retry_strategy,
    there is nothing new to try — return 1 (caller should escalate to tier 2).

    Otherwise, calls _do_retry_subprocess() which uses tenacity @retry with
    stop_after_attempt(3) and wait_exponential(multiplier=2, min=4, max=60).

    Records outcome to outcome_tracker.py (HEAL-07).

    Returns:
        0  — retry dispatched successfully
        1  — strategy unchanged or all tenacity attempts exhausted
    """
    last_error = task.get("last_error", "") or ""
    strategy = get_alternative(last_error)

    current_strategy = task.get("retry_strategy")
    if current_strategy and strategy == current_strategy:
        # No different strategy available — skip to next tier
        return 1

    try:
        _do_retry_subprocess(task_id, strategy)
        _record_outcome(task, 1, "backoff", True)
        return 0
    except Exception:
        _record_outcome(task, 1, "backoff", False)
        return 1


# ---------------------------------------------------------------------------
# Tier 2 — Alternative approach
# ---------------------------------------------------------------------------


def tier2_alternative(task_id: str, task: dict) -> int:
    """Tier 2: switch to a concrete alternative strategy.

    Gets the alternative from get_alternative() and calls task_manager.py retry
    with that strategy. Records outcome to outcome_tracker.py (HEAL-07).

    Returns:
        0  — alternative dispatched
        1  — subprocess failed
    """
    last_error = task.get("last_error", "") or ""
    alternative = get_alternative(last_error)

    try:
        subprocess.run(
            ["python3", TM_PATH, "retry", task_id, "--strategy", alternative],
            check=True,
            capture_output=True,
            text=True,
        )
        _record_outcome(task, 2, "alternative_approach", True)
        return 0
    except subprocess.CalledProcessError:
        _record_outcome(task, 2, "alternative_approach", False)
        return 1


# ---------------------------------------------------------------------------
# Tier 3 — Model fallback chain + delegation
# ---------------------------------------------------------------------------

MODEL_FALLBACK_CHAIN: list[str] = [
    "anthropic/claude-sonnet-4-5",
    "qwen3:14b",
    "huihui_ai/glm-4.7-flash-abliterated",
]

# Maps task goal keywords / task types to specialist agent names in manage.py registry
_AGENT_KEYWORD_MAP: list[tuple[str, str]] = [
    ("article", "content"),
    ("seo", "seo"),
    ("browser", "web"),
    ("web", "web"),
    ("scrape", "web"),
    ("image", "image"),
    ("video", "video"),
    ("book", "content"),
    ("publish", "publish"),
    ("reddit", "social"),
    ("social", "social"),
    ("research", "research"),
    ("code", "code"),
]


def _next_model(current: str, chain: list[str] = MODEL_FALLBACK_CHAIN) -> str | None:
    """Return the next model in the fallback chain after current.

    Returns None if current is the last model in the chain (or not found).
    """
    try:
        idx = chain.index(current)
    except ValueError:
        # Unknown model — start from beginning so caller gets first fallback
        return chain[0] if chain else None
    if idx + 1 < len(chain):
        return chain[idx + 1]
    return None


def _infer_agent(task: dict) -> str | None:
    """Infer a specialist agent name from the task's goal or task_type.

    Matches goal text against _AGENT_KEYWORD_MAP in order.
    Returns None if no specialist match found.
    """
    goal = (task.get("goal") or "").lower()
    task_type = (task.get("context", {}).get("task_type") or "").lower()
    combined = f"{goal} {task_type}"
    for keyword, agent in _AGENT_KEYWORD_MAP:
        if keyword in combined:
            return agent
    return None


def tier3_model_fallback(task_id: str, task: dict) -> int:
    """Tier 3: walk the MODEL_FALLBACK_CHAIN then attempt delegation.

    Step 1: Get the current model from task["context"].get("model").
            Default = MODEL_FALLBACK_CHAIN[0] if not set.
    Step 2: Get the next model via _next_model().
    Step 3: If a next model exists, call task_manager.py retry with strategy
            "model_fallback:{next_model}" and return 0.
    Step 4: If no next model (chain exhausted), attempt delegation:
            - Infer specialist agent via _infer_agent()
            - If no agent found: return 1
            - Check circuit breaker via is_open(agent, task_type)
            - If open: return 3 (breaker-blocked)
            - Call manage.py spawn agent --task <goal> [--mission-id <id>]
            - On success: call task_manager.py delegate, return 0
            - On failure: call record_failure(agent, task_type), return 1

    Returns:
        0  — model switched or delegation dispatched
        1  — no suitable agent / delegation subprocess failed
        3  — circuit breaker open
    """
    context = task.get("context") or {}
    current_model = context.get("model", MODEL_FALLBACK_CHAIN[0])
    task_type = context.get("task_type", "general")

    next_model = _next_model(current_model)

    if next_model is not None:
        # Switch to next model in chain
        strategy = f"model_fallback:{next_model}"
        # Use short model name for approach string (last segment after /)
        short_model = next_model.split("/")[-1].split(":")[0]
        try:
            subprocess.run(
                ["python3", TM_PATH, "retry", task_id, "--strategy", strategy],
                check=True,
                capture_output=True,
                text=True,
            )
            _record_outcome(task, 3, f"model_fallback:{short_model}", True)
            return 0
        except subprocess.CalledProcessError:
            _record_outcome(task, 3, f"model_fallback:{short_model}", False)
            return 1

    # All models exhausted — try delegation
    agent = _infer_agent(task)
    if agent is None:
        _record_outcome(task, 3, "delegation:no_agent", False)
        return 1

    if is_open(agent, task_type):
        _record_outcome(task, 3, f"delegation:{agent}:breaker_open", False)
        return 3

    # Spawn specialist agent
    goal = task.get("goal", "")
    spawn_cmd = ["python3", MANAGE_PATH, "spawn", agent, "--task", goal]
    mission_id = context.get("mission_id")
    if mission_id:
        spawn_cmd += ["--mission-id", str(mission_id)]

    try:
        subprocess.run(spawn_cmd, check=True, capture_output=True, text=True)
        # Mark task as delegated
        subprocess.run(
            ["python3", TM_PATH, "delegate", task_id, "--session-id", f"{agent}-delegated"],
            capture_output=True,
            text=True,
        )
        _record_outcome(task, 3, f"delegation:{agent}", True)
        return 0
    except subprocess.CalledProcessError:
        record_failure(agent, task_type)
        _record_outcome(task, 3, f"delegation:{agent}", False)
        return 1


# ---------------------------------------------------------------------------
# Tier 4 — Escalation (generates Claude Code handoff brief)
# ---------------------------------------------------------------------------


def tier4_escalate(task_id: str, task: dict) -> int:
    """Tier 4: escalate to Claude Code by calling task_manager.py escalate.

    Generates a structured handoff brief for the next human/agent to resolve.
    Does NOT send any Telegram message — escalation is file-based only.
    Records outcome approach "heal_tier4_escalated" to outcome_tracker.py.

    Returns:
        0  — escalation dispatched successfully
        1  — subprocess failed
    """
    _TIER4_ACTION = "heal_tier4_escalated"  # approach string for outcome recording
    try:
        subprocess.run(
            ["python3", TM_PATH, "escalate", task_id],
            capture_output=True,
            text=True,
        )
        _record_outcome(task, 4, "escalated", True)
        return 0
    except Exception:
        _record_outcome(task, 4, "escalated", False)
        return 1


# ---------------------------------------------------------------------------
# Tier 5 — Skip + log  (never contacts Alex)
# ---------------------------------------------------------------------------


def tier5_skip(task_id: str, task: dict) -> int:
    """Tier 5: cancel the task and log failure — all recovery tiers exhausted.

    Calls task_manager.py cancel with reason "all recovery tiers exhausted".
    Records outcome as failure with approach "heal_tier5_skipped".
    NEVER contacts Alex or sends Telegram messages.

    Returns:
        2  — all tiers exhausted (canonical exit code for exhaustion)
    """
    _TIER5_ACTION = "heal_tier5_skipped"  # approach string for outcome recording
    subprocess.run(
        [
            "python3", TM_PATH, "cancel", task_id,
            "--reason", "all recovery tiers exhausted",
        ],
        capture_output=True,
        text=True,
    )
    _record_outcome(task, 5, "skipped", False)
    return 2


# ---------------------------------------------------------------------------
# Dispatch helper + auto attempt
# ---------------------------------------------------------------------------


def _dispatch_tier(tier: int, task_id: str, task: dict) -> int:
    """Dispatch recovery to the specified tier function.

    Returns the tier function's exit code.
    """
    if tier == 1:
        return tier1_retry(task_id, task)
    elif tier == 2:
        return tier2_alternative(task_id, task)
    elif tier == 3:
        return tier3_model_fallback(task_id, task)
    elif tier == 4:
        return tier4_escalate(task_id, task)
    elif tier == 5:
        return tier5_skip(task_id, task)
    else:
        return 1


def attempt(task_id: str, tier: int | None = None) -> int:
    """Attempt recovery for the given task.

    If tier is specified, run that tier directly.
    If tier is None (auto mode), walk the tier ladder starting from select_tier():
      - Run each tier in order
      - If tier returns 1 (failed, try next): RELOAD task state via _load_task(task_id)
        before invoking the next tier. This is CRITICAL because tier N modifies task
        state (retry_strategy, consecutive_step_errors, error_history) via task_manager.py
        and the next tier must see the updated state.
      - Stop if result != 1 (0=success, 2=exhausted, 3=breaker-open)

    Returns:
        0  — recovery dispatched successfully
        1  — failed (specific tier requested and failed)
        2  — all tiers exhausted
        3  — circuit breaker open
    """
    task = _load_task(task_id)
    if not task:
        return 1

    if tier is not None:
        return _dispatch_tier(tier, task_id, task)

    # Auto mode: walk tiers with state reload between each escalation
    selected = select_tier(task)
    for t in range(selected, 6):  # tiers 1-5
        result = _dispatch_tier(t, task_id, task)
        if result != 1:  # 0=success, 2=exhausted, 3=breaker-open
            return result
        # RELOAD task state before next tier — tier t modified it via task_manager.py
        task = _load_task(task_id)

    return 2  # all tiers exhausted


# ---------------------------------------------------------------------------
# Rollback registry (AUTO-09)
# ---------------------------------------------------------------------------


def register_rollback(task_id: str, action_type: str, rollback_cmd: str, reversible: bool = True) -> None:
    """Register a rollback command for a completed action.

    Called by supervisor.py validate_task() on PASS when the action has
    a known rollback procedure. For non-reversible actions, stores
    reversible=False so execute_rollback() can bail cleanly.

    Appends to ROLLBACK_REGISTRY (append-only JSONL, O_APPEND atomic writes).
    """
    # Re-read ROLLBACK_REGISTRY_PATH at call time so env var overrides take effect
    registry = Path(os.environ.get("ROLLBACK_REGISTRY_PATH", str(ROLLBACK_REGISTRY)))
    registry.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "action_type": action_type,
        "rollback_cmd": rollback_cmd,
        "reversible": reversible,
        "status": "available",
    })
    fd = os.open(str(registry), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (entry + "\n").encode())
    finally:
        os.close(fd)


def execute_rollback(task_id: str) -> tuple:
    """Find and execute the rollback command for a task.

    Returns (success: bool, reason: str).
    success=True means rollback command was executed successfully.
    Reasons for False: NO_ROLLBACK_REGISTERED, NOT_REVERSIBLE, ALREADY_ROLLED_BACK,
                       NO_ROLLBACK_CMD, ROLLBACK_FAILED.
    """
    # Re-read ROLLBACK_REGISTRY_PATH at call time so env var overrides take effect
    registry = Path(os.environ.get("ROLLBACK_REGISTRY_PATH", str(ROLLBACK_REGISTRY)))
    if not registry.exists():
        return False, "NO_ROLLBACK_REGISTERED"

    entries = []
    for line in registry.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Find the most recent entry for this task_id
    matching = [e for e in entries if e.get("task_id") == task_id]
    if not matching:
        return False, "NO_ROLLBACK_REGISTERED"

    entry = matching[-1]  # most recent

    if entry.get("status") == "rolled_back":
        return False, "ALREADY_ROLLED_BACK"
    if not entry.get("reversible", False):
        return False, "NOT_REVERSIBLE"

    rollback_cmd = entry.get("rollback_cmd", "")
    if not rollback_cmd:
        return False, "NO_ROLLBACK_CMD"

    # Execute the rollback command
    result = subprocess.run(
        rollback_cmd, shell=True, capture_output=True, text=True, timeout=60
    )

    # Mark as rolled back by appending a new status entry
    mark_entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "action_type": entry.get("action_type", ""),
        "rollback_cmd": rollback_cmd,
        "reversible": True,
        "status": "rolled_back",
        "rollback_exit_code": result.returncode,
    })
    fd = os.open(str(registry), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (mark_entry + "\n").encode())
    finally:
        os.close(fd)

    if result.returncode == 0:
        _record_outcome({"id": task_id}, tier=0, action="rollback", success=True)
        return True, ""
    else:
        _record_outcome({"id": task_id}, tier=0, action="rollback", success=False)
        return False, f"ROLLBACK_FAILED: exit {result.returncode}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_attempt(args) -> int:
    if args.auto:
        return attempt(args.task_id, tier=None)
    elif args.tier:
        return attempt(args.task_id, tier=args.tier)
    else:
        return attempt(args.task_id, tier=None)


def _cmd_classify(args) -> int:
    result = classify_error(args.error)
    print(result)
    return 0


def _cmd_status(args) -> int:
    task = _load_task(args.task_id)
    if not task:
        print(f"ERROR: Could not load task {args.task_id}", file=sys.stderr)
        return 1
    tier = select_tier(task)
    error_class = classify_error(task.get("last_error", "") or "")
    print(json.dumps({
        "task_id": args.task_id,
        "error_class": error_class,
        "selected_tier": tier,
        "consecutive_step_errors": task.get("consecutive_step_errors", 0),
        "blocked_heartbeats": task.get("blocked_heartbeats", 0),
        "attempts": task.get("attempts", 0),
        "max_attempts": task.get("max_attempts", 15),
    }, indent=2))
    return 0


def _cmd_rollback(args) -> int:
    success, reason = execute_rollback(args.task_id)
    if success:
        print(f"ROLLBACK_OK: Task {args.task_id} rolled back successfully")
        return 0
    else:
        print(f"ROLLBACK_FAILED: {reason}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aria self-healing coordinator — dispatch recovery tiers for failing tasks"
    )
    sub = parser.add_subparsers(dest="command")

    # attempt
    p_attempt = sub.add_parser("attempt", help="Attempt recovery for a task")
    p_attempt.add_argument("--task-id", required=True, help="Task ID (e.g. abc123)")
    tier_group = p_attempt.add_mutually_exclusive_group()
    tier_group.add_argument("--tier", type=int, choices=[1, 2, 3, 4, 5],
                            help="Force a specific tier (1-5)")
    tier_group.add_argument("--auto", action="store_true",
                            help="Auto-select tier from task state")

    # classify
    p_classify = sub.add_parser("classify", help="Classify an error string")
    p_classify.add_argument("--error", required=True, help="Error string to classify")

    # status
    p_status = sub.add_parser("status", help="Show tier selection for a task")
    p_status.add_argument("--task-id", required=True, help="Task ID")

    # rollback
    p_rollback = sub.add_parser("rollback", help="Execute rollback for a task")
    p_rollback.add_argument("--task-id", required=True, help="Task ID to roll back")

    args = parser.parse_args()

    if args.command == "attempt":
        sys.exit(_cmd_attempt(args))
    elif args.command == "classify":
        sys.exit(_cmd_classify(args))
    elif args.command == "status":
        sys.exit(_cmd_status(args))
    elif args.command == "rollback":
        sys.exit(_cmd_rollback(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
