"""
Tests for LLM Spec Expander - Gates 2 Enhancement
Tests LLM expansion, fallback behavior, and integration with SpecGenerator.
"""
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import tempfile
import shutil
import os

from config import Config
from gates.build import SpecGenerator
from gates.llm_expander import LLMSpecExpander, SPEC_EXPANSION_PROMPT


@pytest.fixture
def tool_idea():
    """Sample tool-type idea with all fields."""
    return {
        "id": 42,
        "title": "Agent Supply Chain Scanner",
        "description": "CLI tool that scans agent codebases for dependency vulnerabilities and outdated packages.",
        "problem_statement": "AI agent deployments use rapidly evolving dependencies that become stale or vulnerable quickly.",
        "target_audience": "AI/ML engineers deploying autonomous agent systems",
        "artifact_type": "tool",
        "weighted_score": 7.4,
        "opportunity_score": 8.0,
        "problem_score": 7.5,
        "feasibility_score": 8.2,
    }


@pytest.fixture
def agent_idea():
    """Sample agent-type idea."""
    return {
        "id": 43,
        "title": "PR Review Agent",
        "description": "An AI agent that reviews pull requests for code quality, security issues, and style violations.",
        "problem_statement": "Code reviews are a bottleneck and inconsistent across team members.",
        "target_audience": "Development teams using GitHub for code review",
        "artifact_type": "agent",
        "weighted_score": 7.35,
        "opportunity_score": 7.0,
        "problem_score": 8.0,
        "feasibility_score": 7.5,
    }


@pytest.fixture
def template_dir():
    """Get path to spec_templates directory."""
    templates = Path(__file__).parent.parent / "spec_templates"
    assert templates.exists(), f"Template directory not found: {templates}"
    return templates


@pytest.fixture
def output_dir():
    """Create temporary output directory."""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    if temp_dir.exists():
        shutil.rmtree(temp_dir)


def _mock_openai_response(text: str) -> Mock:
    """Create a mock OpenAI chat completion response."""
    message = Mock()
    message.content = text

    choice = Mock()
    choice.message = message

    usage = Mock()
    usage.prompt_tokens = 500
    usage.completion_tokens = 2000

    response = Mock()
    response.choices = [choice]
    response.usage = usage
    return response


