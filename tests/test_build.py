"""
Tests for Build Gate - Spec Generation and Build Orchestration
"""
import pytest
from pathlib import Path
import tempfile
import shutil
import sys
import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from config import Config
from gates.build import SpecGenerator, BuildOrchestrator
from models import BuildJob
from db import StateDB
from audit import AuditLogger


@pytest.fixture
def config():
    """Create test configuration."""
    return Config()


@pytest.fixture
def template_dir():
    """Get path to spec_templates directory."""
    # Assuming tests run from project root
    templates = Path(__file__).parent.parent / "spec_templates"
    assert templates.exists(), f"Template directory not found: {templates}"
    return templates


@pytest.fixture
def output_dir():
    """Create temporary output directory."""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    # Cleanup
    if temp_dir.exists():
        shutil.rmtree(temp_dir)


@pytest.fixture
def tool_idea():
    """Sample tool-type idea."""
    return {
        "id": 1,
        "title": "Code Formatter CLI",
        "description": "A fast CLI tool for formatting code files with multiple language support.",
        "problem_statement": "Developers waste time manually formatting code and dealing with inconsistent styles across projects.",
        "target_audience": "Software developers working on multi-language projects",
        "artifact_type": "tool",
        "weighted_score": 8.5,
        "status": "scored"
    }


@pytest.fixture
def agent_idea():
    """Sample agent-type idea."""
    return {
        "id": 2,
        "title": "Documentation Assistant Agent",
        "description": "An AI agent that automatically generates and maintains technical documentation for codebases.",
        "problem_statement": "Documentation becomes outdated quickly and developers don't have time to keep it current.",
        "target_audience": "Development teams working on large codebases",
        "artifact_type": "agent",
        "weighted_score": 9.0,
        "status": "scored"
    }


@pytest.fixture
def product_idea():
    """Sample product-type idea."""
    return {
        "id": 3,
        "title": "Team Task Manager",
        "description": "A collaborative task management web app with real-time updates and team analytics.",
        "problem_statement": "Teams struggle to coordinate tasks across different tools and lose visibility into project progress.",
        "target_audience": "Small to medium-sized development teams",
        "artifact_type": "product",
        "weighted_score": 7.5,
        "status": "scored"
    }


