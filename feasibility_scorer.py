"""Pre-Build Feasibility Scorer (L5 B2).

Predicts build success/failure before committing resources.
Static heuristic component always active; learned component activates
after ACTIVATION_THRESHOLD post-mortems, penalizing ideas similar to
past failures.

The loop: feasibility score predicts outcome -> actual outcome validates
prediction -> scorer adjusts feature weights -> predictions improve.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# --- Constants ---
ACTIVATION_THRESHOLD = 20  # Post-mortems before learned component activates
REJECT_THRESHOLD = 25      # Below this, reject the build
DEFAULT_FEATURE_WEIGHTS = {
    "scope_clarity": 0.25,
    "dependency_risk": 0.25,
    "factory_fit": 0.30,
    "artifact_type": 0.20,
}

# Category penalty multipliers for learned component (higher = more penalty)
CATEGORY_PENALTY_WEIGHTS = {
    "dependency_error": 1.0,
    "timeout": 0.8,
    "build_error": 0.7,
    "environment_error": 0.6,
    "test_failure": 0.5,
    "spec_unclear": 0.3,
}

# Dependency signal keywords
DEPENDENCY_KEYWORDS = [
    "api", "oauth", "webhook", "stripe", "aws", "gcp", "firebase",
    "supabase", "twilio", "sendgrid", "s3", "redis", "postgres",
    "mongodb", "elasticsearch", "kafka", "rabbitmq", "graphql",
    "grpc", "websocket", "docker", "kubernetes",
]


def _score_scope_clarity(idea: dict) -> float:
    """Score 0-100 based on problem_statement specificity."""
    ps = idea.get("problem_statement", "") or ""
    if not ps:
        return 20.0

    length = len(ps)
    if length > 200:
        score = 90.0
    elif length > 100:
        score = 70.0
    elif length > 50:
        score = 50.0
    else:
        score = 30.0

    # Bonus for specific terms (numbers, metrics, user types)
    specifics = len(re.findall(r'\d+|%|users?|developers?|teams?|minutes?|hours?|daily|weekly', ps, re.IGNORECASE))
    score = min(100, score + specifics * 5)

    return score


def _score_dependency_risk(idea: dict) -> float:
    """Score 0-100 where higher = fewer dependencies = more feasible."""
    text = f"{idea.get('title', '')} {idea.get('description', '')} {idea.get('problem_statement', '')}".lower()
    dep_count = sum(1 for kw in DEPENDENCY_KEYWORDS if kw in text)

    if dep_count == 0:
        return 90.0
    elif dep_count == 1:
        return 70.0
    elif dep_count == 2:
        return 50.0
    elif dep_count <= 4:
        return 30.0
    else:
        return 10.0


def _score_factory_fit(idea: dict) -> float:
    """Pass through factory_fit_score scaled to 0-100."""
    ff = idea.get("factory_fit_score") or idea.get("factory_fit")
    if ff is None:
        return 50.0  # Neutral default
    # IdeaForge scores 0-10, scale to 0-100
    return max(0, min(100, float(ff) * 10))


def _score_artifact_type(idea: dict) -> float:
    """Score based on artifact complexity (tool=high, agent=medium, product=lower)."""
    at = (idea.get("artifact_type") or "").lower()
    if at == "tool":
        return 85.0
    elif at == "agent":
        return 60.0
    elif at == "product":
        return 40.0
    return 55.0  # Unknown defaults to middle


def _compute_keyword_overlap(idea: dict, postmortems_rows: list[dict]) -> float:
    """Compute keyword overlap between idea and past failed ideas.

    Returns a penalty multiplier 0.5-1.0 (lower = more overlap with failures).
    """
    if not postmortems_rows:
        return 1.0

    idea_text = f"{idea.get('title', '')} {idea.get('description', '')}".lower()
    idea_words = set(re.findall(r'\b\w{3,}\b', idea_text))

    if not idea_words:
        return 1.0

    total_penalty = 0.0
    total_weight = 0.0

    for pm in postmortems_rows:
        pm_title = (pm.get("title") or "").lower()
        pm_words = set(re.findall(r'\b\w{3,}\b', pm_title))

        if not pm_words:
            continue

        overlap = len(idea_words & pm_words) / max(len(idea_words), 1)
        category = pm.get("failure_category", "spec_unclear")
        cat_weight = CATEGORY_PENALTY_WEIGHTS.get(category, 0.3)

        total_penalty += overlap * cat_weight
        total_weight += cat_weight

    if total_weight == 0:
        return 1.0

    # Normalize penalty to 0-1 range, then map to 0.5-1.0 multiplier
    normalized = total_penalty / total_weight
    multiplier = 1.0 - (normalized * 0.5)
    return max(0.5, min(1.0, multiplier))


def score_feasibility(idea: dict, state_db) -> dict:
    """Score an idea's build feasibility.

    Returns:
        {
            "score": float 0-100,
            "predicted_outcome": "success" | "failure",
            "breakdown": dict of component scores,
            "feature_weights": dict of weights used,
            "learned_active": bool,
            "penalty_multiplier": float,
        }
    """
    weights = dict(DEFAULT_FEATURE_WEIGHTS)

    breakdown = {
        "scope_clarity": _score_scope_clarity(idea),
        "dependency_risk": _score_dependency_risk(idea),
        "factory_fit": _score_factory_fit(idea),
        "artifact_type": _score_artifact_type(idea),
    }

    # Static component
    static_score = sum(breakdown[k] * weights[k] for k in weights)

    # Learned component (activates after ACTIVATION_THRESHOLD post-mortems)
    learned_active = False
    penalty_multiplier = 1.0

    try:
        state_db.connect()
        pm_count = state_db.conn.execute(
            "SELECT COUNT(*) FROM build_postmortems"
        ).fetchone()[0]

        if pm_count >= ACTIVATION_THRESHOLD:
            learned_active = True
            # Get all postmortem titles for keyword overlap
            rows = state_db.conn.execute(
                "SELECT title, failure_category FROM build_postmortems"
            ).fetchall()
            pm_list = [{"title": r[0], "failure_category": r[1]} for r in rows]
            penalty_multiplier = _compute_keyword_overlap(idea, pm_list)
    except Exception as e:
        logger.warning("Failed to query postmortems for learned component: %s", e)

    final_score = static_score * penalty_multiplier
    final_score = max(0, min(100, round(final_score, 1)))

    predicted_outcome = "failure" if final_score < 50 else "success"

    return {
        "score": final_score,
        "predicted_outcome": predicted_outcome,
        "breakdown": breakdown,
        "feature_weights": weights,
        "learned_active": learned_active,
        "penalty_multiplier": penalty_multiplier,
    }


def record_prediction(
    state_db,
    queue_job_id: str,
    feasibility_score: float,
    predicted_outcome: str,
    feature_weights: dict,
) -> None:
    """Store a feasibility prediction for later accuracy tracking."""
    try:
        state_db.connect()
        now = datetime.now(timezone.utc).isoformat()
        state_db.conn.execute(
            """INSERT INTO feasibility_predictions
            (queue_job_id, feasibility_score, predicted_outcome, feature_weights, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                queue_job_id,
                feasibility_score,
                predicted_outcome,
                json.dumps(feature_weights),
                now,
            ),
        )
        state_db.conn.commit()
        logger.info("Recorded feasibility prediction for %s: %.1f (%s)",
                     queue_job_id, feasibility_score, predicted_outcome)
    except Exception as e:
        logger.warning("Failed to record prediction for %s: %s", queue_job_id, e)


