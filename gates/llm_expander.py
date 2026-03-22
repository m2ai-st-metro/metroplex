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

Given the following idea data, produce a detailed, buildable app specification in Markdown format.
The spec must be specific to THIS idea -- no generic boilerplate. Every feature, architecture decision,
and test step must directly relate to the idea's purpose.

## Idea Data

- **Title**: {title}
- **Description**: {description}
- **Problem Statement**: {problem_statement}
- **Target Audience**: {target_audience}
- **Artifact Type**: {artifact_type}
- **Scores**: opportunity={opportunity_score}, problem={problem_score}, feasibility={feasibility_score}

## Output Format

Produce a Markdown document with EXACTLY these sections (the autonomous build agent depends on this structure):

# <Title> - App Specification

## Overview
<2-3 paragraphs: what this builds, why it matters, who it serves>

**Problem Statement**: <expanded from idea data>
**Target Audience**: <expanded from idea data>

## Tech Stack
<Bullet list of specific technologies. Choose based on artifact_type:
  - tool: Python CLI with argparse/click, pytest
  - agent: Python + anthropic SDK, tool-use pattern, pytest
  - product: React 19 + Vite + Tailwind frontend, FastAPI backend, SQLite>

## Environment Setup

### Prerequisites
<What needs to be installed>

### Configuration
<Environment variables table with Variable | Required | Description columns>

## Architecture
<ASCII diagram showing the main components and data flow, specific to this idea>

## Core Features
<3-5 features, each with:>

### Feature N: <Specific Name>
**Description**: <What this feature does for THIS idea>

**Requirements**:
- <Specific, testable requirement>
- <Another requirement>

**Test Steps**:
1. <Concrete test action> -> <expected result>
2. <Another test>

## Data Models
<Pydantic models or dataclasses specific to this idea's domain>

## File Structure
<Project tree showing directories and key files>

## Success Criteria
<Numbered list of specific, verifiable outcomes>

## Constraints & Notes
<Any special considerations for the build agent>

IMPORTANT RULES:
- Every feature must be SPECIFIC to this idea. Do not produce generic features like "Error Handling" or "Logging".
- Test steps must be concrete and verifiable by running commands.
- Architecture diagram must reflect the actual components of THIS application.
- Data models must use real field names relevant to the idea's domain.
- The init.sh script must set up the environment and start the application.
- Keep the total length between 800-1500 lines of markdown.
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