class TestSpecGenerator:
    """Tests for SpecGenerator class."""

    def test_init_valid_template_dir(self, config, template_dir):
        """Test initialization with valid template directory."""
        generator = SpecGenerator(config, template_dir)
        assert generator.config == config
        assert generator.template_dir == template_dir
        assert generator.env is not None

    def test_init_invalid_template_dir(self, config):
        """Test initialization with non-existent template directory."""
        invalid_dir = Path("/nonexistent/path")
        with pytest.raises(FileNotFoundError) as exc:
            SpecGenerator(config, invalid_dir)
        assert "Template directory not found" in str(exc.value)

    def test_generate_spec_tool_type(self, config, template_dir, output_dir, tool_idea):
        """Test 1: Generate a spec for a 'tool' type idea → verify CLI-appropriate structure."""
        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(tool_idea, output_dir)

        # Verify file was created
        assert output_path.exists()
        assert output_path.name == "app_spec_1.txt"

        # Read generated content
        content = output_path.read_text()

        # Verify CLI-appropriate features are present
        assert "CLI Argument Parsing" in content
        assert "argparse" in content or "click" in content
        assert "Core Command Implementation" in content
        assert "File I/O Operations" in content
        assert "stdin/stdout" in content

        # Verify tech stack is tool-appropriate
        assert "Python 3.11+" in content
        assert "CLI Framework" in content or "CLI Tool" in content

        # Verify no web UI elements
        assert "React" not in content or "Frontend" not in content
        assert "FastAPI" not in content or "Backend" not in content

    def test_generate_spec_agent_type(self, config, template_dir, output_dir, agent_idea):
        """Test 2: Generate a spec for an 'agent' type idea → verify agent-appropriate structure."""
        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(agent_idea, output_dir)

        # Verify file was created
        assert output_path.exists()
        assert output_path.name == "app_spec_2.txt"

        # Read generated content
        content = output_path.read_text()

        # Verify agent-appropriate features are present
        assert "Agent Core Setup" in content or "Agent Core" in content
        assert "Prompt Management" in content or "Prompts" in content
        assert "Tool Implementation" in content or "Tools" in content
        assert "Agent Execution Loop" in content or "Execution" in content
        assert "State and Memory Management" in content or "Memory" in content

        # Verify tech stack is agent-appropriate
        assert "Claude" in content or "LLM" in content
        assert "Agent Framework" in content or "agent" in content.lower()
        assert "ANTHROPIC_API_KEY" in content

    def test_generate_spec_product_type(self, config, template_dir, output_dir, product_idea):
        """Test 3: Generate a spec for a 'product' type idea → verify full-stack structure."""
        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(product_idea, output_dir)

        # Verify file was created
        assert output_path.exists()
        assert output_path.name == "app_spec_3.txt"

        # Read generated content
        content = output_path.read_text()

        # Verify full-stack features are present
        assert "Frontend" in content
        assert "Backend" in content
        assert "Database" in content
        assert "React" in content
        assert "FastAPI" in content

        # Verify product-appropriate features
        assert "Project Foundation" in content or "Foundation" in content
        assert "Database Setup" in content or "Database" in content
        assert "API Endpoints" in content
        assert "Frontend UI Components" in content or "Components" in content
        assert "Frontend-Backend Integration" in content or "Integration" in content

        # Verify API endpoints section exists
        assert "| Method | Path | Description |" in content or "GET" in content

    def test_generate_spec_contains_idea_data(self, config, template_dir, output_dir, tool_idea):
        """Test 4: Verify generated spec contains the idea's title and description verbatim."""
        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(tool_idea, output_dir)

        content = output_path.read_text()

        # Verify title appears in the spec
        assert tool_idea["title"] in content

        # Verify description appears verbatim
        assert tool_idea["description"] in content

        # Verify problem statement appears
        assert tool_idea["problem_statement"] in content

        # Verify target audience appears
        assert tool_idea["target_audience"] in content

    def test_generate_spec_no_jinja_syntax(self, config, template_dir, output_dir, tool_idea):
        """Test 5: Verify output file is valid text (no Jinja2 syntax errors, no {{ }} remaining)."""
        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(tool_idea, output_dir)

        content = output_path.read_text()

        # Verify no remaining Jinja2 syntax
        assert "{{" not in content, "Found unrendered Jinja2 variable syntax: {{"
        assert "}}" not in content, "Found unrendered Jinja2 variable syntax: }}"
        assert "{%" not in content, "Found unrendered Jinja2 block syntax: {%"
        assert "%}" not in content, "Found unrendered Jinja2 block syntax: %}"

        # Verify content is not empty
        assert len(content) > 100, "Generated spec is too short"

        # Verify basic markdown structure
        assert content.startswith("#"), "Spec should start with markdown header"
        assert "## Overview" in content
        assert "## Tech Stack" in content
        assert "## Core Features" in content
        assert "## Success Criteria" in content

    def test_generate_spec_missing_required_fields(self, config, template_dir, output_dir):
        """Test error handling when idea is missing required fields."""
        incomplete_idea = {
            "id": 99,
            "title": "Incomplete Idea",
            # Missing description, problem_statement, target_audience, artifact_type
        }

        generator = SpecGenerator(config, template_dir)

        with pytest.raises(ValueError) as exc:
            generator.generate_spec(incomplete_idea, output_dir)

        assert "missing required fields" in str(exc.value).lower()

    def test_generate_spec_creates_output_dir(self, config, template_dir, tool_idea):
        """Test that output directory is created if it doesn't exist."""
        # Use a nested path that doesn't exist
        with tempfile.TemporaryDirectory() as temp_base:
            output_dir = Path(temp_base) / "nested" / "output" / "dir"
            assert not output_dir.exists()

            generator = SpecGenerator(config, template_dir)
            output_path = generator.generate_spec(tool_idea, output_dir)

            # Verify directory was created
            assert output_dir.exists()
            assert output_path.exists()
            assert output_path.parent == output_dir

    def test_generate_spec_output_path_format(self, config, template_dir, output_dir, agent_idea):
        """Test that output file follows naming convention: app_spec_{idea_id}.txt"""
        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(agent_idea, output_dir)

        # Verify naming convention
        assert output_path.name == f"app_spec_{agent_idea['id']}.txt"
        assert output_path.parent == output_dir

    def test_generate_spec_with_optional_tech_stack(self, config, template_dir, output_dir, tool_idea):
        """Test spec generation with optional tech_stack field."""
        # Add optional tech_stack field
        idea_with_tech = tool_idea.copy()
        idea_with_tech["tech_stack"] = "Redis for caching, Docker for deployment"

        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(idea_with_tech, output_dir)

        content = output_path.read_text()

        # Verify tech_stack appears in generated spec
        assert "Redis for caching, Docker for deployment" in content

    def test_generate_spec_without_optional_tech_stack(self, config, template_dir, output_dir, tool_idea):
        """Test spec generation without optional tech_stack field."""
        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(tool_idea, output_dir)

        content = output_path.read_text()

        # Should generate successfully even without tech_stack
        assert len(content) > 100
        assert "## Tech Stack" in content

    def test_generate_spec_file_encoding(self, config, template_dir, output_dir, product_idea):
        """Test that generated file uses UTF-8 encoding."""
        # Add some unicode characters to test encoding
        idea_with_unicode = product_idea.copy()
        idea_with_unicode["description"] = "A tool with unicode: 你好, здравствуй, مرحبا"

        generator = SpecGenerator(config, template_dir)
        output_path = generator.generate_spec(idea_with_unicode, output_dir)

        # Read with explicit UTF-8 encoding
        content = output_path.read_text(encoding="utf-8")

        # Verify unicode characters are preserved
        assert "你好" in content
        assert "здравствуй" in content
        assert "مرحبا" in content

    def test_generate_spec_multiple_ideas(self, config, template_dir, output_dir, tool_idea, agent_idea, product_idea):
        """Test generating specs for multiple ideas in the same output directory."""
        generator = SpecGenerator(config, template_dir)

        # Generate specs for all three ideas
        path1 = generator.generate_spec(tool_idea, output_dir)
        path2 = generator.generate_spec(agent_idea, output_dir)
        path3 = generator.generate_spec(product_idea, output_dir)

        # Verify all files exist
        assert path1.exists()
        assert path2.exists()
        assert path3.exists()

        # Verify unique filenames
        assert path1.name != path2.name != path3.name
        assert path1.name == "app_spec_1.txt"
        assert path2.name == "app_spec_2.txt"
        assert path3.name == "app_spec_3.txt"

        # Verify each contains correct content
        assert tool_idea["title"] in path1.read_text()
        assert agent_idea["title"] in path2.read_text()
        assert product_idea["title"] in path3.read_text()