def resolve_prediction(
    state_db,
    queue_job_id: str,
    actual_outcome: str,
) -> None:
    """Called when build completes -- fills in actual_outcome and computes correctness."""
    try:
        state_db.connect()
        now = datetime.now(timezone.utc).isoformat()

        # Map actual outcomes to success/failure for correctness check
        success_outcomes = {"completed", "published", "reviewed"}
        actual_binary = "success" if actual_outcome in success_outcomes else "failure"

        # Get the prediction
        row = state_db.conn.execute(
            "SELECT predicted_outcome FROM feasibility_predictions WHERE queue_job_id = ?",
            (queue_job_id,),
        ).fetchone()

        if row is None:
            logger.debug("No prediction found for %s, skipping resolve", queue_job_id)
            return

        correct = 1 if row[0] == actual_binary else 0

        state_db.conn.execute(
            """UPDATE feasibility_predictions
            SET actual_outcome = ?, correct = ?, resolved_at = ?
            WHERE queue_job_id = ?""",
            (actual_outcome, correct, now, queue_job_id),
        )
        state_db.conn.commit()
        logger.info("Resolved prediction for %s: predicted=%s actual=%s correct=%s",
                     queue_job_id, row[0], actual_binary, bool(correct))
    except Exception as e:
        logger.warning("Failed to resolve prediction for %s: %s", queue_job_id, e)


