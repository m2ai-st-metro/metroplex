"""Tests for Gate 4.9 — Readiness (publish readiness checks + auto-fixes)."""
import base64
import json
import subprocess
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from gates.readiness import ReadinessGate
from config import Config
from audit import AuditLogger
from models import PublishJob


@pytest.fixture
def readiness_gate(test_config, in_memory_db, temp_audit_log):
    """ReadinessGate with in-memory DB and no real LLM client."""
    audit = AuditLogger(log_path=str(temp_audit_log))
    gate = ReadinessGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
    gate.client = None
    return gate


@pytest.fixture
def readiness_gate_with_llm(test_config, in_memory_db, temp_audit_log):
    """ReadinessGate with a mocked LLM client."""
    audit = AuditLogger(log_path=str(temp_audit_log))
    gate = ReadinessGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
    gate.client = MagicMock()
    return gate


@pytest.fixture
def published_job():
    """A PublishJob that looks published."""
    return PublishJob(
        build_job_id="metroplex-ideaforge-100",
        title="Test Readiness Tool",
        repo_name="test-readiness-tool",
        repo_url="https://github.com/m2ai-portfolio/test-readiness-tool",
        status="published",
        project_dir="/tmp/fake-project",
        created_at=datetime.now(),
        published_at=datetime.now(),
    )


