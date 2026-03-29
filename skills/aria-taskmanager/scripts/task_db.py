#!/usr/bin/env python3
"""Task SQLite Database — ACID-compliant task state storage.

Provides SQLite backend for task state with:
- ACID transactions
- Concurrent access support (WAL mode)
- Schema migrations
- Full-text search on task goals

Usage:
    # Initialize database (creates tables if not exist)
    task_db.py init

    # Run SQL interactively
    task_db.py query "SELECT * FROM tasks LIMIT 5"

    # Export current tasks to SQLite
    task_db.py import

    # Backup database
    task_db.py backup

    # Show database stats
    task_db.py stats
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DB_PATH = Path("/home/alex/.openclaw/workspace/memory/tasks/state/tasks.db")
TASKS_DIR = Path("/home/alex/.openclaw/workspace/memory/tasks/state")


def get_db_path() -> Path:
    """Get database path, creating directory if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def get_connection() -> sqlite3.Connection:
    """Get database connection with WAL mode for concurrent access."""
    conn = sqlite3.connect(str(get_db_path()), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize database schema."""
    conn = get_connection()

    conn.executescript("""
        -- Tasks table
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'CREATED',
            priority INTEGER DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_heartbeat TEXT,
            attempts INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 15,
            current_step TEXT,
            last_error TEXT,
            last_error_at TEXT,
            retry_strategy TEXT,
            consecutive_step_errors INTEGER DEFAULT 0,
            blocked_heartbeats INTEGER DEFAULT 0,
            step_started_at TEXT,
            source TEXT DEFAULT 'heartbeat',
            deadline TEXT,
            context_json TEXT,
            escalation_json TEXT,
            tags_json TEXT,
            notes_json TEXT,
            error_history_json TEXT
        );

        -- Steps completed
        CREATE TABLE IF NOT EXISTS task_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            step TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        -- Posts (from post_manager)
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            title TEXT,
            content TEXT,
            link TEXT,
            url TEXT,
            status TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            extra_json TEXT
        );

        -- Full-text search virtual table
        CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
            goal, current_step, last_error,
            content='tasks',
            content_rowid='rowid'
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS tasks_ai AFTER INSERT ON tasks BEGIN
            INSERT INTO tasks_fts(rowid, goal, current_step, last_error)
            VALUES (new.rowid, new.goal, new.current_step, new.last_error);
        END;

        CREATE TRIGGER IF NOT EXISTS tasks_ad AFTER DELETE ON tasks BEGIN
            INSERT INTO tasks_fts(tasks_fts, rowid, goal, current_step, last_error)
            VALUES ('delete', old.rowid, old.goal, old.current_step, old.last_error);
        END;

        CREATE TRIGGER IF NOT EXISTS tasks_au AFTER UPDATE ON tasks BEGIN
            INSERT INTO tasks_fts(tasks_fts, rowid, goal, current_step, last_error)
            VALUES ('delete', old.rowid, old.goal, old.current_step, old.last_error);
            INSERT INTO tasks_fts(rowid, goal, current_step, last_error)
            VALUES (new.rowid, new.goal, new.current_step, new.last_error);
        END;

        -- Indexes
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
        CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
        CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id);
        CREATE INDEX IF NOT EXISTS idx_posts_account ON posts(account_id);
    """)

    conn.commit()

    # Phase 1 migrations: new task fields for mission linkage, quality gates, GPU, checkpointing
    migrations = [
        "ALTER TABLE tasks ADD COLUMN mission_id TEXT",
        "ALTER TABLE tasks ADD COLUMN quality_gate_status TEXT",
        "ALTER TABLE tasks ADD COLUMN requires_gpu INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN checkpoint_json TEXT",
        "CREATE INDEX IF NOT EXISTS idx_tasks_mission ON tasks(mission_id)",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column/index already exists — idempotent
    conn.commit()

    print(f"Database initialized at {DB_PATH}")


def import_tasks() -> int:
    """Import existing JSON tasks into SQLite."""
    conn = get_connection()
    imported = 0

    for task_file in TASKS_DIR.glob("task_*.json"):
        if task_file.stem == "tasks":
            continue

        try:
            with open(task_file) as f:
                task = json.load(f)

            # Extract JSON fields
            context_json = json.dumps(task.get("context", {}))
            escalation_json = json.dumps(task.get("escalation", {}))
            tags_json = json.dumps(task.get("tags", []))
            notes_json = json.dumps(task.get("notes", []))
            error_history_json = json.dumps(task.get("error_history", []))

            conn.execute("""
                INSERT OR REPLACE INTO tasks (
                    id, goal, status, priority, created_at, updated_at,
                    last_heartbeat, attempts, max_attempts, current_step,
                    last_error, last_error_at, retry_strategy,
                    consecutive_step_errors, blocked_heartbeats, step_started_at,
                    source, deadline, context_json, escalation_json,
                    tags_json, notes_json, error_history_json,
                    mission_id, quality_gate_status, requires_gpu, checkpoint_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task["id"], task["goal"], task["status"], task.get("priority", 3),
                task["created_at"], task["updated_at"], task.get("last_heartbeat"),
                task["attempts"], task.get("max_attempts", 15), task.get("current_step"),
                task.get("last_error"), task.get("last_error_at"), task.get("retry_strategy"),
                task.get("consecutive_step_errors", 0), task.get("blocked_heartbeats", 0),
                task.get("step_started_at"), task.get("source", "heartbeat"),
                task.get("deadline"), context_json, escalation_json,
                tags_json, notes_json, error_history_json,
                task.get("mission_id"),
                task.get("quality_gate_status"),
                1 if task.get("requires_gpu", False) else 0,
                json.dumps(task.get("checkpoint", {})),
            ))

            # Import steps
            for step in task.get("steps_completed", []):
                conn.execute("""
                    INSERT INTO task_steps (task_id, step, completed_at)
                    VALUES (?, ?, ?)
                """, (task["id"], step["step"], step["completed_at"]))

            imported += 1

        except Exception as e:
            print(f"Skipped {task_file.name}: {e}")

    conn.commit()
    print(f"Imported {imported} tasks")
    return imported


