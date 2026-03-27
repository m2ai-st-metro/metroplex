"""
LLM Spec Expander - Gate 2 Enhancement
Calls Claude (via DeepInfra) to expand thin IdeaForge idea data into rich, idea-specific app specs.
Falls back to Jinja2 template rendering on failure.
"""
import os
import logging
from pathlib import Path
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# Prompt that produces YCE-compatible app_spec.txt content
SPEC_EXPANSION_PROMPT = """\
You are a senior software architect writing a build specification for an autonomous coding agent.

The agent gets EXACTLY 5 iterations to produce a working project. Scope accordingly.

Given the following idea data, produce a focused, minimal-scope app specification in Markdown format.
The spec must be specific to THIS idea -- no generic boilerplate.

## Idea Data

- **Title**: {title}
- **Description**: {description}
- **Problem Statement**: {problem_statement}
- **Target Audience**: {target_audience}
- **Artifact Type**: {artifact_type}
- **Scores**: opportunity={opportunity_score}, problem={problem_score}, feasibility={feasibility_score}

## SCOPE CONSTRAINTS (CRITICAL — read before writing anything)

The builder is a single AI coding agent with 5 iterations. Specs that violate these constraints WILL be rejected:

1. **Maximum 3 core features**. Pick the 3 most essential. Everything else is out of scope.
2. **Maximum 8-12 source files** (excluding tests). If your design needs more, simplify.
3. **ZERO external API dependencies**. No calls to OpenAI, Anthropic, Google, Slack, GitHub API, etc.
   The tool must work entirely offline with local data, stdin/stdout, or local files.
4. **ZERO paid services**. No cloud providers, no SaaS integrations, no API keys required.
5. **Single data store only**: SQLite OR JSON files OR in-memory. Never multiple storage backends.
6. **No multi-process architectures**. No separate servers, workers, message queues, or microservices.
   Everything runs in a single process.
7. **No web frontends** for tool/agent types. CLI only. Products may have a simple single-page UI.
8. **Standard library + 3-5 pip packages max**. Every dependency is a risk.

Think of the SMALLEST thing that solves the core problem. A tool that does ONE thing well
ships and works. A tool that tries to do five things ships broken.

## Output Format

Produce a Markdown document with EXACTLY these sections:

# <Title> - App Specification

## Overview
<1-2 paragraphs: what this builds and who it serves. Be concrete, not aspirational.>

**Problem Statement**: <one sentence>
**Target Audience**: <one sentence>

## Tech Stack
<Short bullet list. For tools: Python 3.11+, click, pytest. Keep it minimal.>

## Environment Setup

### Prerequisites
<Only Python and standard tools>

### Configuration
<Environment variables table — should be 0-2 variables, all optional with defaults>

## Architecture
<Simple ASCII diagram. Should fit in 10 lines. If it needs more, the design is too complex.>

## Core Features
<EXACTLY 2-3 features. Each must be completable in ~1 iteration.>

### Feature N: <Name>
**Description**: <1-2 sentences>

**Requirements**:
- <2-3 specific, testable requirements per feature>

**Test Steps**:
1. <Concrete CLI command> -> <expected output>

## Data Models
<1-2 Pydantic models or dataclasses. Keep fields minimal.>

## File Structure
<Flat structure preferred. 8-12 files max including tests.>

## Success Criteria
<3-5 verifiable outcomes that can be checked by running commands>

## Constraints & Notes
- No external API calls — all processing is local
- Target: working MVP in 5 build iterations
- Prioritize "works correctly" over "feature complete"

IMPORTANT RULES:
- Every feature must be SPECIFIC to this idea. No generic features like "Error Handling" or "Logging".
- Test steps must be concrete CLI commands with expected output.
- If the idea sounds like it needs external APIs, find a LOCAL alternative (mock data, local analysis, file-based).
- Keep the total length between 300-600 lines of markdown. Shorter is better.
- The spec must be buildable by a coding agent with NO human help and NO API keys.
"""


SPEC_SIMPLIFICATION_PROMPT = """\
You are a senior software architect. A previous version of this spec was REJECTED by an independent \
reviewer because it was too complex for an autonomous AI coding agent to build in 5 iterations.

## Original Idea

- **Title**: {title}
- **Description**: {description}
- **Problem Statement**: {problem_statement}
- **Target Audience**: {target_audience}
- **Artifact Type**: {artifact_type}

## Reviewer Rejection

**Reasoning**: {rejection_reasoning}

**Risk Flags**: {risk_flags}

**Suggestions**: {suggestions}

## Your Task

Write a DRAMATICALLY SIMPLER spec that addresses the reviewer's concerns. Specifically:

1. **Cut scope by 50-70%**. Keep only the single most valuable feature from the original idea.
2. **Remove every external dependency** the reviewer flagged.
3. **Remove every integration** — the tool works standalone with local files only.
4. **Target 5-8 source files max** (excluding tests).
5. **Target 2 features max** — the core value proposition and one supporting feature.

The goal is a MINIMAL VIABLE TOOL that a coding agent can build in 5 iterations.
It's better to ship something tiny that works than something ambitious that fails.

Use the same output format as a standard spec (Overview, Tech Stack, Architecture, Core Features, etc.)
but keep it SHORT — 200-400 lines of markdown.
"""


