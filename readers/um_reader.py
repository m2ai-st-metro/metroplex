"""
Ultra-Magnus Database Reader
Read-only SQLite interface for Ultra-Magnus idea factory database.
"""
import sqlite3
from pathlib import Path
from typing import Optional


class UMReader:
    """Read-only reader for Ultra-Magnus database."""

    def __init__(self, db_path: str):
        """
        Initialize Ultra-Magnus reader.

        Args:
            db_path: Path to Ultra-Magnus SQLite database

        Raises:
            FileNotFoundError: If db_path does not exist
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

        if not Path(db_path).exists():
            raise FileNotFoundError(f"Ultra-Magnus database not found at {db_path}")

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

    def get_idea_pipeline_status(self, idea_id: str) -> dict | None:
        """
        Get pipeline status for a specific idea.

        Returns idea with current_stage and current_status.

        Args:
            idea_id: The idea ID to retrieve

        Returns:
            Idea dictionary with current_stage and current_status, or None if not found
        """
        self._connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                id,
                title,
                current_stage,
                current_status
            FROM ideas
            WHERE id = ?
        """, (idea_id,))

        row = cursor.fetchone()
        return dict(row) if row else None

    def get_recent_builds(self, limit: int = 10) -> list[dict]:
        """
        Get recent build results joined with ideas.

        Args:
            limit: Maximum number of builds to return (default: 10)

        Returns:
            List of build result dictionaries with idea information
        """
        self._connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                br.*,
                i.title,
                i.current_stage,
                i.current_status
            FROM build_results br
            LEFT JOIN ideas i ON br.idea_id = i.id
            ORDER BY br.id DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_evaluation_result(self, idea_id: str) -> dict | None:
        """
        Get evaluation results for a specific idea.

        Args:
            idea_id: The idea ID to retrieve evaluation for

        Returns:
            Evaluation result dictionary, or None if not found
        """
        self._connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT *
            FROM evaluation_results
            WHERE idea_id = ?
        """, (idea_id,))

        row = cursor.fetchone()
        return dict(row) if row else None
