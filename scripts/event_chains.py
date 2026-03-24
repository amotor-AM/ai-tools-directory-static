#!/usr/bin/env python3
"""Event chain system — automatically trigger follow-up actions when tasks complete.

When Aria completes a task, this script checks if the task matches any event chain
rules and creates follow-up tasks automatically. This eliminates the need for Aria
to remember to do post-completion steps.

Usage:
  # Check and fire chains for a completed task
  python3 event_chains.py fire --task-id <id> --goal "Published The Debt of Ashes on KDP" --tags "book,publish"

  # List all configured chains
  python3 event_chains.py list

  # Check pending follow-ups (for heartbeat)
  python3 event_chains.py check-followups
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TM = "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py"
CHAINS_FILE = Path("/home/alex/.openclaw/workspace/memory/event_chains.json")
FOLLOWUPS_DIR = Path("/home/alex/.openclaw/workspace/memory/tasks/followups")

# Default event chains — customize in event_chains.json
DEFAULT_CHAINS = [
    {
        "name": "book-published-marketing",
        "trigger_keywords": ["published", "book", "kdp", "d2d"],
        "trigger_tags": ["book", "publish"],
        "min_keyword_matches": 2,
        "followup_tasks": [
            {"goal": "Market newly published book: submit to Google, post to social, start 30-day launch plan", "priority": 2, "delay_hours": 0},
            {"goal": "Check sales and reviews for newly published book (24h check)", "priority": 3, "delay_hours": 24},
            {"goal": "Weekly sales review for recently published book", "priority": 3, "delay_hours": 168},
        ]
    },
    {
        "name": "article-deployed",
        "trigger_keywords": ["article", "deployed", "published", "live"],
        "trigger_tags": ["seo", "article", "content"],
        "min_keyword_matches": 2,
        "followup_tasks": [
            {"goal": "Submit newly published article to Google/Bing indexing and share on social", "priority": 2, "delay_hours": 0},
            {"goal": "Check Google indexing status for recently published article (48h)", "priority": 3, "delay_hours": 48},
        ]
    },
    {
        "name": "site-deployed",
        "trigger_keywords": ["deployed", "site", "vercel", "live"],
        "trigger_tags": ["deploy", "site"],
        "min_keyword_matches": 2,
        "followup_tasks": [
            {"goal": "Verify newly deployed site is healthy: check uptime, load speed, SSL", "priority": 2, "delay_hours": 1},
        ]
    },
    {
        "name": "bug-fixed",
        "trigger_keywords": ["fixed", "bug", "resolved", "patched"],
        "trigger_tags": ["fix", "bug"],
        "min_keyword_matches": 2,
        "followup_tasks": [
            {"goal": "Verify bug fix is holding (24h regression check)", "priority": 3, "delay_hours": 24},
        ]
    },
    {
        "name": "outreach-sent",
        "trigger_keywords": ["outreach", "email", "sent", "pitched"],
        "trigger_tags": ["outreach", "email"],
        "min_keyword_matches": 2,
        "followup_tasks": [
            {"goal": "Follow up on outreach emails sent 3 days ago", "priority": 3, "delay_hours": 72},
        ]
    },
    {
        "name": "account-created",
        "trigger_keywords": ["account", "created", "signed up", "registered"],
        "trigger_tags": ["account"],
        "min_keyword_matches": 2,
        "followup_tasks": [
            {"goal": "Warm up newly created account: complete profile, make first posts", "priority": 3, "delay_hours": 2},
        ]
    },
]


def load_chains():
    """Load event chains from config, falling back to defaults."""
    if CHAINS_FILE.exists():
        with open(CHAINS_FILE) as f:
            return json.load(f)
    # Write defaults on first run
    CHAINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHAINS_FILE, "w") as f:
        json.dump(DEFAULT_CHAINS, f, indent=2)
    return DEFAULT_CHAINS


def matches_chain(chain, goal, tags):
    """Check if a completed task matches a chain's trigger."""
    goal_lower = goal.lower()
    keyword_hits = sum(1 for kw in chain["trigger_keywords"] if kw in goal_lower)
    tag_hits = sum(1 for tag in chain.get("trigger_tags", []) if tag in tags)
    return (keyword_hits + tag_hits) >= chain.get("min_keyword_matches", 2)


def create_task(goal, priority, source_task_id=None):
    """Create a follow-up task via the task manager."""
    ctx = json.dumps({"triggered_by": source_task_id}) if source_task_id else "{}"
    cmd = f'python3 {TM} create --goal "{goal}" --priority {priority} --source self --context \'{ctx}\''
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        # Extract task ID from output
        for line in result.stdout.split("\n"):
            if line.startswith("CREATED:"):
                return line.split(":")[1].strip()
    return None


