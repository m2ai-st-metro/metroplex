"""
ST Records Database Reader
Read-only SQLite interface for ST Records persona metrics database.
Includes one write operation: updating patch status.
"""
import sqlite3
import json
from pathlib import Path
from typing import Optional


class STRecordsReader:
    """Read-only reader for ST Records database (with one write operation)."""

    def __init__(self, db_path: str):
        """
        Initialize ST Records reader.

        Args:
            db_path: Path to ST Records SQLite database

        Raises:
            FileNotFoundError: If db_path does not exist
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

        if not Path(db_path).exists():
            raise FileNotFoundError(f"ST Records database not found at {db_path}")

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

    def get_proposed_patches(self) -> list[dict]:
        """
        Get all proposed persona patches.

        Returns patches where status = 'proposed'.

        Returns:
            List of patch dictionaries with fields:
            - id (int)
            - patch_id (str)
            - persona_id (str)
            - rationale (str)
            - from_version (str)
            - to_version (str)
            - raw_json (dict, parsed from JSON string)
        """
        self._connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                id,
                patch_id,
                persona_id,
                rationale,
                from_version,
                to_version,
                raw_json
            FROM persona_patches
            WHERE status = 'proposed'
        """)

        rows = cursor.fetchall()
        results = []
        for row in rows:
            row_dict = dict(row)
            # Parse raw_json from JSON string to dict
            if row_dict.get('raw_json'):
                try:
                    row_dict['raw_json'] = json.loads(row_dict['raw_json'])
                except (json.JSONDecodeError, TypeError):
                    row_dict['raw_json'] = {}
            else:
                row_dict['raw_json'] = {}
            results.append(row_dict)

        return results

    def get_pending_recommendations(self) -> list[dict]:
        """
        Get all pending improvement recommendations.

        Returns recommendations where status = 'pending'.

        Returns:
            List of recommendation dictionaries
        """
        self._connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                id, recommendation_id, session_id, recommendation_type,
                target_system, title, priority, scope, target_department,
                status, emitted_at, raw_json,
                effectiveness, effectiveness_score, effectiveness_evaluated_at
            FROM improvement_recommendations
            WHERE status = 'pending'
        """)

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def update_patch_status(self, patch_id: str, new_status: str) -> None:
        """
        Update the status of a persona patch.

        This is the ONE write operation. Opens a separate writable connection.

        Args:
            patch_id: The patch ID to update
            new_status: The new status value
        """
        # Open a separate writable connection for this write operation
        write_conn = sqlite3.connect(self.db_path)
        try:
            cursor = write_conn.cursor()
            cursor.execute("""
                UPDATE persona_patches
                SET status = ?
                WHERE patch_id = ?
            """, (new_status, patch_id))
            write_conn.commit()
        finally:
            write_conn.close()

    def get_outcome_records(self, limit: int = 50) -> list[dict]:
        """
        Get recent outcome records.

        Returns recent outcome_records ordered by emitted_at DESC.

        Args:
            limit: Maximum number of records to return (default: 50)

        Returns:
            List of outcome record dictionaries
        """
        self._connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                id, idea_id, idea_title, outcome, overall_score,
                recommendation, capabilities_fit, build_outcome,
                artifact_count, tech_stack, total_duration_seconds,
                tags, github_url, emitted_at, raw_json
            FROM outcome_records
            ORDER BY emitted_at DESC
            LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]
