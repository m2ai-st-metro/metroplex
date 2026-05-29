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
    Length lands ~2300 chars — comfortably above the MIN_AGENT_SPEC_CHARS
    (2000) floor introduced by the 2026-05-12 char-count refactor.
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
        "of the cognitive load described in the Scene below. The agent",
        "absorbs the first 90 seconds of triage that usually eats the",
        "headspace the user needs for everything else that day.",
        "",
        "**The struggling user.** Alex, 35, deals with the problem on a daily basis.",
        "We anchor every test on a moment from their day. Mornings start",
        "with overlapping priorities and a sense of which thing is on fire.",
        "",
        "## Agent shape",
        "",
        "Four file types in the produced project directory. Each is named",
        "explicitly below so the Builder LLM can map it one-to-one to a file.",
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
        "Frontmatter: name, description, trigger. Body 4-8 paragraphs that",
        "describe the decision logic for the one Scene this agent owns.",
        "",
        "### tests/test_e2e_scenes.py",
        "",
        "At least three E2E tests, each describing a Scene from the user's day.",
        "Tests assert on both the Scene input and the agent response shape.",
        "",
        "- test_e2e_morning_scene",
        "- test_e2e_midday_scene",
        "- test_e2e_evening_scene",
        "",
        "### README.md",
        "",
        "Scene-opening story. Four paragraphs. Meets the user before the agent.",
        "Paragraph one: Alex in the moment. Paragraph two: the agent acting.",
        "Paragraph three: invocation example. Paragraph four: deploy note.",
        "",
        "## Constraints",
        "",
        "- No external services.",
        "- No API keys hardcoded; telegram_bot_token_env may be stubbed at T1.",
        "- Skills bundled in agent directory, not loaded from global registry.",
        "- No web frontend at T1.",
        "- Single-purpose. One agent, one Scene.",
        "",
        "## Success criteria",
        "",
        "1. agent.yaml validates as YAML with all four required fields.",
        "2. skills/main_skill/SKILL.md exists with proper frontmatter.",
        "3. All three test_e2e tests pass against a mocked LLM.",
        "4. README opens with a Scene paragraph, not a feature list.",
        "5. Response time on a 60-second input is under five seconds.",
        "",
        "## Out of scope (T1)",
        "",
        "- Multi-user support.",
        "- Cross-session memory.",
        "- Web frontend.",
        "- Voice synthesis.",
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
            adapter=Mock(),
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
            adapter=Mock(),
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
            adapter=Mock(),
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
            adapter=Mock(),
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
            adapter=Mock(),
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


