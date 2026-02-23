"""
Pytest configuration and shared fixtures for Metroplex tests.
"""
import pytest
from pathlib import Path
import tempfile

from config import Config
from db import StateDB


@pytest.fixture
def test_config():
    """Provide a test configuration with safe defaults."""
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
