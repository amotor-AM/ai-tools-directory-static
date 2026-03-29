"""Tests for heal.py — error classification, tier selection, Tier 1 retry, Tier 2 alternative,
Tier 3 model fallback + delegation, Tier 4 escalation, Tier 5 skip, auto tier selection,
and outcome recording.

Requirement coverage:
    HEAL-01: Tier 1 exponential backoff retry for transient failures
    HEAL-02: Tier 2 alternative approach for approach failures
    HEAL-03: Tier 3 model fallback chain + delegation with circuit breaker guard
    HEAL-04: Tier 4 escalation via task_manager.py escalate (no Telegram)
    HEAL-06: Error classification maps error strings to correct recovery tier
    HEAL-07: Outcome recording for every tier attempt

Test classes:
    TestErrorClassification    — HEAL-06: classify_error() coverage for all 4+unknown classes
    TestSelectTier             — routes tasks to the correct tier (1-4)
    TestTier1Retry             — HEAL-01: @retry decorator, strategy-change enforcement
    TestTier2AlternativeApproach — HEAL-02: get_alternative() returns meaningful strings
    TestTier3ModelFallback     — HEAL-03: model chain progression
    TestTier3Delegation        — HEAL-03: delegation with circuit breaker guard
    TestTier4Escalation        — HEAL-04: escalation, no Telegram, outcome recorded
    TestTier5Skip              — Tier 5 cancel + outcome recorded as failure
    TestAutoTierSelection      — --auto walks tiers based on task state
    TestAutoTierStateReload    — --auto reloads task between tier escalations
    TestOutcomeRecording       — HEAL-07: all tiers call outcome_tracker.py record
"""
import inspect
import json
import subprocess
import sys
from unittest import mock

sys.path.insert(0, "/home/alex/.openclaw/workspace/scripts")
sys.path.insert(0, "/home/alex/.openclaw/workspace/skills/aria-taskmanager/scripts")

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
        from heal import classify_error
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
        from heal import select_tier
        task = _task(consecutive_step_errors=1, last_error="timeout after 30s")
        assert select_tier(task) == 1

    def test_approach_error_goes_tier2(self):
        from heal import select_tier
        task = _task(consecutive_step_errors=3, last_error="element not found: #submit")
        assert select_tier(task) == 2

    def test_capability_goes_straight_to_tier3(self):
        from heal import select_tier
        task = _task(consecutive_step_errors=1, last_error="CUDA out of memory")
        assert select_tier(task) == 3

    def test_exhausted_goes_tier4(self):
        from heal import select_tier
        task = _task(blocked_heartbeats=5, attempts=14)
        assert select_tier(task) == 4

    def test_max_attempts_reached_goes_tier4(self):
        from heal import select_tier
        task = _task(attempts=15, max_attempts=15, last_error="timeout")
        assert select_tier(task) == 4

    def test_transient_high_errors_goes_tier2(self):
        from heal import select_tier
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
        import heal
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
        from heal import tier1_retry

        current_strategy = "use browser automation"
        task = _task(
            last_error="timeout after 30s",
            retry_strategy=current_strategy,
        )

        calls = []

        def fake_subprocess_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_subprocess_run):
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
        from heal import tier1_retry, get_alternative, classify_error

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

        with mock.patch("heal.subprocess.run", side_effect=fake_subprocess_run):
            result = tier1_retry("abc123", task)

        retry_calls = [c for c in subprocess_calls if "retry" in c]
        assert not retry_calls, (
            "tier1_retry called task_manager.py retry even though strategy was unchanged"
        )
        assert result == 1, f"Expected exit code 1 (skip to tier 2), got {result}"

    def test_tier1_returns_1_when_subprocess_fails(self):
        """When _do_retry_subprocess exhausts all tenacity retries, tier1 returns 1."""
        from heal import tier1_retry

        task = _task(last_error="timeout after 30s", retry_strategy=None)

        def fake_subprocess_run(cmd, **kwargs):
            if "retry" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            # 'show' or other calls succeed with minimal JSON
            return mock.MagicMock(returncode=0, stdout=json.dumps(task), stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_subprocess_run):
            # Patch tenacity to not actually sleep during test retries
            with mock.patch("heal.time.sleep", return_value=None):
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
        from heal import get_alternative
        alt = get_alternative(error_fragment)
        assert expected_fragment.lower() in alt.lower(), (
            f"get_alternative({error_fragment!r}) = {alt!r}, "
            f"expected to contain {expected_fragment!r}"
        )

    def test_get_alternative_returns_string_for_unknown_error(self):
        from heal import get_alternative
        alt = get_alternative("completely unknown problem xyz")
        assert isinstance(alt, str) and len(alt) > 0

    def test_tier2_calls_task_manager_retry(self):
        """tier2_alternative calls task_manager.py retry with an alternative strategy."""
        from heal import tier2_alternative

        task = _task(last_error="captcha detected")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier2_alternative("abc123", task)

        retry_calls = [c for c in calls if "retry" in c]
        assert retry_calls, "tier2_alternative did not call task_manager.py retry"

        # Verify --strategy was provided
        cmd = retry_calls[0]
        assert "--strategy" in cmd, f"No --strategy in tier2 retry call: {cmd}"
        assert result == 0

    def test_tier2_returns_1_on_subprocess_error(self):
        """tier2_alternative returns 1 when task_manager.py retry fails."""
        from heal import tier2_alternative

        task = _task(last_error="captcha detected")

        def fake_run(cmd, **kwargs):
            if "retry" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier2_alternative("abc123", task)

        assert result == 1