def _make_gh_result(stdout="", stderr="", returncode=0):
    """Helper to create a subprocess.CompletedProcess for gh api mocks."""
    return subprocess.CompletedProcess(
        args=["gh", "api"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# --- DB Schema Tests ---

class TestDBSchema:
    """Test readiness_jobs table and helpers."""

    def test_init_db_creates_readiness_table(self, in_memory_db):
        """init_db creates readiness_jobs table."""
        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='readiness_jobs'")
        assert cursor.fetchone() is not None

    def test_record_readiness_job(self, in_memory_db):
        """record_readiness_job inserts a row."""
        in_memory_db.record_readiness_job(
            build_job_id="test-1",
            repo_name="my-repo",
            repo_url="https://github.com/m2ai-portfolio/my-repo",
            status="completed",
            checks_passed='["has_license", "has_topics"]',
            checks_failed="[]",
        )
        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT * FROM readiness_jobs WHERE repo_name='my-repo'")
        row = cursor.fetchone()
        assert row is not None
        assert dict(row)["status"] == "completed"

    def test_has_readiness_true(self, in_memory_db):
        """has_readiness returns True for completed jobs."""
        in_memory_db.record_readiness_job(
            build_job_id="test-2",
            repo_name="done-repo",
            status="completed",
        )
        assert in_memory_db.has_readiness("test-2") is True

    def test_has_readiness_false(self, in_memory_db):
        """has_readiness returns False for missing jobs."""
        assert in_memory_db.has_readiness("nonexistent") is False

    def test_has_readiness_false_for_failed(self, in_memory_db):
        """has_readiness returns False for failed jobs (not completed)."""
        in_memory_db.record_readiness_job(
            build_job_id="test-3",
            repo_name="failed-repo",
            status="failed",
        )
        assert in_memory_db.has_readiness("test-3") is False

    def test_get_readiness_stats(self, in_memory_db):
        """get_readiness_stats counts by status."""
        in_memory_db.record_readiness_job(build_job_id="a", repo_name="r1", status="completed")
        in_memory_db.record_readiness_job(build_job_id="b", repo_name="r2", status="completed")
        in_memory_db.record_readiness_job(build_job_id="c", repo_name="r3", status="failed")
        stats = in_memory_db.get_readiness_stats()
        assert stats["completed"] == 2
        assert stats["failed"] == 1
        assert stats["total"] == 3

    def test_get_readiness_pending(self, in_memory_db):
        """get_readiness_pending returns published builds without completed readiness."""
        # Insert a publish_jobs row
        cursor = in_memory_db.conn.cursor()
        cursor.execute("""
            INSERT INTO publish_jobs (build_job_id, title, repo_name, repo_url, project_dir, status, created_at)
            VALUES ('job-1', 'My Tool', 'my-tool', 'https://github.com/m2ai-portfolio/my-tool', '/tmp', 'published', datetime('now'))
        """)
        in_memory_db.conn.commit()

        pending = in_memory_db.get_readiness_pending()
        assert len(pending) == 1
        assert pending[0]["build_job_id"] == "job-1"

    def test_get_readiness_pending_excludes_completed(self, in_memory_db):
        """get_readiness_pending excludes builds with completed readiness."""
        cursor = in_memory_db.conn.cursor()
        cursor.execute("""
            INSERT INTO publish_jobs (build_job_id, title, repo_name, repo_url, project_dir, status, created_at)
            VALUES ('job-2', 'Done Tool', 'done-tool', 'https://github.com/m2ai-portfolio/done-tool', '/tmp', 'published', datetime('now'))
        """)
        in_memory_db.conn.commit()
        in_memory_db.record_readiness_job(build_job_id="job-2", repo_name="done-tool", status="completed")

        pending = in_memory_db.get_readiness_pending()
        assert len(pending) == 0


# --- Check Logic Tests ---

class TestChecks:
    """Test individual readiness checks."""

    @patch("gates.readiness.subprocess.run")
    def test_check_no_build_artifacts_clean(self, mock_run, readiness_gate):
        """Passes when no artifact files exist."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps([{"name": "README.md"}, {"name": "main.py"}])
        )
        ok, detail = readiness_gate._check_no_build_artifacts("m2ai-portfolio", "test-repo")
        assert ok is True

    @patch("gates.readiness.subprocess.run")
    def test_check_no_build_artifacts_dirty(self, mock_run, readiness_gate):
        """Fails when artifact files exist."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps([
                {"name": "README.md"},
                {"name": "app_spec.txt"},
                {"name": ".codebase_learnings.json"},
            ])
        )
        ok, detail = readiness_gate._check_no_build_artifacts("m2ai-portfolio", "test-repo")
        assert ok is False
        assert "app_spec.txt" in detail

    @patch("gates.readiness.subprocess.run")
    def test_check_has_license_exists(self, mock_run, readiness_gate):
        """Passes when license exists."""
        mock_run.return_value = _make_gh_result(stdout='{"license": {"key": "mit"}}')
        ok, _ = readiness_gate._check_has_license("m2ai-portfolio", "test-repo")
        assert ok is True

    @patch("gates.readiness.subprocess.run")
    def test_check_has_license_missing(self, mock_run, readiness_gate):
        """Fails when license is missing (404)."""
        mock_run.return_value = _make_gh_result(returncode=1, stderr="Not Found")
        ok, _ = readiness_gate._check_has_license("m2ai-portfolio", "test-repo")
        assert ok is False

    @patch("gates.readiness.subprocess.run")
    def test_check_has_topics_enough(self, mock_run, readiness_gate):
        """Passes when >= 3 topics."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"names": ["python", "cli", "automation", "devops"]})
        )
        ok, _ = readiness_gate._check_has_topics("m2ai-portfolio", "test-repo")
        assert ok is True

    @patch("gates.readiness.subprocess.run")
    def test_check_has_topics_not_enough(self, mock_run, readiness_gate):
        """Fails when < 3 topics."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"names": ["python"]})
        )
        ok, detail = readiness_gate._check_has_topics("m2ai-portfolio", "test-repo")
        assert ok is False
        assert "1 topics" in detail

    @patch("gates.readiness.subprocess.run")
    def test_check_has_description_good(self, mock_run, readiness_gate):
        """Passes when description is long enough."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"description": "A comprehensive tool for automated code quality analysis and improvement"})
        )
        ok, _ = readiness_gate._check_has_description("m2ai-portfolio", "test-repo")
        assert ok is True

    @patch("gates.readiness.subprocess.run")
    def test_check_has_description_too_short(self, mock_run, readiness_gate):
        """Fails when description is too short."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"description": "test"})
        )
        ok, _ = readiness_gate._check_has_description("m2ai-portfolio", "test-repo")
        assert ok is False

    @patch("gates.readiness.subprocess.run")
    def test_check_has_description_none(self, mock_run, readiness_gate):
        """Fails when description is null."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"description": None})
        )
        ok, _ = readiness_gate._check_has_description("m2ai-portfolio", "test-repo")
        assert ok is False

    @patch("gates.readiness.subprocess.run")
    def test_check_no_placeholder_urls_clean(self, mock_run, readiness_gate):
        """Passes when no placeholder URLs in README."""
        readme_b64 = base64.b64encode(b"# My Project\nThis is a real README.").decode()
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"content": readme_b64})
        )
        ok, _ = readiness_gate._check_no_placeholder_urls("m2ai-portfolio", "test-repo")
        assert ok is True

    @patch("gates.readiness.subprocess.run")
    def test_check_no_placeholder_urls_dirty(self, mock_run, readiness_gate):
        """Fails when placeholder URLs found."""
        readme_b64 = base64.b64encode(b"# My Project\nVisit https://example.com/api").decode()
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"content": readme_b64})
        )
        ok, detail = readiness_gate._check_no_placeholder_urls("m2ai-portfolio", "test-repo")
        assert ok is False
        assert "example" in detail.lower()

    @patch("gates.readiness.subprocess.run")
    def test_check_no_placeholder_urls_localhost(self, mock_run, readiness_gate):
        """Fails when localhost found in README."""
        readme_b64 = base64.b64encode(b"# My Tool\nRun at http://localhost:3000").decode()
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"content": readme_b64})
        )
        ok, _ = readiness_gate._check_no_placeholder_urls("m2ai-portfolio", "test-repo")
        assert ok is False

    @patch("gates.readiness.subprocess.run")
    def test_check_has_banner_image_present(self, mock_run, readiness_gate):
        """Passes when banner image found in first 30 lines."""
        readme = "# My Project\n\n![Banner](assets/banner.png)\n\nSome text."
        readme_b64 = base64.b64encode(readme.encode()).decode()
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"content": readme_b64})
        )
        ok, _ = readiness_gate._check_has_banner_image("m2ai-portfolio", "test-repo")
        assert ok is True

    @patch("gates.readiness.subprocess.run")
    def test_check_has_banner_image_missing(self, mock_run, readiness_gate):
        """Fails when no banner image in first 30 lines."""
        readme = "# My Project\n\nJust text, no images here.\n" * 10
        readme_b64 = base64.b64encode(readme.encode()).decode()
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"content": readme_b64})
        )
        ok, _ = readiness_gate._check_has_banner_image("m2ai-portfolio", "test-repo")
        assert ok is False

    @patch("gates.readiness.subprocess.run")
    def test_check_no_placeholder_urls_no_readme(self, mock_run, readiness_gate):
        """Skips check when README doesn't exist (404)."""
        mock_run.return_value = _make_gh_result(returncode=1, stderr="Not Found")
        ok, detail = readiness_gate._check_no_placeholder_urls("m2ai-portfolio", "test-repo")
        assert ok is True
        assert "skipped" in detail.lower()

    @patch("gates.readiness.subprocess.run")
    def test_check_has_banner_image_no_readme(self, mock_run, readiness_gate):
        """Skips check when README doesn't exist (404)."""
        mock_run.return_value = _make_gh_result(returncode=1, stderr="Not Found")
        ok, detail = readiness_gate._check_has_banner_image("m2ai-portfolio", "test-repo")
        assert ok is True
        assert "skipped" in detail.lower()