def get_prediction_accuracy(state_db, window: int = 20) -> float | None:
    """Compute accuracy over last N resolved predictions.

    Returns None if fewer than 10 resolved predictions exist.
    """
    try:
        state_db.connect()
        rows = state_db.conn.execute(
            """SELECT correct FROM feasibility_predictions
            WHERE actual_outcome IS NOT NULL
            ORDER BY resolved_at DESC
            LIMIT ?""",
            (window,),
        ).fetchall()

        if len(rows) < 10:
            return None

        correct_count = sum(1 for r in rows if r[0] == 1)
        return correct_count / len(rows)
    except Exception as e:
        logger.warning("Failed to compute prediction accuracy: %s", e)
        return None


def adjust_feature_weights(state_db) -> dict | None:
    """Adjust learned component based on prediction accuracy.

    If accuracy < 60% over 20 predictions, disable learned component (reset).
    If accuracy > 75%, allow learned component to increase influence.
    Returns new config if adjusted, None otherwise.
    """
    accuracy = get_prediction_accuracy(state_db)
    if accuracy is None:
        return None

    try:
        state_db.connect()
        # Ensure ratchet_state table exists
        state_db.conn.execute("""
            CREATE TABLE IF NOT EXISTS ratchet_state (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        if accuracy < 0.60:
            # Disable learned component by storing reset flag
            now = datetime.now(timezone.utc).isoformat()
            state_db.conn.execute(
                """INSERT OR REPLACE INTO ratchet_state (key, value, updated_at)
                VALUES ('feasibility_learned_disabled', 1, ?)""",
                (now,),
            )
            state_db.conn.commit()
            logger.info("Feasibility learned component DISABLED: accuracy %.1f%% < 60%%",
                         accuracy * 100)
            return {"learned_disabled": True, "accuracy": accuracy}

        if accuracy > 0.75:
            # Allow wider penalty range (multiplier can go lower)
            now = datetime.now(timezone.utc).isoformat()
            state_db.conn.execute(
                """INSERT OR REPLACE INTO ratchet_state (key, value, updated_at)
                VALUES ('feasibility_learned_disabled', 0, ?)""",
                (now,),
            )
            state_db.conn.commit()
            logger.info("Feasibility learned component ENABLED with wider range: accuracy %.1f%%",
                         accuracy * 100)
            return {"learned_disabled": False, "accuracy": accuracy}

        return None
    except Exception as e:
        logger.warning("Failed to adjust feature weights: %s", e)
        return None


def get_reject_threshold(state_db) -> int:
    """Get the current reject threshold (ratchet: can only tighten).

    Returns the stored threshold or the default REJECT_THRESHOLD.
    """
    try:
        state_db.connect()
        # Ensure ratchet_state table exists
        state_db.conn.execute("""
            CREATE TABLE IF NOT EXISTS ratchet_state (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        row = state_db.conn.execute(
            "SELECT value FROM ratchet_state WHERE key = 'feasibility_reject_threshold'"
        ).fetchone()
        if row:
            return int(row[0])
    except Exception:
        pass
    return REJECT_THRESHOLD


def tighten_reject_threshold(state_db, new_threshold: int) -> bool:
    """Tighten the reject threshold (ratchet: only increases allowed).

    Returns True if threshold was actually tightened.
    """
    current = get_reject_threshold(state_db)
    if new_threshold <= current:
        return False

    try:
        state_db.connect()
        now = datetime.now(timezone.utc).isoformat()
        state_db.conn.execute(
            """INSERT OR REPLACE INTO ratchet_state (key, value, updated_at)
            VALUES ('feasibility_reject_threshold', ?, ?)""",
            (float(new_threshold), now),
        )
        state_db.conn.commit()
        logger.info("Feasibility reject threshold tightened: %d -> %d", current, new_threshold)
        return True
    except Exception as e:
        logger.warning("Failed to tighten reject threshold: %s", e)
        return False