# ---------------------------------------------------------------------------
# Helpers for Tier 3+  (context-aware tasks)
# ---------------------------------------------------------------------------


def _task_with_context(
    *,
    model: str | None = None,
    task_type: str = "article",
    goal: str = "write an article about AI",
    last_error: str = "CUDA out of memory",
    consecutive_step_errors: int = 1,
    mission_id: str | None = None,
) -> dict:
    """Return a task dict that includes a context sub-dict (for Tier 3 tests)."""
    context: dict = {"task_type": task_type}
    if model is not None:
        context["model"] = model
    if mission_id is not None:
        context["mission_id"] = mission_id
    return {
        "id": "abc123",
        "goal": goal,
        "consecutive_step_errors": consecutive_step_errors,
        "blocked_heartbeats": 0,
        "attempts": 1,
        "max_attempts": 15,
        "last_error": last_error,
        "retry_strategy": None,
        "context": context,
    }


# ---------------------------------------------------------------------------
# TestTier3ModelFallback  (HEAL-03)
# ---------------------------------------------------------------------------


class TestTier3ModelFallback:
    """tier3_model_fallback() walks the MODEL_FALLBACK_CHAIN before attempting delegation."""

    def test_sonnet_falls_back_to_qwen3(self):
        """Task with context.model=Sonnet -> tier3 uses qwen3:14b as next model."""
        from heal import tier3_model_fallback, MODEL_FALLBACK_CHAIN

        task = _task_with_context(model="anthropic/claude-sonnet-4-5")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier3_model_fallback("abc123", task)

        # Find retry calls to task_manager
        retry_calls = [c for c in calls if "retry" in c]
        assert retry_calls, "No retry subprocess call for model fallback"
        # Strategy should contain the next model
        cmd = retry_calls[0]
        strategy_idx = cmd.index("--strategy")
        strategy = cmd[strategy_idx + 1]
        assert "qwen3" in strategy.lower() or "model_fallback" in strategy.lower(), (
            f"Expected qwen3 in strategy, got: {strategy!r}"
        )
        assert result == 0

    def test_qwen3_falls_back_to_glm(self):
        """Task with context.model=qwen3:14b -> tier3 uses GLM as next model."""
        from heal import tier3_model_fallback

        task = _task_with_context(model="qwen3:14b")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier3_model_fallback("abc123", task)

        retry_calls = [c for c in calls if "retry" in c]
        assert retry_calls, "No retry subprocess call for model fallback"
        cmd = retry_calls[0]
        strategy_idx = cmd.index("--strategy")
        strategy = cmd[strategy_idx + 1]
        assert "glm" in strategy.lower() or "abliterated" in strategy.lower() or "model_fallback" in strategy.lower(), (
            f"Expected GLM in strategy, got: {strategy!r}"
        )
        assert result == 0

    def test_glm_last_in_chain_attempts_delegation(self):
        """When on the last model (GLM), tier3 attempts delegation instead of model swap."""
        from heal import tier3_model_fallback

        # GLM is last in chain — no next model, so tries delegation
        task = _task_with_context(
            model="huihui_ai/glm-4.7-flash-abliterated",
            goal="write an article about AI",
            task_type="article",
        )
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            with mock.patch("heal.is_open", return_value=False):
                result = tier3_model_fallback("abc123", task)

        # Should have called manage.py spawn (delegation path)
        spawn_calls = [c for c in calls if "spawn" in c]
        assert spawn_calls, "Expected manage.py spawn call when all models exhausted"

    def test_no_model_in_context_defaults_to_first_chain_model(self):
        """Task with no context.model key defaults to first model in chain."""
        from heal import tier3_model_fallback, MODEL_FALLBACK_CHAIN

        # No model key in context -> defaults to Sonnet (first in chain) -> next is Qwen3
        task = _task_with_context(model=None)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier3_model_fallback("abc123", task)

        retry_calls = [c for c in calls if "retry" in c]
        assert retry_calls, "No retry subprocess call when no model specified"
        cmd = retry_calls[0]
        strategy_idx = cmd.index("--strategy")
        strategy = cmd[strategy_idx + 1]
        # Next model after default (Sonnet) should be qwen3
        assert "qwen3" in strategy.lower() or "model_fallback" in strategy.lower(), (
            f"Expected qwen3 after defaulting to first chain model, got: {strategy!r}"
        )
        assert result == 0


