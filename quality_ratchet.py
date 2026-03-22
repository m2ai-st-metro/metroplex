"""
Quality Ratchet - Phase 14e
Auto-tunes the minimum quality threshold for build publishing.
Ratchet constraint: thresholds only tighten (increase), never loosen.

Two activation tiers:
- Advisory (15+ scored builds): produces correlation report but does NOT auto-adjust
- Auto-tune (30+ scored builds): proposes and applies threshold changes with ratchet constraint

Stores threshold state in metroplex.db via a simple key-value table.
"""
import logging
from datetime import datetime

from db import StateDB

logger = logging.getLogger(__name__)

ADVISORY_THRESHOLD = 15
MIN_RECORDS_TO_ACTIVATE = 30
# Minimum gap between published avg and threshold to justify tightening
MIN_HEADROOM = 5.0


def get_quality_threshold(state_db: StateDB) -> float | None:
    """Read the current quality threshold from the DB.

    Returns:
        Current threshold value, or None if not yet set.
    """
    state_db.connect()
    cursor = state_db.conn.cursor()

    # Ensure the ratchet_state table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ratchet_state (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    state_db.conn.commit()

    cursor.execute("SELECT value FROM ratchet_state WHERE key = 'quality_threshold'")
    row = cursor.fetchone()
    return row["value"] if row else None


def set_quality_threshold(state_db: StateDB, threshold: float) -> None:
    """Write the quality threshold to the DB."""
    state_db.connect()
    cursor = state_db.conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ratchet_state (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        INSERT OR REPLACE INTO ratchet_state (key, value, updated_at)
        VALUES ('quality_threshold', ?, ?)
    """, (threshold, datetime.now().isoformat()))
    state_db.conn.commit()


def evaluate_ratchet(state_db: StateDB) -> dict:
    """Evaluate whether the quality threshold should be tightened.

    Returns a dict with:
        - activated (bool): whether the ratchet ran (requires 30+ records)
        - current_threshold (float | None): current threshold
        - proposed_threshold (float | None): new threshold if tightening
        - tightened (bool): whether threshold was actually changed
        - reason (str): explanation
        - stats (dict): quality score statistics
    """
    result = {
        "activated": False,
        "current_threshold": None,
        "proposed_threshold": None,
        "tightened": False,
        "reason": "",
        "stats": {},
    }

    state_db.connect()
    cursor = state_db.conn.cursor()

    # Count scored builds
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM build_jobs
        WHERE quality_score IS NOT NULL
        AND id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
    """)
    scored_count = cursor.fetchone()["cnt"]

    if scored_count < ADVISORY_THRESHOLD:
        result["reason"] = (
            f"Insufficient data: {scored_count}/{ADVISORY_THRESHOLD} scored builds for advisory mode"
        )
        result["stats"]["scored_count"] = scored_count
        return result

    advisory_only = scored_count < MIN_RECORDS_TO_ACTIVATE

    result["activated"] = True

    # Get quality scores by terminal state
    cursor.execute("""
        SELECT
            b.quality_score,
            b.status,
            b.review_status,
            p.status as pub_status
        FROM build_jobs b
        LEFT JOIN publish_jobs p ON p.build_job_id = b.queue_job_id AND p.status = 'published'
        WHERE b.quality_score IS NOT NULL
        AND b.id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
    """)
    rows = cursor.fetchall()

    published_scores = []
    failed_scores = []

    for r in rows:
        score = r["quality_score"]
        if r["review_status"] in ("review_failed", "tyrest_rejected"):
            failed_scores.append(score)
        elif r["status"] == "failed":
            failed_scores.append(score)
        elif r["pub_status"] == "published":
            published_scores.append(score)

    all_scores = [r["quality_score"] for r in rows]

    result["stats"] = {
        "scored_count": scored_count,
        "published_count": len(published_scores),
        "failed_count": len(failed_scores),
        "published_avg": round(sum(published_scores) / len(published_scores), 1) if published_scores else None,
        "failed_avg": round(sum(failed_scores) / len(failed_scores), 1) if failed_scores else None,
        "overall_avg": round(sum(all_scores) / len(all_scores), 1),
    }

    current = get_quality_threshold(state_db)
    result["current_threshold"] = current

    # Compute proposed threshold
    if not published_scores:
        result["reason"] = "No published builds to calibrate against"
        return result

    if not failed_scores:
        result["reason"] = "No failed builds to calibrate against"
        return result

    pub_avg = sum(published_scores) / len(published_scores)
    fail_avg = sum(failed_scores) / len(failed_scores)

    # Proposed = midpoint, but ensure headroom above fail_avg
    proposed = round((pub_avg + fail_avg) / 2, 1)

    # Ensure enough headroom between proposed and published avg
    if pub_avg - proposed < MIN_HEADROOM:
        proposed = round(pub_avg - MIN_HEADROOM, 1)

    result["proposed_threshold"] = proposed

    # Advisory mode: report but don't auto-adjust
    if advisory_only:
        result["reason"] = (
            f"Advisory mode ({scored_count}/{MIN_RECORDS_TO_ACTIVATE} for auto-tune): "
            f"suggested threshold {proposed} "
            f"(published avg {pub_avg:.1f}, failed avg {fail_avg:.1f})"
        )
        return result

    # Ratchet constraint: only tighten
    if current is not None and proposed <= current:
        result["reason"] = (
            f"Proposed {proposed} <= current {current} — ratchet prevents loosening"
        )
        return result

    # Apply the tightening
    if current is None:
        result["reason"] = f"Initial threshold set to {proposed}"
    else:
        result["reason"] = f"Tightened from {current} to {proposed}"

    set_quality_threshold(state_db, proposed)
    result["tightened"] = True
    result["current_threshold"] = proposed

    logger.info("Quality ratchet: %s", result["reason"])
    return result