class TestBuildOrchestrator:
    """Tests for BuildOrchestrator class."""

    @pytest.fixture
    def mock_state_db(self):
        """Mock StateDB."""
        db = Mock(spec=StateDB)
        db.record_build_job = Mock()
        return db

    @pytest.fixture
    def mock_audit_logger(self):
        """Mock AuditLogger."""
        logger = Mock(spec=AuditLogger)
        logger.log_decision = Mock()
        logger.log_error = Mock()
        return logger

    @pytest.fixture
    def mock_spec_generator(self, template_dir):
        """Mock SpecGenerator."""
        generator = Mock(spec=SpecGenerator)
        # Mock generate_spec to return a Path
        generator.generate_spec = Mock(return_value=Path("/tmp/app_spec_1.txt"))
        return generator

    @pytest.fixture
    def orchestrator(self, config, mock_state_db, mock_spec_generator, mock_audit_logger):
        """Create BuildOrchestrator instance with mocks."""
        return BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger
        )

    def test_queue_build_success(self, orchestrator, mock_state_db, mock_audit_logger, tool_idea):
        """Test 1: Mock subprocess.run, call queue_build() → verify correct command constructed with idea ID and model."""
        spec_path = Path("/tmp/app_spec_1.txt")

        with patch('subprocess.run') as mock_run:
            # Mock successful subprocess call
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            job = orchestrator.queue_build(tool_idea, spec_path, dry_run=False)

            # Verify subprocess was called with correct command
            assert mock_run.called
            call_args = mock_run.call_args
            command = call_args[0][0]

            # Verify command structure
            assert command[0] == sys.executable
            assert str(orchestrator.queue_runner_path) in command[1]
            assert "add" in command
            assert str(spec_path) in command
            assert "--id" in command
            assert "metroplex-1" in command  # job_id format
            assert "--model" in command
            assert orchestrator.config.build_model in command

            # Verify subprocess options
            assert call_args[1]["capture_output"] is True
            assert call_args[1]["text"] is True
            assert call_args[1]["timeout"] == 30

            # Verify BuildJob was created with correct status
            assert job is not None
            assert job.idea_id == tool_idea["id"]
            assert job.title == tool_idea["title"]
            assert job.spec_path == str(spec_path)
            assert job.queue_job_id == "metroplex-1"
            assert job.status == "queued"

            # Verify state_db was called
            assert mock_state_db.record_build_job.called

            # Verify audit logger was called
            assert mock_audit_logger.log_decision.called

    def test_queue_build_failure(self, orchestrator, mock_state_db, mock_audit_logger, tool_idea):
        """Test 2: Mock subprocess.run returning exit code 1 → verify BuildJob has status="failed"."""
        spec_path = Path("/tmp/app_spec_1.txt")

        with patch('subprocess.run') as mock_run:
            # Mock failed subprocess call
            mock_result = Mock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "Queue runner error"
            mock_run.return_value = mock_result

            job = orchestrator.queue_build(tool_idea, spec_path, dry_run=False)

            # Verify BuildJob was created with failed status
            assert job is not None
            assert job.status == "failed"
            assert job.queue_job_id == "metroplex-1"

            # Verify state_db was called with failed job
            assert mock_state_db.record_build_job.called

            # Verify audit logger logged error
            assert mock_audit_logger.log_error.called
            error_call_args = mock_audit_logger.log_error.call_args
            assert "Failed to queue build" in error_call_args[1]["error"]

    def test_queue_build_dry_run(self, orchestrator, tool_idea, capsys):
        """Test 3: Verify dry_run=True prints command but does not call subprocess.run."""
        spec_path = Path("/tmp/app_spec_1.txt")

        with patch('subprocess.run') as mock_run:
            job = orchestrator.queue_build(tool_idea, spec_path, dry_run=True)

            # Verify subprocess was NOT called
            assert not mock_run.called

            # Verify function returned None
            assert job is None

            # Verify command was printed
            captured = capsys.readouterr()
            assert "[DRY RUN]" in captured.out
            assert "metroplex-1" in captured.out

    def test_check_status(self, orchestrator, mock_audit_logger):
        """Test 4: Call check_status() with mocked subprocess returning JSON → verify parsed correctly."""
        status_dict = {
            "queue_size": 5,
            "running": True,
            "jobs": [
                {"id": "metroplex-1", "status": "queued"},
                {"id": "metroplex-2", "status": "started"}
            ]
        }

        with patch('subprocess.run') as mock_run:
            # Mock successful subprocess call returning JSON
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = json.dumps(status_dict)
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = orchestrator.check_status()

            # Verify subprocess was called with correct command
            assert mock_run.called
            call_args = mock_run.call_args
            command = call_args[0][0]

            assert command[0] == sys.executable
            assert "status" in command
            assert "--json" in command

            # Verify result was parsed correctly
            assert result == status_dict
            assert result["queue_size"] == 5
            assert result["running"] is True
            assert len(result["jobs"]) == 2

    def test_job_id_format(self, orchestrator, mock_state_db, mock_audit_logger):
        """Test 5: Verify job_id format is metroplex-{idea_id}."""
        idea = {"id": 42, "title": "Test Idea"}
        spec_path = Path("/tmp/app_spec_42.txt")

        with patch('subprocess.run') as mock_run:
            # Mock successful subprocess call
            mock_result = Mock()
            mock_result.returncode = 0
            mock_run.return_value = mock_result

            job = orchestrator.queue_build(idea, spec_path, dry_run=False)

            # Verify job_id format
            assert job.queue_job_id == "metroplex-42"

            # Verify it was used in the command
            call_args = mock_run.call_args
            command = call_args[0][0]
            assert "metroplex-42" in command

    def test_start_queue_success(self, orchestrator, mock_audit_logger):
        """Test start_queue with successful execution."""
        with patch('subprocess.run') as mock_run:
            # Mock successful subprocess call
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = orchestrator.start_queue(dry_run=False)

            # Verify subprocess was called
            assert mock_run.called
            call_args = mock_run.call_args
            command = call_args[0][0]

            assert command[0] == sys.executable
            assert "start" in command

            # Verify timeout is None (long-running)
            assert call_args[1]["timeout"] is None

            # Verify result is True
            assert result is True

            # Verify audit logger was called
            assert mock_audit_logger.log_decision.called

    def test_start_queue_dry_run(self, orchestrator, capsys):
        """Test start_queue with dry_run=True."""
        with patch('subprocess.run') as mock_run:
            result = orchestrator.start_queue(dry_run=True)

            # Verify subprocess was NOT called
            assert not mock_run.called

            # Verify result is True (dry run always succeeds)
            assert result is True

            # Verify command was printed
            captured = capsys.readouterr()
            assert "[DRY RUN]" in captured.out
            assert "start" in captured.out

    def test_run_multiple_ideas(self, orchestrator, mock_state_db, mock_spec_generator, mock_audit_logger, tool_idea, agent_idea):
        """Test run() with multiple approved ideas."""
        approved_ideas = [tool_idea, agent_idea]

        with patch('subprocess.run') as mock_run, \
             patch.object(orchestrator, 'start_queue_background', return_value=True) as mock_start_bg:
            # Mock successful subprocess calls (for queue_build "add" commands)
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            jobs = orchestrator.run(approved_ideas, dry_run=False)

            # Verify spec generator was called for each idea
            assert mock_spec_generator.generate_spec.call_count == 2

            # Verify two jobs were created
            assert len(jobs) == 2
            assert jobs[0].idea_id == tool_idea["id"]
            assert jobs[1].idea_id == agent_idea["id"]

            # Verify both jobs have queued status
            assert all(j.status == "queued" for j in jobs)

            # Verify start_queue_background was called
            mock_start_bg.assert_called_once()

    def test_run_dry_run(self, orchestrator, mock_spec_generator, tool_idea, capsys):
        """Test run() with dry_run=True."""
        approved_ideas = [tool_idea]

        with patch('subprocess.run') as mock_run:
            jobs = orchestrator.run(approved_ideas, dry_run=True)

            # Verify subprocess was NOT called
            assert not mock_run.called

            # Verify spec generator was still called (spec generation is not part of dry_run)
            assert mock_spec_generator.generate_spec.called

            # Verify no jobs were returned (dry_run returns None from queue_build)
            assert len(jobs) == 0

    def test_queue_build_timeout(self, orchestrator, mock_state_db, mock_audit_logger, tool_idea):
        """Test queue_build handles timeout gracefully."""
        spec_path = Path("/tmp/app_spec_1.txt")

        with patch('subprocess.run') as mock_run:
            # Mock timeout exception
            import subprocess
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=30)

            job = orchestrator.queue_build(tool_idea, spec_path, dry_run=False)

            # Verify BuildJob was created with failed status
            assert job is not None
            assert job.status == "failed"

            # Verify error was logged
            assert mock_audit_logger.log_error.called
            error_call_args = mock_audit_logger.log_error.call_args
            assert "timed out" in error_call_args[1]["error"]