# --- Fix Logic Tests ---

class TestFixes:
    """Test auto-fix methods."""

    @patch("gates.readiness.subprocess.run")
    def test_fix_remove_build_artifacts(self, mock_run, readiness_gate):
        """Removes known build artifact files."""
        # First call: list contents
        mock_run.side_effect = [
            _make_gh_result(stdout=json.dumps([
                {"name": "app_spec.txt", "sha": "abc123"},
                {"name": "README.md", "sha": "def456"},
            ])),
            # Second call: delete app_spec.txt
            _make_gh_result(stdout="{}"),
        ]
        ok = readiness_gate._fix_remove_build_artifacts("m2ai-portfolio", "test-repo")
        assert ok is True
        assert mock_run.call_count == 2

    @patch("gates.readiness.subprocess.run")
    def test_fix_add_license(self, mock_run, readiness_gate):
        """Adds MIT LICENSE file."""
        mock_run.return_value = _make_gh_result(stdout="{}")
        ok = readiness_gate._fix_add_license("m2ai-portfolio", "test-repo")
        assert ok is True
        # Verify the call was made with PUT method
        call_args = mock_run.call_args
        assert "--method" in call_args[0][0] or any("PUT" in str(a) for a in call_args[0][0])

    @patch("gates.readiness.subprocess.run")
    def test_fix_generate_topics(self, mock_run, readiness_gate_with_llm):
        """Generates and sets topics via LLM."""
        gate = readiness_gate_with_llm
        # Mock README fetch
        readme_b64 = base64.b64encode(b"# Great Tool\nDoes amazing things with Python.").decode()
        # Mock LLM response
        mock_choice = MagicMock()
        mock_choice.message.content = '["python", "cli-tool", "automation", "devops", "testing"]'
        gate.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        mock_run.side_effect = [
            # Fetch README
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            # PUT topics
            _make_gh_result(stdout="{}"),
        ]
        ok = gate._fix_generate_topics("m2ai-portfolio", "test-repo")
        assert ok is True
        gate.client.chat.completions.create.assert_called_once()

    @patch("gates.readiness.subprocess.run")
    def test_fix_generate_topics_too_few(self, mock_run, readiness_gate_with_llm):
        """Fails when LLM generates fewer than 3 topics."""
        gate = readiness_gate_with_llm
        readme_b64 = base64.b64encode(b"# Minimal").decode()
        mock_choice = MagicMock()
        mock_choice.message.content = '["python"]'
        gate.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        mock_run.return_value = _make_gh_result(stdout=json.dumps({"content": readme_b64}))
        ok = gate._fix_generate_topics("m2ai-portfolio", "test-repo")
        assert ok is False

    @patch("gates.readiness.subprocess.run")
    def test_fix_generate_description(self, mock_run, readiness_gate_with_llm):
        """Generates and sets description via LLM."""
        gate = readiness_gate_with_llm
        readme_b64 = base64.b64encode(b"# Great Tool\nA comprehensive solution.").decode()
        mock_choice = MagicMock()
        mock_choice.message.content = "A comprehensive Python toolkit for automated code quality analysis and improvement workflows"
        gate.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        mock_run.side_effect = [
            # Fetch README
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            # PATCH repo
            _make_gh_result(stdout="{}"),
        ]
        ok = gate._fix_generate_description("m2ai-portfolio", "test-repo")
        assert ok is True

    @patch("gates.readiness.subprocess.run")
    def test_fix_generate_description_too_short(self, mock_run, readiness_gate_with_llm):
        """Fails when LLM generates too short a description."""
        gate = readiness_gate_with_llm
        readme_b64 = base64.b64encode(b"# Minimal").decode()
        mock_choice = MagicMock()
        mock_choice.message.content = "A tool"
        gate.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        mock_run.return_value = _make_gh_result(stdout=json.dumps({"content": readme_b64}))
        ok = gate._fix_generate_description("m2ai-portfolio", "test-repo")
        assert ok is False

    def test_fix_no_llm_client_topics(self, readiness_gate):
        """Topics fix fails gracefully without LLM client."""
        ok = readiness_gate._fix_generate_topics("m2ai-portfolio", "test-repo")
        assert ok is False

    def test_fix_no_llm_client_description(self, readiness_gate):
        """Description fix fails gracefully without LLM client."""
        ok = readiness_gate._fix_generate_description("m2ai-portfolio", "test-repo")
        assert ok is False

    def test_apply_fixes_flag_only_checks(self, readiness_gate):
        """Placeholder URLs and banner image are flag-only (go to fixes_failed)."""
        applied, failed = readiness_gate._apply_fixes("test-repo", ["no_placeholder_urls", "has_banner_image"])
        assert applied == []
        assert "no_placeholder_urls" in failed
        assert "has_banner_image" in failed

    @patch("gates.readiness.subprocess.run")
    def test_apply_fixes_llm_limit(self, mock_run, readiness_gate_with_llm):
        """Max 2 LLM calls per repo."""
        gate = readiness_gate_with_llm
        readme_b64 = base64.b64encode(b"# Tool\nDoes stuff with automation and Python.").decode()

        mock_choice = MagicMock()
        mock_choice.message.content = '["python", "cli", "automation", "devops"]'
        gate.client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        mock_run.side_effect = [
            # Topics: fetch README
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            # Topics: PUT
            _make_gh_result(stdout="{}"),
            # Description: fetch README
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            # Description: PATCH
            _make_gh_result(stdout="{}"),
        ]

        # Provide description mock too
        mock_desc_choice = MagicMock()
        mock_desc_choice.message.content = "A comprehensive Python automation framework for building and testing CLI tools"
        gate.client.chat.completions.create.side_effect = [
            MagicMock(choices=[mock_choice]),  # topics
            MagicMock(choices=[mock_desc_choice]),  # description
        ]

        applied, failed = gate._apply_fixes("test-repo", ["has_topics", "has_description"])
        assert len(applied) == 2
        assert gate.client.chat.completions.create.call_count == 2


