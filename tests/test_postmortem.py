"""Tests for structured failure capture (L5 B1)."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from db import StateDB
from postmortem import (
    classify_failure,
    capture_postmortem,
    get_failure_patterns,
    get_postmortem_summary,
)


@pytest.fixture
def state_db():
    """Provide an in-memory StateDB with postmortems table."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


class TestClassifyFailure:
    """Test classify_failure with sample error logs for each category."""

    def test_dependency_error_module_not_found(self):
        log = "ModuleNotFoundError: No module named 'fastapi'"
        cat, stage, sig = classify_failure(log)
        assert cat == "dependency_error"
        assert stage == "install"
        assert "ModuleNotFoundError" in sig

    def test_dependency_error_import(self):
        log = "ImportError: cannot import name 'BaseModel' from 'pydantic'"
        cat, stage, sig = classify_failure(log)
        assert cat == "dependency_error"
        assert stage == "install"

    def test_test_failure(self):
        log = "FAILED tests/test_main.py::test_health - AssertionError: expected 200"
        cat, stage, sig = classify_failure(log)
        assert cat == "test_failure"
        assert stage == "test"

    def test_timeout(self):
        log = "TimeoutError: Build exceeded 90 minute limit"
        cat, stage, sig = classify_failure(log)
        assert cat == "timeout"

    def test_build_error_syntax(self):
        log = "SyntaxError: unexpected indent at line 42"
        cat, stage, sig = classify_failure(log)
        assert cat == "build_error"
        assert stage == "build"

    def test_build_error_indentation(self):
        log = "IndentationError: expected an indented block"
        cat, stage, sig = classify_failure(log)
        assert cat == "build_error"

    def test_environment_error_file_not_found(self):
        log = "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/data.db'"
        cat, stage, sig = classify_failure(log)
        assert cat == "environment_error"
        assert stage == "setup"

    def test_environment_error_permission(self):
        log = "PermissionError: [Errno 13] Permission denied: '/etc/config'"
        cat, stage, sig = classify_failure(log)
        assert cat == "environment_error"

    def test_spec_unclear_no_pattern(self):
        log = "Something went wrong but no known pattern"
        cat, stage, sig = classify_failure(log)
        assert cat == "spec_unclear"
        assert stage == "unknown"

    def test_empty_log(self):
        cat, stage, sig = classify_failure("")
        assert cat == "spec_unclear"
        assert sig == ""

    def test_error_signature_truncation(self):
        """Error signature should be at most 500 chars."""
        log = "ModuleNotFoundError: " + "x" * 1000
        cat, stage, sig = classify_failure(log)
        assert len(sig) <= 500


class TestCapturePostmortem:
    """Test capture_postmortem behavior."""

    def test_basic_capture(self, state_db):
        result = capture_postmortem(
            state_db=state_db,
            queue_job_id="metroplex-ideaforge-42",
            idea_id=42,
            title="Test Idea",
        )
        assert result is True

        # Verify it was stored
        state_db.connect()
        row = state_db.conn.execute(
            "SELECT * FROM build_postmortems WHERE queue_job_id = ?",
            ("metroplex-ideaforge-42",),
        ).fetchone()
        assert row is not None
        assert row["idea_id"] == 42
        assert row["title"] == "Test Idea"
        assert row["failure_category"] == "spec_unclear"  # No log = spec_unclear

    def test_dedup_same_job_id(self, state_db):
        """Same queue_job_id should not create duplicate."""
        r1 = capture_postmortem(
            state_db=state_db,
            queue_job_id="metroplex-ideaforge-42",
            idea_id=42,
            title="Test Idea",
        )
        r2 = capture_postmortem(
            state_db=state_db,
            queue_job_id="metroplex-ideaforge-42",
            idea_id=42,
            title="Test Idea",
        )
        assert r1 is True
        assert r2 is False

        # Only one record
        state_db.connect()
        count = state_db.conn.execute(
            "SELECT COUNT(*) FROM build_postmortems WHERE queue_job_id = ?",
            ("metroplex-ideaforge-42",),
        ).fetchone()[0]
        assert count == 1

    def test_capture_with_log_file(self, state_db, tmp_path):
        """Capture should read and classify from a log file."""
        log_file = tmp_path / "build.log"
        log_file.write_text("ModuleNotFoundError: No module named 'requests'")

        result = capture_postmortem(
            state_db=state_db,
            queue_job_id="metroplex-ideaforge-99",
            idea_id=99,
            title="Failed Build",
            log_path=str(log_file),
        )
        assert result is True

        state_db.connect()
        row = state_db.conn.execute(
            "SELECT * FROM build_postmortems WHERE queue_job_id = ?",
            ("metroplex-ideaforge-99",),
        ).fetchone()
        assert row["failure_category"] == "dependency_error"

    def test_capture_best_effort_bad_input(self, state_db):
        """capture_postmortem should not raise on bad input."""
        # Non-existent log path
        result = capture_postmortem(
            state_db=state_db,
            queue_job_id="metroplex-ideaforge-100",
            idea_id=100,
            title="Bad Input Test",
            log_path="/nonexistent/path/build.log",
        )
        # Should still capture (with empty log)
        assert result is True

    def test_capture_with_optional_fields(self, state_db):
        """Optional fields should be stored correctly."""
        result = capture_postmortem(
            state_db=state_db,
            queue_job_id="metroplex-ideaforge-50",
            idea_id=50,
            title="With Extras",
            spec_path="/tmp/spec.txt",
            idea_score=7.5,
            artifact_type="tool",
        )
        assert result is True

        state_db.connect()
        row = state_db.conn.execute(
            "SELECT * FROM build_postmortems WHERE queue_job_id = ?",
            ("metroplex-ideaforge-50",),
        ).fetchone()
        assert row["spec_path"] == "/tmp/spec.txt"
        assert row["idea_weighted_score"] == 7.5
        assert row["idea_artifact_type"] == "tool"


class TestGetFailurePatterns:
    """Test aggregation of failure patterns."""

    def test_aggregation(self, state_db):
        """Should aggregate by category and stage."""
        for i in range(5):
            capture_postmortem(
                state_db=state_db,
                queue_job_id=f"job-dep-{i}",
                idea_id=i,
                title=f"Dep Error {i}",
            )
        # Manually update categories to dependency_error for test
        state_db.connect()
        state_db.conn.execute(
            "UPDATE build_postmortems SET failure_category = 'dependency_error', failure_stage = 'install'"
        )
        state_db.conn.commit()

        patterns = get_failure_patterns(state_db, min_count=3)
        assert len(patterns) >= 1
        assert patterns[0]["category"] == "dependency_error"
        assert patterns[0]["count"] == 5

    def test_min_count_filter(self, state_db):
        """Should only return patterns with count >= min_count."""
        capture_postmortem(state_db=state_db, queue_job_id="job-1", idea_id=1, title="One")
        capture_postmortem(state_db=state_db, queue_job_id="job-2", idea_id=2, title="Two")

        # With min_count=3, should return nothing
        patterns = get_failure_patterns(state_db, min_count=3)
        assert len(patterns) == 0

    def test_summary(self, state_db):
        """get_postmortem_summary should return category-level stats."""
        for i in range(3):
            capture_postmortem(
                state_db=state_db,
                queue_job_id=f"job-sum-{i}",
                idea_id=i,
                title=f"Summary {i}",
                idea_score=5.0 + i,
            )

        summary = get_postmortem_summary(state_db)
        assert len(summary) >= 1
        assert "category" in summary[0]
        assert "count" in summary[0]
