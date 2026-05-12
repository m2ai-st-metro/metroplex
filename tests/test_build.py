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
from gates.llm_expander import validate_spec, validate_agent_spec
from models import BuildJob
from db import StateDB
from audit import AuditLogger


def _make_valid_spec(title: str, idea_desc: str, problem: str, audience: str) -> str:
    """Build a mock LLM spec that passes all validate_spec checks."""
    # Must have >= 3 ## headers, 50-400 lines, no duplicates, no parrot markers
    lines = [
        f"# {title} - App Specification",
        "",
        "## Overview",
        f"{idea_desc}",
        "",
        f"**Problem Statement**: {problem}",
        f"**Target Audience**: {audience}",
        "",
        "## Tech Stack",
        "- Python 3.11+",
        "- click",
        "- pytest",
        "",
        "## Environment Setup",
        "",
        "### Prerequisites",
        "- Python 3.11+",
        "",
        "### Configuration",
        "| Variable | Required | Description |",
        "|----------|----------|-------------|",
        "| DEBUG | No | Enable debug logging |",
        "",
        "## Architecture",
        "```",
        "CLI -> Core -> Output",
        "```",
        "",
        "## Core Features",
        "",
        f"### Feature 1: Core Analysis",
        f"**Description**: Main analysis engine for {title}",
        "**Requirements**:",
        "- Accept input via CLI arguments",
        "- Process and validate input data",
        "- Output results to stdout in JSON format",
        "**Test Steps**:",
        f"1. `{title.lower().replace(' ', '-')} analyze input.json` -> JSON output with results",
        "",
        f"### Feature 2: Report Generation",
        f"**Description**: Generate human-readable reports",
        "**Requirements**:",
        "- Accept analysis results as input",
        "- Format output as markdown",
        "**Test Steps**:",
        f"1. `{title.lower().replace(' ', '-')} report results.json` -> Markdown report on stdout",
        "",
        "## Data Models",
        "```python",
        "from dataclasses import dataclass",
        "",
        "@dataclass",
        "class AnalysisResult:",
        "    score: float",
        "    findings: list[str]",
        "```",
        "",
        "## File Structure",
        "```",
        f"{title.lower().replace(' ', '-')}/",
        "├── src/",
        "│   ├── __init__.py",
        "│   ├── cli.py",
        "│   ├── core.py",
        "│   └── models.py",
        "├── tests/",
        "│   ├── test_cli.py",
        "│   └── test_core.py",
        "├── requirements.txt",
        "└── README.md",
        "```",
        "",
        "## Success Criteria",
        "1. CLI accepts arguments and displays help correctly",
        "2. Core analysis produces correct output for sample input",
        "3. All tests pass with >80% coverage",
        "",
        "## Constraints & Notes",
        "- No external API calls",
        "- Target: working MVP in 5 build iterations",
        "- Prioritize correctness over features",
    ]
    return "\n".join(lines)


@pytest.fixture
def config(monkeypatch):
    """Create test configuration with LLM enabled (mocked in tests)."""
    monkeypatch.setenv("METROPLEX_SPEC_USE_LLM", "true")
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
    """Sample tool-type idea (life_domain rubric).

    R-A item 1: all SpecGenerator tests dispatch through the agent path now,
    so every fixture carries scoring_rubric='life_domain'. Tests that need
    to exercise the dispatch-rejects-non-life-domain branch override locally.
    """
    return {
        "id": 1,
        "title": "Code Formatter CLI",
        "description": "A fast CLI tool for formatting code files with multiple language support.",
        "problem_statement": "Developers waste time manually formatting code and dealing with inconsistent styles across projects.",
        "target_audience": "Software developers working on multi-language projects",
        "artifact_type": "tool",
        "weighted_score": 8.5,
        "status": "scored",
        "scoring_rubric": "life_domain",
    }


@pytest.fixture
def agent_idea():
    """Sample agent-type idea (life_domain rubric)."""
    return {
        "id": 2,
        "title": "Documentation Assistant Agent",
        "description": "An AI agent that automatically generates and maintains technical documentation for codebases.",
        "problem_statement": "Documentation becomes outdated quickly and developers don't have time to keep it current.",
        "target_audience": "Development teams working on large codebases",
        "artifact_type": "agent",
        "weighted_score": 9.0,
        "status": "scored",
        "scoring_rubric": "life_domain",
    }


@pytest.fixture
def product_idea():
    """Sample product-type idea (life_domain rubric)."""
    return {
        "id": 3,
        "title": "Team Task Manager",
        "description": "A collaborative task management web app with real-time updates and team analytics.",
        "problem_statement": "Teams struggle to coordinate tasks across different tools and lose visibility into project progress.",
        "target_audience": "Small to medium-sized development teams",
        "artifact_type": "product",
        "weighted_score": 7.5,
        "status": "scored",
        "scoring_rubric": "life_domain",
    }


def _make_valid_agent_spec(title: str, idea_desc: str, problem: str, audience: str) -> str:
    """Build a mock LLM agent spec that passes all validate_agent_spec checks.

    Mirrors _make_valid_spec but produces CCOS-agent shape: agent.yaml +
    skills/ + test_e2e_*.py + README references, with the four required
    section headers (Overview, Agent shape, Constraints, Success criteria).
    Length lands ~80 lines — comfortably inside the 60-400 bounds.
    """
    lines = [
        f"# {title} - Agent Specification",
        "",
        "## Overview",
        f"{idea_desc}",
        "",
        f"**Problem Statement**: {problem}",
        f"**Target Audience**: {audience}",
        "",
        f"This is a CCOS agent for {audience}. It captures roughly 60-70%",
        "of the cognitive load described in the Scene below.",
        "",
        "**The struggling user.** Alex, 35, deals with the problem on a daily basis.",
        "We anchor every test on a moment from their day.",
        "",
        "## Agent shape",
        "",
        "Four file types in the produced project directory.",
        "",
        "### agent.yaml",
        "",
        "```yaml",
        f"name: {title}",
        "description: focused single-purpose agent",
        "model: claude-sonnet-4-6",
        "telegram_bot_token_env: MYAGENT_BOT_TOKEN",
        "```",
        "",
        "### skills/main_skill/SKILL.md",
        "",
        "Frontmatter: name, description, trigger. Body 4-8 paragraphs.",
        "",
        "### tests/test_e2e_scenes.py",
        "",
        "At least three E2E tests, each describing a Scene from the user's day.",
        "",
        "- test_e2e_morning_scene",
        "- test_e2e_midday_scene",
        "- test_e2e_evening_scene",
        "",
        "### README.md",
        "",
        "Scene-opening story. Four paragraphs. Meets the user before the agent.",
        "",
        "## Constraints",
        "",
        "- No external services.",
        "- No API keys hardcoded; telegram_bot_token_env may be stubbed at T1.",
        "- Skills bundled in agent directory.",
        "- No web frontend at T1.",
        "",
        "## Success criteria",
        "",
        "1. agent.yaml validates as YAML with all four required fields.",
        "2. skills/main_skill/SKILL.md exists with proper frontmatter.",
        "3. All three test_e2e tests pass against a mocked LLM.",
        "4. README opens with a Scene paragraph, not a feature list.",
        "",
        "## Out of scope (T1)",
        "",
        "- Multi-user support.",
        "- Cross-session memory.",
        "- Web frontend.",
    ]
    return "\n".join(lines)


