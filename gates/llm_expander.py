"""
LLM Spec Expander - Gate 2 Enhancement
Calls Claude (via DeepInfra) to expand thin IdeaForge idea data into rich, idea-specific app specs.
Falls back to Jinja2 template rendering on failure.
Injects past build failure patterns as constraints to improve spec quality over time.
"""
import os
import logging
from pathlib import Path
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# Chain-of-thought leakage markers that indicate the LLM included its reasoning
COT_MARKERS = [
    "let's consider",
    "however, note",
    "alternatively",
    "given the constraints",
    "this is getting messy",
    "too long",
    "we'll need to",
    "count the lines",
    "let me think",
    "on second thought",
    "wait,",
    "hmm,",
]

MIN_SECTION_HEADERS = 3
MIN_SPEC_LINES = 50
MAX_SPEC_LINES = 450

# Template instruction fragments that should never appear in a generated spec.
# Their presence means the LLM parroted the prompt template instead of following it.
PARROT_MARKERS = [
    "Environment variables table — should be 0-2 variables",
    "Simple ASCII diagram. Should fit in 10 lines",
    "Short bullet list. For tools:",
    "1-2 paragraphs: what this builds",
    "EXACTLY 2-3 features",
    "1-2 Pydantic models or dataclasses",
    "Flat structure preferred. 8-12 files max.",
    "Flat structure preferred. 8-12 files max including tests",
]


def validate_spec(spec_text: str) -> tuple[bool, str]:
    """Validate an LLM-generated spec for quality issues.

    Checks for:
    - Chain-of-thought leakage (3+ CoT markers)
    - Minimum section structure (## headers)
    - Length bounds (too short = degenerate, too long = over-scoped)
    - Duplicate content (spec repeated in output)
    - Template parroting (LLM copied prompt instructions verbatim)

    Args:
        spec_text: The generated spec markdown text.

    Returns:
        Tuple of (is_valid, reason). If invalid, reason describes the failure.
    """
    spec_lower = spec_text.lower()

    # Check for chain-of-thought leakage
    cot_hits = [m for m in COT_MARKERS if m in spec_lower]
    if len(cot_hits) >= 3:
        return False, f"CoT leakage detected ({len(cot_hits)} markers: {cot_hits[:5]})"

    # Check for minimum section structure (## headers)
    header_count = spec_text.count("\n## ")
    # Also count if spec starts with ## (no preceding newline)
    if spec_text.startswith("## "):
        header_count += 1
    if header_count < MIN_SECTION_HEADERS:
        return False, f"Insufficient structure: {header_count} section headers (need >= {MIN_SECTION_HEADERS})"

    # Fix A: Length bounds
    line_count = spec_text.count("\n") + 1
    if line_count < MIN_SPEC_LINES:
        return False, f"Degenerate spec: {line_count} lines (need >= {MIN_SPEC_LINES})"
    if line_count > MAX_SPEC_LINES:
        return False, f"Over-scoped spec: {line_count} lines (max {MAX_SPEC_LINES})"

    # Fix B: Duplicate content detection
    overview_count = spec_text.count("## Overview")
    if overview_count > 1:
        return False, f"Duplicate content: '## Overview' appears {overview_count} times"

    # Fix C: Template parrot detection
    parrot_hits = [m for m in PARROT_MARKERS if m in spec_text]
    if parrot_hits:
        return False, f"Template parroting: LLM copied {len(parrot_hits)} instruction fragments"

    # Fix D: Test file presence in File Structure
    # Reject specs where File Structure section exists but has no test references
    import re
    file_structure_match = re.search(
        r"## File Structure.*?```(.*?)```", spec_text, re.DOTALL
    )
    if file_structure_match:
        structure_content = file_structure_match.group(1).lower()
        if "test_" not in structure_content and "tests/" not in structure_content:
            return False, "File Structure missing test files (tests/ directory or test_*.py required)"

    return True, ""

