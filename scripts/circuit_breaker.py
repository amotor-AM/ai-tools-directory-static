#!/usr/bin/env python3
"""File-persisted circuit breaker registry for Aria self-healing layer.

Wraps pybreaker with a custom CircuitFileStorage backend so breaker state
survives across process restarts (each heal.py call is a separate subprocess).
State is stored in JSON at memory/growth/circuit-breakers.json with atomic
writes (tmp file + os.rename).

Usage (CLI):
  python3 circuit_breaker.py status
  python3 circuit_breaker.py is-open --agent webagent --task-type article
  python3 circuit_breaker.py record-failure --agent webagent --task-type article
  python3 circuit_breaker.py record-success --agent webagent --task-type article
  python3 circuit_breaker.py reset --agent webagent --task-type article

Exit codes for is-open: 0 = closed (safe to proceed), 1 = open (blocked).

Usage (import):
  from circuit_breaker import get_breaker, is_open, record_failure, record_success, reset, status

Requirement: HEAL-05 — circuit breaker per tool/site prevents flooding a failing
external service. pybreaker's CircuitMemoryStorage resets per process; this
module replaces it with CircuitFileStorage for cross-process persistence.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pybreaker

# ---------------------------------------------------------------------------
# File path — overridable via HEAL_TEST_DIR for test isolation
# ---------------------------------------------------------------------------
#
# BREAKERS_FILE is re-read from the environment on every internal call so that
# test fixtures that change HEAL_TEST_DIR between test functions always pick up
# the current value.  The module-level name BREAKERS_FILE is kept as a
# convenience for callers and introspection (cb.BREAKERS_FILE) — it is backed by
# the module __getattr__ hook so that `cb.BREAKERS_FILE` also returns the live
# value rather than the snapshot taken at import time.


def _get_breakers_file() -> Path:
    """Return the current BREAKERS_FILE path, re-reading HEAL_TEST_DIR from env."""
    _test_dir = os.environ.get("HEAL_TEST_DIR")
    if _test_dir:
        return Path(_test_dir) / "circuit-breakers.json"
    return Path("/home/alex/.openclaw/workspace/memory/growth/circuit-breakers.json")


# Module __getattr__ makes `cb.BREAKERS_FILE` dynamic (re-reads env each time).
# Direct `from circuit_breaker import BREAKERS_FILE` still works (snapshot).
def __getattr__(name: str) -> object:
    if name == "BREAKERS_FILE":
        return _get_breakers_file()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Per-agent reset timeouts (seconds).  "default" covers any unknown agent.
# ---------------------------------------------------------------------------

BREAKER_TIMEOUTS: dict[str, int] = {
    "webagent": 600,
    "default": 300,
}

# Internal datetime format used when serialising opened_at to JSON
_DT_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"


# ---------------------------------------------------------------------------
# CircuitFileStorage — JSON-backed implementation of CircuitBreakerStorage
# ---------------------------------------------------------------------------


class CircuitFileStorage(pybreaker.CircuitBreakerStorage):
    """JSON-backed circuit breaker storage that persists across process restarts.

    Each instance is keyed by ``key`` (typically "agent_name:task_type").
    All instances share the same JSON file at _get_breakers_file(), with per-key
    sub-dictionaries.  Writes are atomic: we write to a temp file in the same
    directory then os.rename() it into place.
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self._key = key
        self._data = self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        """Read this key's state dict from the breakers file.

        Returns defaults (CLOSED, 0 counters) if file or key is absent.
        """
        defaults: dict = {
            "state": pybreaker.STATE_CLOSED,
            "fail_counter": 0,
            "success_counter": 0,
            "opened_at": None,
        }
        try:
            bf = _get_breakers_file()
            if bf.exists():
                all_data = json.loads(bf.read_text())
                if self._key in all_data:
                    entry = all_data[self._key]
                    return {
                        "state": entry.get("state", pybreaker.STATE_CLOSED),
                        "fail_counter": entry.get("fail_counter", 0),
                        "success_counter": entry.get("success_counter", 0),
                        "opened_at": entry.get("opened_at"),
                    }
        except (json.JSONDecodeError, OSError):
            pass
        return defaults

    def _save(self) -> None:
        """Write this key's state into the breakers file atomically.

        Reads the full JSON, updates only this key's sub-dict, and writes
        to a sibling temp file before renaming — so readers never see a
        partially written file.
        """
        bf = _get_breakers_file()
        bf.parent.mkdir(parents=True, exist_ok=True)

        # Read current full state
        try:
            if bf.exists():
                all_data = json.loads(bf.read_text())
            else:
                all_data = {}
        except (json.JSONDecodeError, OSError):
            all_data = {}

        # Serialize opened_at
        opened_at_val = self._data.get("opened_at")
        if isinstance(opened_at_val, datetime):
            opened_at_str: str | None = opened_at_val.strftime(_DT_FMT)
        else:
            opened_at_str = opened_at_val  # already a str or None

        all_data[self._key] = {
            "state": self._data["state"],
            "fail_counter": self._data["fail_counter"],
            "success_counter": self._data["success_counter"],
            "opened_at": opened_at_str,
        }

        # Atomic write: temp file in same directory, then rename
        tmp_path = bf.parent / f".circuit-breakers-{os.getpid()}.tmp"
        try:
            tmp_path.write_text(json.dumps(all_data, indent=2))
            tmp_path.rename(bf)
        except OSError:
            # Best-effort cleanup
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    # ------------------------------------------------------------------
    # CircuitBreakerStorage interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._data["state"]

    @state.setter
    def state(self, state: str) -> None:
        self._data["state"] = state
        self._save()

    @property
    def counter(self) -> int:
        return self._data["fail_counter"]

    @property
    def success_counter(self) -> int:
        return self._data["success_counter"]

    @property
    def opened_at(self) -> datetime | None:
        raw = self._data.get("opened_at")
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw
        # Parse from string
        try:
            return datetime.strptime(raw, _DT_FMT)
        except (ValueError, TypeError):
            return None

    @opened_at.setter
    def opened_at(self, dt: datetime | None) -> None:
        self._data["opened_at"] = dt
        self._save()

    def increment_counter(self) -> None:
        self._data["fail_counter"] += 1
        self._save()

    def reset_counter(self) -> None:
        self._data["fail_counter"] = 0
        self._save()

    def increment_success_counter(self) -> None:
        self._data["success_counter"] += 1
        self._save()

    def reset_success_counter(self) -> None:
        self._data["success_counter"] = 0
        self._save()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Module-level breaker cache (keyed by "breakers_file_path:agent:task_type").
