"""Tests for heal.py — error classification, tier selection, Tier 1 retry, Tier 2 alternative.

Requirement coverage:
    HEAL-01: Tier 1 exponential backoff retry for transient failures
    HEAL-02: Tier 2 alternative approach for approach failures
    HEAL-06: Error classification maps error strings to correct recovery tier

Test classes:
    TestErrorClassification — HEAL-06: classify_error() coverage for all 4+unknown classes
    TestSelectTier          — routes tasks to the correct tier (1-4)
    TestTier1Retry          — HEAL-01: @retry decorator, strategy-change enforcement
    TestTier2AlternativeApproach — HEAL-02: get_alternative() returns meaningful strings
"""
import inspect
import json
import subprocess
import sys
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(
    *,
    consecutive_step_errors: int = 0,
    blocked_heartbeats: int = 0,
    attempts: int = 0,
    max_attempts: int = 15,
    last_error: str = "",
    retry_strategy: str | None = None,
):
    """Return a minimal task dict matching task_manager.py structure."""
    return {
        "id": "abc123",
        "consecutive_step_errors": consecutive_step_errors,
        "blocked_heartbeats": blocked_heartbeats,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "last_error": last_error,
        "retry_strategy": retry_strategy,
    }


# ---------------------------------------------------------------------------
# TestErrorClassification  (HEAL-06)
# ---------------------------------------------------------------------------