# Prompt that produces YCE-compatible app_spec.txt content
SPEC_EXPANSION_PROMPT = """\
CRITICAL: Output ONLY the final Markdown specification document. No preamble, no reasoning, no alternatives considered, no internal debate. Do not include phrases like "let's consider", "however, note", "alternatively", or any chain-of-thought. Start directly with the markdown heading.

You are a senior software architect writing a build specification for an autonomous coding agent.

The agent gets EXACTLY 5 iterations to produce a working project. Scope accordingly.

Given the following idea data, produce a focused, minimal-scope app specification in Markdown format.
The spec must be specific to THIS idea -- no generic boilerplate.

If problem_statement or target_audience is empty, blank, or "General", INVENT a concrete one from the description. Never leave them blank or generic.

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
2. **Each feature must be implementable in ~50-100 lines**. If a feature requires scoring algorithms, ML inference, embedding models, real-time polling, Canvas/chart rendering, or async orchestration -- it is TOO COMPLEX. Replace with a simpler alternative using basic data structures and string operations.
3. **Maximum 8-12 source files** (excluding tests). If your design needs more, simplify.
4. **ZERO external API dependencies**. No calls to OpenAI, Anthropic, Google, Slack, GitHub API, etc.
   The tool must work entirely offline with local data, stdin/stdout, or local files.
5. **ZERO paid services**. No cloud providers, no SaaS integrations, no API keys required.
6. **Single data store only**: SQLite OR JSON files OR in-memory. Never multiple storage backends.
7. **No multi-process architectures**. No separate servers, workers, message queues, or microservices.
   Everything runs in a single process.
8. **No web frontends** for tool/agent types. CLI only. Products may have a simple single-page UI.
9. **Standard library + 3-5 pip packages max**. Every dependency is a risk.
10. **MANDATORY: Include pytest test files**. The File Structure MUST include a `tests/` directory with at least 2 test files (e.g., `tests/test_core.py`, `tests/test_cli.py`). Every core feature must have corresponding test functions. Builds WITHOUT test files are automatically rejected by the review gate.

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
<Flat structure preferred. 8-12 files max. MUST include a tests/ directory with test_*.py files for each module. Example: tests/test_core.py, tests/test_cli.py>

## Test Plan
<For each core feature, list the pytest test file and function names. Example:
- tests/test_core.py::test_process_valid_input
- tests/test_core.py::test_process_empty_input_raises
- tests/test_cli.py::test_help_flag
Each test must be a proper pytest function with assertions. Minimum 5 test functions total.>

## Success Criteria
<3-5 verifiable outcomes that can be checked by running commands>

## Constraints & Notes
- No external API calls — all processing is local
- Target: working MVP in 5 build iterations
- Prioritize "works correctly" over "feature complete"

IMPORTANT RULES:
- Every feature must be SPECIFIC to this idea. No generic features like "Error Handling" or "Logging".
- Every test step MUST be a concrete CLI command with EXACT expected stdout output. Do NOT write vague test steps like "verify the output is correct".
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


FAILURE_PATTERN_DESCRIPTIONS = {
    "spec_unclear": "Specs were too vague or ambiguous for the builder to implement correctly",
    "dependency_error": "Projects failed at install time due to missing or incompatible dependencies",
    "timeout": "Builds exceeded the 90-minute time limit, usually because scope was too large",
    "test_failure": "Code compiled but tests failed, often due to underspecified test expectations",
    "build_error": "Syntax or type errors in generated code",
    "review_failed": "Automated quality checks rejected the build output",
    "quality_rejected": "QA reviewer rejected the build for low quality",
    "low_quality": "Build scored below minimum quality threshold",
    "environment_error": "Build failed due to filesystem or OS-level issues",
}


# Default constraint mapping -- EGO may override this at runtime via active variant file
_DEFAULT_CONSTRAINTS = {
    "spec_unclear": "Every feature MUST have concrete CLI commands with exact expected output.",
    "dependency_error": "Limit to 3-5 well-known pip packages. Specify exact versions.",
    "timeout": "Reduce scope. Max 2-3 features. Each feature < 100 lines of code.",
    "test_failure": "Test steps must be unambiguous with literal expected output strings.",
    "build_error": "Keep code patterns simple. Avoid complex generics or metaclasses.",
    "review_failed": (
        "Builds MUST include a tests/ directory with pytest test files. "
        "The review gate rejects projects with 3+ source files and zero test files. "
        "Include at least 2 test_*.py files with real assertions."
    ),
    "quality_rejected": (
        "Ensure the project includes README.md, proper test coverage, and no secrets or large files. "
        "Quality reviewer checks spec alignment, completeness, and code quality signals."
    ),
}


def _load_constraint_mapping() -> dict[str, str]:
    """Load the active constraint mapping -- EGO variant if available, else defaults."""
    try:
        from learning.applier import get_active_variant
        active = get_active_variant()
        if active:
            return active
    except ImportError:
        pass
    return _DEFAULT_CONSTRAINTS


def format_failure_feedback(failure_patterns: list[dict]) -> str:
    """Format postmortem failure patterns into a prompt section for spec generation.

    Args:
        failure_patterns: Output of postmortem.get_failure_patterns() -- list of dicts
            with keys: category, stage, count, sample_signatures.

    Returns:
        Markdown section to append to the spec expansion prompt, or empty string if
        no patterns are worth reporting.
    """
    if not failure_patterns:
        return ""

    constraints = _load_constraint_mapping()

    lines = [
        "\n## LESSONS FROM PAST BUILD FAILURES (CRITICAL)\n",
        "Previous builds in this pipeline have failed with these patterns. "
        "Write specs that AVOID triggering these failure modes:\n",
    ]

    for p in failure_patterns:
        category = p["category"]
        count = p["count"]
        stage = p.get("stage", "unknown")
        desc = FAILURE_PATTERN_DESCRIPTIONS.get(category, category)

        lines.append(f"- **{category}** ({count} occurrences, stage: {stage}): {desc}")

        constraint = constraints.get(category)
        if constraint:
            lines.append(f"  -> {constraint}")

    return "\n".join(lines) + "\n"


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

    def expand(self, idea: dict, failure_patterns: list[dict] | None = None) -> str:
        """
        Expand an idea dict into a full app specification using Claude.

        Args:
            idea: Dictionary with fields: title, description, problem_statement,
                  target_audience, artifact_type, and optionally score fields.
            failure_patterns: Optional list of failure pattern dicts from
                  postmortem.get_failure_patterns(). When provided, injects
                  past failure lessons as constraints in the prompt.

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

        # Inject failure feedback from past builds
        if failure_patterns:
            feedback_section = format_failure_feedback(failure_patterns)
            if feedback_section:
                prompt = prompt + feedback_section

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
