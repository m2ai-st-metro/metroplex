"""
Tyrest Gate — Independent QA validation using GPT.

Named after Chief Justice Tyrest from IDW Transformers.
Uses a different model (GPT) than the builder (Claude) to enforce
true model independence in quality validation.

Two modes:
1. Pre-build spec review (Gate 2.5): Buildability assessment + risk flags
2. Post-build project review (Gate 4.25): Source quality + completeness
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from openai import APIError, APITimeoutError, OpenAI, RateLimitError

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_MODEL = "gpt-4o"
DEFAULT_APPROVE_MIN_CONFIDENCE = 0.75
DEFAULT_REJECT_MIN_CONFIDENCE = 0.75
DEFAULT_FORCE_REVIEW_CONFIDENCE = 0.50

# Source file extensions for project scanning
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".rb", ".php", ".cs", ".cpp", ".c", ".swift", ".kt",
}

# Directories to skip when scanning projects
IGNORED_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", ".next", ".git",
    ".tox", ".mypy_cache", ".pytest_cache",
    "target", "vendor",
}

# --- System Prompts ---

SPEC_REVIEW_SYSTEM_PROMPT = (
    "You are Tyrest, an independent pre-build auditor for software projects. "
    "You were NOT involved in creating this specification. You have no "
    "relationship with the specification agent and no shared context. "
    "Your role is to assess whether this specification is BUILDABLE by an "
    "autonomous AI coding agent.\n\n"
    "You will receive an app specification that describes a project to build.\n\n"
    "You must evaluate:\n"
    "- **Buildability**: Can an AI coding agent realistically build this from the spec? "
    "Are requirements clear and actionable? Is the scope achievable in 5 iterations?\n"
    "- **Scope Realism**: Is the scope appropriate for automated building? "
    "Too many integrations, external APIs, or complex state management are red flags.\n"
    "- **Spec Clarity**: Are the requirements unambiguous? Does the spec define "
    "what to build, not just what the product should do?\n\n"
    "Scoring: 0.0 = unbuildable, 1.0 = trivially buildable.\n\n"
    "Verdicts:\n"
    "- APPROVE: Spec is clear, scope is realistic, AI builder can handle this\n"
    "- REQUEST_REVIEW: Mixed signals; some parts are buildable but risks are present\n"
    "- REJECT: Spec is too vague, scope is unrealistic, or critical blockers exist\n\n"
    "Be calibrated. A CLI tool or simple web app with clear requirements should be "
    "APPROVED. A project requiring 15+ files, multiple external APIs, or complex state "
    "management should be scrutinized. Projects requiring hardware, paid APIs without "
    "keys, or real user data should be REJECTED.\n\n"
    "Respond ONLY with valid JSON matching the required schema."
)

SPEC_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["APPROVE", "REQUEST_REVIEW", "REJECT"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "reasoning": {
            "type": "string",
            "description": "2-4 sentence explanation of the verdict",
        },
        "scores": {
            "type": "object",
            "properties": {
                "buildability": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "scope_realism": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "spec_clarity": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "overall": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["buildability", "scope_realism", "spec_clarity", "overall"],
            "additionalProperties": False,
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Risk flags like 'too_many_integrations', 'requires_paid_api', 'scope_too_large'",
        },
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific suggestions to improve buildability (max 3)",
        },
    },
    "required": ["verdict", "confidence", "reasoning", "scores", "risk_flags", "suggestions"],
    "additionalProperties": False,
}

BUILD_REVIEW_SYSTEM_PROMPT = (
    "You are Tyrest, an independent QA auditor for software projects. "
    "You were NOT involved in building this software. You have no "
    "relationship with the building agent and no shared context. "
    "Your role is to objectively assess whether the build is a "
    "reasonable implementation of its specification.\n\n"
    "You will receive:\n"
    "1. The original specification (what was requested)\n"
    "2. A project summary (file listing, test presence, README)\n\n"
    "You must evaluate:\n"
    "- **Spec Alignment**: Does the project structure match what was specified?\n"
    "- **Completeness**: Are expected source files, tests, and docs present?\n"
    "- **Quality Signals**: Are there tests? Is there a README? "
    "Is the file count reasonable for the scope?\n\n"
    "Scoring: 0.0 = completely fails, 1.0 = exceeds expectations.\n\n"
    "Verdicts:\n"
    "- APPROVE: Build appears to match specification with acceptable quality\n"
    "- REQUEST_REVIEW: Uncertain or mixed signals; needs human judgment\n"
    "- REJECT: Build clearly fails to meet specification\n\n"
    "Be calibrated. A project with source files, tests, and a README that "
    "matches the spec should be APPROVED. Do not reject for minor issues. "
    "Do not approve empty or skeleton-only projects.\n\n"
    "Respond ONLY with valid JSON matching the required schema."
)

BUILD_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["APPROVE", "REQUEST_REVIEW", "REJECT"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "reasoning": {
            "type": "string",
            "description": "2-4 sentence explanation of the verdict",
        },
        "scores": {
            "type": "object",
            "properties": {
                "spec_alignment": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "completeness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "quality_signals": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "overall": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["spec_alignment", "completeness", "quality_signals", "overall"],
            "additionalProperties": False,
        },
        "flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Issue flags like 'no_tests', 'no_readme', 'skeleton_only'",
        },
    },
    "required": ["verdict", "confidence", "reasoning", "scores", "flags"],
    "additionalProperties": False,
}


# --- Result Dataclasses ---

@dataclass
class TyrestSpecResult:
    """Result of Tyrest pre-build spec review (Gate 2.5)."""
    verdict: str  # APPROVE, REQUEST_REVIEW, REJECT
    confidence: float
    reasoning: str
    buildability: float
    scope_realism: float
    spec_clarity: float
    overall: float
    risk_flags: list[str]
    suggestions: list[str]
    model_used: str

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"

    @property
    def rejected(self) -> bool:
        return self.verdict == "REJECT"


@dataclass
class TyrestBuildResult:
    """Result of Tyrest post-build review (Gate 4.25)."""
    verdict: str  # APPROVE, REQUEST_REVIEW, REJECT
    confidence: float
    reasoning: str
    spec_alignment: float
    completeness: float
    quality_signals: float
    overall: float
    flags: list[str]
    model_used: str

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"

    @property
    def rejected(self) -> bool:
        return self.verdict == "REJECT"


# --- Project Scanner ---

def scan_project(project_dir: Path) -> dict:
    """Scan a project directory and produce a summary for Tyrest review.

    Returns dict with file_count, source_files, test_files, has_readme, etc.
    """
    source_files = []
    test_files = []
    all_files = []
    has_readme = False
    has_gitignore = False

    for f in project_dir.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(project_dir)
        if IGNORED_DIRS & set(rel.parts):
            continue

        all_files.append(str(rel))

        if f.suffix in CODE_EXTENSIONS:
            source_files.append(str(rel))
            if "test" in f.name.lower() or "test" in str(rel.parent).lower():
                test_files.append(str(rel))

        if f.name.lower().startswith("readme"):
            has_readme = True
        if f.name == ".gitignore":
            has_gitignore = True

    return {
        "file_count": len(all_files),
        "source_file_count": len(source_files),
        "test_file_count": len(test_files),
        "has_readme": has_readme,
        "has_gitignore": has_gitignore,
        "source_files": source_files[:50],  # Cap listing for prompt size
        "test_files": test_files[:20],
    }


# --- Confidence Gating ---

def apply_confidence_gating(
    verdict: str,
    confidence: float,
    approve_min: float = DEFAULT_APPROVE_MIN_CONFIDENCE,
    reject_min: float = DEFAULT_REJECT_MIN_CONFIDENCE,
) -> tuple[str, bool]:
    """Apply confidence thresholds to override low-confidence verdicts.

    Returns (possibly_overridden_verdict, was_overridden).
    """
    # Very low confidence → always escalate
    if confidence < DEFAULT_FORCE_REVIEW_CONFIDENCE:
        if verdict != "REQUEST_REVIEW":
            logger.info(
                "Confidence override: %s → REQUEST_REVIEW (%.2f < %.2f)",
                verdict, confidence, DEFAULT_FORCE_REVIEW_CONFIDENCE,
            )
            return "REQUEST_REVIEW", True
        return verdict, False

    # Low-confidence APPROVE → escalate
    if verdict == "APPROVE" and confidence < approve_min:
        logger.info(
            "Confidence override: APPROVE → REQUEST_REVIEW (%.2f < %.2f)",
            confidence, approve_min,
        )
        return "REQUEST_REVIEW", True

    # Low-confidence REJECT → escalate rather than auto-reject
    if verdict == "REJECT" and confidence < reject_min:
        logger.info(
            "Confidence override: REJECT → REQUEST_REVIEW (%.2f < %.2f)",
            confidence, reject_min,
        )
        return "REQUEST_REVIEW", True

    return verdict, False


# --- Main Gate Class ---

class TyrestGate:
    """Independent QA gate using GPT for model-independent review."""

    def __init__(
        self,
        enabled: bool = True,
        model: str = DEFAULT_MODEL,
        approve_confidence: float = DEFAULT_APPROVE_MIN_CONFIDENCE,
        reject_confidence: float = DEFAULT_REJECT_MIN_CONFIDENCE,
    ):
        self.enabled = enabled
        self.model = model
        self.approve_confidence = approve_confidence
        self.reject_confidence = reject_confidence
        self._client = None

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY not set")
            self._client = OpenAI(api_key=api_key)
        return self._client

    def review_spec(self, spec_text: str, idea_title: str = "") -> TyrestSpecResult:
        """Gate 2.5: Review a generated spec for buildability before YCE dispatch.

        Args:
            spec_text: The app spec content generated by LLMSpecExpander
            idea_title: Optional title for context

        Returns:
            TyrestSpecResult with verdict and scores
        """
        if not self.enabled:
            return TyrestSpecResult(
                verdict="APPROVE", confidence=1.0,
                reasoning="Tyrest disabled — auto-approved.",
                buildability=1.0, scope_realism=1.0, spec_clarity=1.0, overall=1.0,
                risk_flags=[], suggestions=[], model_used="disabled",
            )

        title_line = f"Title: {idea_title}\n\n" if idea_title else ""
        user_prompt = (
            f"# App Specification\n{title_line}{spec_text}\n\n"
            "Evaluate whether this specification is buildable by an autonomous AI coding agent. "
            "The builder gets 5 iterations to produce a working project. "
            "Respond with the JSON schema provided."
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_completion_tokens=1024,
                messages=[
                    {"role": "system", "content": SPEC_REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "spec_review",
                        "strict": True,
                        "schema": SPEC_REVIEW_SCHEMA,
                    },
                },
            )

            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)

            verdict = data["verdict"].upper()
            confidence = data["confidence"]

            # Apply confidence gating
            verdict, _ = apply_confidence_gating(
                verdict, confidence,
                self.approve_confidence, self.reject_confidence,
            )

            result = TyrestSpecResult(
                verdict=verdict,
                confidence=confidence,
                reasoning=data["reasoning"],
                buildability=data["scores"]["buildability"],
                scope_realism=data["scores"]["scope_realism"],
                spec_clarity=data["scores"]["spec_clarity"],
                overall=data["scores"]["overall"],
                risk_flags=data.get("risk_flags", []),
                suggestions=data.get("suggestions", []),
                model_used=self.model,
            )

            logger.info(
                "Tyrest spec review: verdict=%s confidence=%.2f overall=%.2f flags=%s",
                result.verdict, result.confidence, result.overall, result.risk_flags,
            )
            return result

        except (APIError, APITimeoutError, RateLimitError, json.JSONDecodeError, KeyError) as e:
            logger.error("Tyrest spec review API error: %s", e)
            return TyrestSpecResult(
                verdict="REQUEST_REVIEW", confidence=0.0,
                reasoning=f"Tyrest API error: {type(e).__name__}: {e}",
                buildability=0.0, scope_realism=0.0, spec_clarity=0.0, overall=0.0,
                risk_flags=["api_error"],
                suggestions=["Manual review required due to API error"],
                model_used=self.model,
            )

    def review_build(
        self,
        project_dir: Path,
        spec_text: str,
        idea_title: str = "",
    ) -> TyrestBuildResult:
        """Gate 4.25: Review a completed build against its spec.

        Args:
            project_dir: Path to the built project
            spec_text: The original spec the build was based on
            idea_title: Optional title for context

        Returns:
            TyrestBuildResult with verdict and scores
        """
        if not self.enabled:
            return TyrestBuildResult(
                verdict="APPROVE", confidence=1.0,
                reasoning="Tyrest disabled — auto-approved.",
                spec_alignment=1.0, completeness=1.0,
                quality_signals=1.0, overall=1.0,
                flags=[], model_used="disabled",
            )

        # Scan the project directory
        if not project_dir.is_dir():
            return TyrestBuildResult(
                verdict="REJECT", confidence=1.0,
                reasoning=f"Project directory not found: {project_dir}",
                spec_alignment=0.0, completeness=0.0,
                quality_signals=0.0, overall=0.0,
                flags=["project_dir_missing"], model_used="hard_gate",
            )

        summary = scan_project(project_dir)

        # Hard gate: no source files at all
        if summary["source_file_count"] == 0:
            return TyrestBuildResult(
                verdict="REJECT", confidence=1.0,
                reasoning="Build produced zero source code files.",
                spec_alignment=0.0, completeness=0.0,
                quality_signals=0.0, overall=0.0,
                flags=["no_source_code"], model_used="hard_gate",
            )

        # Build project summary for GPT
        project_summary = (
            f"## Project Summary\n"
            f"Total files: {summary['file_count']}\n"
            f"Source files: {summary['source_file_count']}\n"
            f"Test files: {summary['test_file_count']}\n"
            f"Has README: {summary['has_readme']}\n"
            f"Has .gitignore: {summary['has_gitignore']}\n\n"
            f"## Source Files\n" +
            "\n".join(f"- {f}" for f in summary["source_files"]) + "\n"
        )
        if summary["test_files"]:
            project_summary += (
                f"\n## Test Files\n" +
                "\n".join(f"- {f}" for f in summary["test_files"]) + "\n"
            )

        title_line = f"Title: {idea_title}\n\n" if idea_title else ""
        user_prompt = (
            f"# Original Specification\n{title_line}{spec_text}\n\n"
            f"# Built Project\n{project_summary}\n\n"
            "Evaluate this build against its specification. "
            "Respond with the JSON schema provided."
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                max_completion_tokens=1024,
                messages=[
                    {"role": "system", "content": BUILD_REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "build_review",
                        "strict": True,
                        "schema": BUILD_REVIEW_SCHEMA,
                    },
                },
            )

            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)

            verdict = data["verdict"].upper()
            confidence = data["confidence"]

            verdict, _ = apply_confidence_gating(
                verdict, confidence,
                self.approve_confidence, self.reject_confidence,
            )

            result = TyrestBuildResult(
                verdict=verdict,
                confidence=confidence,
                reasoning=data["reasoning"],
                spec_alignment=data["scores"]["spec_alignment"],
                completeness=data["scores"]["completeness"],
                quality_signals=data["scores"]["quality_signals"],
                overall=data["scores"]["overall"],
                flags=data.get("flags", []),
                model_used=self.model,
            )

            logger.info(
                "Tyrest build review: verdict=%s confidence=%.2f overall=%.2f flags=%s",
                result.verdict, result.confidence, result.overall, result.flags,
            )
            return result

        except (APIError, APITimeoutError, RateLimitError, json.JSONDecodeError, KeyError) as e:
            logger.error("Tyrest build review API error: %s", e)
            return TyrestBuildResult(
                verdict="REQUEST_REVIEW", confidence=0.0,
                reasoning=f"Tyrest API error: {type(e).__name__}: {e}",
                spec_alignment=0.0, completeness=0.0,
                quality_signals=0.0, overall=0.0,
                flags=["api_error"], model_used=self.model,
            )