# --- Run / Integration Tests ---

class TestRun:
    """Test the full run() method."""

    def test_dry_run(self, readiness_gate, published_job):
        """Dry run returns pending status without DB writes."""
        results = readiness_gate.run(published_jobs=[published_job], dry_run=True)
        assert len(results) == 1
        assert results[0]["status"] == "pending"
        assert readiness_gate.state_db.has_readiness(published_job.build_job_id) is False

    @patch("gates.readiness.subprocess.run")
    def test_all_checks_pass(self, mock_run, readiness_gate, published_job):
        """When all checks pass, status is completed and recorded in DB."""
        readme_b64 = base64.b64encode(b"# Tool\n\n![Banner](img.png)\nReal content here.").decode()
        mock_run.side_effect = [
            # no_build_artifacts: contents
            _make_gh_result(stdout=json.dumps([{"name": "README.md"}])),
            # has_license
            _make_gh_result(stdout='{"license": {"key": "mit"}}'),
            # has_topics
            _make_gh_result(stdout=json.dumps({"names": ["python", "cli", "automation"]})),
            # has_description
            _make_gh_result(stdout=json.dumps({"description": "A comprehensive tool for automated testing and quality analysis workflows"})),
            # no_placeholder_urls: fetch README
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            # has_banner_image: fetch README
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
        ]
        results = readiness_gate.run(published_jobs=[published_job], dry_run=False)
        assert len(results) == 1
        assert results[0]["status"] == "completed"
        assert readiness_gate.state_db.has_readiness(published_job.build_job_id) is True

    def test_skip_already_completed(self, readiness_gate, published_job, in_memory_db):
        """Jobs with completed readiness are skipped."""
        in_memory_db.record_readiness_job(
            build_job_id=published_job.build_job_id,
            repo_name=published_job.repo_name,
            status="completed",
        )
        results = readiness_gate.run(published_jobs=[published_job], dry_run=False)
        assert len(results) == 0

    @patch("gates.readiness.subprocess.run")
    def test_exception_handling(self, mock_run, readiness_gate, published_job):
        """Exceptions are caught and recorded as failed."""
        mock_run.side_effect = Exception("network error")
        results = readiness_gate.run(published_jobs=[published_job], dry_run=False)
        assert len(results) == 1
        assert results[0]["status"] == "failed"
        assert "network error" in results[0]["error"]

    def test_per_cycle_cap(self, readiness_gate):
        """Respects max_readiness_per_cycle cap."""
        readiness_gate.config.max_readiness_per_cycle = 2
        jobs = [
            PublishJob(
                build_job_id=f"job-{i}",
                title=f"Tool {i}",
                repo_name=f"tool-{i}",
                status="published",
                project_dir="/tmp",
            )
            for i in range(5)
        ]
        results = readiness_gate.run(published_jobs=jobs, dry_run=True)
        assert len(results) == 2

    @patch("gates.readiness.subprocess.run")
    def test_checks_failed_preserves_original_failures(self, mock_run, readiness_gate, published_job):
        """checks_failed records ALL originally-failed checks, even those later fixed."""
        readme_b64 = base64.b64encode(b"# Tool\n\n![Banner](img.png)\nReal content here.").decode()
        mock_run.side_effect = [
            # no_build_artifacts: has artifact
            _make_gh_result(stdout=json.dumps([{"name": "app_spec.txt", "sha": "abc"}])),
            # has_license: missing
            _make_gh_result(returncode=1, stderr="Not Found"),
            # has_topics: ok
            _make_gh_result(stdout=json.dumps({"names": ["python", "cli", "auto"]})),
            # has_description: ok
            _make_gh_result(stdout=json.dumps({"description": "A comprehensive tool for automated testing and quality analysis workflows"})),
            # no_placeholder_urls: ok
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            # has_banner_image: ok
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            # Fix: remove_build_artifacts — list contents
            _make_gh_result(stdout=json.dumps([{"name": "app_spec.txt", "sha": "abc"}])),
            # Fix: remove_build_artifacts — delete
            _make_gh_result(stdout="{}"),
            # Fix: add_license — PUT
            _make_gh_result(stdout="{}"),
        ]
        results = readiness_gate.run(published_jobs=[published_job], dry_run=False)
        assert len(results) == 1
        r = results[0]
        # Both checks originally failed
        assert "no_build_artifacts" in r["checks_failed"]
        assert "has_license" in r["checks_failed"]
        # Both were successfully fixed
        assert "no_build_artifacts" in r["fixes_applied"]
        assert "has_license" in r["fixes_applied"]
        # fixes_failed should be empty (both were fixed)
        assert r["fixes_failed"] == []
        # checks_failed != fixes_failed (the key distinction)
        assert r["checks_failed"] != r["fixes_failed"]