class TestErrorClassification:
    """classify_error() maps error strings to exactly one of:
    transient, approach, capability, permanent, unknown.
    """

    @pytest.mark.parametrize("error_str,expected", [
        # transient
        ("rate limit exceeded", "transient"),
        ("429 Too Many Requests", "transient"),
        ("connection refused", "transient"),
        ("timeout after 30s", "transient"),
        ("503 Service Unavailable", "transient"),
        # approach
        ("element not found: #submit", "approach"),
        ("captcha detected", "approach"),
        ("permission denied", "approach"),
        ("404 page not found", "approach"),
        # capability
        ("context window exceeded", "capability"),
        ("CUDA out of memory", "capability"),
        ("model not implemented", "capability"),
        # permanent
        ("json decode error in response", "permanent"),
        ("validation error: missing field", "permanent"),
        # unknown
        ("unknown weird error xyz", "unknown"),
    ])
    def test_classify(self, error_str, expected):
        from scripts.heal import classify_error
        result = classify_error(error_str)
        assert result == expected, (
            f"classify_error({error_str!r}) returned {result!r}, expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# TestSelectTier
# ---------------------------------------------------------------------------


class TestSelectTier:
    """select_tier() routes tasks to the correct recovery tier."""

    def test_transient_low_errors_goes_tier1(self):
        from scripts.heal import select_tier
        task = _task(consecutive_step_errors=1, last_error="timeout after 30s")
        assert select_tier(task) == 1

    def test_approach_error_goes_tier2(self):
        from scripts.heal import select_tier
        task = _task(consecutive_step_errors=3, last_error="element not found: #submit")
        assert select_tier(task) == 2

    def test_capability_goes_straight_to_tier3(self):
        from scripts.heal import select_tier
        task = _task(consecutive_step_errors=1, last_error="CUDA out of memory")
        assert select_tier(task) == 3

    def test_exhausted_goes_tier4(self):
        from scripts.heal import select_tier
        task = _task(blocked_heartbeats=5, attempts=14)
        assert select_tier(task) == 4

    def test_max_attempts_reached_goes_tier4(self):
        from scripts.heal import select_tier
        task = _task(attempts=15, max_attempts=15, last_error="timeout")
        assert select_tier(task) == 4

    def test_transient_high_errors_goes_tier2(self):
        from scripts.heal import select_tier
        # 3 consecutive errors on a transient error -> tier 2
        task = _task(consecutive_step_errors=3, last_error="rate limit exceeded")
        assert select_tier(task) == 2


# ---------------------------------------------------------------------------
# TestTier1Retry  (HEAL-01)
# ---------------------------------------------------------------------------


class TestTier1Retry:
    """Tier 1 exponential backoff retry using tenacity @retry decorator."""

    def test_retry_decorator_is_present_on_helper(self):
        """_do_retry_subprocess must be wrapped with tenacity @retry."""
        import scripts.heal as heal
        helper = heal._do_retry_subprocess
        # tenacity wraps functions with a wrapper that has a 'retry' attribute
        # OR the __wrapped__ attribute. Either indicates decoration.
        has_retry_attr = hasattr(helper, "retry")
        has_statistics = hasattr(helper, "statistics")
        has_wrapped = hasattr(helper, "__wrapped__")
        assert has_retry_attr or has_statistics or has_wrapped, (
            "_do_retry_subprocess does not appear to be decorated with @retry. "
            "Expected tenacity wrapper attributes (retry, statistics, or __wrapped__)."
        )

    def test_retry_called_with_different_strategy_than_current(self):
        """tier1_retry calls task_manager.py retry with a strategy string different
        from the task's current retry_strategy."""
        from scripts.heal import tier1_retry

        current_strategy = "use browser automation"
        task = _task(
            last_error="timeout after 30s",
            retry_strategy=current_strategy,
        )

        calls = []

        def fake_subprocess_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("scripts.heal.subprocess.run", side_effect=fake_subprocess_run):
            result = tier1_retry("abc123", task)

        # Find the 'retry' call (not 'show')
        retry_calls = [c for c in calls if "retry" in c]
        assert retry_calls, "No 'retry' subprocess call was made"
        retry_cmd = retry_calls[0]

        # Find --strategy argument
        try:
            strategy_idx = retry_cmd.index("--strategy")
            used_strategy = retry_cmd[strategy_idx + 1]
        except (ValueError, IndexError):
            pytest.fail(f"No --strategy argument found in retry call: {retry_cmd}")

        assert used_strategy != current_strategy, (
            f"tier1_retry used the SAME strategy as current: {used_strategy!r}"
        )

    def test_tier1_skips_to_tier2_when_strategy_unchanged(self):
        """If the proposed strategy equals current retry_strategy, tier1 returns 1
        (skip to next tier) without calling task_manager.py retry."""
        from scripts.heal import tier1_retry, get_alternative, classify_error

        # Build a task whose last_error will produce a specific alternative
        last_error = "connection refused"
        alternative = get_alternative(last_error)

        task = _task(
            last_error=last_error,
            retry_strategy=alternative,  # already at the proposed strategy
        )

        subprocess_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            subprocess_calls.append(cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("scripts.heal.subprocess.run", side_effect=fake_subprocess_run):
            result = tier1_retry("abc123", task)

        retry_calls = [c for c in subprocess_calls if "retry" in c]
        assert not retry_calls, (
            "tier1_retry called task_manager.py retry even though strategy was unchanged"
        )
        assert result == 1, f"Expected exit code 1 (skip to tier 2), got {result}"

    def test_tier1_returns_1_when_subprocess_fails(self):
        """When _do_retry_subprocess exhausts all tenacity retries, tier1 returns 1."""
        from scripts.heal import tier1_retry

        task = _task(last_error="timeout after 30s", retry_strategy=None)

        def fake_subprocess_run(cmd, **kwargs):
            if "retry" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            # 'show' or other calls succeed with minimal JSON
            return mock.MagicMock(returncode=0, stdout=json.dumps(task), stderr="")

        with mock.patch("scripts.heal.subprocess.run", side_effect=fake_subprocess_run):
            # Patch tenacity to not actually sleep during test retries
            with mock.patch("scripts.heal.time.sleep", return_value=None):
                result = tier1_retry("abc123", task)

        assert result == 1


# ---------------------------------------------------------------------------
# TestTier2AlternativeApproach  (HEAL-02)
# ---------------------------------------------------------------------------


class TestTier2AlternativeApproach:
    """get_alternative() returns meaningful alternative strategy strings."""

    @pytest.mark.parametrize("error_fragment,expected_fragment", [
        ("captcha detected", "stealth_browser"),
        ("api_error connection", "browser"),
        ("auth failed: 401", "refresh"),
        ("timeout after 60s", "timeout"),
        ("element not found", "browse.py"),
    ])
    def test_get_alternative_returns_meaningful_string(self, error_fragment, expected_fragment):
        from scripts.heal import get_alternative
        alt = get_alternative(error_fragment)
        assert expected_fragment.lower() in alt.lower(), (
            f"get_alternative({error_fragment!r}) = {alt!r}, "
            f"expected to contain {expected_fragment!r}"
        )

    def test_get_alternative_returns_string_for_unknown_error(self):
        from scripts.heal import get_alternative
        alt = get_alternative("completely unknown problem xyz")
        assert isinstance(alt, str) and len(alt) > 0

    def test_tier2_calls_task_manager_retry(self):
        """tier2_alternative calls task_manager.py retry with an alternative strategy."""
        from scripts.heal import tier2_alternative

        task = _task(last_error="captcha detected")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("scripts.heal.subprocess.run", side_effect=fake_run):
            result = tier2_alternative("abc123", task)

        retry_calls = [c for c in calls if "retry" in c]
        assert retry_calls, "tier2_alternative did not call task_manager.py retry"

        # Verify --strategy was provided
        cmd = retry_calls[0]
        assert "--strategy" in cmd, f"No --strategy in tier2 retry call: {cmd}"
        assert result == 0

    def test_tier2_returns_1_on_subprocess_error(self):
        """tier2_alternative returns 1 when task_manager.py retry fails."""
        from scripts.heal import tier2_alternative

        task = _task(last_error="captcha detected")

        def fake_run(cmd, **kwargs):
            if "retry" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("scripts.heal.subprocess.run", side_effect=fake_run):
            result = tier2_alternative("abc123", task)

        assert result == 1