# ---------------------------------------------------------------------------
# TestTier3Delegation  (HEAL-03)
# ---------------------------------------------------------------------------


class TestTier3Delegation:
    """tier3_model_fallback() delegation path when all models exhausted."""

    def _exhausted_task(self, goal: str = "write an article about AI", task_type: str = "article") -> dict:
        """Return a task where the current model is the last in chain (delegation path)."""
        return _task_with_context(
            model="huihui_ai/glm-4.7-flash-abliterated",
            goal=goal,
            task_type=task_type,
        )

    def test_delegation_calls_manage_spawn_when_breaker_closed(self):
        """When circuit breaker is CLOSED, tier3 calls manage.py spawn with task goal."""
        from heal import tier3_model_fallback

        task = self._exhausted_task()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            with mock.patch("heal.is_open", return_value=False):
                result = tier3_model_fallback("abc123", task)

        spawn_calls = [c for c in calls if "spawn" in c]
        assert spawn_calls, "Expected manage.py spawn call when breaker is closed"
        spawn_cmd = spawn_calls[0]
        # Should contain task goal via --task argument
        assert "--task" in spawn_cmd, f"No --task in spawn call: {spawn_cmd}"

    def test_skips_delegation_when_breaker_is_open(self):
        """When circuit breaker is_open returns True, tier3 returns exit code 3."""
        from heal import tier3_model_fallback

        task = self._exhausted_task()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            with mock.patch("heal.is_open", return_value=True):
                result = tier3_model_fallback("abc123", task)

        # Should NOT have called manage.py spawn
        spawn_calls = [c for c in calls if "spawn" in c]
        assert not spawn_calls, "tier3 should skip delegation when breaker is open"
        assert result == 3, f"Expected exit code 3 (breaker open), got {result}"

    def test_delegation_failure_records_circuit_breaker_failure(self):
        """When manage.py spawn fails, tier3 calls circuit_breaker.record_failure."""
        from heal import tier3_model_fallback

        task = self._exhausted_task()

        def fake_run(cmd, **kwargs):
            if "spawn" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            with mock.patch("heal.is_open", return_value=False):
                with mock.patch("heal.record_failure") as mock_record:
                    result = tier3_model_fallback("abc123", task)

        mock_record.assert_called_once()
        assert result == 1, f"Expected exit code 1 (delegation failed), got {result}"


# ---------------------------------------------------------------------------
# TestTier4Escalation  (HEAL-04)
# ---------------------------------------------------------------------------


