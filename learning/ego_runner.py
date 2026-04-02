"""EGO runner -- orchestrates the mutate-evaluate-commit learning loop.

Reads build outcome data from StateDB, generates variant constraint mappings,
evaluates them via LLM judge, and optionally applies winners.
"""

import logging
import sqlite3
from typing import Optional

from .applier import apply_variant, get_active_variant, rollback_variant
from .config import (
    AUTO_APPLY_ENABLED,
    MIN_BUILDS_FOR_EXPERIMENT,
    MIN_FAILURES_FOR_EXPERIMENT,
    ROLLBACK_THRESHOLD,
    ROLLBACK_WINDOW_BUILDS,
)
from .evaluator import Comparison, evaluate
from .ledger import (
    get_experiment_summary,
    get_latest_applied,
    init_ego_tables,
    log_experiment,
    mark_applied,
    mark_rolled_back,
)
from .mutator import generate_variant, get_current_constraint_mapping

logger = logging.getLogger(__name__)


def _get_build_stats(state_db) -> dict:
    """Extract recent build outcome statistics from Metroplex state DB."""
    state_db.connect()
    conn = state_db.conn

    total = conn.execute("SELECT COUNT(*) FROM build_jobs").fetchone()[0]
    successful = conn.execute(
        "SELECT COUNT(*) FROM build_jobs WHERE status = 'completed'"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM build_jobs WHERE status = 'failed'"
    ).fetchone()[0]

    success_rate = successful / total if total > 0 else 0.0
    return {
        "total": total,
        "successful": successful,
        "failed": failed,
        "success_rate": success_rate,
    }


def _get_failure_breakdown(state_db) -> list[dict]:
    """Get failure category counts from postmortems."""
    state_db.connect()
    rows = state_db.conn.execute(
        """
        SELECT failure_category, COUNT(*) as cnt
        FROM build_postmortems
        GROUP BY failure_category
        ORDER BY cnt DESC
        """
    ).fetchall()
    return [{"category": r[0], "count": r[1]} for r in rows]


