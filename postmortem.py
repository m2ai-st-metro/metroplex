"""Structured failure capture for Metroplex builds.

Classifies build failures into categories and stores post-mortems
for pattern analysis. This is the data layer for B2 (failure pattern learning).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Max bytes to read from a log file
MAX_LOG_BYTES = 50 * 1024  # 50KB

# Failure classification patterns (checked in order, first match wins)
FAILURE_PATTERNS: list[tuple[str, list[str]]] = [
    ("dependency_error", [
        r"ModuleNotFoundError",
        r"ImportError",
        r"No module named",
        r"Could not find a version that satisfies",
        r"pip install.*failed",
    ]),
    ("test_failure", [
        r"FAILED tests/",
        r"FAILED\s+tests/",
        r"AssertionError",
        r"AssertionError",  # common misspelling in logs
        r"AssertError",
        r"pytest.*failed",
        r"test.*FAILED",
    ]),
    ("timeout", [
        r"TimeoutError",
        r"timed out",
        r"timeout",
        r"SIGKILL",
        r"watchdog.*kill",
    ]),
    ("build_error", [
        r"SyntaxError",
        r"IndentationError",
        r"NameError",
        r"TypeError.*argument",
        r"compile.*error",
    ]),
    ("environment_error", [
        r"FileNotFoundError",
        r"PermissionError",
        r"IsADirectoryError",
        r"NotADirectoryError",
        r"OSError",
        r"disk.*full",
    ]),
]


def classify_failure(log_text: str) -> tuple[str, str, str]:
    """Classify a build failure from log text.

    Returns:
        (failure_category, failure_stage, error_signature)

    failure_category: one of 'build_error', 'test_failure', 'timeout',
        'dependency_error', 'environment_error', 'spec_unclear'
    failure_stage: rough stage where failure occurred (e.g. 'install', 'test', 'build')
    error_signature: first 500 chars of the most relevant traceback
    """
    if not log_text:
        return ("spec_unclear", "unknown", "")

    # Try each pattern category
    for category, patterns in FAILURE_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, log_text, re.IGNORECASE)
            if match:
                # Extract error signature: grab context around the match
                start = max(0, match.start() - 200)
                end = min(len(log_text), match.end() + 300)
                signature = log_text[start:end].strip()[:500]

                # Determine stage from category
                stage = _infer_stage(category, log_text)
                return (category, stage, signature)

    # No pattern matched
    # Try to extract any traceback as signature
    tb_match = re.search(r"Traceback \(most recent call last\).*?(?:\n\S|\Z)", log_text, re.DOTALL)
    signature = tb_match.group(0)[:500] if tb_match else log_text[-500:].strip()

    return ("spec_unclear", "unknown", signature)


def _infer_stage(category: str, log_text: str) -> str:
    """Infer the build stage from the failure category and log text."""
    if category == "dependency_error":
        return "install"
    if category == "test_failure":
        return "test"
    if category == "timeout":
        if "test" in log_text.lower():
            return "test"
        return "build"
    if category == "build_error":
        return "build"
    if category == "environment_error":
        return "setup"
    return "unknown"


def _classify_from_gate_status(
    review_status: str | None,
    quality_score: float | None,
) -> tuple[str, str, str]:
    """Classify a failure from gate/review metadata when no crash log exists.

    Returns:
        (failure_category, failure_stage, error_signature)
    """
    if review_status == "tyrest_rejected":
        sig = f"Tyrest QA rejected build (quality_score={quality_score})"
        return ("quality_rejected", "review", sig)
    if review_status == "review_failed":
        sig = f"Automated review checks failed (quality_score={quality_score})"
        return ("review_failed", "review", sig)
    if quality_score is not None and quality_score < 40:
        sig = f"Quality score {quality_score} below minimum threshold"
        return ("low_quality", "scoring", sig)
    return ("spec_unclear", "unknown", "")


def _read_log_file(log_path: str | None) -> str:
    """Read a log file, capped at MAX_LOG_BYTES."""
    if not log_path:
        return ""
    try:
        p = Path(log_path)
        if not p.exists():
            return ""
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_LOG_BYTES:
            # Keep the tail (most relevant for errors)
            content = content[-MAX_LOG_BYTES:]
        return content
    except Exception as e:
        logger.warning("Failed to read log file %s: %s", log_path, e)
        return ""


def capture_postmortem(
    state_db,
    queue_job_id: str,
    idea_id: int,
    title: str,
    log_path: str | None = None,
    spec_path: str | None = None,
    idea_score: float | None = None,
    artifact_type: str | None = None,
    retry_count: int | None = None,
    review_status: str | None = None,
    quality_score: float | None = None,
) -> bool:
    """Capture a structured post-mortem for a failed build.

    Best-effort: never raises exceptions that would block the cycle.

    Args:
        state_db: StateDB instance
        queue_job_id: Unique build job ID (used for dedup)
        idea_id: IdeaForge idea ID
        title: Idea/build title
        log_path: Path to build log file (optional)
        spec_path: Path to spec file (optional)
        idea_score: IdeaForge weighted score (optional)
        artifact_type: Artifact type from classification (optional)
        retry_count: Retry attempt number (0 or None = first attempt)
        review_status: Build's review_status from build_jobs (optional)
        quality_score: Build's quality_score from build_jobs (optional)

    Returns:
        True if postmortem was captured, False if skipped or errored
    """
    try:
        state_db.connect()

        # Include retry count in dedup key so retried builds get their own postmortems
        effective_retry = retry_count or 0
        dedup_key = f"{queue_job_id}_retry{effective_retry}" if effective_retry > 0 else queue_job_id

        # Dedup check
        row = state_db.conn.execute(
            "SELECT 1 FROM build_postmortems WHERE queue_job_id = ?",
            (dedup_key,),
        ).fetchone()
        if row is not None:
            logger.debug("Postmortem already exists for %s, skipping", dedup_key)
            return False

        # Read and classify from log text
        log_text = _read_log_file(log_path)
        failure_category, failure_stage, error_signature = classify_failure(log_text)

        # If log-based classification yielded spec_unclear, try gate-based classification
        if failure_category == "spec_unclear" and (review_status or quality_score is not None):
            gate_category, gate_stage, gate_sig = _classify_from_gate_status(
                review_status, quality_score
            )
            if gate_category != "spec_unclear":
                failure_category = gate_category
                failure_stage = gate_stage
                error_signature = gate_sig

        now = datetime.now(timezone.utc).isoformat()

        state_db.conn.execute(
            """INSERT INTO build_postmortems
            (queue_job_id, idea_id, title, failure_category, failure_stage,
             error_signature, spec_path, idea_weighted_score, idea_artifact_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedup_key,
                idea_id,
                title,
                failure_category,
                failure_stage,
                error_signature,
                spec_path,
                idea_score,
                artifact_type,
                now,
            ),
        )
        state_db.conn.commit()
        logger.info(
            "Captured postmortem for %s: category=%s stage=%s",
            dedup_key, failure_category, failure_stage,
        )
        return True

    except Exception as e:
        logger.warning("Failed to capture postmortem for %s: %s", queue_job_id, e)
        return False