# --- Batch Mode Tests ---

class TestBatchRun:
    """Test the run_batch() method."""

    @patch("gates.readiness.subprocess.run")
    @patch("gates.readiness.time.sleep")
    def test_batch_dry_run(self, mock_sleep, mock_run, readiness_gate):
        """Batch dry-run shows checks without fixing."""
        readme_b64 = base64.b64encode(b"# Tool\n\n![Banner](img.png)\nGood content.").decode()
        mock_run.side_effect = [
            # For each check call (6 per repo)
            _make_gh_result(stdout=json.dumps([{"name": "README.md"}])),
            _make_gh_result(stdout='{"license": {"key": "mit"}}'),
            _make_gh_result(stdout=json.dumps({"names": ["python", "cli", "auto"]})),
            _make_gh_result(stdout=json.dumps({"description": "A comprehensive testing framework for modern automation pipelines"})),
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
        ]
        results = readiness_gate.run_batch(["my-repo"], dry_run=True)
        assert len(results) == 1
        assert results[0]["status"] == "pending"

    @patch("gates.readiness.subprocess.run")
    @patch("gates.readiness.time.sleep")
    def test_batch_circuit_breaker(self, mock_sleep, mock_run, readiness_gate):
        """Halts after 3 consecutive API errors."""
        mock_run.side_effect = Exception("API down")
        results = readiness_gate.run_batch(
            ["repo-1", "repo-2", "repo-3", "repo-4", "repo-5"],
            dry_run=False,
        )
        # Should stop after 3 failures
        assert len(results) == 3

    @patch("gates.readiness.subprocess.run")
    @patch("gates.readiness.time.sleep")
    def test_batch_sleeps_between_repos(self, mock_sleep, mock_run, readiness_gate):
        """Sleeps 1s between repos for rate limiting."""
        mock_run.side_effect = Exception("fail")
        readiness_gate.run_batch(["r1", "r2", "r3"], dry_run=False)
        assert mock_sleep.call_count >= 2