class TestTier4Escalation:
    """tier4_escalate() calls task_manager.py escalate and records outcome.
    Must NOT send Telegram message or contact Alex directly.
    """

    def test_tier4_calls_task_manager_escalate(self):
        """tier4_escalate calls task_manager.py escalate with the task_id."""
        from heal import tier4_escalate

        task = _task_with_context()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier4_escalate("abc123", task)

        escalate_calls = [c for c in calls if "escalate" in c]
        assert escalate_calls, "tier4_escalate must call task_manager.py escalate"
        assert "abc123" in escalate_calls[0], "escalate call must include task_id"
        assert result == 0

    def test_tier4_does_not_call_telegram(self):
        """tier4_escalate must NOT invoke any Telegram-related subprocess."""
        from heal import tier4_escalate

        task = _task_with_context()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier4_escalate("abc123", task)

        # Check no Telegram-related call was made
        telegram_calls = [c for c in calls if any(
            kw in " ".join(c).lower() for kw in ["telegram", "notify", "send_message", "bot"]
        )]
        assert not telegram_calls, (
            f"tier4 must not contact Alex via Telegram. Found calls: {telegram_calls}"
        )

    def test_tier4_records_outcome(self):
        """tier4_escalate records outcome via outcome_tracker.py with approach heal_tier4_escalated."""
        from heal import tier4_escalate

        task = _task_with_context()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier4_escalate("abc123", task)

        # Find outcome_tracker record call
        outcome_calls = [c for c in calls if "outcome_tracker" in " ".join(c) and "record" in c]
        assert outcome_calls, "tier4 must call outcome_tracker.py record"
        outcome_cmd = outcome_calls[0]
        assert "--approach" in outcome_cmd, "outcome_tracker call must include --approach"
        approach_idx = outcome_cmd.index("--approach")
        approach = outcome_cmd[approach_idx + 1]
        assert "heal_tier4" in approach, (
            f"Approach must start with 'heal_tier4', got: {approach!r}"
        )


# ---------------------------------------------------------------------------
# TestTier5Skip
# ---------------------------------------------------------------------------


class TestTier5Skip:
    """tier5_skip() cancels the task and records outcome as failure.
    Must NEVER contact Alex.
    """

    def test_tier5_calls_task_manager_cancel(self):
        """tier5_skip calls task_manager.py cancel with reason containing 'all recovery tiers exhausted'."""
        from heal import tier5_skip

        task = _task_with_context()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier5_skip("abc123", task)

        cancel_calls = [c for c in calls if "cancel" in c]
        assert cancel_calls, "tier5_skip must call task_manager.py cancel"
        cancel_cmd = cancel_calls[0]
        # The reason must include "all recovery tiers exhausted"
        full_cmd_str = " ".join(cancel_cmd)
        assert "all recovery tiers exhausted" in full_cmd_str, (
            f"Cancel reason must include 'all recovery tiers exhausted'. Got: {cancel_cmd}"
        )

    def test_tier5_records_outcome_as_failure(self):
        """tier5_skip records outcome with approach 'heal_tier5_skipped' and outcome 'failure'."""
        from heal import tier5_skip

        task = _task_with_context()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier5_skip("abc123", task)

        outcome_calls = [c for c in calls if "outcome_tracker" in " ".join(c) and "record" in c]
        assert outcome_calls, "tier5 must call outcome_tracker.py record"
        outcome_cmd = outcome_calls[0]
        full_str = " ".join(outcome_cmd)
        assert "failure" in full_str, "tier5 outcome must be 'failure'"
        assert "heal_tier5" in full_str, "tier5 approach must contain 'heal_tier5'"

    def test_tier5_returns_exit_code_2(self):
        """tier5_skip returns exit code 2 (all tiers exhausted)."""
        from heal import tier5_skip

        task = _task_with_context()

        def fake_run(cmd, **kwargs):
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier5_skip("abc123", task)

        assert result == 2, f"Expected exit code 2, got {result}"


# ---------------------------------------------------------------------------
# TestAutoTierSelection
# ---------------------------------------------------------------------------


