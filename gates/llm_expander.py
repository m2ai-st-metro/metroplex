"""
LLM Spec Expander - Gate 2 Enhancement
Calls Claude to expand thin IdeaForge idea data into rich, idea-specific app specs.
Falls back to Jinja2 template rendering on failure.
"""
import os
import logging
from pathlib import Path
from typing import Optional

import anthropic

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
    """Expands thin idea data into rich app specs using Claude."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        api_key: Optional[str] = None,
    ):
        """
        Initialize LLM Spec Expander.

        Args:
            model: Claude model to use for expansion
            max_tokens: Maximum tokens in the response
            api_key: Anthropic API key (falls back to ANTHROPIC_API_KEY env var)
        """
        self.model = model
        self.max_tokens = max_tokens

        # Resolve API key
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not set. Provide api_key or set the environment variable."
            )

        self.client = anthropic.Anthropic(api_key=resolved_key)

    def expand(self, idea: dict) -> str:
        """
        Expand an idea dict into a full app specification using Claude.

        Args:
            idea: Dictionary with fields: title, description, problem_statement,
                  target_audience, artifact_type, and optionally score fields.

        Returns:
            Markdown string containing the full app specification.

        Raises:
            anthropic.APIError: On API failure (caller should handle fallback)
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

        message = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from response
        spec_text = ""
        for block in message.content:
            if block.type == "text":
                spec_text += block.text

        logger.info(
            "Spec expansion complete: %d chars, %d input tokens, %d output tokens",
            len(spec_text),
            message.usage.input_tokens,
            message.usage.output_tokens,
        )

        return spec_text