# --- Idempotency Tests ---

class TestIdempotency:
    """Test that running readiness twice doesn't re-process completed repos."""

    @patch("gates.readiness.subprocess.run")
    def test_idempotent_run(self, mock_run, readiness_gate, published_job):
        """Second run skips already-completed repos."""
        readme_b64 = base64.b64encode(b"# Tool\n\n![B](i.png)\nReal stuff.").decode()
        good_responses = [
            _make_gh_result(stdout=json.dumps([{"name": "README.md"}])),
            _make_gh_result(stdout='{"license": {"key": "mit"}}'),
            _make_gh_result(stdout=json.dumps({"names": ["a", "b", "c"]})),
            _make_gh_result(stdout=json.dumps({"description": "A comprehensive tool for workflow automation and testing"})),
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),
        ]
        mock_run.side_effect = good_responses

        # First run
        r1 = readiness_gate.run(published_jobs=[published_job], dry_run=False)
        assert len(r1) == 1
        assert r1[0]["status"] == "completed"

        # Second run — should return empty (already completed)
        r2 = readiness_gate.run(published_jobs=[published_job], dry_run=False)
        assert len(r2) == 0


# --- Edge Cases ---

class TestEdgeCases:
    """Test edge cases: 404, 429, empty README, etc."""

    @patch("gates.readiness.subprocess.run")
    def test_contents_api_failure(self, mock_run, readiness_gate):
        """Handles API failure on contents endpoint."""
        mock_run.return_value = _make_gh_result(returncode=1, stderr="rate limit exceeded")
        ok, detail = readiness_gate._check_no_build_artifacts("m2ai-portfolio", "test-repo")
        assert ok is False
        assert "API error" in detail

    @patch("gates.readiness.subprocess.run")
    def test_empty_readme(self, mock_run, readiness_gate):
        """Handles empty README content."""
        readme_b64 = base64.b64encode(b"").decode()
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"content": readme_b64})
        )
        # Empty README should pass placeholder check (nothing to find)
        ok, _ = readiness_gate._check_no_placeholder_urls("m2ai-portfolio", "test-repo")
        assert ok is True
        # Empty README should fail banner check
        ok, _ = readiness_gate._check_has_banner_image("m2ai-portfolio", "test-repo")
        assert ok is False

    @patch("gates.readiness.subprocess.run")
    def test_invalid_json_response(self, mock_run, readiness_gate):
        """Handles invalid JSON from API."""
        mock_run.return_value = _make_gh_result(stdout="not json")
        ok, detail = readiness_gate._check_no_build_artifacts("m2ai-portfolio", "test-repo")
        assert ok is False
        assert "Invalid JSON" in detail

    @patch("gates.readiness.subprocess.run")
    def test_topics_empty_names(self, mock_run, readiness_gate):
        """Handles empty topics names array."""
        mock_run.return_value = _make_gh_result(
            stdout=json.dumps({"names": []})
        )
        ok, detail = readiness_gate._check_has_topics("m2ai-portfolio", "test-repo")
        assert ok is False
        assert "0 topics" in detail

    def test_repo_name_from_url(self, readiness_gate):
        """Extracts repo name from URL when repo_name is empty."""
        job = PublishJob(
            build_job_id="test-url",
            title="URL Tool",
            repo_name="",
            repo_url="https://github.com/m2ai-portfolio/url-tool",
            status="published",
            project_dir="/tmp",
        )
        results = readiness_gate.run(published_jobs=[job], dry_run=True)
        assert results[0]["repo_name"] == "url-tool"