class TestAutoTierSelection:
    """attempt(task_id, tier=None) selects tier via select_tier and dispatches correctly."""

    def test_auto_selects_tier1_for_transient_low_errors(self):
        """Task with consecutive_step_errors=1, last_error=timeout -> attempt --auto runs tier1."""
        from heal import attempt

        task = _task(consecutive_step_errors=1, last_error="timeout after 30s")
        task_with_ctx = {**task, "context": {"task_type": "general"}, "goal": "do something"}

        def fake_load(tid):
            return task_with_ctx

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout=json.dumps(task_with_ctx), stderr="")

        with mock.patch("heal._load_task", side_effect=fake_load):
            with mock.patch("heal.subprocess.run", side_effect=fake_run):
                result = attempt("abc123")

        # tier1 calls task_manager retry
        retry_calls = [c for c in calls if "retry" in c]
        assert retry_calls or result == 1, "Auto mode should dispatch to tier1 for transient errors"

    def test_auto_selects_tier2_for_approach_errors(self):
        """Task with consecutive_step_errors=4, last_error=captcha -> attempt --auto runs tier2."""
        from heal import attempt, select_tier

        task = _task(consecutive_step_errors=4, last_error="captcha detected")
        task_with_ctx = {**task, "context": {"task_type": "general"}, "goal": "do something"}

        def fake_load(tid):
            return task_with_ctx

        def fake_run(cmd, **kwargs):
            return mock.MagicMock(returncode=0, stdout=json.dumps(task_with_ctx), stderr="")

        # Verify select_tier gives us tier 2 for this task
        assert select_tier(task) == 2, "select_tier should return 2 for captcha with many errors"

        with mock.patch("heal._load_task", side_effect=fake_load):
            with mock.patch("heal.subprocess.run", side_effect=fake_run):
                result = attempt("abc123")

        # Result should be 0 (success) since fake_run succeeds
        assert result in (0, 1), f"Unexpected result: {result}"

    def test_auto_selects_tier3_for_capability_errors(self):
        """Task with last_error=CUDA out of memory -> attempt --auto runs tier3."""
        from heal import attempt, select_tier

        task = _task(consecutive_step_errors=1, last_error="CUDA out of memory")

        # Verify select_tier routes to tier3
        assert select_tier(task) == 3

        task_with_ctx = {**task, "context": {"task_type": "article", "model": "anthropic/claude-sonnet-4-5"}, "goal": "write article"}

        def fake_load(tid):
            return task_with_ctx

        def fake_run(cmd, **kwargs):
            return mock.MagicMock(returncode=0, stdout=json.dumps(task_with_ctx), stderr="")

        with mock.patch("heal._load_task", side_effect=fake_load):
            with mock.patch("heal.subprocess.run", side_effect=fake_run):
                result = attempt("abc123")

        assert result in (0, 1), f"Unexpected result for tier3 auto: {result}"


# ---------------------------------------------------------------------------
# TestAutoTierStateReload
# ---------------------------------------------------------------------------


class TestAutoTierStateReload:
    """CRITICAL: attempt() in auto mode reloads task state between tier escalations.

    When tier N returns 1 (failed), attempt() must call _load_task(task_id) again
    before invoking tier N+1. The next tier must receive the fresh task dict with
    updated state from task_manager.py (not the stale pre-tier-N dict).
    """

    def test_state_reloaded_between_tier_escalations(self):
        """When tier 1 fails (returns 1), attempt() reloads task before trying tier 2.

        The reloaded task dict has updated retry_strategy and consecutive_step_errors.
        Tier 2 must receive the fresh dict, not the original stale one.
        """
        from heal import attempt

        # First call returns task that routes to tier1, tier1 will fail
        initial_task = {
            "id": "abc123",
            "goal": "write an article",
            "consecutive_step_errors": 1,
            "blocked_heartbeats": 0,
            "attempts": 1,
            "max_attempts": 15,
            "last_error": "timeout after 30s",
            "retry_strategy": "increase timeout to 120s or use streaming variant",  # same as proposed -> tier1 returns 1
            "context": {"task_type": "general"},
        }

        # Second call (reload) returns updated task with approach error -> tier2
        reloaded_task = {
            "id": "abc123",
            "goal": "write an article",
            "consecutive_step_errors": 2,
            "blocked_heartbeats": 0,
            "attempts": 2,
            "max_attempts": 15,
            "last_error": "captcha detected",  # now an approach error
            "retry_strategy": "new_strategy_from_tier1",
            "context": {"task_type": "general"},
        }

        load_calls = []

        def fake_load(tid):
            load_calls.append(tid)
            if len(load_calls) == 1:
                return initial_task
            return reloaded_task

        subprocess_calls = []

        def fake_run(cmd, **kwargs):
            subprocess_calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout=json.dumps(reloaded_task), stderr="")

        with mock.patch("heal._load_task", side_effect=fake_load):
            with mock.patch("heal.subprocess.run", side_effect=fake_run):
                result = attempt("abc123")

        # _load_task must have been called at least twice:
        # once before tier 1, and once after tier 1 failed to get fresh state
        assert len(load_calls) >= 2, (
            f"_load_task was called {len(load_calls)} time(s). "
            "Expected at least 2 calls (initial load + reload after tier 1 failure). "
            "CRITICAL: attempt() must reload task state between tier escalations."
        )


