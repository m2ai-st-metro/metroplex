"""Tests for Gate 4.7 — README enhancement gate."""
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gates.readme import ReadmeGate, BANANA_MAKER_SCRIPT
from config import Config
from db import StateDB
from models import PublishJob


@pytest.fixture
def readme_gate(test_config, in_memory_db, temp_audit_log):
    """ReadmeGate with in-memory DB and no real LLM client."""
    from audit import AuditLogger
    audit = AuditLogger(log_path=str(temp_audit_log))
    gate = ReadmeGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
    # Override client to None so tests don't need DEEPINFRA_API_KEY
    gate.client = None
    return gate


@pytest.fixture
def sample_project(tmp_path):
    """A minimal project directory with git init."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("# Placeholder")
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_hello(): pass")
    return tmp_path


@pytest.fixture
def published_job(sample_project):
    """A PublishJob that looks published."""
    return PublishJob(
        build_job_id="metroplex-ideaforge-99",
        title="Test Tool",
        repo_name="test-tool",
        repo_url="https://github.com/m2ai-portfolio/test-tool",
        status="published",
        project_dir=str(sample_project),
        created_at=datetime.now(),
        published_at=datetime.now(),
    )


class TestReadmeGateInit:
    """Test ReadmeGate initialization."""

    def test_initializes_without_api_key(self, test_config, in_memory_db, temp_audit_log):
        """Gate initializes even without DEEPINFRA_API_KEY (client will be None)."""
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        with patch.dict("os.environ", {"DEEPINFRA_API_KEY": ""}, clear=False):
            gate = ReadmeGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
            assert gate.client is None

    def test_initializes_with_api_key(self, test_config, in_memory_db, temp_audit_log):
        """Gate initializes OpenAI client when key is set."""
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        with patch.dict("os.environ", {"DEEPINFRA_API_KEY": "test-key-123"}, clear=False):
            gate = ReadmeGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
            assert gate.client is not None


class TestDryRun:
    """Test dry_run mode doesn't modify anything."""

    def test_dry_run_returns_pending(self, readme_gate, published_job):
        """Dry run returns pending status without modifying the DB."""
        results = readme_gate.run(published_jobs=[published_job], dry_run=True)
        assert len(results) == 1
        assert results[0]["status"] == "pending"
        assert results[0]["build_job_id"] == "metroplex-ideaforge-99"

    def test_dry_run_no_db_records(self, readme_gate, published_job, in_memory_db):
        """Dry run doesn't create readme_jobs records."""
        readme_gate.run(published_jobs=[published_job], dry_run=True)
        assert not in_memory_db.has_readme("metroplex-ideaforge-99")

    def test_dry_run_skips_already_enhanced(self, readme_gate, published_job, in_memory_db):
        """Dry run skips jobs that already have a completed readme."""
        in_memory_db.record_readme_job(
            build_job_id="metroplex-ideaforge-99",
            repo_url="https://github.com/m2ai-portfolio/test-tool",
            status="completed",
        )
        results = readme_gate.run(published_jobs=[published_job], dry_run=True)
        assert len(results) == 0


class TestReadmeContentGeneration:
    """Test README content generation prompt construction."""

    def test_generate_readme_prompt_includes_title(self, readme_gate):
        """Prompt includes the project title."""
        readme_gate.client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "# Test Tool\n\nA test tool."
        readme_gate.client.chat.completions.create.return_value = mock_response

        result = readme_gate._generate_readme_content(
            spec_text="Test spec content",
            file_tree="test-tool/\n├── main.py",
            title="Test Tool",
        )

        call_args = readme_gate.client.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "Test Tool" in user_msg
        assert "Test spec content" in user_msg
        assert "main.py" in user_msg

    def test_strips_code_fence_wrapper(self, readme_gate):
        """LLM output wrapped in code fences gets cleaned."""
        readme_gate.client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "```markdown\n# Title\n\nContent\n```"
        readme_gate.client.chat.completions.create.return_value = mock_response

        result = readme_gate._generate_readme_content("spec", "tree", "Title")
        assert not result.startswith("```")
        assert not result.endswith("```")
        assert "# Title" in result


