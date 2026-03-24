"""Tests for circuit_breaker.py — file-persisted circuit breaker registry.

Requirement coverage:
  HEAL-05: circuit breaker per tool/site prevents flooding a failing external service

Test classes:
  TestBreakerTrips        — trip at fail_max=3, isolation between keys, not-yet-tripped at 2
  TestFilePersistence     — new CircuitFileStorage instance reads persisted state from JSON
  TestHalfOpen            — transitions to half-open after reset_timeout, closes on success
  TestReset               — reset() clears state back to CLOSED with 0 counters
  TestStatus              — status() returns all registered breakers
  TestCLI                 — subprocess "python3 circuit_breaker.py status" returns valid JSON
  TestEnvIsolation        — HEAL_TEST_DIR env var overrides BREAKERS_FILE path
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pybreaker
import pytest

# Import module under test (must be importable)
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import circuit_breaker as cb


class TestBreakerTrips:
    """Verify the circuit breaker trips at fail_max=3 and isolates by key."""

    def test_three_failures_opens_breaker(self, heal_test_dir):
        """record_failure x3 -> is_open == True."""
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        assert cb.is_open("webagent", "article") is True

    def test_two_failures_not_yet_open(self, heal_test_dir):
        """record_failure x2 -> is_open == False (not yet at fail_max)."""
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        assert cb.is_open("webagent", "article") is False

    def test_isolation_between_keys(self, heal_test_dir):
        """Tripping webagent:article does not affect other:article."""
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        assert cb.is_open("webagent", "article") is True
        assert cb.is_open("other", "article") is False


class TestFilePersistence:
    """Verify circuit breaker state survives across separate Python processes (via JSON)."""

    def test_state_persists_across_storage_instances(self, heal_test_dir):
        """New CircuitFileStorage with same key reads persisted OPEN state."""
        key = "webagent:article"
        storage1 = cb.CircuitFileStorage(key)

        # Manually write 3 failures through storage1
        storage1.increment_counter()
        storage1.increment_counter()
        storage1.increment_counter()

        # Transition state to OPEN manually (simulating what pybreaker does internally)
        storage1.state = pybreaker.STATE_OPEN
        storage1.opened_at = datetime.now(timezone.utc)

        # Create a brand-new instance with the same key
        storage2 = cb.CircuitFileStorage(key)

        assert storage2.state == pybreaker.STATE_OPEN
        assert storage2.counter == 3

    def test_json_file_contains_key_after_save(self, heal_test_dir):
        """BREAKERS_FILE is valid JSON and contains the expected key after a save."""
        key = "webagent:article"
        storage = cb.CircuitFileStorage(key)
        storage.increment_counter()

        assert cb.BREAKERS_FILE.exists()
        data = json.loads(cb.BREAKERS_FILE.read_text())
        assert key in data

    def test_json_is_valid_after_multiple_operations(self, heal_test_dir):
        """Multiple reads/writes still produce valid JSON."""
        cb.record_failure("webagent", "article")
        cb.record_failure("other", "task")
        data = json.loads(cb.BREAKERS_FILE.read_text())
        assert isinstance(data, dict)


class TestHalfOpen:
    """Verify the half-open transition after reset_timeout and probe behaviour."""

    def test_half_open_after_timeout(self, heal_test_dir):
        """After opening, setting opened_at in the past exposes half-open state."""
        key = "webagent:article"
        storage = cb.CircuitFileStorage(key)

        # Manually set state to OPEN with an expired opened_at
        storage.state = pybreaker.STATE_OPEN
        # opened_at is reset_timeout + 1 seconds in the past
        reset_timeout = cb.BREAKER_TIMEOUTS.get("webagent", cb.BREAKER_TIMEOUTS["default"])
        storage.opened_at = datetime.now(timezone.utc) - timedelta(seconds=reset_timeout + 1)

        # A fresh get_breaker call should now see HALF_OPEN
        breaker = cb.get_breaker("webagent", "article")
        assert breaker.current_state == pybreaker.STATE_HALF_OPEN

    def test_success_closes_half_open_breaker(self, heal_test_dir):
        """record_success on a half-open breaker closes it."""
        key = "webagent:article"
        storage = cb.CircuitFileStorage(key)

        # Set up half-open state
        storage.state = pybreaker.STATE_OPEN
        reset_timeout = cb.BREAKER_TIMEOUTS.get("webagent", cb.BREAKER_TIMEOUTS["default"])
        storage.opened_at = datetime.now(timezone.utc) - timedelta(seconds=reset_timeout + 1)

        # Probe succeeds — should close the breaker
        cb.record_success("webagent", "article")
        assert cb.is_open("webagent", "article") is False


class TestReset:
    """Verify reset() returns a breaker to CLOSED with zeroed counters."""

    def test_reset_clears_open_breaker(self, heal_test_dir):
        """reset() after trip -> is_open == False."""
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        assert cb.is_open("webagent", "article") is True

        cb.reset("webagent", "article")
        assert cb.is_open("webagent", "article") is False

    def test_reset_zeroes_counters(self, heal_test_dir):
        """reset() produces a storage instance with counter == 0."""
        cb.record_failure("webagent", "article")
        cb.reset("webagent", "article")

        key = "webagent:article"
        storage = cb.CircuitFileStorage(key)
        assert storage.counter == 0
        assert storage.state == pybreaker.STATE_CLOSED


class TestStatus:
    """Verify status() returns all registered breakers with correct shape."""

    def test_status_returns_list(self, heal_test_dir):
        """status() returns a list."""
        result = cb.status()
        assert isinstance(result, list)

    def test_status_includes_recorded_breakers(self, heal_test_dir):
        """status() includes entries for every key that had activity."""
        cb.record_failure("webagent", "article")
        cb.record_failure("other", "task")

        result = cb.status()
        keys = [entry["key"] for entry in result]
        assert "webagent:article" in keys
        assert "other:task" in keys

    def test_status_entry_has_required_fields(self, heal_test_dir):
        """Each status entry has key, state, fail_counter, opened_at."""
        cb.record_failure("webagent", "article")
        result = cb.status()
        entry = next(e for e in result if e["key"] == "webagent:article")
        assert "key" in entry
        assert "state" in entry
        assert "fail_counter" in entry
        assert "opened_at" in entry


class TestCLI:
    """Verify the CLI entry point works as a subprocess."""

    def test_status_cli_exits_zero(self, heal_test_dir):
        """'python3 circuit_breaker.py status' exits 0."""
        script = Path(__file__).parent.parent / "scripts" / "circuit_breaker.py"
        result = subprocess.run(
            [sys.executable, str(script), "status"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_status_cli_outputs_json(self, heal_test_dir):
        """'python3 circuit_breaker.py status' outputs valid JSON."""
        script = Path(__file__).parent.parent / "scripts" / "circuit_breaker.py"
        result = subprocess.run(
            [sys.executable, str(script), "status"],
            capture_output=True,
            text=True,
        )
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, list)

    def test_is_open_cli_closed_exits_zero(self, heal_test_dir):
        """'python3 circuit_breaker.py is-open --agent X --task-type Y' exits 0 when closed."""
        script = Path(__file__).parent.parent / "scripts" / "circuit_breaker.py"
        result = subprocess.run(
            [sys.executable, str(script), "is-open", "--agent", "webagent", "--task-type", "article"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0  # 0 = closed

    def test_is_open_cli_open_exits_one(self, heal_test_dir):
        """'python3 circuit_breaker.py is-open ...' exits 1 when open."""
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")
        cb.record_failure("webagent", "article")

        script = Path(__file__).parent.parent / "scripts" / "circuit_breaker.py"
        result = subprocess.run(
            [sys.executable, str(script), "is-open", "--agent", "webagent", "--task-type", "article"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1  # 1 = open


class TestEnvIsolation:
    """Verify HEAL_TEST_DIR env var overrides the production BREAKERS_FILE path."""

    def test_heal_test_dir_overrides_path(self, heal_test_dir):
        """BREAKERS_FILE points into HEAL_TEST_DIR, not the production path."""
        assert str(heal_test_dir) in str(cb.BREAKERS_FILE)

    def test_no_writes_to_production_path(self, heal_test_dir):
        """Recording failures writes only to the test path."""
        production_path = Path("/home/alex/.openclaw/workspace/memory/growth/circuit-breakers.json")
        cb.record_failure("webagent", "article")
        # Production path should NOT have been touched by this test
        # (it may exist from real runs, but should not have been modified just now)
        assert str(heal_test_dir) in str(cb.BREAKERS_FILE)
        assert cb.BREAKERS_FILE != production_path
