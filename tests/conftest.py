"""Shared pytest fixtures for Aria test suite.

Provides isolated directories for mission engine and task manager tests,
mirroring the ARIA_TASK_DIR pattern established in Phase 1.
"""
import os
import pytest


@pytest.fixture
def mission_dir(tmp_path):
    """Isolated mission directory for testing. Parallel to ARIA_TASK_DIR pattern."""
    mdir = tmp_path / "missions"
    mdir.mkdir()
    (mdir / "archive").mkdir()
    (mdir / "schema").mkdir()
    os.environ["MISSION_DIR"] = str(mdir)
    yield mdir
    del os.environ["MISSION_DIR"]


@pytest.fixture
def task_state_dir(tmp_path):
    """Isolated task state directory for mission-task integration tests."""
    sdir = tmp_path / "task_state"
    sdir.mkdir()
    os.environ["ARIA_TASK_DIR"] = str(sdir)
    yield sdir
    del os.environ["ARIA_TASK_DIR"]