def get_failure_patterns(state_db, min_count: int = 3) -> list[dict]:
    """Get aggregated failure patterns from post-mortems.

    Returns categories with count >= min_count, sorted by frequency.
    This is the data B2 (failure pattern learning) will consume.
    """
    try:
        state_db.connect()
        rows = state_db.conn.execute(
            """SELECT failure_category, failure_stage, COUNT(*) as count,
                      GROUP_CONCAT(error_signature, ' ||| ') as signatures
               FROM build_postmortems
               GROUP BY failure_category, failure_stage
               HAVING count >= ?
               ORDER BY count DESC""",
            (min_count,),
        ).fetchall()

        return [
            {
                "category": row[0],
                "stage": row[1],
                "count": row[2],
                "sample_signatures": (row[3] or "").split(" ||| ")[:3],
            }
            for row in rows
        ]
    except Exception as e:
        logger.warning("Failed to get failure patterns: %s", e)
        return []


def get_postmortem_summary(state_db) -> list[dict]:
    """Get a summary of all failure categories for CLI display."""
    try:
        state_db.connect()
        rows = state_db.conn.execute(
            """SELECT failure_category, COUNT(*) as count,
                      AVG(idea_weighted_score) as avg_score
               FROM build_postmortems
               GROUP BY failure_category
               ORDER BY count DESC"""
        ).fetchall()

        return [
            {
                "category": row[0],
                "count": row[1],
                "avg_score": round(row[2], 2) if row[2] is not None else None,
            }
            for row in rows
        ]
    except Exception as e:
        logger.warning("Failed to get postmortem summary: %s", e)
        return []