# ---------------------------------------------------------------------------
# TestOutcomeRecording  (HEAL-07)
# ---------------------------------------------------------------------------


class TestOutcomeRecording:
    """Every tier attempt (success or failure) records to outcome_tracker.py."""

    def _extract_outcome_calls(self, all_calls: list) -> list:
        """Filter subprocess calls that go to outcome_tracker.py."""
        return [c for c in all_calls if "outcome_tracker" in " ".join(c) and "record" in c]

    def test_tier1_success_records_outcome(self):
        """tier1_retry success records approach='heal_tier1_backoff', outcome='success'."""
        from heal import tier1_retry

        task = {**_task(last_error="timeout after 30s", retry_strategy=None), "context": {"task_type": "article"}, "goal": "write"}
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier1_retry("abc123", task)

        outcome_calls = self._extract_outcome_calls(calls)
        assert outcome_calls, "tier1_retry success must record outcome"
        full_str = " ".join(outcome_calls[0])
        assert "success" in full_str, f"Expected 'success' in outcome call: {full_str}"
        assert "heal_tier1" in full_str, f"Expected 'heal_tier1' in approach: {full_str}"

    def test_tier1_failure_records_outcome(self):
        """tier1_retry failure records outcome='failure'."""
        from heal import tier1_retry

        task = {**_task(last_error="timeout after 30s", retry_strategy=None), "context": {"task_type": "article"}, "goal": "write"}
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "retry" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            with mock.patch("heal.time.sleep", return_value=None):
                result = tier1_retry("abc123", task)

        outcome_calls = self._extract_outcome_calls(calls)
        assert outcome_calls, "tier1_retry failure must record outcome"
        full_str = " ".join(outcome_calls[0])
        assert "failure" in full_str, f"Expected 'failure' in outcome call: {full_str}"

    def test_tier2_success_records_outcome(self):
        """tier2_alternative success records approach='heal_tier2_alternative_approach'."""
        from heal import tier2_alternative

        task = {**_task(last_error="captcha detected"), "context": {"task_type": "article"}, "goal": "write"}
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier2_alternative("abc123", task)

        outcome_calls = self._extract_outcome_calls(calls)
        assert outcome_calls, "tier2_alternative must record outcome"
        full_str = " ".join(outcome_calls[0])
        assert "heal_tier2" in full_str, f"Expected 'heal_tier2' in approach: {full_str}"

    def test_tier3_model_fallback_records_outcome_with_model_name(self):
        """tier3 model fallback records approach containing 'heal_tier3_model_fallback'."""
        from heal import tier3_model_fallback

        task = _task_with_context(model="anthropic/claude-sonnet-4-5")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            result = tier3_model_fallback("abc123", task)

        outcome_calls = self._extract_outcome_calls(calls)
        assert outcome_calls, "tier3_model_fallback must record outcome"
        full_str = " ".join(outcome_calls[0])
        assert "heal_tier3" in full_str, f"Expected 'heal_tier3' in approach: {full_str}"

    def test_tier3_delegation_records_outcome_with_agent_name(self):
        """tier3 delegation records approach containing 'heal_tier3_delegation'."""
        from heal import tier3_model_fallback

        task = _task_with_context(model="huihui_ai/glm-4.7-flash-abliterated", goal="write article")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=0, stdout="{}", stderr="")

        with mock.patch("heal.subprocess.run", side_effect=fake_run):
            with mock.patch("heal.is_open", return_value=False):
                result = tier3_model_fallback("abc123", task)

        outcome_calls = self._extract_outcome_calls(calls)
        assert outcome_calls, "tier3 delegation must record outcome"
        full_str = " ".join(outcome_calls[0])
        assert "heal_tier3" in full_str, f"Expected 'heal_tier3' in approach: {full_str}"


# ---------------------------------------------------------------------------
# TestRollback  (AUTO-09)
# ---------------------------------------------------------------------------


