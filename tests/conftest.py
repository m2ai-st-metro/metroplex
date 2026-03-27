"""
Pytest configuration and shared fixtures for Metroplex tests.
"""
import os
from unittest.mock import patch

import pytest
from pathlib import Path
import tempfile

from config import Config
from db import StateDB

# Env vars that may be set in ~/.env.shared but should not affect test defaults
_OVERRIDE_VARS = [
    "METROPLEX_MAX_APPROVE_PER_CYCLE",
    "METROPLEX_CYCLE_SLEEP_SECONDS",
    "METROPLEX_MAX_CONCURRENT_BUILDS",
    "METROPLEX_APPROVE_THRESHOLD",
    "METROPLEX_MAX_DEFERRALS",
]


@pytest.fixture
def test_config():
    """Provide a test configuration with safe defaults (env vars stripped)."""
    clean_env = {k: v for k, v in os.environ.items() if k not in _OVERRIDE_VARS}
    with patch.dict(os.environ, clean_env, clear=True):
        return Config()


@pytest.fixture
def in_memory_db():
    """Provide an in-memory database for testing."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def temp_db():
    """Provide a temporary file-based database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = StateDB(db_path)
    db.init_db()
    yield db
    db.close()

    # Clean up
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def temp_audit_log():
    """Provide a temporary audit log file."""
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="w") as f:
        log_path = f.name

    yield log_path

    # Clean up
    Path(log_path).unlink(missing_ok=True)
