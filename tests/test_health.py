"""Tests for the health module, focusing on _check_self_healing_daemon."""
import time
from pathlib import Path

import pytest

from health import (
    CheckResult,
    HealthStatus,
    _check_self_healing_daemon,
)


@pytest.fixture
def queue_root(tmp_path):
    """Create a temporary self_healing_queue directory."""
    q = tmp_path / "self_healing_queue"
    q.mkdir()
    return q


def _write_heartbeat(queue_root: Path, age_seconds: float = 0) -> Path:
    """Write a heartbeat file with an mtime adjusted by age_seconds in the past."""
    hb = queue_root / "heartbeat-worker-1.txt"
    hb.touch()
    if age_seconds > 0:
        mtime = time.time() - age_seconds
        import os
        os.utime(hb, (mtime, mtime))
    return hb


# ---------------------------------------------------------------------------
# Missing heartbeat file
# ---------------------------------------------------------------------------

def test_missing_heartbeat_is_crit(queue_root):
    result = _check_self_healing_daemon(queue_root)
    assert result.status == HealthStatus.CRIT
    assert "missing" in result.message.lower()


def test_missing_queue_dir_is_crit(tmp_path):
    """Queue root that does not exist should CRIT (heartbeat absent)."""
    result = _check_self_healing_daemon(tmp_path / "nonexistent")
    assert result.status == HealthStatus.CRIT


# ---------------------------------------------------------------------------
# Fresh heartbeat
# ---------------------------------------------------------------------------

def test_fresh_heartbeat_is_ok(queue_root):
    _write_heartbeat(queue_root, age_seconds=0)
    result = _check_self_healing_daemon(queue_root)
    assert result.status == HealthStatus.OK


def test_heartbeat_10min_is_ok(queue_root):
    _write_heartbeat(queue_root, age_seconds=600)
    result = _check_self_healing_daemon(queue_root)
    assert result.status == HealthStatus.OK


# ---------------------------------------------------------------------------
# WARN threshold: >15 min stale
# ---------------------------------------------------------------------------

def test_heartbeat_16min_is_warn(queue_root):
    _write_heartbeat(queue_root, age_seconds=960)  # 16 min
    result = _check_self_healing_daemon(queue_root)
    assert result.status == HealthStatus.WARN
    assert "15 min" in result.message or "stuck" in result.message.lower()


def test_heartbeat_exactly_at_warn_boundary(queue_root):
    _write_heartbeat(queue_root, age_seconds=901)  # just over 15 min
    result = _check_self_healing_daemon(queue_root)
    assert result.status == HealthStatus.WARN


# ---------------------------------------------------------------------------
# CRIT threshold: >30 min stale
# ---------------------------------------------------------------------------

def test_heartbeat_31min_is_crit(queue_root):
    _write_heartbeat(queue_root, age_seconds=1860)  # 31 min
    result = _check_self_healing_daemon(queue_root)
    assert result.status == HealthStatus.CRIT
    assert "30 min" in result.message or "dead" in result.message.lower()


def test_heartbeat_exactly_at_crit_boundary(queue_root):
    _write_heartbeat(queue_root, age_seconds=1801)  # just over 30 min
    result = _check_self_healing_daemon(queue_root)
    assert result.status == HealthStatus.CRIT


# ---------------------------------------------------------------------------
# Return type sanity
# ---------------------------------------------------------------------------

def test_returns_check_result(queue_root):
    _write_heartbeat(queue_root)
    result = _check_self_healing_daemon(queue_root)
    assert isinstance(result, CheckResult)
    assert result.name == "self_healing_daemon"
    assert isinstance(result.message, str)