def schedule_followup(task_id, goal, priority, delay_hours, source_task_id):
    """Schedule a delayed follow-up task."""
    FOLLOWUPS_DIR.mkdir(parents=True, exist_ok=True)
    due_at = datetime.now(timezone.utc) + timedelta(hours=delay_hours)
    followup = {
        "goal": goal,
        "priority": priority,
        "due_at": due_at.isoformat(),
        "source_task_id": source_task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    fname = f"followup_{source_task_id}_{int(due_at.timestamp())}.json"
    with open(FOLLOWUPS_DIR / fname, "w") as f:
        json.dump(followup, f, indent=2)
    return fname


def fire_chains(args):
    """Check if a completed task triggers any event chains."""
    chains = load_chains()
    goal = args.goal
    tags = args.tags.split(",") if args.tags else []
    task_id = args.task_id

    # Guard: skip chains for mission sub-tasks to prevent cascading explosion
    source = getattr(args, "source", None) or ""
    if source == "mission-subtask":
        print(f"CHAIN_SKIPPED: task {task_id} is a mission sub-task, skipping event chains")
        return

    # Fallback: read source from task file if not provided via CLI
    if not source:
        task_state_dir = Path(os.environ.get(
            "ARIA_TASK_DIR",
            "/home/alex/.openclaw/workspace/memory/tasks/state"
        ))
        task_file = task_state_dir / f"task_{task_id}.json"
        if task_file.exists():
            try:
                with open(task_file) as f:
                    task_data = json.load(f)
                source = task_data.get("source", "")
                if source == "mission-subtask":
                    print(f"CHAIN_SKIPPED: task {task_id} is a mission sub-task (from file), skipping event chains")
                    return
            except (json.JSONDecodeError, OSError):
                pass  # If we can't read, proceed with chains

    fired = []
    for chain in chains:
        if matches_chain(chain, goal, tags):
            fired.append(chain["name"])
            for followup in chain["followup_tasks"]:
                if followup["delay_hours"] == 0:
                    # Create immediately
                    tid = create_task(followup["goal"], followup["priority"], task_id)
                    print(f"  CHAIN [{chain['name']}]: Created task {tid} — {followup['goal']}")
                else:
                    # Schedule for later
                    fname = schedule_followup(
                        task_id, followup["goal"], followup["priority"],
                        followup["delay_hours"], task_id
                    )
                    print(f"  CHAIN [{chain['name']}]: Scheduled in {followup['delay_hours']}h — {followup['goal']}")

    if fired:
        print(f"\nEVENT_CHAINS_FIRED: {', '.join(fired)}")
    else:
        print("NO_CHAINS_MATCHED")


def check_followups(args):
    """Check for due follow-up tasks and create them."""
    if not FOLLOWUPS_DIR.exists():
        print("NO_PENDING_FOLLOWUPS")
        return

    now = datetime.now(timezone.utc)
    created = 0
    for f in sorted(FOLLOWUPS_DIR.glob("followup_*.json")):
        try:
            with open(f) as fh:
                followup = json.load(fh)
            due = datetime.fromisoformat(followup["due_at"])
            if now >= due:
                tid = create_task(
                    followup["goal"], followup["priority"],
                    followup.get("source_task_id")
                )
                print(f"  FOLLOWUP_DUE: Created task {tid} — {followup['goal']}")
                f.unlink()  # Remove the followup file
                created += 1
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    remaining = len(list(FOLLOWUPS_DIR.glob("followup_*.json")))
    if created:
        print(f"\nFOLLOWUPS_CREATED: {created}")
    if remaining:
        print(f"FOLLOWUPS_PENDING: {remaining}")
    elif not created:
        print("NO_PENDING_FOLLOWUPS")


def list_chains(args):
    """List all configured event chains."""
    chains = load_chains()
    print(f"EVENT CHAINS: {len(chains)} configured\n")
    for c in chains:
        triggers = ", ".join(c["trigger_keywords"][:4])
        n_followups = len(c["followup_tasks"])
        print(f"  {c['name']}: triggers on [{triggers}] → {n_followups} follow-up(s)")
        for fu in c["followup_tasks"]:
            delay = f"immediately" if fu["delay_hours"] == 0 else f"after {fu['delay_hours']}h"
            print(f"    P{fu['priority']} {delay}: {fu['goal'][:70]}")


def main():
    parser = argparse.ArgumentParser(description="Event chain system — auto-trigger follow-up actions")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("fire", help="Fire chains for a completed task")
    p.add_argument("--task-id", required=True, help="Completed task ID")
    p.add_argument("--goal", required=True, help="Completed task goal")
    p.add_argument("--tags", default="", help="Comma-separated task tags")
    p.add_argument("--source", default="", help="Source of the completing task (e.g., mission-subtask)")

    sub.add_parser("check-followups", help="Create tasks for due follow-ups")
    sub.add_parser("list", help="List all configured chains")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"fire": fire_chains, "check-followups": check_followups, "list": list_chains}[args.command](args)


if __name__ == "__main__":
    main()