# Including the file path in the key ensures test fixtures that change
# HEAL_TEST_DIR between tests never share a cached breaker instance, since
# the path changes on each test (each test gets a fresh tmp_path).
_BREAKER_CACHE: dict[str, pybreaker.CircuitBreaker] = {}


def get_breaker(agent_name: str, task_type: str) -> pybreaker.CircuitBreaker:
    """Return (or create) the CircuitBreaker for the given agent+task_type pair.

    The breaker uses CircuitFileStorage so state persists across process restarts.
    fail_max=3 and reset_timeout is looked up from BREAKER_TIMEOUTS (per-agent or default).

    The cache is keyed by (breakers_file_path, agent_name, task_type) so that test
    fixtures changing HEAL_TEST_DIR always get a fresh breaker instance pointed at
    the correct tmp directory.
    """
    bf = _get_breakers_file()
    key = f"{agent_name}:{task_type}"
    cache_key = f"{bf}|{key}"
    if cache_key not in _BREAKER_CACHE:
        timeout = BREAKER_TIMEOUTS.get(agent_name, BREAKER_TIMEOUTS["default"])
        storage = CircuitFileStorage(key)
        _BREAKER_CACHE[cache_key] = pybreaker.CircuitBreaker(
            fail_max=3,
            reset_timeout=timeout,
            state_storage=storage,
            name=key,
        )
    return _BREAKER_CACHE[cache_key]


def is_open(agent_name: str, task_type: str) -> bool:
    """Return True if the circuit breaker for agent+task_type is currently OPEN."""
    breaker = get_breaker(agent_name, task_type)
    return breaker.current_state == pybreaker.STATE_OPEN