class TestSpecGenerator:
    """Tests for SpecGenerator class."""

    def test_init_valid_template_dir(self, config, template_dir):
        """Test initialization with valid template directory."""
        with patch("gates.build.LLMSpecExpander"):
            generator = SpecGenerator(config, template_dir)
            assert generator.config == config
            assert generator.template_dir == template_dir

    def test_init_invalid_template_dir(self, config):
        """Test initialization with non-existent template directory."""
        invalid_dir = Path("/nonexistent/path")
        with pytest.raises(FileNotFoundError) as exc:
            SpecGenerator(config, invalid_dir)
        assert "Template directory not found" in str(exc.value)

    def test_generate_spec_with_mock_llm(self, config, template_dir, output_dir, tool_idea):
        """Test spec generation via LLM produces valid output (life_domain path)."""
        spec = _make_valid_agent_spec(
            tool_idea["title"], tool_idea["description"],
            tool_idea["problem_statement"], tool_idea["target_audience"],
        )
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = spec
            generator = SpecGenerator(config, template_dir)
            path = generator.generate_spec(tool_idea, output_dir)

        assert path.exists()
        assert path.name == "app_spec_1.txt"
        content = path.read_text()
        assert tool_idea["title"] in content
        assert "## Overview" in content
        assert "## Agent shape" in content

    def test_generate_spec_contains_idea_data(self, config, template_dir, output_dir, tool_idea):
        """Test generated spec contains idea title, description, problem, audience."""
        spec = _make_valid_agent_spec(
            tool_idea["title"], tool_idea["description"],
            tool_idea["problem_statement"], tool_idea["target_audience"],
        )
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = spec
            generator = SpecGenerator(config, template_dir)
            path = generator.generate_spec(tool_idea, output_dir)

        content = path.read_text()
        assert tool_idea["title"] in content
        assert tool_idea["description"] in content
        assert tool_idea["problem_statement"] in content
        assert tool_idea["target_audience"] in content

    def test_generate_spec_no_llm_raises(self, template_dir, output_dir, tool_idea):
        """Test that generate_spec raises RuntimeError when LLM is unavailable.

        Note: the strict-rubric ValueError fires BEFORE the LLM-availability
        check by design (cheap field check first). So this test passes
        tool_idea which carries scoring_rubric='life_domain' to reach the
        LLM-availability branch.
        """
        config = Config()
        config.spec_use_llm = False
        generator = SpecGenerator(config, template_dir)
        assert generator.llm_expander is None

        with pytest.raises(RuntimeError, match="LLM expander not configured"):
            generator.generate_spec(tool_idea, output_dir)

    def test_generate_spec_llm_failure_raises(self, config, template_dir, output_dir, tool_idea):
        """Test that LLM API failure raises RuntimeError (no Jinja2 fallback)."""
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.side_effect = Exception("API error")
            generator = SpecGenerator(config, template_dir)

            with pytest.raises(RuntimeError, match="LLM expansion failed"):
                generator.generate_spec(tool_idea, output_dir)

    def test_generate_spec_validation_failure_raises(self, config, template_dir, output_dir, tool_idea):
        """Test that a bad LLM spec raises ValueError (no Jinja2 fallback)."""
        bad_spec = "# Title\nToo short."  # Fails length check
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = bad_spec
            generator = SpecGenerator(config, template_dir)

            with pytest.raises(ValueError, match="LLM spec rejected"):
                generator.generate_spec(tool_idea, output_dir)

    def test_generate_spec_missing_required_fields(self, config, template_dir, output_dir):
        """Test error handling when idea is missing required fields."""
        incomplete_idea = {
            "id": 99,
            "title": "Incomplete Idea",
        }
        with patch("gates.build.LLMSpecExpander"):
            generator = SpecGenerator(config, template_dir)
            with pytest.raises(ValueError, match="missing required fields"):
                generator.generate_spec(incomplete_idea, output_dir)

    def test_generate_spec_creates_output_dir(self, config, template_dir, tool_idea):
        """Test that output directory is created if it doesn't exist."""
        spec = _make_valid_agent_spec(
            tool_idea["title"], tool_idea["description"],
            tool_idea["problem_statement"], tool_idea["target_audience"],
        )
        with tempfile.TemporaryDirectory() as temp_base:
            output_dir = Path(temp_base) / "nested" / "output" / "dir"
            assert not output_dir.exists()

            with patch("gates.build.LLMSpecExpander") as MockExp:
                MockExp.return_value.expand_agent.return_value = spec
                generator = SpecGenerator(config, template_dir)
                output_path = generator.generate_spec(tool_idea, output_dir)

            assert output_dir.exists()
            assert output_path.exists()

    def test_generate_spec_output_path_format(self, config, template_dir, output_dir, agent_idea):
        """Test that output file follows naming convention: app_spec_{idea_id}.txt"""
        spec = _make_valid_agent_spec(
            agent_idea["title"], agent_idea["description"],
            agent_idea["problem_statement"], agent_idea["target_audience"],
        )
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = spec
            generator = SpecGenerator(config, template_dir)
            output_path = generator.generate_spec(agent_idea, output_dir)

        assert output_path.name == f"app_spec_{agent_idea['id']}.txt"
        assert output_path.parent == output_dir

    def test_generate_spec_file_encoding(self, config, template_dir, output_dir, product_idea):
        """Test that generated file uses UTF-8 encoding."""
        idea_with_unicode = product_idea.copy()
        idea_with_unicode["description"] = "A tool with unicode: 你好, здравствуй, مرحبا"
        spec = _make_valid_agent_spec(
            idea_with_unicode["title"], idea_with_unicode["description"],
            idea_with_unicode["problem_statement"], idea_with_unicode["target_audience"],
        )
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = spec
            generator = SpecGenerator(config, template_dir)
            output_path = generator.generate_spec(idea_with_unicode, output_dir)

        content = output_path.read_text(encoding="utf-8")
        assert "你好" in content
        assert "здравствуй" in content
        assert "مرحبا" in content

    def test_generate_spec_multiple_ideas(self, config, template_dir, output_dir, tool_idea, agent_idea, product_idea):
        """Test generating specs for multiple ideas in the same output directory."""
        specs = {}
        for idea in [tool_idea, agent_idea, product_idea]:
            specs[idea["id"]] = _make_valid_agent_spec(
                idea["title"], idea["description"],
                idea["problem_statement"], idea["target_audience"],
            )

        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.side_effect = (
                lambda idea, **kwargs: specs[idea["id"]]
            )
            generator = SpecGenerator(config, template_dir)

            path1 = generator.generate_spec(tool_idea, output_dir)
            path2 = generator.generate_spec(agent_idea, output_dir)
            path3 = generator.generate_spec(product_idea, output_dir)

        assert path1.exists() and path2.exists() and path3.exists()
        assert path1.name == "app_spec_1.txt"
        assert path2.name == "app_spec_2.txt"
        assert path3.name == "app_spec_3.txt"
        assert tool_idea["title"] in path1.read_text()
        assert agent_idea["title"] in path2.read_text()
        assert product_idea["title"] in path3.read_text()


