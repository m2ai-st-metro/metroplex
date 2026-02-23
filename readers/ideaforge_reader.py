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
        Get all scored ideas ready for processing.

        Returns ideas where status = 'scored' AND weighted_score IS NOT NULL.
        Results sorted by weighted_score DESC (highest scores first).

        Returns:
            List of idea dictionaries with fields:
            - id (int)
            - title (str)
            - description (str)
            - problem_statement (str)
            - target_audience (str)
            - weighted_score (float)
            - opportunity_score (float)
            - problem_score (float)
            - feasibility_score (float)
            - why_now_score (float)
            - competition_score (float)
            - artifact_type (str)
            - signal_count (int)
            - status (str)
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
            WHERE status = 'scored'
                AND weighted_score IS NOT NULL
            ORDER BY weighted_score DESC
        """)

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

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