def query_tasks(sql: str) -> list[dict]:
    """Execute a query and return results."""
    conn = get_connection()
    cursor = conn.execute(sql)
    rows = cursor.fetchall()

    results = []
    for row in rows:
        results.append(dict(row))

    return results


def show_stats() -> dict[str, Any]:
    """Show database statistics."""
    conn = get_connection()

    # Task counts by status
    status_counts = conn.execute("""
        SELECT status, COUNT(*) as count FROM tasks GROUP BY status
    """).fetchall()

    # Priority distribution
    priority_counts = conn.execute("""
        SELECT priority, COUNT(*) as count FROM tasks GROUP BY priority
    """).fetchall()

    # Total steps
    total_steps = conn.execute("SELECT COUNT(*) FROM task_steps").fetchone()[0]

    # Posts
    post_count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    # Database size
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    return {
        "db_path": str(DB_PATH),
        "db_size_bytes": db_size,
        "tasks_by_status": {row["status"]: row["count"] for row in status_counts},
        "tasks_by_priority": {row["priority"]: row["count"] for row in priority_counts},
        "total_steps": total_steps,
        "total_posts": post_count,
    }


def backup_db(backup_path: str = None) -> Path:
    """Create a backup of the database."""
    if backup_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"/home/alex/.openclaw/workspace/memory/tasks/state/tasks_backup_{timestamp}.db"

    conn = get_connection()
    backup_conn = sqlite3.connect(backup_path)
    conn.backup(backup_conn)
    backup_conn.close()

    print(f"Backup created: {backup_path}")
    return Path(backup_path)


def search_tasks(query: str) -> list[dict]:
    """Full-text search on tasks."""
    conn = get_connection()
    cursor = conn.execute("""
        SELECT t.* FROM tasks t
        JOIN tasks_fts fts ON t.rowid = fts.rowid
        WHERE tasks_fts MATCH ?
        ORDER BY rank
    """, (query,))

    return [dict(row) for row in cursor.fetchall()]


# CLI
def main():
    parser = argparse.ArgumentParser(description="Task SQLite Database")
    subparsers = parser.add_subparsers(dest="cmd", help="Command")

    # init
    subparsers.add_parser("init", help="Initialize database schema")

    # import
    subparsers.add_parser("import", help="Import JSON tasks to SQLite")

    # query
    p = subparsers.add_parser("query", help="Run SQL query")
    p.add_argument("sql", help="SQL to execute")

    # search
    p = subparsers.add_parser("search", help="Full-text search")
    p.add_argument("query", help="Search query")

    # backup
    p = subparsers.add_parser("backup", help="Create backup")
    p.add_argument("--path", help="Backup file path")

    # stats
    subparsers.add_parser("stats", help="Show database statistics")

    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    if args.cmd == "init":
        init_db()

    elif args.cmd == "import":
        import_tasks()

    elif args.cmd == "query":
        results = query_tasks(args.sql)
        print(json.dumps(results, indent=2, default=str))

    elif args.cmd == "search":
        results = search_tasks(args.query)
        print(json.dumps(results, indent=2, default=str))

    elif args.cmd == "backup":
        backup_db(args.path)

    elif args.cmd == "stats":
        stats = show_stats()
        print(json.dumps(stats, indent=2, default=str))


if __name__ == "__main__":
    main()