class TestInfographicCommand:
    """Test infographic command construction."""

    def test_command_includes_all_args(self, readme_gate):
        """Built command includes all required arguments."""
        cmd = readme_gate.build_infographic_command(
            title="Test Tool",
            features="CLI, automation, fast",
            output_path="/tmp/infographic.png",
        )
        assert cmd[0] == "python3"
        assert str(BANANA_MAKER_SCRIPT) in cmd[1]
        assert "--model" in cmd
        assert "flash" in cmd
        assert "--output" in cmd
        assert "/tmp/infographic.png" in cmd
        assert "--aspect-ratio" in cmd
        assert "16:9" in cmd

    def test_prompt_includes_title_and_features(self, readme_gate):
        """The prompt argument includes title and features."""
        cmd = readme_gate.build_infographic_command(
            title="My Tool",
            features="feature-a, feature-b",
            output_path="/tmp/out.png",
        )
        # The prompt is cmd[2]
        assert "My Tool" in cmd[2]
        assert "feature-a" in cmd[2]
        assert "feature-b" in cmd[2]


class TestFileTree:
    """Test file tree building."""

    def test_builds_tree(self, readme_gate, sample_project):
        """File tree includes project files."""
        tree = readme_gate._build_file_tree(sample_project)
        assert "main.py" in tree
        assert "tests" in tree

    def test_excludes_git(self, readme_gate, sample_project):
        """File tree excludes .git directory."""
        tree = readme_gate._build_file_tree(sample_project)
        assert ".git" not in tree


class TestFeatureExtraction:
    """Test feature extraction from README content."""

    def test_extracts_bullet_features(self, readme_gate):
        """Extracts feature names from bullet lists under a Features heading."""
        content = """# Title

## Features

- Fast CLI interface
- Automated testing
- Plugin system

## Tech Stack
"""
        features = readme_gate._extract_features(content)
        assert "Fast CLI interface" in features
        assert "Automated testing" in features

    def test_fallback_on_no_features(self, readme_gate):
        """Returns default when no features section found."""
        features = readme_gate._extract_features("# Title\n\nSome content")
        assert "developer tool" in features


class TestDBMethods:
    """Test readme_jobs DB methods."""

    def test_has_readme_false(self, in_memory_db):
        """has_readme returns False for unknown build."""
        assert not in_memory_db.has_readme("nonexistent-id")

    def test_has_readme_true_after_record(self, in_memory_db):
        """has_readme returns True after recording a completed job."""
        in_memory_db.record_readme_job(
            build_job_id="test-id",
            repo_url="https://github.com/test/test",
            status="completed",
        )
        assert in_memory_db.has_readme("test-id")

    def test_has_readme_false_for_failed(self, in_memory_db):
        """has_readme returns False for failed readme jobs."""
        in_memory_db.record_readme_job(
            build_job_id="test-id",
            repo_url="https://github.com/test/test",
            status="failed",
            error="some error",
        )
        assert not in_memory_db.has_readme("test-id")

    def test_get_readme_pending_with_published(self, in_memory_db):
        """get_readme_pending returns published builds without completed readme."""
        # Insert a publish job
        job = PublishJob(
            build_job_id="build-1",
            title="Test",
            repo_name="test",
            repo_url="https://github.com/test/test",
            status="published",
            project_dir="/tmp/test",
            published_at=datetime.now(),
        )
        in_memory_db.record_publish_job(job)

        pending = in_memory_db.get_readme_pending()
        assert len(pending) == 1
        assert pending[0]["build_job_id"] == "build-1"

    def test_get_readme_pending_excludes_completed(self, in_memory_db):
        """get_readme_pending excludes builds with completed readme."""
        job = PublishJob(
            build_job_id="build-1",
            title="Test",
            repo_name="test",
            repo_url="https://github.com/test/test",
            status="published",
            project_dir="/tmp/test",
            published_at=datetime.now(),
        )
        in_memory_db.record_publish_job(job)
        in_memory_db.record_readme_job(
            build_job_id="build-1",
            repo_url="https://github.com/test/test",
            status="completed",
        )

        pending = in_memory_db.get_readme_pending()
        assert len(pending) == 0


class TestProcessOneFailsGracefully:
    """Test that _process_one handles errors correctly."""

    def test_missing_project_dir(self, readme_gate, in_memory_db):
        """Returns failed status when project_dir doesn't exist."""
        result = readme_gate._process_one(
            build_job_id="test-id",
            title="Test",
            project_dir="/nonexistent/path",
            repo_url="https://github.com/test/test",
        )
        assert result["status"] == "failed"
        assert "not found" in result["error"]

    def test_no_llm_client(self, readme_gate, sample_project, in_memory_db):
        """Returns failed status when no LLM client is available."""
        readme_gate.client = None
        result = readme_gate._process_one(
            build_job_id="test-id",
            title="Test",
            project_dir=str(sample_project),
            repo_url="https://github.com/test/test",
        )
        assert result["status"] == "failed"
        assert "DEEPINFRA_API_KEY" in result["error"]