class TestLLMSpecExpander:
    """Tests for LLMSpecExpander class."""

    def test_init_with_api_key(self):
        """Test initialization with explicit API key."""
        with patch("gates.llm_expander.OpenAI"):
            expander = LLMSpecExpander(api_key="test-key-123")
            assert expander.model == "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
            assert expander.max_tokens == 8192

    def test_init_with_env_key(self):
        """Test initialization falls back to DEEPINFRA_API_KEY env var."""
        with patch.dict(os.environ, {"DEEPINFRA_API_KEY": "env-key-456"}):
            with patch("gates.llm_expander.OpenAI"):
                expander = LLMSpecExpander()
                assert expander.client is not None

    def test_init_no_key_raises(self):
        """Test initialization raises ValueError when no API key available."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove DEEPINFRA_API_KEY if set
            env = os.environ.copy()
            env.pop("DEEPINFRA_API_KEY", None)
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(ValueError, match="DEEPINFRA_API_KEY"):
                    LLMSpecExpander()

    def test_init_custom_model(self):
        """Test initialization with custom model."""
        with patch("gates.llm_expander.OpenAI"):
            expander = LLMSpecExpander(
                model="meta-llama/Llama-3.3-70B-Instruct",
                max_tokens=4096,
                api_key="test-key",
            )
            assert expander.model == "meta-llama/Llama-3.3-70B-Instruct"
            assert expander.max_tokens == 4096

    def test_expand_calls_api(self, tool_idea):
        """Test expand() calls OpenAI-compatible API with correct parameters."""
        mock_response = _mock_openai_response("# Agent Supply Chain Scanner\n\n## Overview\n...")

        with patch("gates.llm_expander.OpenAI") as MockClient:
            mock_client = MockClient.return_value
            mock_client.chat.completions.create.return_value = mock_response

            expander = LLMSpecExpander(api_key="test-key")
            result = expander.expand(tool_idea)

            # Verify API was called
            mock_client.chat.completions.create.assert_called_once()
            call_kwargs = mock_client.chat.completions.create.call_args[1]

            assert call_kwargs["model"] == "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
            assert call_kwargs["max_tokens"] == 8192
            assert len(call_kwargs["messages"]) == 1
            assert call_kwargs["messages"][0]["role"] == "user"

            # Verify prompt contains idea data
            prompt = call_kwargs["messages"][0]["content"]
            assert "Agent Supply Chain Scanner" in prompt
            assert "tool" in prompt
            assert "AI/ML engineers" in prompt

    def test_expand_returns_text(self, tool_idea):
        """Test expand() returns the text content from the response."""
        spec_text = "# Agent Supply Chain Scanner - App Specification\n\n## Overview\nA CLI tool..."
        mock_response = _mock_openai_response(spec_text)

        with patch("gates.llm_expander.OpenAI") as MockClient:
            mock_client = MockClient.return_value
            mock_client.chat.completions.create.return_value = mock_response

            expander = LLMSpecExpander(api_key="test-key")
            result = expander.expand(tool_idea)

            assert result == spec_text

    def test_expand_handles_missing_optional_fields(self):
        """Test expand() handles ideas with only required fields."""
        minimal_idea = {
            "id": 99,
            "title": "Minimal Tool",
            "description": "A basic tool",
            "artifact_type": "tool",
        }
        mock_response = _mock_openai_response("# Minimal Tool\n## Overview\n...")

        with patch("gates.llm_expander.OpenAI") as MockClient:
            mock_client = MockClient.return_value
            mock_client.chat.completions.create.return_value = mock_response

            expander = LLMSpecExpander(api_key="test-key")
            result = expander.expand(minimal_idea)

            # Should not raise, uses defaults for missing fields
            assert "Minimal Tool" in result or len(result) > 0

    def test_expand_propagates_api_error(self, tool_idea):
        """Test expand() propagates API errors for caller to handle."""
        with patch("gates.llm_expander.OpenAI") as MockClient:
            mock_client = MockClient.return_value
            mock_client.chat.completions.create.side_effect = Exception("API rate limit exceeded")

            expander = LLMSpecExpander(api_key="test-key")

            with pytest.raises(Exception, match="API rate limit"):
                expander.expand(tool_idea)

    def test_prompt_template_contains_required_sections(self):
        """Verify the prompt template asks for all YCE-required sections."""
        required_sections = [
            "Overview",
            "Tech Stack",
            "Environment Setup",
            "Architecture",
            "Core Features",
            "Data Models",
            "File Structure",
            "Success Criteria",
        ]
        for section in required_sections:
            assert section in SPEC_EXPANSION_PROMPT, (
                f"Prompt template missing required section: {section}"
            )


class TestSpecGeneratorLLMIntegration:
    """Tests for SpecGenerator with LLM expansion enabled."""

    def test_llm_enabled_generates_via_llm(self, template_dir, output_dir, tool_idea):
        """Test that SpecGenerator uses LLM when enabled and available."""
        config = Config()
        config.spec_use_llm = True

        llm_spec = "# Agent Supply Chain Scanner - App Specification\n\n## Overview\nLLM-generated content..."

        with patch("gates.build.LLMSpecExpander") as MockExpander:
            mock_instance = MockExpander.return_value
            mock_instance.expand.return_value = llm_spec

            generator = SpecGenerator(config, template_dir)
            path = generator.generate_spec(tool_idea, output_dir)

            assert path.exists()
            content = path.read_text()
            assert "LLM-generated content" in content
            mock_instance.expand.assert_called_once_with(tool_idea)

    def test_llm_failure_falls_back_to_jinja2(self, template_dir, output_dir, tool_idea):
        """Test that SpecGenerator falls back to Jinja2 when LLM fails."""
        config = Config()
        config.spec_use_llm = True

        with patch("gates.build.LLMSpecExpander") as MockExpander:
            mock_instance = MockExpander.return_value
            mock_instance.expand.side_effect = Exception("API error")

            generator = SpecGenerator(config, template_dir)
            path = generator.generate_spec(tool_idea, output_dir)

            # Should still produce output via Jinja2
            assert path.exists()
            content = path.read_text()
            # Jinja2 template contains these generic sections
            assert "## Tech Stack" in content
            assert "CLI Argument Parsing" in content  # Generic Jinja2 feature

    def test_llm_disabled_uses_jinja2(self, template_dir, output_dir, tool_idea):
        """Test that SpecGenerator uses Jinja2 when LLM is disabled."""
        config = Config()
        config.spec_use_llm = False

        generator = SpecGenerator(config, template_dir)
        assert generator.llm_expander is None

        path = generator.generate_spec(tool_idea, output_dir)
        assert path.exists()
        content = path.read_text()
        assert tool_idea["title"] in content
        assert "## Tech Stack" in content

    def test_llm_init_failure_falls_back_gracefully(self, template_dir, output_dir, tool_idea):
        """Test that SpecGenerator works even if LLMSpecExpander init fails."""
        config = Config()
        config.spec_use_llm = True

        with patch("gates.build.LLMSpecExpander", side_effect=ValueError("No API key")):
            generator = SpecGenerator(config, template_dir)
            assert generator.llm_expander is None

            # Should still work via Jinja2
            path = generator.generate_spec(tool_idea, output_dir)
            assert path.exists()

    def test_output_path_format_with_llm(self, template_dir, output_dir, tool_idea):
        """Test output path follows convention even with LLM expansion."""
        config = Config()
        config.spec_use_llm = True

        with patch("gates.build.LLMSpecExpander") as MockExpander:
            mock_instance = MockExpander.return_value
            mock_instance.expand.return_value = "# Spec Content"

            generator = SpecGenerator(config, template_dir)
            path = generator.generate_spec(tool_idea, output_dir)

            assert path.name == f"app_spec_{tool_idea['id']}.txt"
            assert path.parent == output_dir

    def test_different_artifact_types_in_prompt(self, template_dir, output_dir, agent_idea):
        """Test that agent-type ideas include artifact_type in LLM prompt."""
        config = Config()
        config.spec_use_llm = True

        with patch("gates.build.LLMSpecExpander") as MockExpander:
            mock_instance = MockExpander.return_value
            mock_instance.expand.return_value = "# PR Review Agent\n## Overview\n..."

            generator = SpecGenerator(config, template_dir)
            generator.generate_spec(agent_idea, output_dir)

            # Verify the idea dict was passed to expand
            call_args = mock_instance.expand.call_args[0][0]
            assert call_args["artifact_type"] == "agent"
            assert call_args["title"] == "PR Review Agent"


class TestConfigSpecSettings:
    """Tests for spec generation config settings."""

    def test_spec_use_llm_default_true(self):
        """Test spec_use_llm defaults to True when env var is not set."""
        env = os.environ.copy()
        env.pop("METROPLEX_SPEC_USE_LLM", None)
        with patch.dict(os.environ, env, clear=True):
            config = Config()
            assert config.spec_use_llm is True

    def test_spec_use_llm_env_false(self):
        """Test spec_use_llm can be disabled via env var."""
        with patch.dict(os.environ, {"METROPLEX_SPEC_USE_LLM": "false"}):
            config = Config()
            assert config.spec_use_llm is False

    def test_spec_use_llm_env_zero(self):
        """Test spec_use_llm disabled with '0'."""
        with patch.dict(os.environ, {"METROPLEX_SPEC_USE_LLM": "0"}):
            config = Config()
            assert config.spec_use_llm is False

    def test_spec_llm_model_default(self):
        """Test default model is Nemotron-3."""
        config = Config()
        assert "nemotron" in config.spec_llm_model.lower() or "nvidia" in config.spec_llm_model.lower()

    def test_spec_llm_model_env_override(self):
        """Test model can be overridden via env var."""
        with patch.dict(os.environ, {"METROPLEX_SPEC_LLM_MODEL": "claude-3-5-haiku-20241022"}):
            config = Config()
            assert config.spec_llm_model == "claude-3-5-haiku-20241022"

    def test_spec_llm_max_tokens_default(self):
        """Test default max tokens."""
        config = Config()
        assert config.spec_llm_max_tokens == 8192

    def test_spec_llm_max_tokens_env_override(self):
        """Test max tokens can be overridden via env var."""
        with patch.dict(os.environ, {"METROPLEX_SPEC_LLM_MAX_TOKENS": "4096"}):
            config = Config()
            assert config.spec_llm_max_tokens == 4096