# --- Config Tests ---

class TestConfig:
    """Test readiness config integration."""

    def test_config_defaults(self, test_config):
        """Config has readiness defaults."""
        assert test_config.max_readiness_per_cycle == 5
        assert test_config.readiness_enabled is True

    def test_config_env_override(self):
        """Config reads env vars."""
        with patch.dict("os.environ", {
            "METROPLEX_MAX_READINESS_PER_CYCLE": "10",
            "METROPLEX_READINESS_ENABLED": "false",
        }):
            config = Config()
            assert config.max_readiness_per_cycle == 10
            assert config.readiness_enabled is False


# --- Cost Capture Tests ---

def _make_response(content: str, prompt_tokens: int = 80, completion_tokens: int = 15):
    """OpenAI chat completion mock with usage telemetry."""
    choice = MagicMock()
    choice.message.content = content
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


class TestCostCapture:
    """Gate 4.9 records LLM cost in the ledger after retry loops."""

    @patch("gates.readiness.subprocess.run")
    def test_description_records_cost_after_retries(self, mock_run, readiness_gate_with_llm, in_memory_db):
        """Two invalid attempts then a valid description -- one ledger row with totals across all 3 calls."""
        gate = readiness_gate_with_llm
        readme_b64 = base64.b64encode(b"# Tool\nDoes useful things.").decode()

        # Two CoT-style replies that get rejected (CoT prefix; no quoted span; no
        # sentence longer than the 20-char fallback threshold), then a clean
        # description on the third attempt.
        bad = "Okay. Sure. Hmm."
        good = "A comprehensive Python toolkit for automated code quality analysis and improvement workflows"
        gate.client.chat.completions.create.side_effect = [
            _make_response(bad),
            _make_response(bad),
            _make_response(good),
        ]

        mock_run.side_effect = [
            _make_gh_result(stdout=json.dumps({"content": readme_b64})),  # fetch README
            _make_gh_result(stdout="{}"),  # PATCH repo
        ]

        ok = gate._fix_generate_description("m2ai-portfolio", "test-repo")
        assert ok is True
        assert gate.client.chat.completions.create.call_count == 3

        in_memory_db.connect()
        rows = in_memory_db.conn.execute(
            "SELECT source, input_tokens, output_tokens FROM cost_ledger WHERE source = 'readiness_description'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 240
        assert rows[0]["output_tokens"] == 45
