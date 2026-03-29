#!/usr/bin/env python3
"""GPU serialization mutex with VRAM pre-check.

Purpose
-------
Prevents VRAM contention on the RTX 3090 (24 GB) when multiple GPU tasks
(FramePack, SDXL, Qwen3) could otherwise run simultaneously.  The heartbeat
currently relies on LLM prose inference for GPU awareness; this module
provides code-level enforcement.

Lock file
---------
A single JSON file at GPU_LOCK_PATH acts as the mutex.  Only one task may
hold the lock at a time.  Stale locks (where the holder task is no longer
RUNNING) are automatically released on the next acquire attempt.

Environment variables
---------------------
GPU_LOCK_PATH   Override the default lock file path.  Used by tests to avoid
                touching the real lock.  Defaults to:
                /home/alex/.openclaw/workspace/memory/guardrails/gpu.lock

CLI usage
---------
  gpu_lock.py acquire --task-id <id> [--type <type>] [--min-gb <float>]
      Acquire the GPU lock.  Exits 0 on success, 1 on failure (reason on stderr).

  gpu_lock.py release --task-id <id>
      Release the GPU lock.  Idempotent — exits 0 even if no lock exists.

  gpu_lock.py status
      Print current lock holder (JSON) or "GPU: available".  Exits 0.

  gpu_lock.py vram-check [--min-gb <float>]
      Exit 0 if free VRAM >= min-gb, exit 1 otherwise.

Exported functions
------------------
  acquire_gpu_lock(task_id, task_type="", min_vram_gb=DEFAULT_MIN_VRAM_GB)
      -> tuple[bool, str]  — (success, error_reason)

  release_gpu_lock(task_id)  -> None

  check_vram(min_gb=DEFAULT_MIN_VRAM_GB)  -> bool

  gpu_status()  -> dict
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — all override-able for test isolation
# ---------------------------------------------------------------------------

_DEFAULT_GPU_LOCK = "/home/alex/.openclaw/workspace/memory/guardrails/gpu.lock"
GPU_LOCK_PATH = Path(os.environ.get("GPU_LOCK_PATH", _DEFAULT_GPU_LOCK))

TM_PATH = "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts/task_manager.py"

DEFAULT_MIN_VRAM_GB = 10.0


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def check_vram(min_gb: float = DEFAULT_MIN_VRAM_GB) -> bool:
    """Return True if free VRAM (MiB) >= min_gb.

    Queries nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits.
    Returns False on any subprocess error (safe default — don't acquire if
    GPU state is unknown).
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        free_mb = int(result.stdout.strip().splitlines()[0].strip())
        free_gb = free_mb / 1024.0
        return free_gb >= min_gb
    except Exception:
        return False


def _is_holder_active(task_id: str) -> bool:
    """Return True if the task is currently RUNNING or IN_PROGRESS.

    Calls task_manager.py show <task_id> and inspects stdout for the status
    field.  Returns False for DONE, CANCELLED, ERROR, or if the task is not
    found (non-zero exit code).
    """
    try:
        result = subprocess.run(
            [sys.executable, TM_PATH, "show", task_id],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return False
        output = result.stdout
        for line in output.splitlines():
            if "status:" in line.lower():
                parts = line.split(":", 1)
                if len(parts) == 2:
                    status = parts[1].strip().upper()
                    return status in ("RUNNING", "IN_PROGRESS")
        return False
    except Exception:
        return False


def acquire_gpu_lock(
    task_id: str,
    task_type: str = "",
    min_vram_gb: float = DEFAULT_MIN_VRAM_GB,
) -> tuple:
    """Attempt to acquire the GPU lock for task_id.

    Returns (True, "") on success.
    Returns (False, reason) on failure.

    Logic:
      1. VRAM pre-check — fail fast if insufficient memory.
      2. Read existing lock — if present, check if holder is still active.
         - Active holder: return busy.
         - Stale holder (DONE/gone): auto-release the stale lock.
      3. Write new lock JSON atomically.
    """
    # Step 1: VRAM pre-check
    if not check_vram(min_vram_gb):
        return (False, "VRAM insufficient: not enough free GPU memory")

    # Step 2: Check existing lock
    if GPU_LOCK_PATH.exists():
        try:
            data = json.loads(GPU_LOCK_PATH.read_text())
            holder = data.get("task_id", "unknown")
        except (json.JSONDecodeError, OSError):
            holder = "unknown"
            data = {}

        if _is_holder_active(holder):
            return (False, f"GPU busy: task {holder} is running")

        # Stale lock — auto-release
        GPU_LOCK_PATH.unlink(missing_ok=True)

    # Step 3: Write new lock
    GPU_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_data = {
        "task_id": task_id,
        "task_type": task_type,
        "acquired_at": datetime.now(timezone.utc).isoformat(),
    }
    GPU_LOCK_PATH.write_text(json.dumps(lock_data, indent=2))
    return (True, "")


def release_gpu_lock(task_id: str) -> None:
    """Release the GPU lock.  Idempotent — does nothing if no lock exists."""
    GPU_LOCK_PATH.unlink(missing_ok=True)


def gpu_status() -> dict:
    """Return current GPU lock state.

    Returns the lock JSON if a lock exists, or {"status": "available"} if not.
    """
    if GPU_LOCK_PATH.exists():
        try:
            return json.loads(GPU_LOCK_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {"status": "available"}
    return {"status": "available"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GPU serialization mutex with VRAM pre-check.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # acquire
    acq = sub.add_parser("acquire", help="Acquire GPU lock")
    acq.add_argument("--task-id", required=True, help="Task ID requesting the lock")
    acq.add_argument("--type", dest="task_type", default="", help="Task type (e.g. video, sdxl)")
    acq.add_argument("--min-gb", type=float, default=DEFAULT_MIN_VRAM_GB,
                     help="Minimum free VRAM in GB (default: %(default)s)")

    # release
    rel = sub.add_parser("release", help="Release GPU lock")
    rel.add_argument("--task-id", required=True, help="Task ID releasing the lock")

    # status
    sub.add_parser("status", help="Show current GPU lock status")

    # vram-check
    vc = sub.add_parser("vram-check", help="Check if enough free VRAM is available")
    vc.add_argument("--min-gb", type=float, default=DEFAULT_MIN_VRAM_GB,
                    help="Minimum free VRAM in GB (default: %(default)s)")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "acquire":
        ok, reason = acquire_gpu_lock(args.task_id, args.task_type, args.min_gb)
        if ok:
            sys.exit(0)
        else:
            print(reason, file=sys.stderr)
            sys.exit(1)

    elif args.command == "release":
        release_gpu_lock(args.task_id)
        sys.exit(0)

    elif args.command == "status":
        status = gpu_status()
        if status.get("status") == "available":
            print("GPU: available")
        else:
            print(json.dumps(status, indent=2))
        sys.exit(0)

    elif args.command == "vram-check":
        if check_vram(args.min_gb):
            sys.exit(0)
        else:
            print(f"VRAM insufficient: less than {args.min_gb} GB free", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
