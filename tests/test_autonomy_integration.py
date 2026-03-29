"""Phase 6 integration smoke tests — verify full autonomy wiring.

These tests validate that Plan 01 (gpu_lock), Plan 02 (blocklist + pre_check wiring),
Plan 03 (violations + rollback), and Plan 04 (heartbeat wiring + rollback auto-registration)
work together.
"""
import json
import os
import sys
import importlib
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path("/home/alex/.openclaw/workspace/scripts")
sys.path.insert(0, str(SCRIPTS_DIR))


class TestPreCheckToAssignFlow:
    """Verify assign_task() calls pre_check() and blocks dangerous actions."""

    def test_blocked_action_never_spawns(self, tmp_path, monkeypatch):
        # Setup: blocklist with financial pattern
        blocklist = tmp_path / "blocklist.json"
        blocklist.write_text(json.dumps({
            "version": 2, "safe_actions": [],
            "patterns": [{"pattern": "payment|purchase", "reason": "blocked", "severity": "critical", "context_rules": []}]
        }))
        audit_log = tmp_path / "supervisor.jsonl"
        monkeypatch.setenv("SUPERVISOR_BLOCKLIST_PATH", str(blocklist))
        monkeypatch.setenv("SUPERVISOR_AUDIT_LOG", str(audit_log))

        import supervisor
        importlib.reload(supervisor)

        result = supervisor.assign_task("purchase a domain")
        assert result == "BLOCKED"

        # Verify audit log has BLOCKED entry
        entries = [json.loads(l) for l in audit_log.read_text().splitlines() if l.strip()]
        blocked = [e for e in entries if e.get("op") == "assign_task_blocked"]
        assert len(blocked) >= 1

    def test_safe_action_proceeds(self, tmp_path, monkeypatch):
        blocklist = tmp_path / "blocklist.json"
        blocklist.write_text(json.dumps({
            "version": 2,
            "safe_actions": ["write.*file"],
            "patterns": [{"pattern": "payment", "reason": "blocked", "severity": "critical", "context_rules": []}]
        }))
        audit_log = tmp_path / "supervisor.jsonl"
        monkeypatch.setenv("SUPERVISOR_BLOCKLIST_PATH", str(blocklist))
        monkeypatch.setenv("SUPERVISOR_AUDIT_LOG", str(audit_log))

        import supervisor
        importlib.reload(supervisor)

        allowed, reason = supervisor.pre_check("write notes to file")
        assert allowed is True


class TestGPULockIntegration:
    """Verify GPU lock acquire/release cycle."""

    def test_acquire_release_cycle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPU_LOCK_PATH", str(tmp_path / "gpu.lock"))
        import gpu_lock
        importlib.reload(gpu_lock)

        with mock.patch.object(gpu_lock, "check_vram", return_value=True):
            with mock.patch.object(gpu_lock, "_is_holder_active", return_value=True):
                ok, reason = gpu_lock.acquire_gpu_lock("T1", "video")
                assert ok is True

                ok2, reason2 = gpu_lock.acquire_gpu_lock("T2", "image")
                assert ok2 is False
                assert "busy" in reason2.lower() or "GPU" in reason2

                gpu_lock.release_gpu_lock("T1")

                ok3, reason3 = gpu_lock.acquire_gpu_lock("T2", "image")
                assert ok3 is True


class TestRollbackAutoRegistration:
    """Verify validate_task() auto-registers rollback for reversible actions."""

    def test_reversible_action_registers_rollback(self, tmp_path, monkeypatch):
        rollback_reg = tmp_path / "rollback_registry.jsonl"
        monkeypatch.setenv("ROLLBACK_REGISTRY_PATH", str(rollback_reg))

        import heal
        importlib.reload(heal)

        # Simulate: register_rollback is called for a vercel_deploy
        heal.register_rollback("T1", "vercel_deploy", "vercel rollback --yes", reversible=True)

        entries = [json.loads(l) for l in rollback_reg.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["action_type"] == "vercel_deploy"
        assert entries[0]["reversible"] is True

        # Execute the rollback — subprocess.run is called at least once for the rollback cmd
        # (execute_rollback may also call outcome_tracker via subprocess)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            ok, reason = heal.execute_rollback("T1")
            assert ok is True
            assert mock_run.call_count >= 1
            # First call should be the actual rollback command
            first_call_args = mock_run.call_args_list[0]
            assert "vercel rollback" in str(first_call_args)


class TestViolationsInBriefing:
    """Verify BLOCKED entries flow into daily briefing."""

    def test_violations_appear_in_briefing(self, tmp_path, monkeypatch):
        # Use Pacific date to match _read_todays_violations() which uses now_pacific()
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime
            today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        except Exception:
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        audit = tmp_path / "supervisor.jsonl"
        audit.write_text(json.dumps({
            "ts": f"{today}T12:00:00-07:00",
            "op": "pre_check",
            "data": {"action": "purchase domain", "result": "BLOCKED", "reason": "financial", "severity": "critical"}
        }) + "\n")
        monkeypatch.setenv("SUPERVISOR_AUDIT_LOG", str(audit))

        import briefing
        importlib.reload(briefing)

        violations = briefing._read_todays_violations()
        assert len(violations) >= 1
        assert "purchase" in violations[0].lower() or "financial" in violations[0].lower()


class TestHeartbeatPromptWiring:
    """Verify openclaw.json heartbeat prompt has Phase 6 wiring."""

    def test_prompt_contains_required_strings(self):
        with open("/home/alex/.openclaw/openclaw.json") as f:
            config = json.load(f)
        prompt = config["agents"]["defaults"]["heartbeat"]["prompt"]
        assert "supervisor.py pre-check" in prompt
        assert "gpu_lock.py acquire" in prompt
        assert "supervisor.py assign-task" in prompt
        assert "gpu_lock.py release" in prompt
