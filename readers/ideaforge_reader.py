"""
IdeaForge Database Reader
Read-only SQLite interface for IdeaForge ideas database.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class IdeaForgeReader:
    """Read-only reader for IdeaForge database."""

    def __init__(self, db_path: str):
        """
        Initialize IdeaForge reader.

        Args:
            db_path: Path to IdeaForge SQLite database

        Raises:
            FileNotFoundError: If db_path does not exist
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        # Cached PRAGMA-detected column presence (populated lazily).
        # R-A item 3 (2026-05-12) introduced scoring_rubric on the IdeaForge
        # ideas table; older snapshots (or out-of-band test fixtures) may
        # lack the column. Falling back gracefully prevents an
        # OperationalError from halting the triage/build refresh path.
        self._has_scoring_rubric: Optional[bool] = None

        if not Path(db_path).exists():
            raise FileNotFoundError(f"IdeaForge database not found at {db_path}")

        self._connect()

    def _connect(self):
        """Establish read-only database connection."""
        if self.conn is None:
            # Open in read-only mode using URI
            self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row

    def _detect_scoring_rubric_column(self) -> bool:
        """Cache and return whether the ideas table has a scoring_rubric column.

        Used to make get_unprocessed_ideas / get_idea_by_id resilient against
        older IdeaForge snapshots that pre-date the rubric column. Caching
        avoids a PRAGMA round-trip per call.
        """
        if self._has_scoring_rubric is not None:
            return self._has_scoring_rubric
        self._connect()
        cursor = self.conn.cursor()
        try:
            cursor.execute("PRAGMA table_info(ideas)")
            cols = {row["name"] for row in cursor.fetchall()}
        except sqlite3.Error as exc:
            logger.warning(
                "PRAGMA table_info(ideas) failed; assuming no scoring_rubric: %s",
                exc,
            )
            cols = set()
        self._has_scoring_rubric = "scoring_rubric" in cols
        if not self._has_scoring_rubric:
            logger.warning(
                "IdeaForge schema at %s lacks 'scoring_rubric' column; "
                "falling back to legacy query (no rubric filter, no rubric "
                "field in returned dicts). Upgrade IdeaForge or skip the "
                "rubric path until the column is added.",
                self.db_path,
            )
        return self._has_scoring_rubric

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __del__(self):
        """Cleanup connection on deletion."""
        self.close()

    def get_unprocessed_ideas(self) -> list[dict]:
        """
        Get scored ideas ready for triage.

        Returns ideas where status IN ('scored', 'classified') AND
        weighted_score IS NOT NULL AND not already claimed by Metroplex.
        Classification is no longer required — score threshold handles
        dismissal directly. Results sorted by weighted_score DESC.

        Rubric filter (R-A item 3, 2026-05-12): admits only rows with
        ``scoring_rubric = 'life_domain'``. The 'tech' rubric is the legacy
        dead path (11 tech rows moved to status='archived' on 2026-05-11
        per pivot decision D1); NULL-rubric rows are pre-rubric legacy
        and intentionally excluded for the same reason. New rubrics
        require an explicit code change here — allow-list, not
        exclude-list, is the correct safety posture for "what enters the
        build queue".

        Returns:
            List of idea dictionaries with fields:
            - id, title, description, problem_statement, target_audience
            - weighted_score, opportunity_score, problem_score, feasibility_score,
              why_now_score, competition_score
            - artifact_type, signal_count, status, strategic_theme,
              scoring_rubric
        """
        self._connect()
        cursor = self.conn.cursor()

        # Resilient query construction: when the upstream DB lacks the
        # scoring_rubric column (older IdeaForge snapshots), fall back to
        # the legacy query shape rather than raising OperationalError.
        # NOTE: scoring_rubric is bound as a parameter (not a literal) so that
        # any future "switch on env var / config" change inherits the
        # parameterized call shape. Today the value is a hard-coded constant.
        has_rubric = self._detect_scoring_rubric_column()
        if has_rubric:
            cursor.execute("""
                SELECT
                    id,
                    title,
                    description,
                    problem_statement,
                    target_audience,
                    weighted_score,
                    opportunity_score,
                    problem_score,
                    feasibility_score,
                    why_now_score,
                    competition_score,
                    artifact_type,
                    signal_count,
                    status,
                    strategic_theme,
                    scoring_rubric
                FROM ideas
                WHERE status = 'classified'
                    AND weighted_score IS NOT NULL
                    AND artifact_type IS NOT NULL
                    AND scoring_rubric = ?
                    AND (claimed_by IS NULL OR claimed_by = '' OR claimed_by = 'metroplex')
                ORDER BY weighted_score DESC
            """, ('life_domain',))
        else:
            # Legacy fallback: pre-rubric schemas. No rubric filter, no
            # rubric field in returned dicts. Triage/build paths that
            # depend on the rubric will treat the result as 'tech'-style
            # work (which they already handle).
            cursor.execute("""
                SELECT
                    id,
                    title,
                    description,
                    problem_statement,
                    target_audience,
                    weighted_score,
                    opportunity_score,
                    problem_score,
                    feasibility_score,
                    why_now_score,
                    competition_score,
                    artifact_type,
                    signal_count,
                    status,
                    strategic_theme
                FROM ideas
                WHERE status = 'classified'
                    AND weighted_score IS NOT NULL
                    AND artifact_type IS NOT NULL
                    AND (claimed_by IS NULL OR claimed_by = '' OR claimed_by = 'metroplex')
                ORDER BY weighted_score DESC
            """)

        rows = cursor.fetchall()
        results = [dict(row) for row in rows]

        # R-A item 3 / Codex Round 3 ops finding: when the upstream IdeaForge
        # schema is legacy (pre-rubric), every returned dict is missing the
        # 'scoring_rubric' field. Combined with the fail-closed dequeue guard
        # in gates/build.py, this means ALL ideaforge items will be rejected
        # at dequeue. That is the correct safety posture (don't dispatch
        # unknown-rubric ideas), but it is operationally surprising. Log a
        # loud WARNING on every non-empty return so operators see why no
        # builds are progressing.
        if results and not has_rubric:
            logger.warning(
                "IdeaForge legacy-schema fallback active: returning %d "
                "rows without scoring_rubric. The build gate's fail-closed "
                "dequeue guard will reject every one. Upgrade the upstream "
                "IdeaForge schema (add 'scoring_rubric' column) to restore "
                "normal flow.",
                len(results),
            )
        return results

    def claim_idea(self, idea_id: int, claimed_by: str = "metroplex") -> bool:
        """
        Mark an idea as claimed in IdeaForge without changing its status.

        Writes to claimed_by/claimed_at columns so IdeaForge knows the idea
        has been processed by an external system, while preserving IdeaForge's
        own status lifecycle (classified/dismissed/exported).

        Args:
            idea_id: The idea ID to claim
            claimed_by: System name claiming the idea (default: 'metroplex')

        Returns:
            True if a row was updated, False otherwise
        """
        try:
            write_conn = sqlite3.connect(self.db_path)
            write_conn.execute("PRAGMA busy_timeout=5000")
            cursor = write_conn.cursor()
            cursor.execute(
                "UPDATE ideas SET claimed_by = ?, claimed_at = datetime('now') WHERE id = ?",
                (claimed_by, idea_id),
            )
            changed = cursor.rowcount > 0
            write_conn.commit()
            write_conn.close()
            return changed
        except Exception:
            return False

    def update_idea_status(self, idea_id: int, status: str) -> bool:
        """
        Update an idea's status in the IdeaForge database.

        Opens a separate writable connection (the default connection is read-only)
        to perform the update, then closes it immediately.

        NOTE: Only use for terminal statuses like 'exported'. Do NOT write
        'triaged' — use claim_idea() instead to avoid stomping IdeaForge's
        status lifecycle.

        Args:
            idea_id: The idea ID to update
            status: The new status value (e.g. 'exported')

        Returns:
            True if a row was updated, False otherwise
        """
        try:
            write_conn = sqlite3.connect(self.db_path)
            write_conn.execute("PRAGMA busy_timeout=5000")
            cursor = write_conn.cursor()
            cursor.execute("UPDATE ideas SET status = ? WHERE id = ?", (status, idea_id))
            changed = cursor.rowcount > 0
            write_conn.commit()
            write_conn.close()
            return changed
        except Exception:
            return False

    def get_idea_by_id(self, idea_id: int) -> dict | None:
        """
        Get a specific idea by ID.

        Args:
            idea_id: The idea ID to retrieve

        Returns:
            Idea dictionary with all fields, or None if not found
        """
        self._connect()
        cursor = self.conn.cursor()

        has_rubric = self._detect_scoring_rubric_column()
        if has_rubric:
            cursor.execute("""
                SELECT
                    id,
                    title,
                    description,
                    problem_statement,
                    target_audience,
                    struggling_user,
                    weighted_score,
                    opportunity_score,
                    problem_score,
                    feasibility_score,
                    why_now_score,
                    competition_score,
                    artifact_type,
                    signal_count,
                    status,
                    scoring_rubric
                FROM ideas
                WHERE id = ?
            """, (idea_id,))
        else:
            cursor.execute("""
                SELECT
                    id,
                    title,
                    description,
                    problem_statement,
                    target_audience,
                    struggling_user,
                    weighted_score,
                    opportunity_score,
                    problem_score,
                    feasibility_score,
                    why_now_score,
                    competition_score,
                    artifact_type,
                    signal_count,
                    status
                FROM ideas
                WHERE id = ?
            """, (idea_id,))

        row = cursor.fetchone()
        return dict(row) if row else None