def record_failure(agent_name: str, task_type: str) -> None:
    """Record one failure for agent+task_type.

    Calls the breaker's protected function with a lambda that raises RuntimeError,
    which is how pybreaker counts failures.  Both CircuitBreakerError (when open)
    and RuntimeError (when counting) are swallowed — callers don't need to handle
    them; they check is_open() separately.
    """
    breaker = get_breaker(agent_name, task_type)
    try:
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("recorded failure")))
    except (pybreaker.CircuitBreakerError, RuntimeError):
        pass


def record_success(agent_name: str, task_type: str) -> None:
    """Record a successful probe for agent+task_type.

    When the breaker is in HALF_OPEN state, a successful call closes it.
    """
    breaker = get_breaker(agent_name, task_type)
    try:
        breaker.call(lambda: True)
    except pybreaker.CircuitBreakerError:
        pass


def reset(agent_name: str, task_type: str) -> None:
    """Reset the circuit breaker for agent+task_type back to CLOSED with zeroed counters."""
    key = f"{agent_name}:{task_type}"
    bf = _get_breakers_file()
    cache_key = f"{bf}|{key}"

    # Remove from in-process cache so a fresh storage instance is created next time
    _BREAKER_CACHE.pop(cache_key, None)

    # Reset the JSON entry to defaults
    try:
        if bf.exists():
            all_data = json.loads(bf.read_text())
        else:
            all_data = {}
    except (json.JSONDecodeError, OSError):
        all_data = {}

    all_data[key] = {
        "state": pybreaker.STATE_CLOSED,
        "fail_counter": 0,
        "success_counter": 0,
        "opened_at": None,
    }

    bf.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = bf.parent / f".circuit-breakers-{os.getpid()}.tmp"
    tmp_path.write_text(json.dumps(all_data, indent=2))
    tmp_path.rename(bf)


def status() -> list[dict]:
    """Return a list of all registered breakers with their current state.

    Each entry: {key, state, fail_counter, opened_at}.
    Returns an empty list if the breakers file doesn't exist yet.
    """
    bf = _get_breakers_file()
    if not bf.exists():
        return []
    try:
        all_data = json.loads(bf.read_text())
    except (json.JSONDecodeError, OSError):
        return []

    result = []
    for key, entry in all_data.items():
        result.append({
            "key": key,
            "state": entry.get("state", pybreaker.STATE_CLOSED),
            "fail_counter": entry.get("fail_counter", 0),
            "opened_at": entry.get("opened_at"),
        })
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Circuit breaker registry — per-agent+task-type file-persisted breakers"
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="Show all circuit breakers and their states (JSON output)")

    # is-open
    p = sub.add_parser("is-open", help="Check if a breaker is open (exit 0=closed, 1=open)")
    p.add_argument("--agent", required=True, help="Agent name (e.g. webagent)")
    p.add_argument("--task-type", required=True, help="Task type (e.g. article)")

    # record-failure
    p = sub.add_parser("record-failure", help="Record a failure for agent+task-type")
    p.add_argument("--agent", required=True)
    p.add_argument("--task-type", required=True)

    # record-success
    p = sub.add_parser("record-success", help="Record a successful probe for agent+task-type")
    p.add_argument("--agent", required=True)
    p.add_argument("--task-type", required=True)

    # reset
    p = sub.add_parser("reset", help="Reset a breaker back to CLOSED")
    p.add_argument("--agent", required=True)
    p.add_argument("--task-type", required=True)

    args = parser.parse_args()

    if args.command == "status":
        print(json.dumps(status(), indent=2))
        sys.exit(0)

    elif args.command == "is-open":
        if is_open(args.agent, args.task_type):
            print(f"OPEN: {args.agent}:{args.task_type}")
            sys.exit(1)
        else:
            print(f"CLOSED: {args.agent}:{args.task_type}")
            sys.exit(0)

    elif args.command == "record-failure":
        record_failure(args.agent, args.task_type)
        print(f"Recorded failure for {args.agent}:{args.task_type}")
        print(json.dumps({"open": is_open(args.agent, args.task_type)}))
        sys.exit(0)

    elif args.command == "record-success":
        record_success(args.agent, args.task_type)
        print(f"Recorded success for {args.agent}:{args.task_type}")
        print(json.dumps({"open": is_open(args.agent, args.task_type)}))
        sys.exit(0)

    elif args.command == "reset":
        reset(args.agent, args.task_type)
        print(f"Reset {args.agent}:{args.task_type} to CLOSED")
        sys.exit(0)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
