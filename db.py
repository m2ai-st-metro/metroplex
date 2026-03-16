"""
Metroplex State Database Manager
Manages metroplex.db SQLite database for tracking all decisions, jobs, patches, and cycles.
"""
import sqlite3
import json
from datetime import datetime, timedelta
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
                publish_count INTEGER DEFAULT 0,
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
                completed_at TEXT,
                dispatch_task_id TEXT
            )
        """)

        # Migrate: add dispatch_task_id if missing (schema added after initial deploy)
        cursor.execute("PRAGMA table_info(priority_queue)")
        pq_columns = {row[1] for row in cursor.fetchall()}
        if "dispatch_task_id" not in pq_columns:
            cursor.execute("ALTER TABLE priority_queue ADD COLUMN dispatch_task_id TEXT")

        # Migrate: add project_dir to build_jobs (UM bridge writes actual dir path)
        cursor.execute("PRAGMA table_info(build_jobs)")
        bj_columns = {row[1] for row in cursor.fetchall()}
        if "project_dir" not in bj_columns:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN project_dir TEXT")

        # Migrate: add review_status to build_jobs (Phase 13c review gate)
        if "review_status" not in bj_columns:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN review_status TEXT DEFAULT NULL")

        # Migrate: add retry columns to build_jobs (Phase 13f auto-retry)
        if "retry_count" not in bj_columns:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "next_retry_at" not in bj_columns:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN next_retry_at TEXT DEFAULT NULL")

        # Migrate: add quality_score to build_jobs (Phase 14b structural quality)
        if "quality_score" not in bj_columns:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN quality_score REAL DEFAULT NULL")

        # Migrate: add publish_count to cycles if missing
        cursor.execute("PRAGMA table_info(cycles)")
        cy_columns = {row[1] for row in cursor.fetchall()}
        if "publish_count" not in cy_columns:
            cursor.execute("ALTER TABLE cycles ADD COLUMN publish_count INTEGER DEFAULT 0")

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

        # Migrate: add estimated_cost to build_jobs
        cursor.execute("PRAGMA table_info(build_jobs)")
        bj_columns_refresh = {row[1] for row in cursor.fetchall()}
        if "estimated_cost" not in bj_columns_refresh:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN estimated_cost REAL DEFAULT NULL")

        # Cost ledger table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0.0,
                queue_job_id TEXT,
                details TEXT DEFAULT '{}'
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cost_ledger_timestamp ON cost_ledger(timestamp)")

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

    def get_triaged_idea_ids(self, decisions: tuple[str, ...] | None = None) -> set[int]:
        """Return set of idea IDs that already have a triage decision.

        Args:
            decisions: If provided, only return IDs with these decision types.
                       e.g. ("approve", "reject") to exclude deferred ideas
                       from the filter, allowing them to be re-triaged.
                       If None, returns all triaged idea IDs (legacy behavior).
        """
        self.connect()
        cursor = self.conn.cursor()
        if decisions:
            placeholders = ",".join("?" for _ in decisions)
            cursor.execute(
                f"SELECT DISTINCT idea_id FROM triage_decisions WHERE decision IN ({placeholders})",
                decisions,
            )
        else:
            cursor.execute("SELECT DISTINCT idea_id FROM triage_decisions")
        return {row[0] for row in cursor.fetchall()}

    def get_approved_titles(self) -> list[str]:
        """Get titles of all approved ideas (for dedup checking)."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT DISTINCT title FROM triage_decisions WHERE decision = 'approve'"
        )
        return [row[0] for row in cursor.fetchall()]

    def get_deferral_count(self, idea_id: int) -> int:
        """Count how many times an idea has been deferred."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM triage_decisions WHERE idea_id = ? AND decision = 'defer'",
            (idea_id,),
        )
        return cursor.fetchone()[0]

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

    def end_cycle(self, cycle_id: str, triage_count: int, build_count: int, patch_count: int, errors: list[str], publish_count: int = 0):
        """End a cycle."""
        self.connect()
        cursor = self.conn.cursor()

        completed_at = datetime.now()

        cursor.execute("""
            UPDATE cycles
            SET completed_at = ?, triage_count = ?, build_count = ?, patch_count = ?, publish_count = ?, errors = ?
            WHERE cycle_id = ?
        """, (
            completed_at.isoformat(),
            triage_count,
            build_count,
            patch_count,
            publish_count,
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

    def set_dispatch_task_id(self, item_id: int, task_id: str):
        """Store the ClaudeClaw dispatch_queue task ID on a priority queue item."""
        self.connect()
        self.conn.execute(
            "UPDATE priority_queue SET dispatch_task_id = ? WHERE id = ?",
            (task_id, item_id),
        )
        self.conn.commit()

    def get_dispatched_items(self, sources: tuple[str, ...] | None = None) -> list[dict]:
        """Get all priority_queue items with status='dispatched' and a dispatch_task_id.

        Args:
            sources: If provided, only return items from these sources.

        Returns:
            List of dicts with id, source, source_id, dispatch_task_id.
        """
        self.connect()
        cursor = self.conn.cursor()

        if sources:
            placeholders = ",".join("?" for _ in sources)
            cursor.execute(f"""
                SELECT id, source, source_id, dispatch_task_id
                FROM priority_queue
                WHERE status = 'dispatched'
                  AND dispatch_task_id IS NOT NULL
                  AND source IN ({placeholders})
            """, sources)
        else:
            cursor.execute("""
                SELECT id, source, source_id, dispatch_task_id
                FROM priority_queue
                WHERE status = 'dispatched'
                  AND dispatch_task_id IS NOT NULL
            """)

        return [dict(row) for row in cursor.fetchall()]

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

        # Discover and set project_dir if not already set
        if status in ("completed", "failed"):
            self._backfill_project_dir(cursor, queue_job_id)

        # Parse queue_job_id to extract source and source_id
        source = None
        source_id = None

        parts = queue_job_id.split("-", 2)  # Split into at most 3 parts

        if len(parts) >= 3 and parts[0] == "metroplex" and parts[1] in ("ideaforge", "skylynx", "linear", "academy"):
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

    def update_build_job_project_dir(self, queue_job_id: str, project_dir: str) -> bool:
        """Store the actual project directory path on a build job.

        Called by UM bridge worker after build completes, so the publish
        gate can find the output regardless of naming convention.
        """
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET project_dir = ? WHERE queue_job_id = ?",
            (project_dir, queue_job_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def reset_build_for_retry(self, queue_job_id: str) -> bool:
        """Reset a failed build so it can be re-dispatched.

        Resets the latest build_jobs entry from 'failed' to 'queued' and
        the priority_queue entry from 'failed' to 'pending'.

        Returns:
            True if the build was found and reset.
        """
        self.connect()
        cursor = self.conn.cursor()

        # Reset the latest build_jobs entry for this queue_job_id
        cursor.execute(
            "UPDATE build_jobs SET status = 'queued', project_dir = NULL "
            "WHERE id = (SELECT MAX(id) FROM build_jobs WHERE queue_job_id = ? AND status = 'failed')",
            (queue_job_id,),
        )
        if cursor.rowcount == 0:
            return False

        # Parse queue_job_id to find the priority_queue entry
        parts = queue_job_id.split("-", 2)
        source = None
        source_id = None

        if len(parts) >= 3 and parts[0] == "metroplex" and parts[1] in ("ideaforge", "skylynx", "linear", "academy"):
            source = parts[1]
            source_id = parts[2]
        elif len(parts) == 2 and parts[0] == "metroplex" and parts[1].isdigit():
            source = "ideaforge"
            source_id = parts[1]

        if source and source_id:
            cursor.execute(
                "UPDATE priority_queue SET status = 'pending', completed_at = NULL "
                "WHERE source = ? AND source_id = ? AND status = 'failed'",
                (source, source_id),
            )

        self.conn.commit()
        return True

    def _backfill_project_dir(self, cursor, queue_job_id: str) -> None:
        """Discover and set project_dir on a build_job if not already set.

        Searches YCE generations directory for both naming conventions:
        1. Directory named after queue_job_id (e.g., metroplex-ideaforge-43/)
        2. um-{title}-{uuid} pattern (UM bridge naming)
        """
        from pathlib import Path as _Path

        # Check if project_dir already set
        cursor.execute(
            "SELECT project_dir FROM build_jobs WHERE queue_job_id = ?",
            (queue_job_id,),
        )
        row = cursor.fetchone()
        if not row or (row["project_dir"] and row["project_dir"].strip()):
            return

        yce_generations = _Path(__file__).parent.parent / "yce-harness" / "generations"
        if not yce_generations.is_dir():
            return

        # Convention 1: directory named after queue_job_id
        candidate = yce_generations / queue_job_id
        if candidate.is_dir():
            cursor.execute(
                "UPDATE build_jobs SET project_dir = ? WHERE queue_job_id = ?",
                (str(candidate), queue_job_id),
            )
            return

        # Convention 2: scan for any directory containing the queue_job_id as substring
        for entry in yce_generations.iterdir():
            if entry.is_dir() and queue_job_id in entry.name:
                cursor.execute(
                    "UPDATE build_jobs SET project_dir = ? WHERE queue_job_id = ?",
                    (str(entry), queue_job_id),
                )
                return

    # --- Review Gate (Phase 13c) ---

    def get_reviewable_builds(self) -> list[dict]:
        """Get completed builds that haven't been reviewed yet.

        Returns builds with status='completed' and review_status IS NULL.
        """
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT DISTINCT b.queue_job_id, b.title, b.project_dir
            FROM build_jobs b
            WHERE b.status = 'completed'
            AND (b.review_status IS NULL OR b.review_status = '')
        """)
        return [
            {"queue_job_id": row["queue_job_id"], "title": row["title"], "project_dir": row["project_dir"]}
            for row in cursor.fetchall()
        ]

    def get_build_by_queue_job_id(self, queue_job_id: str) -> dict | None:
        """Get a build job by its queue_job_id."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM build_jobs WHERE queue_job_id = ? ORDER BY id DESC LIMIT 1",
            (queue_job_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_build_review_status(self, queue_job_id: str, review_status: str) -> bool:
        """Set the review_status on a completed build.

        Args:
            queue_job_id: Build job queue ID
            review_status: 'reviewed' (passed) or 'review_failed'

        Returns:
            True if row was updated.
        """
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET review_status = ? WHERE queue_job_id = ? AND status = 'completed'",
            (review_status, queue_job_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # --- Build Retry (Phase 13f) ---

    MAX_RETRIES = 3
    RETRY_BACKOFF_MINUTES = [5, 20, 60]  # Exponential-ish backoff

    def get_retryable_builds(self) -> list[dict]:
        """Get failed builds eligible for automatic retry.

        A build is retryable if:
        - status = 'failed'
        - retry_count < MAX_RETRIES
        - next_retry_at <= now (or next_retry_at is NULL for first retry)
        """
        self.connect()
        cursor = self.conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute("""
            SELECT queue_job_id, title, idea_id, retry_count
            FROM build_jobs
            WHERE status = 'failed'
            AND (retry_count IS NULL OR retry_count < ?)
            AND (next_retry_at IS NULL OR next_retry_at <= ?)
            AND id IN (
                SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id
            )
        """, (self.MAX_RETRIES, now))
        return [dict(row) for row in cursor.fetchall()]

    def mark_build_for_retry(self, queue_job_id: str) -> bool:
        """Reset a failed build for retry, incrementing retry_count and setting next_retry_at.

        Returns:
            True if the build was reset for retry.
        """
        self.connect()
        cursor = self.conn.cursor()

        # Get current retry count
        cursor.execute(
            "SELECT retry_count FROM build_jobs "
            "WHERE queue_job_id = ? AND status = 'failed' ORDER BY id DESC LIMIT 1",
            (queue_job_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False

        current_count = row["retry_count"] or 0
        if current_count >= self.MAX_RETRIES:
            return False

        new_count = current_count + 1
        backoff_idx = min(new_count - 1, len(self.RETRY_BACKOFF_MINUTES) - 1)
        backoff_minutes = self.RETRY_BACKOFF_MINUTES[backoff_idx]
        next_retry = (datetime.now() + timedelta(minutes=backoff_minutes)).isoformat()

        # Reset status to queued, bump retry_count, set next_retry_at
        cursor.execute(
            "UPDATE build_jobs SET status = 'queued', retry_count = ?, next_retry_at = ?, project_dir = NULL "
            "WHERE id = (SELECT MAX(id) FROM build_jobs WHERE queue_job_id = ? AND status = 'failed')",
            (new_count, next_retry, queue_job_id),
        )
        build_updated = cursor.rowcount > 0

        # Also reset priority_queue entry
        parts = queue_job_id.split("-", 2)
        source = None
        source_id = None
        if len(parts) >= 3 and parts[0] == "metroplex" and parts[1] in ("ideaforge", "skylynx", "linear", "academy"):
            source = parts[1]
            source_id = parts[2]
        elif len(parts) == 2 and parts[0] == "metroplex" and parts[1].isdigit():
            source = "ideaforge"
            source_id = parts[1]

        if source and source_id:
            cursor.execute(
                "UPDATE priority_queue SET status = 'pending', completed_at = NULL "
                "WHERE source = ? AND source_id = ? AND status = 'failed'",
                (source, source_id),
            )

        self.conn.commit()
        return build_updated

    # --- Publish Jobs ---

    def get_unpublished_builds(self, require_review: bool = True) -> list[dict]:
        """Get builds that haven't been published yet.

        Args:
            require_review: If True (default, L5 strict mode), only return builds
                with review_status='reviewed'. If False, also include builds with
                NULL review_status for backward compatibility.

        Returns distinct queue_job_ids with status='completed' that have no
        'published' entry in publish_jobs.
        """
        self.connect()
        cursor = self.conn.cursor()

        if require_review:
            cursor.execute("""
                SELECT DISTINCT b.queue_job_id, b.title, b.project_dir
                FROM build_jobs b
                WHERE b.status = 'completed'
                AND b.review_status = 'reviewed'
                AND b.queue_job_id NOT IN (
                    SELECT build_job_id FROM publish_jobs WHERE status = 'published'
                )
            """)
        else:
            cursor.execute("""
                SELECT DISTINCT b.queue_job_id, b.title, b.project_dir
                FROM build_jobs b
                WHERE b.status = 'completed'
                AND (b.review_status = 'reviewed' OR b.review_status IS NULL)
                AND b.queue_job_id NOT IN (
                    SELECT build_job_id FROM publish_jobs WHERE status = 'published'
                )
            """)
        return [{"queue_job_id": row["queue_job_id"], "title": row["title"], "project_dir": row["project_dir"]} for row in cursor.fetchall()]

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

    # --- Cost Ledger (Phase 13e) ---

    def record_cost(
        self,
        source: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        queue_job_id: str | None = None,
        details: str = "{}",
    ) -> int:
        """Record a cost entry in the ledger.

        Args:
            source: Component that incurred the cost (e.g. 'spec_expander', 'um_bridge')
            model: Model name used
            input_tokens: Input token count
            output_tokens: Output token count
            estimated_cost: Estimated cost in USD
            queue_job_id: Optional build job ID for correlation
            details: JSON string with additional context

        Returns:
            Row ID of the inserted record.
        """
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO cost_ledger (timestamp, source, model, input_tokens, output_tokens, estimated_cost, queue_job_id, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            source,
            model,
            input_tokens,
            output_tokens,
            estimated_cost,
            queue_job_id,
            details,
        ))
        self.conn.commit()
        return cursor.lastrowid

    def get_daily_spend(self, date: str | None = None) -> float:
        """Get total spend for a given date (YYYY-MM-DD). Defaults to today."""
        self.connect()
        cursor = self.conn.cursor()
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0.0) FROM cost_ledger WHERE timestamp LIKE ?",
            (f"{date}%",),
        )
        return cursor.fetchone()[0]

    def get_monthly_spend(self, month: str | None = None) -> float:
        """Get total spend for a given month (YYYY-MM). Defaults to current month."""
        self.connect()
        cursor = self.conn.cursor()
        if month is None:
            month = datetime.now().strftime("%Y-%m")
        cursor.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0.0) FROM cost_ledger WHERE timestamp LIKE ?",
            (f"{month}%",),
        )
        return cursor.fetchone()[0]

    def get_cost_breakdown(self, days: int = 7) -> list[dict]:
        """Get daily cost breakdown for the last N days.

        Returns list of dicts with keys: date, total_cost, entry_count.
        """
        self.connect()
        cursor = self.conn.cursor()
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor.execute("""
            SELECT
                SUBSTR(timestamp, 1, 10) as date,
                SUM(estimated_cost) as total_cost,
                COUNT(*) as entry_count
            FROM cost_ledger
            WHERE timestamp >= ?
            GROUP BY SUBSTR(timestamp, 1, 10)
            ORDER BY date DESC
        """, (start_date,))
        return [dict(row) for row in cursor.fetchall()]

    def update_build_quality_score(self, queue_job_id: str, quality_score: float) -> bool:
        """Set quality_score on a build job (Phase 14b)."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET quality_score = ? WHERE queue_job_id = ?",
            (quality_score, queue_job_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def update_build_estimated_cost(self, queue_job_id: str, estimated_cost: float) -> bool:
        """Set estimated_cost on a build job."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET estimated_cost = ? WHERE queue_job_id = ?",
            (estimated_cost, queue_job_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0