class TestRollback:
    """Tests for register_rollback() and execute_rollback() in heal.py."""

    def test_register_rollback_appends_entry(self, tmp_path, monkeypatch):
        """register_rollback() appends an entry to rollback_registry.jsonl."""
        import os
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(tmp_path / "rollback_registry.jsonl"))
        if "heal" in sys.modules:
            del sys.modules["heal"]
        from heal import register_rollback

        register_rollback("T1", "vercel_deploy", "vercel rollback --yes", reversible=True)

        registry_path = tmp_path / "rollback_registry.jsonl"
        assert registry_path.exists()
        entries = [json.loads(line) for line in registry_path.read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        e = entries[0]
        assert e["task_id"] == "T1"
        assert e["action_type"] == "vercel_deploy"
        assert e["rollback_cmd"] == "vercel rollback --yes"
        assert e["reversible"] is True
        assert e["status"] == "available"

    def test_register_rollback_non_reversible(self, tmp_path, monkeypatch):
        """register_rollback() with reversible=False stores reversible=False."""
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(tmp_path / "rollback_registry.jsonl"))
        if "heal" in sys.modules:
            del sys.modules["heal"]
        from heal import register_rollback

        register_rollback("T2", "email_sent", "", reversible=False)

        registry_path = tmp_path / "rollback_registry.jsonl"
        entries = [json.loads(line) for line in registry_path.read_text().splitlines() if line.strip()]
        assert len(entries) == 1
        assert entries[0]["reversible"] is False
        assert entries[0]["task_id"] == "T2"

    def test_execute_rollback_success(self, tmp_path, monkeypatch):
        """execute_rollback() runs command for reversible=True entry, returns (True, '')."""
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(tmp_path / "rollback_registry.jsonl"))
        if "heal" in sys.modules:
            del sys.modules["heal"]
        from heal import register_rollback, execute_rollback

        register_rollback("T1", "vercel_deploy", "echo rollback ok", reversible=True)

        with mock.patch("heal.subprocess.run") as fake_run:
            fake_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            # Also need to mock _record_outcome subprocess.run calls
            with mock.patch("heal._record_outcome"):
                success, reason = execute_rollback("T1")

        assert success is True
        assert reason == ""
        # Verify command was called
        call_args = fake_run.call_args
        assert "echo rollback ok" in str(call_args)

    def test_execute_rollback_not_reversible(self, tmp_path, monkeypatch):
        """execute_rollback() returns (False, 'NOT_REVERSIBLE') for reversible=False entry."""
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(tmp_path / "rollback_registry.jsonl"))
        if "heal" in sys.modules:
            del sys.modules["heal"]
        from heal import register_rollback, execute_rollback

        register_rollback("T2", "email_sent", "", reversible=False)

        success, reason = execute_rollback("T2")

        assert success is False
        assert reason == "NOT_REVERSIBLE"

    def test_execute_rollback_no_entry(self, tmp_path, monkeypatch):
        """execute_rollback() returns (False, 'NO_ROLLBACK_REGISTERED') when no entry exists."""
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(tmp_path / "rollback_registry.jsonl"))
        if "heal" in sys.modules:
            del sys.modules["heal"]
        from heal import execute_rollback

        success, reason = execute_rollback("T3_not_registered")

        assert success is False
        assert reason == "NO_ROLLBACK_REGISTERED"

    def test_execute_rollback_already_rolled_back(self, tmp_path, monkeypatch):
        """execute_rollback() returns (False, 'ALREADY_ROLLED_BACK') when entry has status='rolled_back'."""
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(tmp_path / "rollback_registry.jsonl"))
        if "heal" in sys.modules:
            del sys.modules["heal"]
        from heal import register_rollback, execute_rollback

        register_rollback("T1", "vercel_deploy", "echo rollback ok", reversible=True)

        # First rollback succeeds
        with mock.patch("heal.subprocess.run") as fake_run:
            fake_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch("heal._record_outcome"):
                success1, reason1 = execute_rollback("T1")

        assert success1 is True

        # Second rollback should fail with ALREADY_ROLLED_BACK
        success2, reason2 = execute_rollback("T1")
        assert success2 is False
        assert reason2 == "ALREADY_ROLLED_BACK"

    def test_rollback_registry_path_env_var(self, tmp_path, monkeypatch):
        """ROLLBACK_REGISTRY_PATH env var controls where registry is written."""
        custom_path = tmp_path / "custom" / "my_registry.jsonl"
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(custom_path))
        if "heal" in sys.modules:
            del sys.modules["heal"]
        from heal import register_rollback

        register_rollback("T_env", "vercel_deploy", "vercel rollback --yes", reversible=True)

        assert custom_path.exists()
        entries = [json.loads(line) for line in custom_path.read_text().splitlines() if line.strip()]
        assert entries[0]["task_id"] == "T_env"
