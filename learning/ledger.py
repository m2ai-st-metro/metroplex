"""EGO experiment ledger -- tracks all mutation experiments and their outcomes."""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def init_ego_tables(conn: sqlite3.Connection) -> None:
    """Create EGO tables if they don't exist. Safe to call repeatedly."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ego_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            parameter TEXT NOT NULL,
            baseline_value TEXT NOT NULL,
            variant_value TEXT NOT NULL,
            baseline_score REAL,
            variant_score REAL,
            improvement_pct REAL,
            is_winner INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'completed', 'applied', 'rolled_back', 'rejected')),
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            applied_at TEXT,
            rolled_back_at TEXT,
            builds_before_apply INTEGER,
            success_rate_before REAL,
            success_rate_after REAL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_target ON ego_experiments(target)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_ego_status ON ego_experiments(status)"
    )
    conn.commit()


def log_experiment(
    conn: sqlite3.Connection,
    target: str,
    parameter: str,
    baseline_value: str,
    variant_value: str,
    baseline_score: float,
    variant_score: float,
    improvement_pct: float,
    is_winner: bool,
    reason: str,
) -> int:
    """Record an experiment result. Returns the experiment ID."""
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO ego_experiments
            (target, parameter, baseline_value, variant_value,
             baseline_score, variant_score, improvement_pct,
             is_winner, status, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target,
            parameter,
            baseline_value,
            variant_value,
            baseline_score,
            variant_score,
            improvement_pct,
            1 if is_winner else 0,
            "completed",
            reason,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def mark_applied(
    conn: sqlite3.Connection,
    experiment_id: int,
    builds_before: int,
    success_rate_before: float,
) -> None:
    """Mark an experiment as applied to production."""
    conn.execute(
        """
        UPDATE ego_experiments
        SET status = 'applied',
            applied_at = ?,
            builds_before_apply = ?,
            success_rate_before = ?
        WHERE id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            builds_before,
            success_rate_before,
            experiment_id,
        ),
    )
    conn.commit()


def mark_rolled_back(
    conn: sqlite3.Connection,
    experiment_id: int,
    success_rate_after: float,
    reason: str,
) -> None:
    """Mark an experiment as rolled back."""
    conn.execute(
        """
        UPDATE ego_experiments
        SET status = 'rolled_back',
            rolled_back_at = ?,
            success_rate_after = ?,
            reason = reason || ' | ROLLBACK: ' || ?
        WHERE id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            success_rate_after,
            reason,
            experiment_id,
        ),
    )
    conn.commit()


def get_latest_applied(
    conn: sqlite3.Connection, target: str
) -> Optional[dict]:
    """Get the most recently applied experiment for a target, if any."""
    row = conn.execute(
        """
        SELECT * FROM ego_experiments
        WHERE target = ? AND status = 'applied'
        ORDER BY applied_at DESC LIMIT 1
        """,
        (target,),
    ).fetchone()
    return dict(row) if row else None


def get_experiment_summary(conn: sqlite3.Connection) -> dict:
    """Return summary stats for all EGO experiments."""
    cursor = conn.cursor()
    total = cursor.execute("SELECT COUNT(*) FROM ego_experiments").fetchone()[0]
    winners = cursor.execute(
        "SELECT COUNT(*) FROM ego_experiments WHERE is_winner = 1"
    ).fetchone()[0]
    applied = cursor.execute(
        "SELECT COUNT(*) FROM ego_experiments WHERE status = 'applied'"
    ).fetchone()[0]
    rolled_back = cursor.execute(
        "SELECT COUNT(*) FROM ego_experiments WHERE status = 'rolled_back'"
    ).fetchone()[0]
    return {
        "total": total,
        "winners": winners,
        "applied": applied,
        "rolled_back": rolled_back,
    }
