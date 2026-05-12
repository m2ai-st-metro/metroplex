"""
Sky-Lynx Recommendation Reader
Read-only SQLite interface for Sky-Lynx improvement recommendations
stored in the ST Records persona_metrics.db.

Sky-Lynx recommendations are a distinct intake stream from IdeaForge:
they bypass triage and enqueue directly into the Metroplex priority queue.
"""
import sqlite3
import json
from pathlib import Path
from typing import Optional


# Map Sky-Lynx priority labels to numeric base scores (0-100 scale).
# These get multiplied by config.skylynx_weight (default 1.5x) when enqueued.
PRIORITY_SCORE_MAP = {
    "critical": 95.0,
    "high": 85.0,
    "medium": 70.0,
    "low": 50.0,
}


class SkyLynxReader:
    """Reader for Sky-Lynx improvement recommendations from ST Records DB."""

    def __init__(self, db_path: str):
        """
        Initialize Sky-Lynx reader.

        Args:
            db_path: Path to ST Records persona_metrics.db

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

    def get_pending_recommendations(self) -> list[dict]:
        """
        Get all pending improvement recommendations.

        Returns recommendations where status = 'pending'.
        Parses raw_json into a dict for each recommendation.

        Returns:
            List of recommendation dictionaries with fields:
            - id (int)
            - recommendation_id (str)
            - session_id (str)
            - recommendation_type (str)
            - target_system (str)
            - title (str)
            - priority (str)
            - scope (str)
            - target_department (str|None)
            - status (str)
            - emitted_at (str)
            - raw_json (dict, parsed from JSON string)
        """
        self._connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                id,
                recommendation_id,
                session_id,
                recommendation_type,
                target_system,
                title,
                priority,
                scope,
                target_department,
                status,
                emitted_at,
                raw_json
            FROM improvement_recommendations
            WHERE status = 'pending'
              AND target_system IN ('pipeline', 'claude_md')
            ORDER BY
                CASE priority
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END
        """)

        rows = cursor.fetchall()
        results = []
        for row in rows:
            row_dict = dict(row)
            # Parse raw_json
            if row_dict.get("raw_json"):
                try:
                    row_dict["raw_json"] = json.loads(row_dict["raw_json"])
                except (json.JSONDecodeError, TypeError):
                    row_dict["raw_json"] = {}
            else:
                row_dict["raw_json"] = {}
            results.append(row_dict)

        return results

    def priority_to_score(self, priority: str) -> float:
        """
        Convert a Sky-Lynx priority label to a numeric score.

        Args:
            priority: Priority string (critical/high/medium/low)

        Returns:
            Numeric score on 0-100 scale
        """
        return PRIORITY_SCORE_MAP.get(priority, 60.0)

    def recommendation_to_idea(self, rec: dict) -> dict:
        """
        Convert a recommendation dict into an idea dict suitable for build spec generation.

        The idea_data stored in the priority queue must contain the fields
        expected by SpecGenerator (id, title, description, problem_statement,
        target_audience, artifact_type).

        Args:
            rec: Recommendation dictionary from get_pending_recommendations()

        Returns:
            Idea dictionary compatible with SpecGenerator
        """
        raw = rec.get("raw_json", {})
        description = raw.get("description", rec["title"])
        suggested_change = raw.get("suggested_change", "")

        # Build a combined description for the spec
        full_description = description
        if suggested_change:
            full_description = f"{description}\n\nSuggested change: {suggested_change}"

        # Map recommendation_type to artifact_type
        rec_type = rec.get("recommendation_type", "")
        if rec_type in ("pipeline_change", "infrastructure"):
            artifact_type = "tool"
        elif rec_type in ("claude_md_update", "persona_update"):
            artifact_type = "agent"
        else:
            artifact_type = "tool"

        return {
            "id": rec["recommendation_id"],
            "title": rec["title"],
            "description": full_description,
            "problem_statement": description,
            "target_audience": f"ST Metro ecosystem ({rec.get('target_system', 'general')})",
            "artifact_type": artifact_type,
            "weighted_score": self.priority_to_score(rec.get("priority", "medium")) / 10.0,
            "_source": "skylynx",
            "_recommendation_type": rec_type,
            "_scope": rec.get("scope", ""),
        }

    def mark_dispatched(self, recommendation_id: str) -> None:
        """
        Mark a recommendation as dispatched to the priority queue.

        Opens a separate writable connection.

        Args:
            recommendation_id: The recommendation_id to update
        """
        write_conn = sqlite3.connect(self.db_path)
        write_conn.execute("PRAGMA busy_timeout=5000")
        try:
            cursor = write_conn.cursor()
            cursor.execute("""
                UPDATE improvement_recommendations
                SET status = 'dispatched'
                WHERE recommendation_id = ?
            """, (recommendation_id,))
            write_conn.commit()
        finally:
            write_conn.close()