class TestSpecGeneratorRubricDispatch:
    """R-A item 1: SpecGenerator.generate_spec dispatches on scoring_rubric.

    life_domain -> AGENT_SPEC_EXPANSION_PROMPT + validate_agent_spec.
    Anything else (None, 'tech', unknown) -> ValueError before any LLM call.
    Defense-in-depth: the queue-level guard from R-A item 3 already rejects
    non-life_domain at dequeue; this enforces the same invariant at the
    Builder so a future code path that bypasses the queue can't silently
    produce the wrong shape.
    """

    def test_life_domain_routes_to_expand_agent(self, config, template_dir, output_dir, tool_idea):
        spec = _make_valid_agent_spec(
            tool_idea["title"], tool_idea["description"],
            tool_idea["problem_statement"], tool_idea["target_audience"],
        )
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = spec
            # The tech-path expand() must NOT be called for life_domain.
            MockExp.return_value.expand.side_effect = AssertionError(
                "expand() must not be called for life_domain rubric"
            )
            generator = SpecGenerator(config, template_dir)
            path = generator.generate_spec(tool_idea, output_dir)

        assert path.exists()
        MockExp.return_value.expand_agent.assert_called_once()
        MockExp.return_value.expand.assert_not_called()

    def test_missing_rubric_raises_before_llm_call(self, config, template_dir, output_dir, tool_idea):
        """ValueError fires BEFORE any LLM call when scoring_rubric is absent."""
        idea = dict(tool_idea)
        idea.pop("scoring_rubric", None)

        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.side_effect = AssertionError(
                "LLM must not be called when rubric is missing"
            )
            MockExp.return_value.expand.side_effect = AssertionError(
                "LLM must not be called when rubric is missing"
            )
            generator = SpecGenerator(config, template_dir)
            with pytest.raises(ValueError, match=r"Builder requires scoring_rubric='life_domain'"):
                generator.generate_spec(idea, output_dir)
            MockExp.return_value.expand_agent.assert_not_called()
            MockExp.return_value.expand.assert_not_called()

    def test_none_rubric_raises(self, config, template_dir, output_dir, tool_idea):
        idea = dict(tool_idea)
        idea["scoring_rubric"] = None

        with patch("gates.build.LLMSpecExpander"):
            generator = SpecGenerator(config, template_dir)
            with pytest.raises(ValueError, match=r"got None"):
                generator.generate_spec(idea, output_dir)

    def test_tech_rubric_raises(self, config, template_dir, output_dir, tool_idea):
        idea = dict(tool_idea)
        idea["scoring_rubric"] = "tech"

        with patch("gates.build.LLMSpecExpander"):
            generator = SpecGenerator(config, template_dir)
            with pytest.raises(ValueError, match=r"got 'tech'"):
                generator.generate_spec(idea, output_dir)

    def test_unknown_rubric_raises(self, config, template_dir, output_dir, tool_idea):
        idea = dict(tool_idea)
        idea["scoring_rubric"] = "banana"

        with patch("gates.build.LLMSpecExpander"):
            generator = SpecGenerator(config, template_dir)
            with pytest.raises(ValueError, match=r"got 'banana'"):
                generator.generate_spec(idea, output_dir)

    def test_retry_loop_fires_on_invalid_output(self, config, template_dir, output_dir, tool_idea):
        """Three attempts at a bad spec, then ValueError matching the existing
        tech-path 'LLM spec rejected' message so log greps continue to work."""
        bad_spec = "# Tiny\n## A\n## B\n## C\n"  # 4 lines, fails length check
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = bad_spec
            generator = SpecGenerator(config, template_dir)

            with pytest.raises(ValueError, match=r"LLM spec rejected"):
                generator.generate_spec(tool_idea, output_dir)

            assert MockExp.return_value.expand_agent.call_count == 3

    def test_golden_fixture_e2e_passes_real_validator(self, config, template_dir, output_dir, tool_idea):
        """The Builder's prompt + validator pair accept the hand-crafted
        golden fixture end-to-end. This is the stability anchor: if a future
        prompt edit drifts the validator's expectations, this test fires."""
        fixture_path = Path(__file__).parent.parent / "spec_templates" / "fixtures" / "agent_spec_golden.md"
        assert fixture_path.exists(), f"golden fixture missing: {fixture_path}"
        golden_text = fixture_path.read_text(encoding="utf-8")

        # Real validator must accept the fixture
        ok, reason = validate_agent_spec(golden_text)
        assert ok, f"golden fixture rejected by validate_agent_spec: {reason}"

        # And generate_spec writes it through
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = golden_text
            generator = SpecGenerator(config, template_dir)
            path = generator.generate_spec(tool_idea, output_dir)

        assert path.read_text(encoding="utf-8") == golden_text

    def test_queue_job_id_threaded_to_expand_agent(self, config, template_dir, output_dir, tool_idea):
        spec = _make_valid_agent_spec(
            tool_idea["title"], tool_idea["description"],
            tool_idea["problem_statement"], tool_idea["target_audience"],
        )
        with patch("gates.build.LLMSpecExpander") as MockExp:
            MockExp.return_value.expand_agent.return_value = spec
            generator = SpecGenerator(config, template_dir)
            generator.generate_spec(tool_idea, output_dir, queue_job_id="metroplex-ideaforge-1")

        kwargs = MockExp.return_value.expand_agent.call_args.kwargs
        assert kwargs.get("queue_job_id") == "metroplex-ideaforge-1"


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
            assert "python" in command[0]
            assert str(orchestrator.queue_runner_path) in command[1]
            assert "add" in command
            assert str(spec_path) in command
            assert "--id" in command
            assert "metroplex-ideaforge-1" in command  # job_id format: metroplex-{source}-{id}
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
            assert job.queue_job_id == "metroplex-ideaforge-1"
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
            assert job.queue_job_id == "metroplex-ideaforge-1"

            # Verify state_db was called with failed job
            assert mock_state_db.record_build_job.called

            # Verify audit logger logged error
            assert mock_audit_logger.log_error.called
            error_call_args = mock_audit_logger.log_error.call_args
            assert "Queue runner error" in error_call_args[1]["error"]

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
            assert "metroplex-ideaforge-1" in captured.out

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

            assert "python" in command[0]
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
            assert job.queue_job_id == "metroplex-ideaforge-42"

            # Verify it was used in the command
            call_args = mock_run.call_args
            command = call_args[0][0]
            assert "metroplex-ideaforge-42" in command

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

            assert "python" in command[0]
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

    def test_queue_build_parallel_flags(self, mock_state_db, mock_spec_generator, mock_audit_logger, tool_idea):
        """Test queue_build passes --parallel and --max-workers when config.build_parallel is True."""
        config = Config()
        config.build_parallel = True
        config.build_max_workers = 3

        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
        )
        spec_path = Path("/tmp/app_spec_1.txt")

        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            orch.queue_build(tool_idea, spec_path, dry_run=False)

            call_args = mock_run.call_args
            command = call_args[0][0]

            assert "--parallel" in command
            assert "--max-workers" in command
            idx = command.index("--max-workers")
            assert command[idx + 1] == "3"

    def test_queue_build_no_parallel_flags_when_disabled(self, orchestrator, mock_state_db, mock_audit_logger, tool_idea):
        """Test queue_build does NOT pass --parallel when config.build_parallel is False."""
        orchestrator.config.build_parallel = False  # Ensure disabled regardless of .env
        spec_path = Path("/tmp/app_spec_1.txt")

        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            orchestrator.queue_build(tool_idea, spec_path, dry_run=False)

            call_args = mock_run.call_args
            command = call_args[0][0]

            assert "--parallel" not in command
            assert "--max-workers" not in command

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

    # --- Level 2: Concurrency tests ---

    def test_start_queue_background_passes_concurrency(self, mock_state_db, mock_spec_generator, mock_audit_logger):
        """Test start_queue_background includes --concurrency flag from config."""
        config = Config()
        config.max_concurrent_builds = 3

        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
        )

        with patch('subprocess.Popen') as mock_popen, \
             patch.object(orch, 'is_runner_active', return_value=False), \
             patch('builtins.open', MagicMock()):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            result = orch.start_queue_background(dry_run=False)

            assert result is True
            call_args = mock_popen.call_args
            command = call_args[0][0]
            assert "--concurrency" in command
            idx = command.index("--concurrency")
            assert command[idx + 1] == "3"

    def test_start_queue_background_dry_run_shows_concurrency(self, orchestrator, capsys):
        """Test dry-run prints the --concurrency flag."""
        orchestrator.config.max_concurrent_builds = 2
        result = orchestrator.start_queue_background(dry_run=True)
        assert result is True

        captured = capsys.readouterr()
        assert "[DRY RUN]" in captured.out
        assert "concurrency" in captured.out
        assert "2" in captured.out

    def test_start_queue_background_skips_if_active(self, orchestrator, mock_audit_logger, capsys):
        """Test start_queue_background skips if runner already active."""
        with patch.object(orchestrator, 'is_runner_active', return_value=True):
            result = orchestrator.start_queue_background(dry_run=False)
            assert result is True

            captured = capsys.readouterr()
            assert "already active" in captured.out

    def test_poll_and_sync_returns_list_format(self, orchestrator, mock_state_db, mock_audit_logger):
        """Test poll_and_sync_status returns running as list[str] with running_count."""
        status_dict = {
            "jobs": [
                {"id": "metroplex-1", "status": "running"},
                {"id": "metroplex-2", "status": "running"},
                {"id": "metroplex-3", "status": "completed"},
                {"id": "metroplex-4", "status": "failed"},
            ]
        }

        with patch.object(orchestrator, 'check_status', return_value=status_dict), \
             patch.object(orchestrator, 'is_runner_active', return_value=True):
            # Mock state_db.update_build_job_status
            mock_state_db.update_build_job_status = Mock()

            result = orchestrator.poll_and_sync_status()

            assert isinstance(result["running"], list)
            assert result["running"] == ["metroplex-1", "metroplex-2"]
            assert result["running_count"] == 2
            assert result["completed"] == ["metroplex-3"]
            assert result["failed"] == ["metroplex-4"]
            assert "metroplex-3" in result["newly_synced"]
            assert "metroplex-4" in result["newly_synced"]

    def test_poll_and_sync_empty_status(self, orchestrator, mock_audit_logger):
        """Test poll_and_sync_status with empty status returns zero-filled dict."""
        with patch.object(orchestrator, 'check_status', return_value={}), \
             patch.object(orchestrator, 'is_runner_active', return_value=False):
            result = orchestrator.poll_and_sync_status()

            assert result["running"] == []
            assert result["running_count"] == 0
            assert result["completed"] == []
            assert result["failed"] == []
            assert result["newly_synced"] == []

    # --- Gap 1: Multi-source poll_and_sync tests ---

    def test_poll_and_sync_multi_source_jobs(self, orchestrator, mock_state_db, mock_audit_logger):
        """poll_and_sync handles jobs from multiple sources."""
        status_dict = {
            "jobs": [
                {"id": "metroplex-ideaforge-5", "status": "completed"},
                {"id": "metroplex-skylynx-sl-abc", "status": "completed"},
                {"id": "metroplex-linear-TOO-42", "status": "running"},
            ]
        }
        with patch.object(orchestrator, 'check_status', return_value=status_dict), \
             patch.object(orchestrator, 'is_runner_active', return_value=True):
            mock_state_db.update_build_job_status = Mock()
            result = orchestrator.poll_and_sync_status()
            assert result["running"] == ["metroplex-linear-TOO-42"]
            assert result["running_count"] == 1
            assert "metroplex-ideaforge-5" in result["newly_synced"]
            assert "metroplex-skylynx-sl-abc" in result["newly_synced"]
            # Fix A: running jobs now also get an update_build_job_status call
            # (transition queued → started). 2 completed + 1 running = 3 calls.
            assert "metroplex-linear-TOO-42" in result["newly_synced"]
            assert mock_state_db.update_build_job_status.call_count == 3

    def test_poll_and_sync_unexpected_status_ignored(self, orchestrator, mock_state_db, mock_audit_logger):
        """Jobs with unexpected status are not categorized."""
        status_dict = {
            "jobs": [
                {"id": "metroplex-ideaforge-1", "status": "queued"},
                {"id": "metroplex-ideaforge-2", "status": "completed"},
            ]
        }
        with patch.object(orchestrator, 'check_status', return_value=status_dict), \
             patch.object(orchestrator, 'is_runner_active', return_value=True):
            mock_state_db.update_build_job_status = Mock()
            result = orchestrator.poll_and_sync_status()
            assert "metroplex-ideaforge-1" not in result["running"]
            assert "metroplex-ideaforge-1" not in result["completed"]
            assert "metroplex-ideaforge-2" in result["newly_synced"]

    def test_poll_and_sync_all_running_writes_started(self, orchestrator, mock_state_db, mock_audit_logger):
        """Fix A: running jobs transition queued → started so stale-queued
        recovery doesn't destroy rows for long-running Opus builds."""
        status_dict = {
            "jobs": [
                {"id": "metroplex-ideaforge-1", "status": "running"},
                {"id": "metroplex-skylynx-sl-x", "status": "running"},
            ]
        }
        with patch.object(orchestrator, 'check_status', return_value=status_dict), \
             patch.object(orchestrator, 'is_runner_active', return_value=True):
            mock_state_db.update_build_job_status = Mock(return_value=True)
            result = orchestrator.poll_and_sync_status()
            assert result["running_count"] == 2
            # Both running jobs should have been marked 'started'
            assert mock_state_db.update_build_job_status.call_count == 2
            started_calls = [
                c for c in mock_state_db.update_build_job_status.call_args_list
                if c[0][1] == "started"
            ]
            assert len(started_calls) == 2
            assert "metroplex-ideaforge-1" in result["newly_synced"]
            assert "metroplex-skylynx-sl-x" in result["newly_synced"]

    def test_poll_and_sync_db_error_continues(self, orchestrator, mock_state_db, mock_audit_logger):
        """A DB error syncing one job does not prevent syncing others."""
        status_dict = {
            "jobs": [
                {"id": "metroplex-ideaforge-1", "status": "completed"},
                {"id": "metroplex-ideaforge-2", "status": "completed"},
            ]
        }
        call_count = {"n": 0}
        def side_effect(*args):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("DB locked")
            return True
        with patch.object(orchestrator, 'check_status', return_value=status_dict), \
             patch.object(orchestrator, 'is_runner_active', return_value=True):
            mock_state_db.update_build_job_status = Mock(side_effect=side_effect)
            result = orchestrator.poll_and_sync_status()
            assert mock_state_db.update_build_job_status.call_count == 2
            # Second job should have synced even though first failed
            assert len(result["newly_synced"]) >= 1

    def test_poll_and_sync_runner_not_active(self, orchestrator, mock_audit_logger):
        """When runner is not active and no status, returns empty result."""
        with patch.object(orchestrator, 'is_runner_active', return_value=False), \
             patch.object(orchestrator, 'check_status', return_value={}):
            result = orchestrator.poll_and_sync_status()
            assert result["running"] == []
            assert result["running_count"] == 0
            assert result["newly_synced"] == []

    # --- Stale Queued Build Detection ---

    def test_poll_and_sync_resets_stale_queued_builds(self, orchestrator, mock_state_db, mock_audit_logger):
        """Stale queued builds are detected and reset to pending."""
        stale_builds = [
            {
                "queue_job_id": "metroplex-ideaforge-133",
                "idea_id": 133,
                "title": "AgentFlow Orchestrator",
                "queued_at": "2026-03-23T06:19:36.965141",
                "priority_queue_id": 31,
                "source": "ideaforge",
                "source_id": "133",
            }
        ]
        mock_state_db.get_stale_queued_builds = Mock(return_value=stale_builds)
        mock_state_db.reset_stale_queued_build = Mock()

        with patch.object(orchestrator, 'check_status', return_value={"jobs": []}), \
             patch.object(orchestrator, 'is_runner_active', return_value=False):
            result = orchestrator.poll_and_sync_status()

        mock_state_db.reset_stale_queued_build.assert_called_once_with(
            "metroplex-ideaforge-133", 31
        )
        assert result.get("stale_reset") == ["metroplex-ideaforge-133"]
        mock_audit_logger.log_decision.assert_called()
        logged = mock_audit_logger.log_decision.call_args
        assert logged[1]["action"] == "stale_queued_reset" or logged[0][1] == "stale_queued_reset"

    def test_poll_and_sync_no_stale_builds(self, orchestrator, mock_state_db, mock_audit_logger):
        """No stale builds means no resets."""
        mock_state_db.get_stale_queued_builds = Mock(return_value=[])
        mock_state_db.reset_stale_queued_build = Mock()

        with patch.object(orchestrator, 'check_status', return_value={"jobs": []}), \
             patch.object(orchestrator, 'is_runner_active', return_value=False):
            result = orchestrator.poll_and_sync_status()

        mock_state_db.reset_stale_queued_build.assert_not_called()
        assert "stale_reset" not in result

    def test_poll_and_sync_stale_check_error_does_not_crash(self, orchestrator, mock_state_db, mock_audit_logger):
        """Stale build check failure is logged but doesn't break the poll."""
        mock_state_db.get_stale_queued_builds = Mock(side_effect=Exception("DB locked"))

        with patch.object(orchestrator, 'check_status', return_value={"jobs": []}), \
             patch.object(orchestrator, 'is_runner_active', return_value=False):
            result = orchestrator.poll_and_sync_status()

        # Should still return a valid result
        assert result["running"] == []
        mock_audit_logger.log_error.assert_called()

    # --- Fix A: running jobs transition queued → started ---

    def test_fix_a_running_job_marked_started(self, orchestrator, mock_state_db, mock_audit_logger):
        """A running job triggers update_build_job_status(job_id, 'started')."""
        status_dict = {"jobs": [{"id": "metroplex-ideaforge-38", "status": "running"}]}
        with patch.object(orchestrator, 'check_status', return_value=status_dict), \
             patch.object(orchestrator, 'is_runner_active', return_value=True):
            mock_state_db.update_build_job_status = Mock(return_value=True)
            result = orchestrator.poll_and_sync_status()
            mock_state_db.update_build_job_status.assert_called_once_with(
                "metroplex-ideaforge-38", "started"
            )
            assert "metroplex-ideaforge-38" in result["newly_synced"]
            assert result["running"] == ["metroplex-ideaforge-38"]

    def test_fix_a_running_sync_error_is_logged_not_raised(self, orchestrator, mock_state_db, mock_audit_logger):
        """If update_build_job_status raises, error is logged and poll continues."""
        status_dict = {
            "jobs": [
                {"id": "metroplex-ideaforge-99", "status": "running"},
                {"id": "metroplex-ideaforge-100", "status": "completed"},
            ]
        }
        def side_effect(job_id, status):
            if status == "started":
                raise Exception("DB busy")
            return True
        with patch.object(orchestrator, 'check_status', return_value=status_dict), \
             patch.object(orchestrator, 'is_runner_active', return_value=True):
            mock_state_db.update_build_job_status = Mock(side_effect=side_effect)
            result = orchestrator.poll_and_sync_status()
            assert "metroplex-ideaforge-99" in result["running"]
            # Completed job should still sync
            assert "metroplex-ideaforge-100" in result["newly_synced"]
            mock_audit_logger.log_error.assert_called()

    # --- Fix C: YCE queue.json read excludes running jobs from stale check ---

    def test_fix_c_excludes_running_yce_jobs_from_stale(self, orchestrator, mock_state_db, mock_audit_logger, tmp_path):
        """Jobs marked running in YCE queue.json are excluded from stale check."""
        yce_dir = tmp_path / "yce"
        (yce_dir / "data").mkdir(parents=True)
        queue_path = yce_dir / "data" / "queue.json"
        queue_path.write_text(json.dumps([
            {"job_id": "metroplex-ideaforge-38", "status": "running"},
            {"job_id": "metroplex-ideaforge-50", "status": "pending"},
        ]))
        orchestrator.config.yce_dir = str(yce_dir)

        mock_state_db.get_stale_queued_builds = Mock(return_value=[])

        with patch.object(orchestrator, 'check_status', return_value={"jobs": []}), \
             patch.object(orchestrator, 'is_runner_active', return_value=False):
            orchestrator.poll_and_sync_status()

        # Verify get_stale_queued_builds was called with the running set
        mock_state_db.get_stale_queued_builds.assert_called_once()
        kwargs = mock_state_db.get_stale_queued_builds.call_args.kwargs
        assert "exclude_job_ids" in kwargs
        assert "metroplex-ideaforge-38" in kwargs["exclude_job_ids"]
        assert "metroplex-ideaforge-50" in kwargs["exclude_job_ids"]

    def test_fix_c_missing_yce_queue_does_not_crash(self, orchestrator, mock_state_db, mock_audit_logger, tmp_path):
        """If YCE queue.json is missing, stale check still runs with empty exclude set."""
        yce_dir = tmp_path / "yce-missing"
        yce_dir.mkdir()
        orchestrator.config.yce_dir = str(yce_dir)

        mock_state_db.get_stale_queued_builds = Mock(return_value=[])

        with patch.object(orchestrator, 'check_status', return_value={"jobs": []}), \
             patch.object(orchestrator, 'is_runner_active', return_value=False):
            orchestrator.poll_and_sync_status()

        mock_state_db.get_stale_queued_builds.assert_called_once()
        kwargs = mock_state_db.get_stale_queued_builds.call_args.kwargs
        assert kwargs.get("exclude_job_ids") == set()

    def test_fix_c_malformed_yce_queue_logs_error(self, orchestrator, mock_state_db, mock_audit_logger, tmp_path):
        """A malformed queue.json is logged but poll continues."""
        yce_dir = tmp_path / "yce-bad"
        (yce_dir / "data").mkdir(parents=True)
        (yce_dir / "data" / "queue.json").write_text("{not json")
        orchestrator.config.yce_dir = str(yce_dir)

        mock_state_db.get_stale_queued_builds = Mock(return_value=[])

        with patch.object(orchestrator, 'check_status', return_value={"jobs": []}), \
             patch.object(orchestrator, 'is_runner_active', return_value=False):
            orchestrator.poll_and_sync_status()

        mock_audit_logger.log_error.assert_called()
        mock_state_db.get_stale_queued_builds.assert_called_once()

    def test_run_from_queue_capacity_dispatch(self, mock_state_db, mock_spec_generator, mock_audit_logger):
        """Test run_from_queue dispatches up to max_approve_per_cycle items."""
        config = Config()
        config.max_concurrent_builds = 3
        config.max_approve_per_cycle = 2

        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
        )

        # Create mock priority items (3 available, but per-cycle cap is 2).
        # R-A item 3: every ideaforge fixture must carry scoring_rubric='life_domain'
        # to clear the post-pivot dequeue guard. These tests exercise capacity
        # behavior, not the rubric filter — so we give them the rubric value the
        # production filter requires.
        mock_items = [
            Mock(id=10, source="ideaforge", source_id=10, title="Idea A", description="A CLI tool that scans codebases for issues",
                 idea_data=json.dumps({"id": 10, "title": "Idea A", "description": "A CLI tool that scans codebases for issues",
                                       "problem_statement": "P", "target_audience": "T",
                                       "artifact_type": "tool", "scoring_rubric": "life_domain"})),
            Mock(id=11, source="ideaforge", source_id=11, title="Idea B", description="A markdown linter for documentation files",
                 idea_data=json.dumps({"id": 11, "title": "Idea B", "description": "A markdown linter for documentation files",
                                       "problem_statement": "P", "target_audience": "T",
                                       "artifact_type": "tool", "scoring_rubric": "life_domain"})),
        ]
        mock_state_db.claim_next_pending = Mock(side_effect=mock_items + [None])
        mock_state_db.update_item_status = Mock()
        mock_state_db.has_completed_build = Mock(return_value=False)
        mock_state_db.has_exhausted_retries = Mock(return_value=False)
        mock_state_db.count_failed_builds = Mock(return_value=0)

        # Mock spec generation and queue_build
        mock_spec_path = Mock()
        mock_spec_path.read_text.return_value = "test spec"
        mock_spec_path.resolve.return_value = mock_spec_path
        mock_spec_generator.generate_spec = Mock(return_value=mock_spec_path)

        with patch.object(orch, 'queue_build') as mock_queue, \
             patch.object(orch, 'start_queue_background'):
            mock_queue.return_value = BuildJob(
                idea_id=10, title="Idea A", spec_path="",
                queue_job_id="metroplex-ideaforge-10", status="queued",
                queued_at=datetime.now(),
            )
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

            assert len(jobs) == 2
            assert mock_queue.call_count == 2

    def test_run_from_queue_empty_queue_prints_message(self, mock_state_db, mock_spec_generator, mock_audit_logger, capsys):
        """Test run_from_queue prints message when queue is empty."""
        config = Config()
        config.max_approve_per_cycle = 3

        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
        )

        # Empty queue
        mock_state_db.get_next_pending = Mock(return_value=None)

        jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        assert jobs == []
        captured = capsys.readouterr()
        assert "No pending items" in captured.out

    def test_is_runner_active_no_pid_file(self, orchestrator):
        """Test is_runner_active returns False when PID file doesn't exist."""
        with patch('gates.build.RUNNER_PID_FILE') as mock_pid:
            mock_pid.exists.return_value = False
            assert orchestrator.is_runner_active() is False

    def test_is_runner_active_stale_pid(self, orchestrator, tmp_path):
        """Test is_runner_active cleans up stale PID file."""
        pid_file = tmp_path / "runner.pid"
        pid_file.write_text("99999999")  # Non-existent PID

        with patch('gates.build.RUNNER_PID_FILE', pid_file):
            result = orchestrator.is_runner_active()
            assert result is False
            # Stale PID file should be cleaned up
            assert not pid_file.exists()


