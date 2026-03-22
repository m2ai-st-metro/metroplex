"""
Tests for ReviewGate has_adequate_tests check (Phase D2).
"""
from datetime import datetime
from pathlib import Path

import pytest

from config import Config
from db import StateDB
from audit import AuditLogger
from gates.review import ReviewGate
from quality_ratchet import set_test_coverage_threshold


@pytest.fixture
def state_db():
    """Create in-memory state database."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def review_gate(state_db):
    """Create ReviewGate with in-memory DB."""
    config = Config()
    audit = AuditLogger(log_path="/dev/null")
    return ReviewGate(config=config, state_db=state_db, audit_logger=audit)


def _make_project(tmp_path, source_files: list[str], test_files: list[str]) -> Path:
    """Create a temporary project directory with the given source and test files."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Create README and .git so other checks pass
    (project_dir / "README.md").write_text("# Test Project")
    (project_dir / ".git").mkdir()
    (project_dir / ".gitignore").write_text("*.pyc\n")

    for f in source_files:
        (project_dir / f).write_text(f"# {f}\npass\n")

    tests_dir = project_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    for f in test_files:
        (tests_dir / f).write_text(f"# {f}\ndef test_example(): pass\n")

    return project_dir


class TestHasAdequateTests:
    """Test the has_adequate_tests review check."""

    def test_5_source_0_tests_fails(self, review_gate, tmp_path):
        """5 source files, 0 tests -> fails (hard floor: need at least 1 test)."""
        project = _make_project(
            tmp_path,
            source_files=["app.py", "models.py", "utils.py", "config.py", "main.py"],
            test_files=[],
        )
        passed, failed = review_gate._run_checks(project)
        test_checks = [c for c in failed if "has_adequate_tests" in c]
        assert len(test_checks) == 1, f"Expected has_adequate_tests failure, got: {failed}"

    def test_5_source_1_test_passes_at_zero_threshold(self, review_gate, state_db, tmp_path):
        """5 source files, 1 test -> passes when threshold is 0.0 (default)."""
        project = _make_project(
            tmp_path,
            source_files=["app.py", "models.py", "utils.py", "config.py", "main.py"],
            test_files=["test_app.py"],
        )
        passed, failed = review_gate._run_checks(project)
        test_passes = [c for c in passed if "has_adequate_tests" in c]
        assert len(test_passes) == 1, f"Expected has_adequate_tests pass, got passed={passed}, failed={failed}"

    def test_2_source_0_tests_passes_exemption(self, review_gate, tmp_path):
        """2 source files, 0 tests -> passes (< 3 files exemption)."""
        project = _make_project(
            tmp_path,
            source_files=["app.py", "main.py"],
            test_files=[],
        )
        passed, failed = review_gate._run_checks(project)
        test_passes = [c for c in passed if "has_adequate_tests" in c]
        assert len(test_passes) == 1, f"Expected exemption pass, got passed={passed}, failed={failed}"

    def test_10_source_1_test_fails_at_high_threshold(self, review_gate, state_db, tmp_path):
        """10 source files, 1 test, threshold 0.2 -> fails (ratio 0.1 < 0.2)."""
        set_test_coverage_threshold(state_db, 0.2)

        project = _make_project(
            tmp_path,
            source_files=[
                "app.py", "models.py", "utils.py", "config.py", "main.py",
                "api.py", "auth.py", "db.py", "cache.py", "helpers.py",
            ],
            test_files=["test_app.py"],
        )
        passed, failed = review_gate._run_checks(project)
        test_checks = [c for c in failed if "has_adequate_tests" in c]
        assert len(test_checks) == 1, f"Expected has_adequate_tests failure, got passed={passed}, failed={failed}"
        assert "0.10" in test_checks[0]  # ratio should show ~0.10

    def test_test_ratio_stored_on_build(self, state_db, review_gate, tmp_path):
        """Verify test_ratio is persisted on build_jobs during review."""
        project = _make_project(
            tmp_path,
            source_files=["app.py", "models.py", "utils.py", "config.py", "main.py"],
            test_files=["test_app.py", "test_models.py"],
        )

        # Insert a build job to review
        now = datetime.now().isoformat()
        state_db.conn.execute(
            "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at, review_status, project_dir) "
            "VALUES (1, 'Test Build', '/tmp/spec', 'metroplex-test-1', 'completed', ?, NULL, ?)",
            (now, str(project)),
        )
        state_db.conn.commit()

        # Run the review gate
        review_gate.run(dry_run=False)

        # Check test_ratio was stored
        row = state_db.conn.execute(
            "SELECT test_ratio FROM build_jobs WHERE queue_job_id = 'metroplex-test-1'"
        ).fetchone()
        assert row is not None
        assert row["test_ratio"] is not None
        # 2 test files / 5 source files = 0.4
        assert abs(row["test_ratio"] - 0.4) < 0.01
