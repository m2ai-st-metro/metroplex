"""
Metroplex State Database Manager
Manages metroplex.db SQLite database for tracking all decisions, jobs, patches, and cycles.
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import TriageDecision, BuildJob, PatchApplication, CycleResult, GateStatus, PriorityItem, PublishJob


class StateDB:
    """State database manager for Metroplex."""

    def __init__(self, db_path: str = "data/metroplex.db"):
        """Initialize database connection."""
        self.db_path = db_path

        # Create data directory if using file-based DB
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Connect to database."""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def init_db(self):
        """Initialize database schema."""
        self.connect()

        cursor = self.conn.cursor()

        # Triage decisions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS triage_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                weighted_score REAL NOT NULL,
                scaled_score REAL NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('approve', 'reject', 'defer')),
                reason TEXT NOT NULL DEFAULT '',
                decided_at TEXT NOT NULL
            )
        """)

        # Build jobs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS build_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                spec_path TEXT NOT NULL,
                queue_job_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'started', 'completed', 'failed')),
                queued_at TEXT NOT NULL
            )
        """)

        # Patch applications table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS patch_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patch_id TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                from_version TEXT,
                to_version TEXT,
                status TEXT NOT NULL CHECK(status IN ('applied', 'failed', 'skipped')),
                reason TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL
            )
        """)

        # Cycles table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL UNIQUE,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                triage_count INTEGER DEFAULT 0,
                build_count INTEGER DEFAULT 0,
                patch_count INTEGER DEFAULT 0,
                errors TEXT DEFAULT '[]'
            )
        """)

        # Gate status table (migrated to include 'publish' gate)
        # Check if existing table has old CHECK constraint missing 'publish'
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='gate_status'")
        gs_row = cursor.fetchone()
        if gs_row and "'publish'" not in (gs_row[0] or ""):
            # Migrate: recreate with updated CHECK constraint
            cursor.execute("ALTER TABLE gate_status RENAME TO gate_status_old")
            cursor.execute("""
                CREATE TABLE gate_status (
                    gate TEXT PRIMARY KEY CHECK(gate IN ('triage', 'build', 'patch', 'publish')),
                    consecutive_failures INTEGER DEFAULT 0,
                    halted INTEGER DEFAULT 0,
                    last_error TEXT
                )
            """)
            cursor.execute("INSERT INTO gate_status SELECT * FROM gate_status_old")
            cursor.execute("DROP TABLE gate_status_old")
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS gate_status (
                    gate TEXT PRIMARY KEY CHECK(gate IN ('triage', 'build', 'patch', 'publish')),
                    consecutive_failures INTEGER DEFAULT 0,
                    halted INTEGER DEFAULT 0,
                    last_error TEXT
                )
            """)

        # Priority queue table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS priority_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL CHECK(source IN ('ideaforge', 'skylynx', 'linear', 'academy')),
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                priority_score REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL CHECK(status IN ('pending', 'dispatched', 'completed', 'failed')) DEFAULT 'pending',
                idea_data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                dispatched_at TEXT,
                completed_at TEXT
            )
        """)

        # Publish jobs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS publish_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                build_job_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                repo_name TEXT NOT NULL,
                repo_url TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending', 'published', 'failed')),
                error TEXT,
                project_dir TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_triage_decisions_idea ON triage_decisions(idea_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_triage_decisions_decision ON triage_decisions(decision)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_build_jobs_status ON build_jobs(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cycles_started ON cycles(started_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_priority_queue_status ON priority_queue(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_priority_queue_score ON priority_queue(priority_score DESC)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_priority_queue_source ON priority_queue(source, source_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_publish_jobs_status ON publish_jobs(status)")

        # Initialize gate status for all gates
        for gate in ["triage", "build", "patch", "publish"]:
            cursor.execute("""
                INSERT OR IGNORE INTO gate_status (gate, consecutive_failures, halted)
                VALUES (?, 0, 0)
            """, (gate,))

        self.conn.commit()

    def record_triage_decision(self, decision: TriageDecision):
        """Record a triage decision."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO triage_decisions (idea_id, title, weighted_score, scaled_score, decision, reason, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            decision.idea_id,
            decision.title,
            decision.weighted_score,
            decision.scaled_score,
            decision.decision,
            decision.reason,
            decision.decided_at.isoformat()
        ))

        self.conn.commit()

    def record_build_job(self, job: BuildJob):
        """Record a build job."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            job.idea_id,
            job.title,
            job.spec_path,
            job.queue_job_id,
            job.status,
            job.queued_at.isoformat()
        ))

        self.conn.commit()

    def get_triaged_idea_ids(self) -> set[int]:
        """Return set of idea IDs that already have a triage decision."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT DISTINCT idea_id FROM triage_decisions")
        return {row[0] for row in cursor.fetchall()}

    def record_patch_application(self, patch: PatchApplication):
        """Record a patch application."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO patch_applications (patch_id, persona_id, from_version, to_version, status, reason, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            patch.patch_id,
            patch.persona_id,
            patch.from_version,
            patch.to_version,
            patch.status,
            patch.reason,
            patch.applied_at.isoformat()
        ))

        self.conn.commit()

    def start_cycle(self, cycle_id: str) -> CycleResult:
        """Start a new cycle."""
        self.connect()
        cursor = self.conn.cursor()

        started_at = datetime.now()

        cursor.execute("""
            INSERT INTO cycles (cycle_id, started_at, triage_count, build_count, patch_count, errors)
            VALUES (?, ?, 0, 0, 0, '[]')
        """, (cycle_id, started_at.isoformat()))

        self.conn.commit()

        return CycleResult(
            cycle_id=cycle_id,
            started_at=started_at,
            completed_at=None,
            triage_count=0,
            build_count=0,
            patch_count=0,
            errors=[]
        )

    def end_cycle(self, cycle_id: str, triage_count: int, build_count: int, patch_count: int, errors: list[str]):
        """End a cycle."""
        self.connect()
        cursor = self.conn.cursor()

        completed_at = datetime.now()

        cursor.execute("""
            UPDATE cycles
            SET completed_at = ?, triage_count = ?, build_count = ?, patch_count = ?, errors = ?
            WHERE cycle_id = ?
        """, (
            completed_at.isoformat(),
            triage_count,
            build_count,
            patch_count,
            json.dumps(errors),
            cycle_id
        ))

        self.conn.commit()

    def get_gate_status(self, gate: str) -> GateStatus:
        """Get status of a gate."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("SELECT * FROM gate_status WHERE gate = ?", (gate,))
        row = cursor.fetchone()

        if row:
            return GateStatus(
                gate=row["gate"],
                consecutive_failures=row["consecutive_failures"],
                halted=bool(row["halted"]),
                last_error=row["last_error"]
            )
        else:
            # Return default status if not found
            return GateStatus(gate=gate, consecutive_failures=0, halted=False)

    def update_gate_status(self, status: GateStatus):
        """Update gate status."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO gate_status (gate, consecutive_failures, halted, last_error)
            VALUES (?, ?, ?, ?)
        """, (
            status.gate,
            status.consecutive_failures,
            1 if status.halted else 0,
            status.last_error
        ))

        self.conn.commit()

    # --- Priority Queue ---

    def enqueue_item(self, item: PriorityItem) -> int:
        """Add an item to the priority queue. Returns the row ID. Skips duplicates."""
        self.connect()
        cursor = self.conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO priority_queue (source, source_id, title, description, priority_score, status, idea_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.source,
                item.source_id,
                item.title,
                item.description,
                item.priority_score,
                item.status,
                item.idea_data,
                item.created_at.isoformat()
            ))
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Duplicate (source, source_id) -- skip silently
            return 0

    def get_next_pending(self, sources: tuple[str, ...] | None = None) -> PriorityItem | None:
        """Get the highest-priority pending item, optionally filtered by source.

        Args:
            sources: If provided, only return items from these sources
                     (e.g. ("ideaforge", "linear") to exclude skylynx).
        """
        self.connect()
        cursor = self.conn.cursor()

        if sources:
            placeholders = ",".join("?" for _ in sources)
            cursor.execute(f"""
                SELECT * FROM priority_queue
                WHERE status = 'pending' AND source IN ({placeholders})
                ORDER BY priority_score DESC
                LIMIT 1
            """, sources)
        else:
            cursor.execute("""
                SELECT * FROM priority_queue
                WHERE status = 'pending'
                ORDER BY priority_score DESC
                LIMIT 1
            """)
        row = cursor.fetchone()

        if row:
            return PriorityItem(
                id=row["id"],
                source=row["source"],
                source_id=row["source_id"],
                title=row["title"],
                description=row["description"],
                priority_score=row["priority_score"],
                status=row["status"],
                idea_data=row["idea_data"],
                created_at=datetime.fromisoformat(row["created_at"]),
                dispatched_at=datetime.fromisoformat(row["dispatched_at"]) if row["dispatched_at"] else None,
                completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            )
        return None

    def update_item_status(self, item_id: int, status: str, timestamp_field: str | None = None):
        """Update a priority queue item's status and optional timestamp."""
        self.connect()
        cursor = self.conn.cursor()

        if timestamp_field in ("dispatched_at", "completed_at"):
            cursor.execute(f"""
                UPDATE priority_queue SET status = ?, {timestamp_field} = ? WHERE id = ?
            """, (status, datetime.now().isoformat(), item_id))
        else:
            cursor.execute("UPDATE priority_queue SET status = ? WHERE id = ?", (status, item_id))

        self.conn.commit()

    def get_queue_summary(self) -> dict:
        """Get priority queue summary counts by status."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT status, COUNT(*) as count FROM priority_queue GROUP BY status
        """)
        summary = {row["status"]: row["count"] for row in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) as total FROM priority_queue")
        summary["total"] = cursor.fetchone()["total"]

        return summary

    def update_build_job_status(self, queue_job_id: str, status: str) -> bool:
        """Update a build job's status by its queue_job_id.

        Supports all queue_job_id formats:
        - New:    'metroplex-{source}-{source_id}' (e.g. 'metroplex-linear-TOO-42')
        - Legacy: 'metroplex-{numeric_id}' (assumes ideaforge source)

        Also updates the corresponding priority_queue item if the status
        is terminal (completed/failed).

        Returns:
            True if the build_jobs row was actually changed (not already in this status).
        """
        self.connect()
        cursor = self.conn.cursor()

        # Only update rows not already in the target status
        cursor.execute(
            "UPDATE build_jobs SET status = ? WHERE queue_job_id = ? AND status != ?",
            (status, queue_job_id, status)
        )
        changed = cursor.rowcount > 0

        # Parse queue_job_id to extract source and source_id
        source = None
        source_id = None

        parts = queue_job_id.split("-", 2)  # Split into at most 3 parts

        if len(parts) >= 3 and parts[0] == "metroplex" and parts[1] in ("ideaforge", "skylynx", "linear"):
            # New format: metroplex-source-source_id (source_id may contain hyphens)
            source = parts[1]
            source_id = parts[2]
        elif len(parts) == 2 and parts[0] == "metroplex" and parts[1].isdigit():
            # Legacy format: metroplex-numeric_id (assume ideaforge)
            source = "ideaforge"
            source_id = parts[1]
        elif len(parts) >= 2 and parts[0] == "metroplex":
            # Fallback: try to find source_id in priority_queue by matching any source
            candidate_id = queue_job_id[len("metroplex-"):]
            cursor.execute(
                "SELECT source FROM priority_queue WHERE source_id = ? LIMIT 1",
                (candidate_id,)
            )
            row = cursor.fetchone()
            if row:
                source = row["source"]
                source_id = candidate_id

        if changed and source and source_id:
            pq_status = "completed" if status == "completed" else "failed"
            cursor.execute("""
                UPDATE priority_queue
                SET status = ?, completed_at = ?
                WHERE source = ? AND source_id = ?
            """, (pq_status, datetime.now().isoformat(), source, source_id))

        self.conn.commit()
        return changed

    # --- Publish Jobs ---

    def get_unpublished_builds(self) -> list[dict]:
        """Get completed builds that haven't been published yet.

        Returns distinct queue_job_ids with status='completed' that have
        no entry (or only 'failed' entries) in publish_jobs.
        """
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT DISTINCT b.queue_job_id, b.title
            FROM build_jobs b
            WHERE b.status = 'completed'
            AND b.queue_job_id NOT IN (
                SELECT build_job_id FROM publish_jobs WHERE status = 'published'
            )
        """)
        return [{"queue_job_id": row["queue_job_id"], "title": row["title"]} for row in cursor.fetchall()]

    def record_publish_job(self, job: PublishJob):
        """Record a publish job. Uses INSERT OR REPLACE to handle retries of failed jobs."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO publish_jobs
                (build_job_id, title, repo_name, repo_url, status, error, project_dir, created_at, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job.build_job_id,
            job.title,
            job.repo_name,
            job.repo_url,
            job.status,
            job.error,
            job.project_dir,
            job.created_at.isoformat(),
            job.published_at.isoformat() if job.published_at else None,
        ))

        self.conn.commit()

    def get_publish_summary(self) -> dict:
        """Get publish jobs summary counts by status."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("SELECT status, COUNT(*) as count FROM publish_jobs GROUP BY status")
        summary = {row["status"]: row["count"] for row in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) as total FROM publish_jobs")
        summary["total"] = cursor.fetchone()["total"]

        return summary

    def get_all_publish_jobs(self) -> list[dict]:
        """Get all publish jobs for display."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT build_job_id, title, repo_name, repo_url, status, error, project_dir, created_at, published_at
            FROM publish_jobs
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]