class TestSelfHealingLivenessGuard:
    """Tests for BuildGate liveness guard protecting the self-healing daemon path."""

    @pytest.fixture
    def mock_state_db(self):
        db = Mock(spec=StateDB)
        db.record_build_job = Mock()
        db.release_claim = Mock()
        db.update_item_status = Mock()
        db.has_completed_build = Mock(return_value=False)
        db.has_exhausted_retries = Mock(return_value=False)
        db.count_failed_builds = Mock(return_value=0)
        return db

    @pytest.fixture
    def mock_audit_logger(self):
        logger = Mock(spec=AuditLogger)
        logger.log_decision = Mock()
        logger.log_error = Mock()
        return logger

    @pytest.fixture
    def mock_spec_generator(self):
        generator = Mock(spec=SpecGenerator)
        mock_spec_path = Mock()
        mock_spec_path.read_text.return_value = "test spec"
        mock_spec_path.resolve.return_value = mock_spec_path
        generator.generate_spec = Mock(return_value=mock_spec_path)
        return generator

    @pytest.fixture
    def queued_result(self):
        from build_adapter import BuildAdapterResult
        return BuildAdapterResult(
            job_id="metroplex-ideaforge-1",
            status="queued",
            runtime="self_healing",
        )

    def _make_orchestrator(self, build_target, mock_state_db, mock_spec_generator,
                           mock_audit_logger, adapter):
        config = Config()
        config.build_target = build_target
        config.max_approve_per_cycle = 1
        return BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
            adapter=adapter,
        )

    def test_queue_build_skips_when_self_healing_daemon_down(
        self, mock_state_db, mock_spec_generator, mock_audit_logger, tool_idea, caplog,
    ):
        from build_adapter import BuildAdapter
        adapter = MagicMock(spec=BuildAdapter)
        adapter.is_active.return_value = False
        orch = self._make_orchestrator(
            "self_healing", mock_state_db, mock_spec_generator, mock_audit_logger, adapter,
        )

        with caplog.at_level("WARNING", logger="gates.build"):
            job = orch.queue_build(tool_idea, Path("/tmp/app_spec_1.txt"), dry_run=False)

        assert job is None
        adapter.queue.assert_not_called()
        mock_state_db.record_build_job.assert_not_called()
        combined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "self-healing daemon" in combined
        assert "/self-healing-daemon start" in combined

    def test_queue_build_dispatches_when_self_healing_daemon_up(
        self, mock_state_db, mock_spec_generator, mock_audit_logger, tool_idea, queued_result,
    ):
        from build_adapter import BuildAdapter
        adapter = MagicMock(spec=BuildAdapter)
        adapter.is_active.return_value = True
        adapter.queue.return_value = queued_result
        orch = self._make_orchestrator(
            "self_healing", mock_state_db, mock_spec_generator, mock_audit_logger, adapter,
        )

        job = orch.queue_build(tool_idea, Path("/tmp/app_spec_1.txt"), dry_run=False)

        assert adapter.queue.call_count == 1
        assert job is not None
        assert job.queue_job_id == "metroplex-ideaforge-1"
        assert job.status == "queued"
        mock_state_db.record_build_job.assert_called_once()

    def test_queue_build_local_target_ignores_heartbeat(
        self, mock_state_db, mock_spec_generator, mock_audit_logger, tool_idea, queued_result,
    ):
        from build_adapter import BuildAdapter
        adapter = MagicMock(spec=BuildAdapter)
        adapter.is_active.return_value = False  # stale, but target is local
        adapter.queue.return_value = queued_result
        orch = self._make_orchestrator(
            "local", mock_state_db, mock_spec_generator, mock_audit_logger, adapter,
        )

        job = orch.queue_build(tool_idea, Path("/tmp/app_spec_1.txt"), dry_run=False)

        # Guard must be a no-op for non-self_healing targets
        assert adapter.queue.call_count == 1
        assert job is not None

    def test_run_from_queue_releases_claim_when_daemon_down(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        from build_adapter import BuildAdapter
        adapter = MagicMock(spec=BuildAdapter)
        adapter.is_active.return_value = False
        orch = self._make_orchestrator(
            "self_healing", mock_state_db, mock_spec_generator, mock_audit_logger, adapter,
        )

        item = Mock(
            id=42, source="ideaforge", source_id=10, title="Idea A",
            description="A CLI tool that scans codebases for issues",
            idea_data=json.dumps({
                "id": 10, "title": "Idea A",
                "description": "A CLI tool that scans codebases for issues",
                "problem_statement": "P", "target_audience": "T",
                "artifact_type": "tool",
                "scoring_rubric": "life_domain",
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[item, None])

        with patch.object(orch, 'start_queue_background'):
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        mock_state_db.release_claim.assert_called_once_with(42)
        adapter.queue.assert_not_called()
        # update_item_status should NOT be invoked with "failed" for this item:
        failed_calls = [
            c for c in mock_state_db.update_item_status.call_args_list
            if len(c.args) >= 2 and c.args[0] == 42 and c.args[1] == "failed"
        ]
        assert failed_calls == []
        assert jobs == []

    def test_skip_does_not_raise(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        from build_adapter import BuildAdapter
        adapter = MagicMock(spec=BuildAdapter)
        adapter.is_active.return_value = False
        orch = self._make_orchestrator(
            "self_healing", mock_state_db, mock_spec_generator, mock_audit_logger, adapter,
        )

        item = Mock(
            id=7, source="ideaforge", source_id=7, title="Idea X",
            description="A CLI tool that generates changelogs automatically",
            idea_data=json.dumps({
                "id": 7, "title": "Idea X",
                "description": "A CLI tool that generates changelogs automatically",
                "problem_statement": "P", "target_audience": "T",
                "artifact_type": "tool",
                "scoring_rubric": "life_domain",
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[item, None])

        with patch.object(orch, 'start_queue_background'):
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        assert jobs == []

    def test_run_from_queue_releases_claim_when_backoff_active(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        """When a build is skipped due to active backoff, Gate 2 must release
        the priority_queue claim. Otherwise the row stays atomically claimed
        and the orchestrator's auto-retry path mis-reads it as
        'Gate 2 never consumed', marking the build retry_stuck_abandoned and
        permanently stranding the idea.
        """
        from build_adapter import BuildAdapter
        adapter = MagicMock(spec=BuildAdapter)
        adapter.is_active.return_value = True
        orch = self._make_orchestrator(
            "self_healing", mock_state_db, mock_spec_generator, mock_audit_logger, adapter,
        )

        # Simulate retry scenario: prior failed attempt + active backoff timer
        mock_state_db.count_failed_builds = Mock(return_value=1)
        mock_state_db.is_backoff_active = Mock(return_value=True)

        item = Mock(
            id=99, source="ideaforge", source_id=42, title="Backoff Active",
            description="A CLI tool that exists primarily to test backoff handling",
            idea_data=json.dumps({
                "id": 42, "title": "Backoff Active",
                "description": "A CLI tool that exists primarily to test backoff handling",
                "problem_statement": "P", "target_audience": "T",
                "artifact_type": "tool",
                "scoring_rubric": "life_domain",
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[item, None])

        with patch.object(orch, 'start_queue_background'):
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        # KEY assertion: the claim must be released so the row returns to pending
        mock_state_db.release_claim.assert_called_once_with(99)
        # No build was dispatched
        adapter.queue.assert_not_called()
        mock_state_db.record_build_job.assert_not_called()
        assert jobs == []


class TestBuildDequeueRubricFilter:
    """R-A item 3 / Codex Round 1 defense-in-depth: tech-rubric items
    sitting stale in the priority_queue must be drained at dequeue, not
    dispatched."""

    @pytest.fixture
    def mock_state_db(self):
        db = Mock(spec=StateDB)
        db.record_build_job = Mock()
        db.release_claim = Mock()
        db.update_item_status = Mock()
        db.has_completed_build = Mock(return_value=False)
        db.has_exhausted_retries = Mock(return_value=False)
        db.count_failed_builds = Mock(return_value=0)
        return db

    @pytest.fixture
    def mock_audit_logger(self):
        logger = Mock(spec=AuditLogger)
        logger.log_decision = Mock()
        logger.log_error = Mock()
        return logger

    @pytest.fixture
    def mock_spec_generator(self):
        generator = Mock(spec=SpecGenerator)
        return generator

    def test_tech_rubric_dequeue_is_rejected(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        """An ideaforge priority_queue item carrying scoring_rubric='tech'
        is rejected at dequeue. No spec generation, no BuildJob recorded.
        """
        config = Config()
        config.max_approve_per_cycle = 1
        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
        )

        tech_item = Mock(
            id=701, source="ideaforge", source_id=701, title="Legacy Tech",
            description="A CLI tool for tech things",
            idea_data=json.dumps({
                "id": 701, "title": "Legacy Tech",
                "description": "A CLI tool for tech things",
                "problem_statement": "P", "target_audience": "T",
                "artifact_type": "tool",
                "scoring_rubric": "tech",
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[tech_item, None])

        with patch.object(orch, 'start_queue_background'):
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        assert jobs == [], "tech-rubric item must not dispatch"
        mock_spec_generator.generate_spec.assert_not_called()
        mock_state_db.record_build_job.assert_not_called()
        # Item is finalized (status='failed') so it doesn't re-appear next cycle.
        mock_state_db.update_item_status.assert_called_with(
            701, "failed", "completed_at",
        )
        # Audit log captured the rejection with the rubric value.
        rejection_calls = [
            c for c in mock_audit_logger.log_decision.call_args_list
            if c.kwargs.get("action") == "rubric_rejected"
        ]
        assert len(rejection_calls) == 1
        details = rejection_calls[0].kwargs["details"]
        assert details["scoring_rubric"] == "tech"

    def test_life_domain_rubric_dequeue_proceeds(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        """A life_domain item dequeues normally — guard is rubric-specific."""
        config = Config()
        config.max_approve_per_cycle = 1
        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
        )

        mock_spec_path = Mock()
        mock_spec_path.read_text.return_value = "spec"
        mock_spec_path.resolve.return_value = mock_spec_path
        mock_spec_generator.generate_spec = Mock(return_value=mock_spec_path)

        life_item = Mock(
            id=702, source="ideaforge", source_id=702, title="Life Domain Idea",
            description="A self-care reflection journal that lives on your phone",
            idea_data=json.dumps({
                "id": 702, "title": "Life Domain Idea",
                "description": "A self-care reflection journal that lives on your phone",
                "problem_statement": "Problem statement here", "target_audience": "Adults",
                "artifact_type": "agent",
                "scoring_rubric": "life_domain",
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[life_item, None])

        with patch.object(orch, 'queue_build') as mock_queue, \
             patch.object(orch, 'start_queue_background'):
            mock_queue.return_value = BuildJob(
                idea_id=702, title="Life Domain Idea", spec_path="",
                queue_job_id="metroplex-ideaforge-702", status="queued",
                queued_at=datetime.now(),
                scoring_rubric="life_domain",
            )
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        # No rubric_rejected audit entry for life_domain.
        rejection_calls = [
            c for c in mock_audit_logger.log_decision.call_args_list
            if c.kwargs.get("action") == "rubric_rejected"
        ]
        assert rejection_calls == [], (
            "life_domain items must NOT trigger the rubric_rejected guard"
        )
        # And the item dispatched normally.
        assert len(jobs) == 1
        mock_queue.assert_called_once()

    def test_null_rubric_in_snapshot_triggers_refresh_then_rejects_if_still_non_life_domain(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        """When the priority_queue snapshot has scoring_rubric=None AND the
        upstream refresh fills it in as 'tech', the dequeue guard still
        rejects (defense in depth even after the refresh).
        """
        config = Config()
        config.max_approve_per_cycle = 1

        ideaforge_reader = Mock()
        ideaforge_reader.get_idea_by_id = Mock(return_value={
            "id": 703, "title": "Stale Tech Refresh",
            "description": "tech path", "problem_statement": "P",
            "target_audience": "T", "artifact_type": "tool",
            "scoring_rubric": "tech",
        })

        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
            ideaforge_reader=ideaforge_reader,
        )

        item = Mock(
            id=703, source="ideaforge", source_id=703,
            title="Stale Tech", description="Stale tech in queue",
            idea_data=json.dumps({
                "id": 703, "title": "Stale Tech",
                "description": "Stale tech in queue",
                "problem_statement": "P", "target_audience": "T",
                # Note: scoring_rubric absent (pre-rubric snapshot).
                "artifact_type": None,  # forces refresh
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[item, None])

        with patch.object(orch, 'start_queue_background'):
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        # Reader was queried.
        ideaforge_reader.get_idea_by_id.assert_called_with(703)
        # And the rubric_rejected audit entry fired with 'tech'.
        rejection_calls = [
            c for c in mock_audit_logger.log_decision.call_args_list
            if c.kwargs.get("action") == "rubric_rejected"
        ]
        assert len(rejection_calls) == 1
        assert rejection_calls[0].kwargs["details"]["scoring_rubric"] == "tech"
        assert jobs == []

    def test_null_rubric_dequeue_is_rejected_fail_closed(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        """Codex Round 2 Medium 3: fail-closed.

        When the priority_queue snapshot has NO scoring_rubric AND no
        ideaforge_reader is wired (or the refresh fails), the dequeue guard
        MUST reject the item rather than dispatch it with rubric=None.
        This closes the refresh-failure gap: a NULL rubric on an ideaforge
        item is treated as 'unknown, do not dispatch'.
        """
        config = Config()
        config.max_approve_per_cycle = 1
        # No ideaforge_reader wired -> refresh path is skipped.
        orch = BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
            ideaforge_reader=None,
        )

        item = Mock(
            id=704, source="ideaforge", source_id=704,
            title="Stale Null Rubric", description="A self-care reflection journal",
            idea_data=json.dumps({
                "id": 704, "title": "Stale Null Rubric",
                "description": "A self-care reflection journal",
                "problem_statement": "P", "target_audience": "T",
                "artifact_type": "agent",
                # No scoring_rubric at all.
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[item, None])

        with patch.object(orch, 'start_queue_background'):
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        rejection_calls = [
            c for c in mock_audit_logger.log_decision.call_args_list
            if c.kwargs.get("action") == "rubric_rejected"
        ]
        assert len(rejection_calls) == 1
        assert rejection_calls[0].kwargs["details"]["scoring_rubric"] is None
        assert jobs == []
        mock_state_db.update_item_status.assert_called_with(
            704, "failed", "completed_at",
        )


class TestBuildDequeueSourceRubricFilter:
    """R-A item 1 / Codex Round 1 HIGH: non-ideaforge sources (linear,
    academy, skylynx) do not carry scoring_rubric. Since SpecGenerator now
    hard-fails non-life_domain, those sources MUST be filtered at the queue
    so the failure is audited and the build slot is not wasted on an LLM
    call that would have raised ValueError downstream.
    """

    @pytest.fixture
    def mock_state_db(self):
        db = Mock(spec=StateDB)
        db.record_build_job = Mock()
        db.release_claim = Mock()
        db.update_item_status = Mock()
        db.has_completed_build = Mock(return_value=False)
        db.has_exhausted_retries = Mock(return_value=False)
        db.count_failed_builds = Mock(return_value=0)
        return db

    @pytest.fixture
    def mock_audit_logger(self):
        logger = Mock(spec=AuditLogger)
        logger.log_decision = Mock()
        logger.log_error = Mock()
        return logger

    @pytest.fixture
    def mock_spec_generator(self):
        generator = Mock(spec=SpecGenerator)
        return generator

    def _make_orch(self, mock_state_db, mock_spec_generator, mock_audit_logger):
        config = Config()
        config.max_approve_per_cycle = 1
        return BuildOrchestrator(
            config=config,
            state_db=mock_state_db,
            spec_generator=mock_spec_generator,
            audit_logger=mock_audit_logger,
        )

    @pytest.mark.parametrize("source", ["linear", "academy"])
    def test_non_ideaforge_source_dequeue_is_rejected(
        self, mock_state_db, mock_spec_generator, mock_audit_logger, source,
    ):
        """linear/academy items dequeue → source_rubric_rejected, no spec gen."""
        orch = self._make_orch(mock_state_db, mock_spec_generator, mock_audit_logger)

        item = Mock(
            id=801, source=source, source_id=801, title=f"{source} item",
            description="A description long enough to pass the gate",
            idea_data=json.dumps({
                "id": 801, "title": f"{source} item",
                "description": "A description long enough to pass the gate",
                "problem_statement": "P", "target_audience": "T",
                "artifact_type": "tool",
                # No scoring_rubric — these sources don't carry one today.
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[item, None])

        with patch.object(orch, 'start_queue_background'):
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        assert jobs == []
        mock_spec_generator.generate_spec.assert_not_called()
        mock_state_db.record_build_job.assert_not_called()
        # Audit log captured the rejection with the source value.
        rejection_calls = [
            c for c in mock_audit_logger.log_decision.call_args_list
            if c.kwargs.get("action") == "source_rubric_rejected"
        ]
        assert len(rejection_calls) == 1
        details = rejection_calls[0].kwargs["details"]
        assert details["source"] == source
        # Item is finalized to prevent re-dispatch next cycle.
        mock_state_db.update_item_status.assert_called_with(
            801, "failed", "completed_at",
        )

    def test_ideaforge_life_domain_still_dispatches_after_source_filter(
        self, mock_state_db, mock_spec_generator, mock_audit_logger,
    ):
        """Source filter is non-ideaforge specific; ideaforge life_domain still flows."""
        orch = self._make_orch(mock_state_db, mock_spec_generator, mock_audit_logger)

        mock_spec_path = Mock()
        mock_spec_path.read_text.return_value = "spec"
        mock_spec_path.resolve.return_value = mock_spec_path
        mock_spec_generator.generate_spec = Mock(return_value=mock_spec_path)

        item = Mock(
            id=901, source="ideaforge", source_id=901, title="Life Domain Idea",
            description="A self-care reflection journal that lives on your phone",
            idea_data=json.dumps({
                "id": 901, "title": "Life Domain Idea",
                "description": "A self-care reflection journal that lives on your phone",
                "problem_statement": "Problem statement here",
                "target_audience": "Adults",
                "artifact_type": "agent",
                "scoring_rubric": "life_domain",
            }),
        )
        mock_state_db.claim_next_pending = Mock(side_effect=[item, None])

        with patch.object(orch, 'queue_build') as mock_queue, \
             patch.object(orch, 'start_queue_background'):
            mock_queue.return_value = BuildJob(
                idea_id=901, title="Life Domain Idea", spec_path="",
                queue_job_id="metroplex-ideaforge-901", status="queued",
                queued_at=datetime.now(),
                scoring_rubric="life_domain",
            )
            jobs = orch.run_from_queue(mock_state_db, dry_run=False)

        # No source_rubric_rejected entry for ideaforge.
        source_rejection_calls = [
            c for c in mock_audit_logger.log_decision.call_args_list
            if c.kwargs.get("action") == "source_rubric_rejected"
        ]
        assert source_rejection_calls == [], (
            "ideaforge items must NOT trigger source_rubric_rejected guard"
        )
        assert len(jobs) == 1


