#!/usr/bin/env python3
"""Daily briefing engine — Aria's evening status delivery to Alex.

Subcommands:
  should-send   Exit 0 if 8-10 PM Pacific and not yet sent today; exit 1 otherwise.
  generate      Print formatted briefing text to stdout (no send).
  send          Full pipeline: generate + validate + send to Telegram + mark-sent.
  mark-sent     Mark today as sent without sending (for manual sends).
  alert         Send emergency alert immediately, bypassing time window.

Usage:
  python3 briefing.py should-send
  python3 briefing.py generate
  python3 briefing.py send
  python3 briefing.py mark-sent
  python3 briefing.py alert --text "Need Stripe key" --category credentials_needed

Environment variables for test isolation (mirrors Phase 2/3 pattern):
  BRIEFING_STATE_PATH  — override path to briefing-state.json
  ARIA_TASK_DIR        — override path to task state directory
  MISSIONS_DIR         — override path to missions directory
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Audit log_action import (AUTO-05) — graceful fallback if supervisor unavailable
# ---------------------------------------------------------------------------

try:
    from supervisor import log_action
except ImportError:
    def log_action(op, data):
        pass  # Graceful fallback if supervisor.py not available

# ---------------------------------------------------------------------------
# Path setup — add file_lock to sys.path
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent
_FILELOCK_DIR = _SCRIPTS_DIR.parent / "skills" / "aria-taskmanager" / "scripts"
if str(_FILELOCK_DIR) not in sys.path:
    sys.path.insert(0, str(_FILELOCK_DIR))

from file_lock import FileLock  # noqa: E402

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from output_schema import DailyBriefing, AlertMessage, HumanMessage  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACIFIC = ZoneInfo("America/Los_Angeles")
ALEX_CHAT_ID = 6142452416
DASHBOARD_URL = "http://localhost:8080"
MAX_SAFE = 3900  # truncate at 3900 to leave room for suffix within 4096

# Paths — all overridable via env vars for test isolation
_BASE_DIR = Path(os.environ.get("BRIEFING_BASE_DIR", "/home/alex/.openclaw/workspace"))
_MEMORY_DIR = _BASE_DIR / "memory"
_GROWTH_DIR = _MEMORY_DIR / "growth"

BRIEFING_STATE = Path(os.environ.get("BRIEFING_STATE_PATH", str(_GROWTH_DIR / "briefing-state.json")))
TASKS_DIR = Path(os.environ.get("ARIA_TASK_DIR", str(_MEMORY_DIR / "tasks" / "state")))
MISSIONS_DIR = Path(os.environ.get("MISSIONS_DIR", str(_MEMORY_DIR / "missions")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_pacific() -> datetime:
    """Return current DST-aware Pacific time via ZoneInfo — not hardcoded UTC-7."""
    return datetime.now(PACIFIC)


def load_json(path, default=None):
    """Safe JSON loader — returns default on missing or corrupt files."""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default if default is not None else {}


def atomic_write(path: Path, data: dict) -> None:
    """Write JSON to a temp file then rename for atomicity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_bot_token() -> str:
    """Read Telegram bot token from openclaw.json at runtime."""
    config_path = Path("/home/alex/.openclaw/openclaw.json")
    try:
        config = json.loads(config_path.read_text())
        return config["channels"]["telegram"]["botToken"]
    except (KeyError, json.JSONDecodeError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _read_todays_violations() -> list:
    """Read today's BLOCKED entries from supervisor.jsonl, formatted for briefing.

    Returns list of human-readable strings like "Blocked: {action} ({reason})".
    Capped at 5 entries to prevent briefing overflow (AUTO-07).
    """
    today = now_pacific().date().isoformat()
    audit_path = Path(os.environ.get(
        "SUPERVISOR_AUDIT_LOG",
        "/home/alex/.openclaw/workspace/memory/audit/supervisor.jsonl"
    ))
    if not audit_path.exists():
        return []
    violations = []
    for line in audit_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if (record.get("op") == "pre_check" and
                    record.get("data", {}).get("result") == "BLOCKED" and
                    record.get("ts", "").startswith(today)):
                action = record["data"].get("action", "unknown")[:60]
                reason = record["data"].get("reason", "")[:40]
                violations.append(f"Blocked: {action} ({reason})")
        except json.JSONDecodeError:
            continue
    return violations[:5]  # Cap at 5 to prevent briefing overflow


def should_send() -> bool:
    """Return True if in 8-10 PM Pacific window and not yet sent today."""
    now = now_pacific()
    hour = now.hour
    if hour < 20 or hour >= 22:
        return False
    state = load_json(BRIEFING_STATE)
    today_str = now.strftime("%Y-%m-%d")
    if state.get("last_sent_date", "") == today_str:
        return False
    return True


def collect_briefing_data(task_dir: Path, missions_dir: Path) -> dict:
    """Aggregate data from task state files and mission ledger.

    Returns dict with: done, active, action_items, tomorrow, done_count.
    """
    now = now_pacific()
    today_str = now.strftime("%Y-%m-%d")

    done_today = []
    action_items = []

    # Read task state files
    task_dir = Path(task_dir)
    if task_dir.exists():
        for task_file in sorted(task_dir.glob("task_*.json")):
            try:
                task = json.loads(task_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            status = task.get("status", "")
            goal = (task.get("goal") or "?")[:60]

            # DONE tasks completed today
            if status in {"DONE", "COMPLETED"}:
                completed_at = task.get("completed_at", "") or ""
                if completed_at[:10] == today_str:
                    done_today.append(goal)

            # Escalated/blocked tasks need Alex
            if status in {"ESCALATED", "BLOCKED"}:
                error = (task.get("last_error") or "?")[:30]
                item = f"Input needed: {goal} ({error})"
                # Cap at 80 chars for DailyBriefing schema
                if len(item) > 80:
                    item = item[:77] + "..."
                action_items.append(item)

    # Read active missions from ledger.json
    active_missions = []
    missions_dir = Path(missions_dir)
    ledger_path = missions_dir / "ledger.json"
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text())
            for m in ledger.get("missions", []):
                if m.get("status") in {"ACTIVE", "ADAPTING"}:
                    goal_short = (m.get("goal") or "?")[:60]
                    active_missions.append(goal_short)
        except (json.JSONDecodeError, OSError):
            pass

    # Tomorrow = active mission goals (up to 3, not already in active)
    tomorrow = active_missions[:3]

    return {
        "done": done_today[:5],
        "active": active_missions[:3],
        "action_items": action_items[:3],
        "tomorrow": tomorrow,
        "done_count": len(done_today),
    }


def compute_delta(current_done_count: int, state: dict):
    """Return delta string showing momentum, not cumulative state.

    Returns None only when both prev and current are 0 (nothing to report).
    """
    prev = state.get("previous_brief") or {}
    prev_done = prev.get("done_count", 0)
    delta = current_done_count - prev_done

    if delta > 0:
        return f"+{delta} tasks completed today"
    if delta == 0 and current_done_count > 0:
        return "same as yesterday"
    if current_done_count == 0 and prev_done == 0:
        return None  # Nothing either day — omit entirely
    # delta < 0 — fewer completions than yesterday
    return f"{delta} tasks vs yesterday"


def truncate_with_link(text: str) -> str:
    """Truncate text at 3900 chars, appending dashboard link if needed."""
    if len(text) <= MAX_SAFE:
        return text
    cutoff = text.rfind("\n", 0, MAX_SAFE)
    if cutoff == -1:
        cutoff = MAX_SAFE
    suffix = f"\n...[full report: {DASHBOARD_URL}]"
    return text[:cutoff] + suffix


def format_for_telegram(briefing: DailyBriefing) -> str:
    """Render DailyBriefing to formatted Telegram Markdown text."""
    now_p = now_pacific()
    lines = [f"*Aria \u2014 {now_p.strftime('%b %d')}*"]  # em-dash for elegance

    if briefing.delta_summary:
        lines.append(f"_{briefing.delta_summary}_")

    if briefing.action_items:
        lines.append("")
        lines.append("*ACTION NEEDED*")
        for item in briefing.action_items:
            lines.append(f"\u2022 {item}")

    if briefing.done:
        lines.append("")
        lines.append("*Done today*")
        for item in briefing.done:
            lines.append(f"\u2022 {item}")

    if briefing.active:
        lines.append("")
        lines.append("*Active*")
        for item in briefing.active:
            lines.append(f"\u2022 {item}")

    if briefing.tomorrow:
        lines.append("")
        lines.append("*Tomorrow*")
        for item in briefing.tomorrow:
            lines.append(f"\u2022 {item}")

    if briefing.flag:
        lines.append("")
        lines.append(f"FYI: {briefing.flag}")

    if briefing.guardrail_violations:
        lines.append("")
        lines.append("*Blocked today*")
        for item in briefing.guardrail_violations:
            lines.append(f"\u2022 {item}")

    text = "\n".join(lines)
    return truncate_with_link(text)


def generate() -> "DailyBriefing | None":
    """Collect data, compute delta, and build a DailyBriefing.

    Returns None if no content (empty briefing should be skipped).
    """
    data = collect_briefing_data(TASKS_DIR, MISSIONS_DIR)
    violations = _read_todays_violations()  # AUTO-07

    # Minimum content gate: skip if nothing to report (violations count as content)
    if not data["done"] and not data["active"] and not data["action_items"] and not violations:
        return None

    state = load_json(BRIEFING_STATE)
    delta_summary = compute_delta(data["done_count"], state)

    return DailyBriefing(
        done=data["done"],
        active=data["active"],
        tomorrow=data["tomorrow"],
        action_items=data["action_items"],
        delta_summary=delta_summary,
        guardrail_violations=violations,
    )


def send_telegram(text: str) -> bool:
    """Send a message to Alex via Telegram Bot API.

    Returns True on success.
    """
    token = get_bot_token()
    if not token:
        print("ERROR: No bot token available", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": ALEX_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"ERROR: Telegram send failed: {e}", file=sys.stderr)
        return False


def mark_sent(done_count: int = 0) -> None:
    """Update briefing-state.json sentinel with today's date and previous brief snapshot."""
    now = now_pacific()
    BRIEFING_STATE.parent.mkdir(parents=True, exist_ok=True)
    state = load_json(BRIEFING_STATE)
    state["last_sent_date"] = now.strftime("%Y-%m-%d")
    state["last_sent_time"] = now.strftime("%H:%M")
    state["total_sent"] = state.get("total_sent", 0) + 1
    state["previous_brief"] = {"done_count": done_count}
    atomic_write(BRIEFING_STATE, state)


def send() -> int:
    """Full briefing pipeline: generate + validate + send + mark-sent.

    Uses FileLock to prevent double-send in concurrent heartbeat runs.
    Returns 0 on success or skip, 1 on failure.
    """
    lock = FileLock(BRIEFING_STATE, timeout=10)
    if not lock.acquire():
        print("LOCK_FAILED: Could not acquire briefing lock", file=sys.stderr)
        return 1

    try:
        if not should_send():
            print("SKIP: Not in send window or already sent today")
            return 1

        briefing_obj = generate()

        if briefing_obj is None:
            # Empty briefing — mark sent but skip Telegram
            print("SKIP_EMPTY: No content for briefing, marking sent")
            mark_sent(done_count=0)
            return 0

        formatted = format_for_telegram(briefing_obj)

        # Validate via HumanMessage — enforces 4096 char limit
        try:
            HumanMessage(text=formatted, urgency="briefing")
        except Exception as e:
            print(f"ERROR: Message validation failed: {e}", file=sys.stderr)
            return 1

        success = send_telegram(formatted)
        if success:
            mark_sent(done_count=len(briefing_obj.done))
            log_action("briefing_sent", {
                "type": "daily_brief",
                "violations_count": len(briefing_obj.guardrail_violations),
                "done_count": len(briefing_obj.done),
                "active_count": len(briefing_obj.active),
            })
            print("SENT: Daily briefing delivered")
            return 0
        else:
            print("ERROR: Telegram send failed", file=sys.stderr)
            return 1

    finally:
        lock.release()


def alert(text: str, category: str, task_id: str = None, tried: list = None) -> int:
    """Send emergency alert immediately, bypassing time window checks.

    Validates via AlertMessage schema (500-char limit).
    Returns 0 on success, 1 on failure.
    """
    if tried is None:
        tried = []

    # Validate via AlertMessage — raises ValueError if text > 500 chars
    msg = AlertMessage(text=text, category=category, task_id=task_id, tried=tried)

    tried_str = ""
    if msg.tried:
        tried_str = f"\nTried: {', '.join(msg.tried)}"
    formatted = f"ALERT [{msg.category}]: {msg.text}{tried_str}"

    success = send_telegram(formatted)
    if success:
        print(f"ALERT_SENT: [{category}] {text[:60]}")
        return 0
    else:
        print("ERROR: Alert send failed", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_should_send(args):
    if should_send():
        print("SEND_NOW: In 8-10 PM Pacific window, not yet sent today")
        sys.exit(0)
    else:
        print("SKIP: Not in send window or already sent today")
        sys.exit(1)


def cmd_generate(args):
    briefing_obj = generate()
    if briefing_obj is None:
        print("SKIP_EMPTY")
    else:
        print(format_for_telegram(briefing_obj))


def cmd_send(args):
    sys.exit(send())


def cmd_mark_sent(args):
    mark_sent(done_count=0)
    print(f"MARKED: Briefing marked sent for {now_pacific().strftime('%Y-%m-%d')}")


def cmd_alert(args):
    tried = args.tried or []
    sys.exit(alert(
        text=args.text,
        category=args.category,
        task_id=args.task_id,
        tried=tried,
    ))


def main():
    parser = argparse.ArgumentParser(
        description="Aria daily briefing engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("should-send", help="Exit 0 if ready to send briefing, 1 otherwise")
    sub.add_parser("generate", help="Generate and print formatted briefing (no send)")
    sub.add_parser("send", help="Full pipeline: generate + validate + send + mark-sent")
    sub.add_parser("mark-sent", help="Mark today as sent without generating")

    alert_p = sub.add_parser("alert", help="Send emergency alert immediately")
    alert_p.add_argument("--text", required=True, help="Alert description (max 500 chars)")
    alert_p.add_argument(
        "--category",
        required=True,
        choices=["credentials_needed", "money_needed", "blocker_critical"],
        help="Alert category",
    )
    alert_p.add_argument("--task-id", dest="task_id", default=None, help="Related task ID")
    alert_p.add_argument(
        "--tried",
        action="append",
        default=[],
        help="What was already attempted (repeat for multiple)",
    )

    args = parser.parse_args()

    dispatch = {
        "should-send": cmd_should_send,
        "generate": cmd_generate,
        "send": cmd_send,
        "mark-sent": cmd_mark_sent,
        "alert": cmd_alert,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
