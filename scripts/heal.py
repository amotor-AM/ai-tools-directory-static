#!/usr/bin/env python3
"""Aria self-healing coordinator — 4-tier recovery stack.

This module handles the first two tiers of the recovery stack:

  Tier 1  — Transient failure: exponential backoff retry via tenacity.
  Tier 2  — Approach failure: switch to an alternative strategy.
  Tier 3  — Capability / model fallback (added in Plan 03).
  Tier 4  — Escalate to Claude Code (added in Plan 03).

Exit codes (per research Pattern 1):
  0  — success / recovery dispatched
  1  — failed, try next tier
  2  — all tiers exhausted
  3  — circuit breaker open, do not attempt

Usage (CLI):
  python3 heal.py attempt --task-id <id> [--tier 1|2] [--auto]
  python3 heal.py classify --error "rate limit exceeded"
  python3 heal.py status --task-id <id>
"""

import argparse
import json
import subprocess
import sys
import time

from tenacity import retry, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TM_PATH = "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py"
OUTCOME_TRACKER = "/home/alex/.openclaw/workspace/scripts/outcome_tracker.py"
MANAGE_PATH = "/home/alex/.openclaw/workspace/agents/manage.py"

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
# Tier 1 — Exponential backoff retry
# ---------------------------------------------------------------------------


def tier1_retry(task_id: str, task: dict) -> int:
    """Tier 1: exponential backoff retry with a fresh strategy.

    Determines a retry strategy from classify_error + get_alternative.
    If the proposed strategy is identical to the task's current retry_strategy,
    there is nothing new to try — return 1 (caller should escalate to tier 2).

    Otherwise, calls _do_retry_subprocess() which uses tenacity @retry with
    stop_after_attempt(3) and wait_exponential(multiplier=2, min=4, max=60).

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
        return 0
    except Exception:
        return 1


# ---------------------------------------------------------------------------
# Tier 2 — Alternative approach
# ---------------------------------------------------------------------------


def tier2_alternative(task_id: str, task: dict) -> int:
    """Tier 2: switch to a concrete alternative strategy.

    Gets the alternative from get_alternative() and calls task_manager.py retry
    with that strategy.

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
        return 0
    except subprocess.CalledProcessError:
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_attempt(args) -> int:
    task = _load_task(args.task_id)
    if not task:
        print(f"ERROR: Could not load task {args.task_id}", file=sys.stderr)
        return 1

    if args.auto:
        tier = select_tier(task)
    elif args.tier:
        tier = args.tier
    else:
        tier = select_tier(task)

    print(f"Dispatching tier {tier} recovery for task {args.task_id}")

    if tier == 1:
        return tier1_retry(args.task_id, task)
    elif tier == 2:
        return tier2_alternative(args.task_id, task)
    elif tier == 3:
        print("Tier 3 (model fallback) not implemented yet — planned for Plan 03")
        return 1
    elif tier == 4:
        print("Tier 4 (Claude Code escalation) not implemented yet — planned for Plan 03")
        return 1
    else:
        print(f"Unknown tier: {tier}", file=sys.stderr)
        return 1


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aria self-healing coordinator — dispatch recovery tiers for failing tasks"
    )
    sub = parser.add_subparsers(dest="command")

    # attempt
    p_attempt = sub.add_parser("attempt", help="Attempt recovery for a task")
    p_attempt.add_argument("--task-id", required=True, help="Task ID (e.g. abc123)")
    tier_group = p_attempt.add_mutually_exclusive_group()
    tier_group.add_argument("--tier", type=int, choices=[1, 2, 3, 4],
                            help="Force a specific tier (1-4)")
    tier_group.add_argument("--auto", action="store_true",
                            help="Auto-select tier from task state")

    # classify
    p_classify = sub.add_parser("classify", help="Classify an error string")
    p_classify.add_argument("--error", required=True, help="Error string to classify")

    # status
    p_status = sub.add_parser("status", help="Show tier selection for a task")
    p_status.add_argument("--task-id", required=True, help="Task ID")

    args = parser.parse_args()

    if args.command == "attempt":
        sys.exit(_cmd_attempt(args))
    elif args.command == "classify":
        sys.exit(_cmd_classify(args))
    elif args.command == "status":
        sys.exit(_cmd_status(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