def _get_error_samples(state_db, limit: int = 5) -> list[str]:
    """Get recent error signatures from postmortems."""
    state_db.connect()
    rows = state_db.conn.execute(
        """
        SELECT error_signature FROM build_postmortems
        WHERE error_signature IS NOT NULL AND error_signature != ''
        ORDER BY created_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def _get_recent_success_rate(state_db, window: int) -> float:
    """Get success rate of the most recent N builds."""
    state_db.connect()
    rows = state_db.conn.execute(
        """
        SELECT status FROM build_jobs
        ORDER BY queued_at DESC LIMIT ?
        """,
        (window,),
    ).fetchall()
    if not rows:
        return 0.0
    successes = sum(1 for r in rows if r[0] == "completed")
    return successes / len(rows)


def check_rollback(state_db, conn: sqlite3.Connection) -> bool:
    """Check if the currently applied variant should be rolled back.

    Compares success rate before application vs recent success rate.
    If it dropped by more than ROLLBACK_THRESHOLD, roll back.

    Returns True if a rollback was performed.
    """
    for target in ("failure_feedback",):
        applied = get_latest_applied(conn, target)
        if not applied:
            continue

        rate_before = applied.get("success_rate_before")
        if rate_before is None:
            continue

        rate_after = _get_recent_success_rate(state_db, ROLLBACK_WINDOW_BUILDS)
        drop = rate_before - rate_after

        if drop >= ROLLBACK_THRESHOLD:
            logger.warning(
                "EGO rollback triggered for %s: rate dropped %.1f%% -> %.1f%% (threshold: %.1f%%)",
                target, rate_before * 100, rate_after * 100, ROLLBACK_THRESHOLD * 100,
            )
            mark_rolled_back(
                conn,
                applied["id"],
                rate_after,
                f"Success rate dropped {drop:.1%} (before={rate_before:.1%}, after={rate_after:.1%})",
            )
            rollback_variant()
            return True

    return False


def run_ego_cycle(
    state_db,
    event_emitter=None,
    dry_run: bool = False,
) -> Optional[Comparison]:
    """Run one EGO experiment cycle.

    Args:
        state_db: Metroplex StateDB instance (already connected).
        event_emitter: Optional EventEmitter for Sky-Lynx notifications.
        dry_run: If True, evaluate but don't apply winners.

    Returns:
        Comparison result, or None if preconditions not met.
    """
    state_db.connect()
    conn = state_db.conn

    # Initialize EGO tables
    init_ego_tables(conn)

    # Check rollback first
    if check_rollback(state_db, conn):
        if event_emitter:
            event_emitter.emit("ego_rollback", {"target": "failure_feedback"})
        return None

    # Check preconditions
    build_stats = _get_build_stats(state_db)

    if build_stats["total"] < MIN_BUILDS_FOR_EXPERIMENT:
        logger.info(
            "EGO: insufficient builds (%d < %d) -- skipping",
            build_stats["total"], MIN_BUILDS_FOR_EXPERIMENT,
        )
        return None

    if build_stats["failed"] < MIN_FAILURES_FOR_EXPERIMENT:
        logger.info(
            "EGO: insufficient failures (%d < %d) -- skipping",
            build_stats["failed"], MIN_FAILURES_FOR_EXPERIMENT,
        )
        return None

    failure_breakdown = _get_failure_breakdown(state_db)
    error_samples = _get_error_samples(state_db)

    # Get current mapping (baseline)
    active = get_active_variant()
    if active:
        baseline_mapping = active
        logger.info("EGO: using active variant as baseline")
    else:
        baseline_mapping = get_current_constraint_mapping()
        logger.info("EGO: using hardcoded defaults as baseline")

    # Generate variant
    logger.info("EGO: generating variant constraint mapping...")
    variant_mapping = generate_variant(
        current_mapping=baseline_mapping,
        build_stats=build_stats,
        failure_breakdown=failure_breakdown,
        error_samples=error_samples,
    )

    if variant_mapping == baseline_mapping:
        logger.info("EGO: variant is identical to baseline -- skipping evaluation")
        return None

    # Evaluate via LLM judge
    logger.info("EGO: evaluating baseline vs variant...")
    comparison = evaluate(
        baseline_mapping=baseline_mapping,
        variant_mapping=variant_mapping,
        failure_breakdown=failure_breakdown,
        error_samples=error_samples,
    )

    # Log to ledger
    import json
    experiment_id = log_experiment(
        conn=conn,
        target="failure_feedback",
        parameter="constraint_mapping",
        baseline_value=json.dumps(baseline_mapping),
        variant_value=json.dumps(variant_mapping),
        baseline_score=comparison.baseline_score,
        variant_score=comparison.variant_score,
        improvement_pct=comparison.improvement_pct,
        is_winner=comparison.is_winner,
        reason=comparison.reason,
    )

    logger.info(
        "EGO experiment #%d: baseline=%.1f variant=%.1f improvement=%.1f%% winner=%s",
        experiment_id,
        comparison.baseline_score,
        comparison.variant_score,
        comparison.improvement_pct * 100,
        comparison.is_winner,
    )

    # Apply winner
    if comparison.is_winner and not dry_run:
        if AUTO_APPLY_ENABLED:
            success_rate_before = build_stats["success_rate"]
            applied = apply_variant(variant_mapping, experiment_id)
            if applied:
                mark_applied(conn, experiment_id, build_stats["total"], success_rate_before)
                logger.info("EGO: variant applied (experiment #%d)", experiment_id)
                if event_emitter:
                    event_emitter.emit("ego_variant_applied", {
                        "experiment_id": experiment_id,
                        "improvement_pct": comparison.improvement_pct,
                        "reason": comparison.reason,
                    })
        else:
            logger.info(
                "EGO: winner found but AUTO_APPLY_ENABLED=False -- "
                "set EGO_AUTO_APPLY=true to enable auto-application"
            )

    # Emit experiment event regardless
    if event_emitter:
        event_emitter.emit("ego_experiment", {
            "experiment_id": experiment_id,
            "baseline_score": comparison.baseline_score,
            "variant_score": comparison.variant_score,
            "improvement_pct": comparison.improvement_pct,
            "is_winner": comparison.is_winner,
            "reason": comparison.reason,
        })

    return comparison


def ego_status(state_db) -> str:
    """Return a human-readable status string for EGO."""
    state_db.connect()
    conn = state_db.conn
    init_ego_tables(conn)

    summary = get_experiment_summary(conn)
    active = get_active_variant()
    build_stats = _get_build_stats(state_db)

    lines = [
        "EGO Learning System Status",
        f"  Experiments: {summary['total']} total, {summary['winners']} winners",
        f"  Applied: {summary['applied']}, Rolled back: {summary['rolled_back']}",
        f"  Auto-apply: {'ENABLED' if AUTO_APPLY_ENABLED else 'DISABLED'}",
        f"  Active variant: {'YES (experiment #{})'.format(active.get('experiment_id', '?')) if active else 'NO (using defaults)'}",
        f"  Build stats: {build_stats['total']} total, {build_stats['success_rate']:.0%} success rate",
        f"  Min builds for experiment: {MIN_BUILDS_FOR_EXPERIMENT}",
        f"  Min failures for experiment: {MIN_FAILURES_FOR_EXPERIMENT}",
    ]
    return "\n".join(lines)
