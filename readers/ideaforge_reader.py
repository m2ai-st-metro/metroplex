"""
IdeaForge Database Reader
Read-only SQLite interface for IdeaForge ideas database.
"""
import sqlite3
from pathlib import Path
from typing import Optional


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

        if not Path(db_path).exists():
            raise FileNotFoundError(f"IdeaForge database not found at {db_path}")

        self._connect()

    def _connect(self):
        """Establish read-only database connection."""
        if self.conn is None:
            # Open in read-only mode using URI
            self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row

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

        Returns:
            List of idea dictionaries with fields:
            - id, title, description, problem_statement, target_audience
            - weighted_score, opportunity_score, problem_score, feasibility_score,
              why_now_score, competition_score
            - artifact_type, signal_count, status
        """
        self._connect()
        cursor = self.conn.cursor()

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
        return [dict(row) for row in rows]

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
                status
            FROM ideas
            WHERE id = ?
        """, (idea_id,))

        row = cursor.fetchone()
        return dict(row) if row else None