class LLMSpecExpander:
    """Expands thin idea data into rich app specs using Claude via DeepInfra."""

    def __init__(
        self,
        model: str = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
        max_tokens: int = 8192,
        api_key: Optional[str] = None,
        state_db=None,
    ):
        """
        Initialize LLM Spec Expander.

        Args:
            model: Model to use for expansion (DeepInfra model ID)
            max_tokens: Maximum tokens in the response
            api_key: DeepInfra API key (falls back to DEEPINFRA_API_KEY env var)
        """
        self.model = model
        self.max_tokens = max_tokens
        self.state_db = state_db

        # Resolve API key
        resolved_key = api_key or os.environ.get("DEEPINFRA_API_KEY")
        if not resolved_key:
            raise ValueError(
                "DEEPINFRA_API_KEY not set. Provide api_key or set the environment variable."
            )

        self.client = OpenAI(
            api_key=resolved_key,
            base_url="https://api.deepinfra.com/v1/openai",
        )

    def expand(self, idea: dict) -> str:
        """
        Expand an idea dict into a full app specification using Claude.

        Args:
            idea: Dictionary with fields: title, description, problem_statement,
                  target_audience, artifact_type, and optionally score fields.

        Returns:
            Markdown string containing the full app specification.

        Raises:
            openai.APIError: On API failure (caller should handle fallback)
        """
        prompt = SPEC_EXPANSION_PROMPT.format(
            title=idea.get("title", "Untitled"),
            description=idea.get("description", "No description provided"),
            problem_statement=idea.get("problem_statement", idea.get("description", "")),
            target_audience=idea.get("target_audience", "General developers"),
            artifact_type=idea.get("artifact_type", "tool"),
            opportunity_score=idea.get("opportunity_score", "N/A"),
            problem_score=idea.get("problem_score", "N/A"),
            feasibility_score=idea.get("feasibility_score", "N/A"),
        )

        logger.info(
            "Expanding spec for '%s' (type=%s) using %s",
            idea.get("title"),
            idea.get("artifact_type"),
            self.model,
        )

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from response
        spec_text = response.choices[0].message.content or ""

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens

        logger.info(
            "Spec expansion complete: %d chars, %d input tokens, %d output tokens",
            len(spec_text),
            input_tokens,
            output_tokens,
        )

        # Record cost if state_db is available
        if self.state_db is not None:
            try:
                from cost_rates import estimate_cost
                cost = estimate_cost(self.model, input_tokens, output_tokens)
                self.state_db.record_cost(
                    source="spec_expander",
                    model=self.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=cost,
                )
            except Exception as e:
                logger.warning("Failed to record spec expansion cost: %s", e)

        return spec_text

    def expand_simplified(self, idea: dict, rejection_reasoning: str, risk_flags: list[str], suggestions: list[str]) -> str:
        """
        Generate a simplified spec using Tyrest rejection feedback.

        Called when Tyrest rejects the initial spec. Feeds the rejection
        reasons back into the LLM to produce a dramatically simpler version.

        Args:
            idea: Original idea dictionary
            rejection_reasoning: Tyrest's rejection explanation
            risk_flags: List of risk flags from Tyrest
            suggestions: List of improvement suggestions from Tyrest

        Returns:
            Markdown string containing the simplified app specification.
        """
        prompt = SPEC_SIMPLIFICATION_PROMPT.format(
            title=idea.get("title", "Untitled"),
            description=idea.get("description", "No description provided"),
            problem_statement=idea.get("problem_statement", idea.get("description", "")),
            target_audience=idea.get("target_audience", "General developers"),
            artifact_type=idea.get("artifact_type", "tool"),
            rejection_reasoning=rejection_reasoning,
            risk_flags=", ".join(risk_flags) if risk_flags else "None specified",
            suggestions=", ".join(suggestions) if suggestions else "None specified",
        )

        logger.info(
            "Generating simplified spec for '%s' after Tyrest rejection",
            idea.get("title"),
        )

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        spec_text = response.choices[0].message.content or ""

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens

        logger.info(
            "Simplified spec complete: %d chars, %d input tokens, %d output tokens",
            len(spec_text),
            input_tokens,
            output_tokens,
        )

        if self.state_db is not None:
            try:
                from cost_rates import estimate_cost
                cost = estimate_cost(self.model, input_tokens, output_tokens)
                self.state_db.record_cost(
                    source="spec_simplifier",
                    model=self.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=cost,
                )
            except Exception as e:
                logger.warning("Failed to record spec simplification cost: %s", e)

        return spec_text
