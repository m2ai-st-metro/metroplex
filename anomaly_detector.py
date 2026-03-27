"""
Anomaly Detector — Phase D
Per-cycle anomaly detection for the Metroplex pipeline.
Catches duplicate emissions, zero-output stalls, scoring drift, and ratchet issues.

Uses read-only sqlite3 connections — does NOT modify the state DB.
"""
import logging
import os
import sqlite3
import statistics
from datetime import datetime, timedelta
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class _Notifier(Protocol):
    """Minimal notifier protocol (matches notifier.Notifier)."""

    def notify(self, message: str, level: str = "info") -> bool: ...


class AnomalyDetector:
    """Per-cycle anomaly detection for the Metroplex pipeline.

    All queries use a separate read-only sqlite3 connection so they
    cannot accidentally mutate the state DB.
    """

    # Anomalies at or above this severity trigger a Telegram alert.
    CRIT_PREFIX = "CRIT:"

    def __init__(self, db_path: str, notifier: Optional[_Notifier] = None):
        self.db_path = db_path
        self.notifier = notifier

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection to the state DB."""
        # file: URI with mode=ro ensures no writes are possible.
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            # Fallback for older sqlite builds that don't support URI mode.
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _safe_query(self, conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a SELECT, returning [] if the table doesn't exist."""
        try:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()
        except sqlite3.OperationalError as e:
            # "no such table" is expected when the DB is fresh.
            logger.debug("Query skipped (table missing): %s", e)
            return []

    # ------------------------------------------------------------------
    # Detection methods
    # ------------------------------------------------------------------

    def detect_duplicate_triage(
        self, window_hours: int = 24, threshold: int = 10
    ) -> list[str]:
        """Flag idea_ids triaged more than *threshold* times in the last *window_hours*.

        Returns a list of human-readable warning strings.
        """
        warnings: list[str] = []
        cutoff = (datetime.now() - timedelta(hours=window_hours)).isoformat()

        conn = self._connect()
        try:
            rows = self._safe_query(
                conn,
                """
                SELECT idea_id, COUNT(*) AS cnt
                FROM triage_decisions
                WHERE decided_at >= ?
                GROUP BY idea_id
                HAVING cnt > ?
                ORDER BY cnt DESC
                """,
                (cutoff, threshold),
            )
            for row in rows:
                msg = f"Idea {row['idea_id']} triaged {row['cnt']} times in {window_hours}h"
                warnings.append(msg)
                logger.warning("Duplicate triage: %s", msg)
        finally:
            conn.close()

        return warnings

    def detect_zero_output(self, cycle_count: int = 5) -> list[str]:
        """Detect sustained zero-output stalls.

        If the last *cycle_count* cycles all produced zero builds AND zero
        publishes while there are pending items in the priority queue, that
        strongly suggests the pipeline is stuck.
        """
        warnings: list[str] = []

        conn = self._connect()
        try:
            # Last N completed cycles (most recent first).
            cycles = self._safe_query(
                conn,
                """
                SELECT build_count, publish_count
                FROM cycles
                WHERE completed_at IS NOT NULL
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (cycle_count,),
            )

            if len(cycles) < cycle_count:
                logger.debug(
                    "Only %d completed cycles (need %d) — skipping zero-output check",
                    len(cycles),
                    cycle_count,
                )
                conn.close()
                return warnings

            all_zero = all(
                (row["build_count"] or 0) == 0 and (row["publish_count"] or 0) == 0
                for row in cycles
            )

            if not all_zero:
                conn.close()
                return warnings

            # Check pending queue depth.
            pending_rows = self._safe_query(
                conn,
                "SELECT COUNT(*) AS cnt FROM priority_queue WHERE status = 'pending'",
            )
            pending_count = pending_rows[0]["cnt"] if pending_rows else 0

            if pending_count > 0:
                msg = (
                    f"{self.CRIT_PREFIX} Zero builds/publishes in last {cycle_count} "
                    f"cycles despite {pending_count} pending queue items"
                )
                warnings.append(msg)
                logger.warning("Zero-output stall: %s", msg)
        finally:
            conn.close()

        return warnings

    def detect_scoring_drift(self, window_days: int = 14) -> list[str]:
        """Detect compressed or clustered scoring distributions.

        Two checks:
        1. Standard deviation of scaled_score < 5.0  → scores collapsed to a
           narrow band (no meaningful differentiation).
        2. > 60 % of scores fall within ±5 of the approve threshold → the
           model is hedging instead of deciding.
        """
        warnings: list[str] = []
        cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()

        conn = self._connect()
        try:
            rows = self._safe_query(
                conn,
                """
                SELECT scaled_score
                FROM triage_decisions
                WHERE decided_at >= ?
                """,
                (cutoff,),
            )

            if len(rows) < 10:
                logger.debug(
                    "Only %d triage decisions in last %d days — skipping drift check",
                    len(rows),
                    window_days,
                )
                conn.close()
                return warnings

            scores = [float(row["scaled_score"]) for row in rows]

            # Check 1: compressed stdev
            stdev = statistics.stdev(scores)
            if stdev < 5.0:
                msg = (
                    f"Scoring drift: stdev={stdev:.1f} over {len(scores)} decisions "
                    f"in {window_days}d (scores compressed to narrow band)"
                )
                warnings.append(msg)
                logger.warning(msg)

            # Check 2: clustering around approve threshold
            approve_threshold = float(
                os.environ.get("METROPLEX_APPROVE_THRESHOLD", "55")
            )
            near_threshold = sum(
                1
                for s in scores
                if abs(s - approve_threshold) <= 5.0
            )
            pct_near = (near_threshold / len(scores)) * 100
            if pct_near > 60.0:
                msg = (
                    f"Scoring drift: {pct_near:.0f}% of scores within ±5 of "
                    f"approve threshold ({approve_threshold}) — model may be hedging"
                )
                warnings.append(msg)
                logger.warning(msg)
        finally:
            conn.close()

        return warnings

    def detect_ratchet_stuck(self) -> list[str]:
        """Detect a quality ratchet that hasn't moved in a long time.

        Reads ``quality_unchanged_count`` from the ``ratchet_state`` table and
        compares it against 50 % of the ``METROPLEX_RATCHET_STALE_CYCLES`` env
        var (default 10).
        """
        warnings: list[str] = []

        conn = self._connect()
        try:
            rows = self._safe_query(
                conn,
                "SELECT value FROM ratchet_state WHERE key = 'quality_unchanged_count'",
            )
            if not rows:
                logger.debug("No ratchet_state data — skipping ratchet check")
                conn.close()
                return warnings

            unchanged_count = int(float(rows[0]["value"]))
            stale_cycles = int(os.environ.get("METROPLEX_RATCHET_STALE_CYCLES", "10"))
            warn_at = stale_cycles // 2  # 50 % of stale threshold

            if unchanged_count > warn_at:
                msg = (
                    f"Quality ratchet stuck: unchanged for {unchanged_count} cycles "
                    f"(warn threshold {warn_at}, stale limit {stale_cycles})"
                )
                warnings.append(msg)
                logger.warning(msg)
        finally:
            conn.close()

        return warnings

    # ------------------------------------------------------------------
    # Aggregate runner
    # ------------------------------------------------------------------

    def run_all(self) -> list[str]:
        """Run every detection method and return all warnings.

        If any CRIT-level warnings are found and a notifier is available,
        a consolidated Telegram alert is sent.
        """
        all_warnings: list[str] = []

        detectors = [
            self.detect_duplicate_triage,
            self.detect_zero_output,
            self.detect_scoring_drift,
            self.detect_ratchet_stuck,
        ]

        for detector in detectors:
            try:
                all_warnings.extend(detector())
            except Exception as e:
                logger.debug("Anomaly detector %s failed: %s", detector.__name__, e)

        if not all_warnings:
            logger.debug("Anomaly detection: no issues found")
            return all_warnings

        # Send Telegram alert for CRIT-level issues.
        crit_warnings = [w for w in all_warnings if w.startswith(self.CRIT_PREFIX)]
        if crit_warnings and self.notifier:
            alert_body = "Anomaly alert:\n" + "\n".join(f"• {w}" for w in crit_warnings)
            try:
                self.notifier.notify(alert_body, "warning")
            except Exception as e:
                logger.debug("Anomaly notifier failed: %s", e)

        return all_warnings
