"""Tests for Gate 4.5 — Automated code review gate."""
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gates.review import ReviewGate, ReviewResult
from config import Config
from db import StateDB


@pytest.fixture
def review_gate(test_config, in_memory_db, temp_audit_log):
    """ReviewGate with in-memory DB."""
    from audit import AuditLogger
    audit = AuditLogger(log_path=str(temp_audit_log))
    return ReviewGate(config=test_config, state_db=in_memory_db, audit_logger=audit)


@pytest.fixture
def good_project(tmp_path):
    """A project directory that passes all checks."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("# My Project")
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_hello(): pass")
    return tmp_path


@pytest.fixture
def bad_project_no_readme(tmp_path):
    """A project without a README."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "main.py").write_text("print('hello')")
    return tmp_path


@pytest.fixture
def bad_project_secrets(tmp_path):
    """A project with secret files."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("# My Project")
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / ".env").write_text("SECRET=abc123")
    return tmp_path


@pytest.fixture
def bad_project_empty(tmp_path):
    """A project with no source code."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("# Empty")
    return tmp_path


class TestReviewChecks:
    """Test individual review checks."""

    def test_good_project_passes(self, review_gate, good_project):
        passed, failed = review_gate._run_checks(good_project)
        assert not failed
        assert "has_source_code" in passed
        assert "has_readme" in passed
        assert "no_secrets" in passed
        assert "has_git" in passed

    def test_no_readme_fails(self, review_gate, bad_project_no_readme):
        passed, failed = review_gate._run_checks(bad_project_no_readme)
        assert "has_readme" in failed
        assert "has_source_code" in passed

    def test_secret_files_fail(self, review_gate, bad_project_secrets):
        passed, failed = review_gate._run_checks(bad_project_secrets)
        secret_check = [f for f in failed if f.startswith("no_secrets")]
        assert len(secret_check) == 1
        assert ".env" in secret_check[0]

    def test_no_source_code_fails(self, review_gate, bad_project_empty):
        passed, failed = review_gate._run_checks(bad_project_empty)
        assert "has_source_code" in failed

    def test_no_git_fails(self, review_gate, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Hi")
        passed, failed = review_gate._run_checks(tmp_path)
        assert "has_git" in failed


class TestSafetyHardChecks:
    """Gate 4.5 zero-LLM safety hard-checks (#436 regression guards)."""

    @pytest.fixture
    def dirty_436_project(self, tmp_path):
        """A #436-like project that FAILS every safety hard-check:
        - runtime data dir + populated *.jsonl, NO .gitignore   -> (a) + (b)
        - default data path resolves to the install dir         -> (c)
        """
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").write_text("# Elder Care Companion")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("def test_x(): pass")
        (tmp_path / "app.py").write_text(
            "from pathlib import Path\n"
            "DATA = Path(__file__).parent / 'data'\n"
            "def main():\n    return DATA\n"
        )
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "incidents.jsonl").write_text(
            '{"user":"Margaret","incident":"fall in bathroom","note":"hit head"}\n'
        )
        return tmp_path

    @pytest.fixture
    def clean_safety_project(self, tmp_path):
        """A safety-domain project that PASSES every hard-check:
        - data dir + *.jsonl present BUT covered by .gitignore
        - the tracked *.jsonl is empty (no PII content)
        - default data path uses XDG / home, never install dir
        """
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").write_text("# Safe Companion")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("def test_x(): pass")
        (tmp_path / "app.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "DATA = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'companion'\n"
            "def main():\n    return DATA\n"
        )
        (tmp_path / ".gitignore").write_text("data/\n*.jsonl\n__pycache__/\n")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / ".gitkeep").write_text("")
        return tmp_path

    def test_dirty_fails_gitignore_check(self, review_gate, dirty_436_project):
        passed, failed = review_gate._run_checks(dirty_436_project)
        assert any(f.startswith("gitignore_covers_data") for f in failed)

    def test_dirty_fails_pii_check(self, review_gate, dirty_436_project):
        passed, failed = review_gate._run_checks(dirty_436_project)
        pii = [f for f in failed if f.startswith("no_pii_artifact")]
        assert len(pii) == 1
        assert "incidents.jsonl" in pii[0]

    def test_dirty_fails_install_dir_data_path(self, review_gate, dirty_436_project):
        passed, failed = review_gate._run_checks(dirty_436_project)
        assert any(f.startswith("data_path_not_install_dir") for f in failed)

    def test_clean_passes_all_safety_checks(self, review_gate, clean_safety_project):
        passed, failed = review_gate._run_checks(clean_safety_project)
        assert "gitignore_covers_data" in passed
        assert "no_pii_artifact" in passed
        assert "data_path_not_install_dir" in passed
        assert not any(
            f.startswith(("gitignore_covers_data", "no_pii_artifact", "data_path_not_install_dir"))
            for f in failed
        )

    def test_good_project_unaffected_by_safety_checks(self, review_gate, good_project):
        """good_project has no data dir / .gitignore — the artifact-gated checks
        must NOT fire, and the static path scan must pass."""
        passed, failed = review_gate._run_checks(good_project)
        assert not failed
        # artifact-gated checks did not fire (no data artifacts present)
        assert not any(f.startswith("gitignore_covers_data") for f in passed + failed)
        assert not any(f.startswith("no_pii_artifact") for f in passed + failed)
        # static path scan ran and passed
        assert "data_path_not_install_dir" in passed

    def test_allowlist_exempts_fixture_jsonl(self, review_gate, tmp_path):
        """An explicit allowlist marker keeps a legit sample log from failing."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").write_text("# X")
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "sample.jsonl").write_text('// metroplex:review-allow\n{"demo":true}\n')
        passed, failed = review_gate._run_checks(tmp_path)
        # the only data artifact is allowlisted -> artifact-gated checks do not fire
        assert not any(f.startswith("gitignore_covers_data") for f in failed)
        assert not any(f.startswith("no_pii_artifact") for f in failed)


class TestReviewGateRun:
    """Test the full review gate run cycle."""

    def test_no_reviewable_builds(self, review_gate):
        results = review_gate.run()
        assert results == []

    def test_pass_updates_review_status(self, review_gate, good_project, in_memory_db):
        # Insert a completed build
        from models import BuildJob
        job = BuildJob(
            idea_id=1,
            title="Test Project",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        in_memory_db.update_build_job_status("metroplex-ideaforge-1", "completed")
        in_memory_db.update_build_job_project_dir("metroplex-ideaforge-1", str(good_project))

        results = review_gate.run()
        assert len(results) == 1
        assert results[0].verdict == "pass"

        # Check DB was updated
        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT review_status FROM build_jobs WHERE queue_job_id = 'metroplex-ideaforge-1'")
        row = cursor.fetchone()
        assert row["review_status"] == "reviewed"

    def test_fail_updates_review_status(self, review_gate, bad_project_empty, in_memory_db):
        from models import BuildJob
        job = BuildJob(
            idea_id=2,
            title="Empty Project",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-2",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        in_memory_db.update_build_job_status("metroplex-ideaforge-2", "completed")
        in_memory_db.update_build_job_project_dir("metroplex-ideaforge-2", str(bad_project_empty))

        results = review_gate.run()
        assert len(results) == 1
        assert results[0].verdict == "fail"

        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT review_status FROM build_jobs WHERE queue_job_id = 'metroplex-ideaforge-2'")
        row = cursor.fetchone()
        assert row["review_status"] == "review_failed"

    def test_missing_project_dir_skips(self, review_gate, in_memory_db):
        from models import BuildJob
        job = BuildJob(
            idea_id=3,
            title="Missing Dir",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-3",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        in_memory_db.update_build_job_status("metroplex-ideaforge-3", "completed")
        in_memory_db.update_build_job_project_dir("metroplex-ideaforge-3", "/nonexistent/path")

        results = review_gate.run()
        assert len(results) == 1
        assert results[0].verdict == "skip"

    def test_dry_run_does_not_update_db(self, review_gate, good_project, in_memory_db):
        from models import BuildJob
        job = BuildJob(
            idea_id=4,
            title="Dry Run Project",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-4",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        in_memory_db.update_build_job_status("metroplex-ideaforge-4", "completed")
        in_memory_db.update_build_job_project_dir("metroplex-ideaforge-4", str(good_project))

        results = review_gate.run(dry_run=True)
        assert len(results) == 1
        assert results[0].verdict == "pass"

        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT review_status FROM build_jobs WHERE queue_job_id = 'metroplex-ideaforge-4'")
        row = cursor.fetchone()
        assert row["review_status"] is None  # Not updated in dry run
