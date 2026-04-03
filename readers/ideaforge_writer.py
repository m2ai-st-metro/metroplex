"""
IdeaForge Database Writer (L5 B3)
Writes build outcomes back to IdeaForge's ideas.build_outcome column.
Follows the same connection pattern as ideaforge_reader.py but with
write access.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class IdeaForgeWriter:
    """Write-only interface for IdeaForge database outcome feedback."""

    def __init__(self, db_path: str):
        """
        Initialize IdeaForge writer.

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
        """Establish database connection with write access."""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=5000")
            # Ensure build_outcome column exists
            try:
                self.conn.execute(
                    "ALTER TABLE ideas ADD COLUMN build_outcome TEXT DEFAULT NULL"
                )
                self.conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __del__(self):
        """Cleanup connection on deletion."""
        self.close()

    def write_build_outcome(self, idea_id: int, outcome: str) -> bool:
        """Write a build outcome back to IdeaForge.

        Args:
            idea_id: IdeaForge idea ID
            outcome: One of 'published', 'build_failed', 'review_failed', 'tyrest_rejected'

        Returns:
            True if the row was updated, False otherwise.
        """
        valid_outcomes = {"published", "build_failed", "review_failed", "tyrest_rejected"}
        if outcome not in valid_outcomes:
            logger.warning("Invalid build outcome '%s', must be one of %s", outcome, valid_outcomes)
            return False

        try:
            self._connect()
            cursor = self.conn.execute(
                "UPDATE ideas SET build_outcome = ? WHERE id = ?",
                (outcome, idea_id),
            )
            self.conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                logger.info("Wrote build outcome '%s' for idea %d", outcome, idea_id)
            else:
                logger.warning("No idea found with id %d to update outcome", idea_id)
            return updated
        except Exception as e:
            logger.warning("Failed to write build outcome for idea %d: %s", idea_id, e)
            return False
