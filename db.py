"""
Metroplex State Database Manager
Manages metroplex.db SQLite database for tracking all decisions, jobs, patches, and cycles.
"""
import re
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from models import TriageDecision, BuildJob, PatchApplication, AgentPatchApplication, CycleResult, GateStatus, PriorityItem, PublishJob


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
        """Connect to database.

        Forces a WAL checkpoint on each connect call to ensure the connection
        sees writes committed by other processes (e.g., previous Metroplex runs).
        Without this, WAL snapshot isolation can make completed builds invisible
        to long-lived connections.
        """
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
            # Set busy_timeout on every new connection so concurrent access
            # retries instead of immediately raising "database is locked".
            self.conn.execute("PRAGMA busy_timeout=5000")
        # Force WAL checkpoint so this connection sees all committed writes
        try:
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass  # Not in WAL mode or DB not yet initialized

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def init_db(self):
        """Initialize database schema."""
        self.connect()

        # Enable WAL mode and busy_timeout for concurrent access safety.
        # WAL allows concurrent readers + one writer without "database is locked".
        # busy_timeout tells SQLite to retry for up to 5 seconds before failing.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

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
                queued_at TEXT NOT NULL,
                completed_at TEXT
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

        # Agent patch applications table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_patch_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patch_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                target TEXT NOT NULL,
                section TEXT NOT NULL,
                operation TEXT NOT NULL,
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

        # Migrate: add claimed_by/claimed_at for atomic checkout (Paperclip pattern)
        if "claimed_by" not in pq_columns:
            cursor.execute("ALTER TABLE priority_queue ADD COLUMN claimed_by TEXT DEFAULT NULL")
        if "claimed_at" not in pq_columns:
            cursor.execute("ALTER TABLE priority_queue ADD COLUMN claimed_at TEXT DEFAULT NULL")
        # Migrate: add strategic_theme for goal traceability (Paperclip pattern)
        if "strategic_theme" not in pq_columns:
            cursor.execute("ALTER TABLE priority_queue ADD COLUMN strategic_theme TEXT DEFAULT NULL")

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

        # Migrate: add base_job_id to build_jobs (attempt-grouping for retry dedup)
        if "base_job_id" not in bj_columns:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN base_job_id TEXT DEFAULT NULL")
            # Backfill: no suffixed IDs exist yet, so base = queue_job_id
            cursor.execute("UPDATE build_jobs SET base_job_id = queue_job_id WHERE base_job_id IS NULL")

        # Migrate: add a2a_task_id to build_jobs (Phase E A2A dispatch tracking)
        if "a2a_task_id" not in bj_columns:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN a2a_task_id TEXT DEFAULT NULL")

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

        # Build postmortems table (L5 B1: structured failure capture)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS build_postmortems (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_job_id TEXT NOT NULL UNIQUE,
                idea_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                failure_category TEXT NOT NULL,
                failure_stage TEXT,
                error_signature TEXT,
                spec_path TEXT,
                idea_weighted_score REAL,
                idea_artifact_type TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_postmortems_category ON build_postmortems(failure_category)")

        # Feasibility predictions table (L5 B2: pre-build feasibility scoring)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feasibility_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_job_id TEXT NOT NULL,
                feasibility_score REAL NOT NULL,
                predicted_outcome TEXT NOT NULL,
                actual_outcome TEXT,
                correct INTEGER,
                feature_weights TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feasibility_queue_job ON feasibility_predictions(queue_job_id)")

        # Migrate: add feasibility_score to build_jobs (L5 B2)
        cursor.execute("PRAGMA table_info(build_jobs)")
        bj_columns_b2 = {row[1] for row in cursor.fetchall()}
        if "feasibility_score" not in bj_columns_b2:
            cursor.execute("ALTER TABLE build_jobs ADD COLUMN feasibility_score REAL DEFAULT NULL")

        # Migrate: add test_ratio to build_jobs (L5 D2 — test coverage enforcement)
        if "test_ratio" not in bj_columns_b2:
            try:
                cursor.execute("ALTER TABLE build_jobs ADD COLUMN test_ratio REAL DEFAULT NULL")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Migrate: add strategic_theme to build_jobs (Paperclip goal traceability)
        if "strategic_theme" not in bj_columns_b2:
            try:
                cursor.execute("ALTER TABLE build_jobs ADD COLUMN strategic_theme TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Migrate: add completed_at to build_jobs (review grace period — race condition fix)
        if "completed_at" not in bj_columns_b2:
            try:
                cursor.execute("ALTER TABLE build_jobs ADD COLUMN completed_at TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Readme jobs table (Gate 4.7 — enhanced README generation tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS readme_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publish_job_id TEXT,
                build_job_id TEXT,
                repo_url TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_readme_jobs_build ON readme_jobs(build_job_id)")

        # Readiness jobs table (Gate 4.9 — publish readiness checks + auto-fixes)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS readiness_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                build_job_id TEXT,
                repo_name TEXT NOT NULL,
                repo_url TEXT,
                status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'completed', 'partial', 'failed')),
                checks_passed TEXT DEFAULT '[]',
                checks_failed TEXT DEFAULT '[]',
                fixes_applied TEXT DEFAULT '[]',
                fixes_failed TEXT DEFAULT '[]',
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_readiness_jobs_build ON readiness_jobs(build_job_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_readiness_jobs_repo ON readiness_jobs(repo_name)")

        # Build sessions table (Paperclip pattern: session compaction between retries)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS build_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_job_id TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                session_summary TEXT NOT NULL DEFAULT '',
                input_tokens_total INTEGER DEFAULT 0,
                output_tokens_total INTEGER DEFAULT 0,
                session_age_seconds INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(base_job_id, attempt)
            )
        """)

        # Budget events table (Paperclip pattern: hard-stop audit trail)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS budget_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL CHECK(event_type IN ('hard_stop', 'warning', 'resume')),
                trigger TEXT NOT NULL,
                daily_spend REAL,
                monthly_spend REAL,
                builds_killed INTEGER DEFAULT 0,
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_build_jobs_base_job_id ON build_jobs(base_job_id)")

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
        """Record a build job.

        Inherits retry_count from previous attempts for the same queue_job_id
        to prevent infinite retry loops when new rows are inserted.
        """
        self.connect()
        cursor = self.conn.cursor()

        # Derive base_job_id by stripping any -rN retry suffix
        base_job_id = re.sub(r'-r\d+$', '', job.queue_job_id)

        # Carry forward retry count from any previous attempt
        cursor.execute(
            "SELECT MAX(retry_count) FROM build_jobs WHERE base_job_id = ?",
            (base_job_id,),
        )
        row = cursor.fetchone()
        inherited_retry = (row[0] or 0) if row and row[0] is not None else 0

        cursor.execute("""
            INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at, retry_count, base_job_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job.idea_id,
            job.title,
            job.spec_path,
            job.queue_job_id,
            job.status,
            job.queued_at.isoformat(),
            inherited_retry,
            base_job_id,
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

    def record_agent_patch_application(self, patch: AgentPatchApplication):
        """Record an agent patch application."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO agent_patch_applications
            (patch_id, agent_id, target, section, operation, status, reason, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            patch.patch_id,
            patch.agent_id,
            patch.target,
            patch.section,
            patch.operation,
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
                INSERT INTO priority_queue (source, source_id, title, description, priority_score, status, idea_data, created_at, strategic_theme)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.source,
                item.source_id,
                item.title,
                item.description,
                item.priority_score,
                item.status,
                item.idea_data,
                item.created_at.isoformat(),
                item.strategic_theme,
            ))
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Duplicate (source, source_id) -- skip silently
            return 0

    def get_next_pending(self, sources: tuple[str, ...] | None = None) -> PriorityItem | None:
        """Get the highest-priority pending item, optionally filtered by source.

        NOTE: This is a read-only query. For concurrent-safe dispatch, use
        claim_next_pending() instead which atomically claims the item.

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
                claimed_by=row["claimed_by"] if "claimed_by" in row.keys() else None,
                claimed_at=row["claimed_at"] if "claimed_at" in row.keys() else None,
            )
        return None

    def claim_next_pending(self, claimer_id: str, sources: tuple[str, ...] | None = None) -> PriorityItem | None:
        """Atomically claim the highest-priority pending item.

        Uses UPDATE...WHERE claimed_by IS NULL to prevent double-claiming.
        Returns the claimed item, or None if nothing available or contention.

        Args:
            claimer_id: Identifier for the claiming process (e.g. "build-12345")
            sources: If provided, only claim from these sources.
        """
        self.connect()
        cursor = self.conn.cursor()

        # Step 1: Find candidate
        if sources:
            placeholders = ",".join("?" for _ in sources)
            cursor.execute(f"""
                SELECT id FROM priority_queue
                WHERE status = 'pending' AND (claimed_by IS NULL) AND source IN ({placeholders})
                ORDER BY priority_score DESC
                LIMIT 1
            """, sources)
        else:
            cursor.execute("""
                SELECT id FROM priority_queue
                WHERE status = 'pending' AND (claimed_by IS NULL)
                ORDER BY priority_score DESC
                LIMIT 1
            """)

        row = cursor.fetchone()
        if not row:
            return None

        candidate_id = row["id"]
        now = datetime.now().isoformat()

        # Step 2: Atomic claim — only succeeds if still unclaimed and pending
        cursor.execute("""
            UPDATE priority_queue
            SET claimed_by = ?, claimed_at = ?, status = 'dispatched', dispatched_at = ?
            WHERE id = ? AND status = 'pending' AND claimed_by IS NULL
        """, (claimer_id, now, now, candidate_id))
        self.conn.commit()

        if cursor.rowcount == 0:
            # Contention: another process claimed it between SELECT and UPDATE
            return None

        # Step 3: Re-read the full row to return
        cursor.execute("SELECT * FROM priority_queue WHERE id = ?", (candidate_id,))
        claimed_row = cursor.fetchone()
        if not claimed_row:
            return None

        return PriorityItem(
            id=claimed_row["id"],
            source=claimed_row["source"],
            source_id=claimed_row["source_id"],
            title=claimed_row["title"],
            description=claimed_row["description"],
            priority_score=claimed_row["priority_score"],
            status=claimed_row["status"],
            idea_data=claimed_row["idea_data"],
            created_at=datetime.fromisoformat(claimed_row["created_at"]),
            dispatched_at=datetime.fromisoformat(claimed_row["dispatched_at"]) if claimed_row["dispatched_at"] else None,
            completed_at=datetime.fromisoformat(claimed_row["completed_at"]) if claimed_row["completed_at"] else None,
            claimed_by=claimed_row["claimed_by"],
            claimed_at=claimed_row["claimed_at"],
        )

    def release_claim(self, item_id: int) -> None:
        """Release a claimed item back to pending status.

        Used for cleanup when processing fails after claiming.
        """
        self.connect()
        self.conn.execute("""
            UPDATE priority_queue
            SET claimed_by = NULL, claimed_at = NULL, status = 'pending', dispatched_at = NULL
            WHERE id = ?
        """, (item_id,))
        self.conn.commit()

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
        if status in ("completed", "failed"):
            completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                "UPDATE build_jobs SET status = ?, completed_at = ? WHERE queue_job_id = ? AND status != ?",
                (status, completed_at, queue_job_id, status)
            )
        else:
            cursor.execute(
                "UPDATE build_jobs SET status = ? WHERE queue_job_id = ? AND status != ?",
                (status, queue_job_id, status)
            )
        changed = cursor.rowcount > 0

        # Discover and set project_dir if not already set
        if status in ("completed", "failed"):
            self._backfill_project_dir(cursor, queue_job_id)

        # Parse queue_job_id to extract source and source_id
        # Strip -rN retry suffix before parsing
        base_id = re.sub(r'-r\d+$', '', queue_job_id)
        source = None
        source_id = None

        parts = base_id.split("-", 2)  # Split into at most 3 parts

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
            candidate_id = base_id[len("metroplex-"):]
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
        # Strip -rN retry suffix before parsing
        base_id = re.sub(r'-r\d+$', '', queue_job_id)
        parts = base_id.split("-", 2)
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
                "UPDATE priority_queue SET status = 'pending', completed_at = NULL, "
                "claimed_by = NULL, claimed_at = NULL "
                "WHERE source = ? AND source_id = ? AND status IN ('failed', 'completed')",
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
            AND (b.completed_at IS NULL OR b.completed_at <= datetime('now', '-5 minutes'))
        """)
        return [
            {"queue_job_id": row["queue_job_id"], "title": row["title"], "project_dir": row["project_dir"]}
            for row in cursor.fetchall()
        ]

    def has_completed_build(self, queue_job_id: str) -> bool:
        """Check if a completed build exists for the given job ID (any attempt suffix)."""
        self.connect()
        cursor = self.conn.cursor()
        base_job_id = re.sub(r'-r\d+$', '', queue_job_id)
        cursor.execute(
            "SELECT COUNT(*) FROM build_jobs WHERE base_job_id = ? AND status = 'completed'",
            (base_job_id,),
        )
        return cursor.fetchone()[0] > 0

    def has_exhausted_retries(self, queue_job_id: str) -> bool:
        """Check if all retry attempts have been used for the given job ID (any attempt suffix).

        Uses COUNT of failed build rows as a hard cap. This is more reliable
        than MAX(retry_count) which can drift when multiple rows are created
        per retry cycle (the bug that caused idea-115's infinite loop).
        """
        self.connect()
        cursor = self.conn.cursor()
        base_job_id = re.sub(r'-r\d+$', '', queue_job_id)
        cursor.execute(
            "SELECT COUNT(*) FROM build_jobs WHERE base_job_id = ? AND status = 'failed'",
            (base_job_id,),
        )
        failed_count = cursor.fetchone()[0]
        return failed_count >= self.MAX_RETRIES

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

    # --- Stale Queued Build Detection ---

    STALE_QUEUED_THRESHOLD_MINUTES = 30

    def get_stale_queued_builds(self) -> list[dict]:
        """Get builds stuck in 'queued' status past the staleness threshold.

        A build is stale-queued when:
        - build_jobs.status = 'queued'
        - queued_at is older than STALE_QUEUED_THRESHOLD_MINUTES ago
        - The corresponding priority_queue item has status = 'dispatched'
          (meaning the dispatch loop won't re-pick it up)

        Returns:
            List of dicts with queue_job_id, idea_id, title, queued_at,
            source, source_id, priority_queue_id.
        """
        self.connect()
        cursor = self.conn.cursor()
        cutoff = (
            datetime.now() - timedelta(minutes=self.STALE_QUEUED_THRESHOLD_MINUTES)
        ).isoformat()

        cursor.execute("""
            SELECT b.queue_job_id, b.idea_id, b.title, b.queued_at,
                   pq.id AS priority_queue_id, pq.source, pq.source_id
            FROM build_jobs b
            JOIN priority_queue pq
              ON pq.source_id = CAST(b.idea_id AS TEXT)
              AND pq.status = 'dispatched'
            WHERE b.status = 'queued'
              AND b.queued_at < ?

            UNION

            SELECT b.queue_job_id, b.idea_id, b.title, b.queued_at,
                   pq.id AS priority_queue_id, pq.source, pq.source_id
            FROM build_jobs b
            JOIN priority_queue pq
              ON pq.source_id = CAST(b.idea_id AS TEXT)
              AND pq.status = 'pending'
              AND pq.claimed_by IS NOT NULL
            WHERE b.status = 'queued'
              AND b.queued_at < ?
        """, (cutoff, cutoff))
        return [dict(row) for row in cursor.fetchall()]

    def reset_stale_queued_build(self, queue_job_id: str, priority_queue_id: int):
        """Reset a stale queued build so it can be re-dispatched.

        Deletes the stale build_jobs row and resets the priority_queue
        item back to 'pending'.
        """
        self.connect()
        self.conn.execute(
            "DELETE FROM build_jobs WHERE queue_job_id = ? AND status = 'queued'",
            (queue_job_id,),
        )
        self.conn.execute(
            "UPDATE priority_queue SET status = 'pending', dispatched_at = NULL "
            "WHERE id = ? AND status = 'dispatched'",
            (priority_queue_id,),
        )
        self.conn.commit()

    # --- Build Retry (Phase 13f) ---

    MAX_RETRIES = 3
    RETRY_BACKOFF_MINUTES = [5, 20, 60]  # Exponential-ish backoff

    def count_failed_builds(self, base_job_id: str) -> int:
        """Count failed build attempts for a base job ID (any suffix)."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM build_jobs "
            "WHERE base_job_id = ? AND status = 'failed'",
            (base_job_id,),
        )
        return cursor.fetchone()[0]

    def get_retryable_builds(self) -> list[dict]:
        """Get failed builds eligible for automatic retry.

        A build is retryable if:
        - The latest row for this queue_job_id has status = 'failed'
        - Total failed rows for this queue_job_id < MAX_RETRIES
        - next_retry_at <= now (or next_retry_at is NULL for first retry)

        Uses COUNT of failed rows as the hard cap instead of retry_count,
        which can drift when run_from_queue creates new rows per retry cycle.
        """
        self.connect()
        cursor = self.conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute("""
            SELECT b.queue_job_id, b.base_job_id, b.title, b.idea_id, b.retry_count
            FROM build_jobs b
            INNER JOIN (
                SELECT base_job_id, MAX(id) AS max_id
                FROM build_jobs
                GROUP BY base_job_id
            ) latest ON b.id = latest.max_id
            WHERE b.status = 'failed'
            AND COALESCE(b.next_retry_at, '') != 'abandoned'
            AND (b.next_retry_at IS NULL OR b.next_retry_at <= ?)
            AND (
                SELECT COUNT(*) FROM build_jobs b2
                WHERE b2.base_job_id = b.base_job_id AND b2.status = 'failed'
            ) < ?
        """, (now, self.MAX_RETRIES))
        return [dict(row) for row in cursor.fetchall()]

    def get_exhausted_builds(self) -> list[dict]:
        """Get failed builds that have exhausted all retries but haven't been abandoned yet.

        Returns builds where:
        - The latest row has status = 'failed'
        - Total failed rows for this base_job_id >= MAX_RETRIES
        - No 'completed' build exists for this base_job_id
        - The build hasn't already been flagged as abandoned (next_retry_at = 'abandoned')
        """
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT b.queue_job_id, b.base_job_id, b.title, b.idea_id, b.retry_count
            FROM build_jobs b
            INNER JOIN (
                SELECT base_job_id, MAX(id) AS max_id
                FROM build_jobs
                GROUP BY base_job_id
            ) latest ON b.id = latest.max_id
            WHERE b.status = 'failed'
            AND COALESCE(b.next_retry_at, '') != 'abandoned'
            AND (
                SELECT COUNT(*) FROM build_jobs b2
                WHERE b2.base_job_id = b.base_job_id AND b2.status = 'failed'
            ) >= ?
            AND NOT EXISTS (
                SELECT 1 FROM build_jobs b3
                WHERE b3.base_job_id = b.base_job_id AND b3.status = 'completed'
            )
        """, (self.MAX_RETRIES,))
        return [dict(row) for row in cursor.fetchall()]

    def mark_build_for_retry(self, queue_job_id: str) -> bool:
        """Flag a failed build for retry by setting next_retry_at with backoff.

        Does NOT reset priority_queue — the orchestrator handles re-dispatch
        only when the backoff timer expires (via get_retryable_builds).
        This prevents the dual-path bug where priority_queue reset bypassed
        backoff and caused infinite retry loops with new build_jobs rows.

        Returns:
            True if the build was flagged for retry.
        """
        self.connect()
        cursor = self.conn.cursor()

        # Derive base_job_id for grouping across attempt suffixes
        base_job_id = re.sub(r'-r\d+$', '', queue_job_id)

        # Skip retry if a successful build already exists for this idea
        cursor.execute(
            "SELECT COUNT(*) FROM build_jobs WHERE base_job_id = ? AND status = 'completed'",
            (base_job_id,),
        )
        if cursor.fetchone()[0] > 0:
            return False

        # Use COUNT of failed rows as hard cap (immune to retry_count drift)
        cursor.execute(
            "SELECT COUNT(*) FROM build_jobs WHERE base_job_id = ? AND status = 'failed'",
            (base_job_id,),
        )
        failed_count = cursor.fetchone()[0]
        if failed_count >= self.MAX_RETRIES:
            return False

        # Get the latest failed row (use queue_job_id for the specific row)
        cursor.execute(
            "SELECT id, retry_count, next_retry_at FROM build_jobs "
            "WHERE queue_job_id = ? AND status = 'failed' ORDER BY id DESC LIMIT 1",
            (queue_job_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False

        # Guard: if this row was already marked for retry (next_retry_at is set),
        # don't mark it again. This prevents infinite retry loops when Gate 2
        # is blocked (circuit breaker, budget, etc.) and no new build row is
        # created to increment the failed count. Each failed row can only be
        # marked for retry once. A new build attempt creates a fresh row with
        # next_retry_at=NULL, which can then be marked independently.
        if row["next_retry_at"] is not None:
            return False

        new_count = failed_count  # Use total failed count, not per-row retry_count
        backoff_idx = min(new_count - 1, len(self.RETRY_BACKOFF_MINUTES) - 1)
        backoff_minutes = self.RETRY_BACKOFF_MINUTES[backoff_idx]
        next_retry = (datetime.now() + timedelta(minutes=backoff_minutes)).isoformat()

        # Set next_retry_at on the latest failed row (keep status='failed' —
        # the orchestrator will reset priority_queue when backoff expires)
        cursor.execute(
            "UPDATE build_jobs SET retry_count = ?, next_retry_at = ? "
            "WHERE id = ?",
            (new_count, next_retry, row["id"]),
        )
        build_updated = cursor.rowcount > 0

        self.conn.commit()
        return build_updated

    def mark_build_abandoned(self, queue_job_id: str) -> bool:
        """Mark a build as permanently abandoned after exhausting retries.

        Sets next_retry_at='abandoned' on the latest build_jobs row (sentinel
        that prevents get_exhausted_builds from returning it again) and sets
        priority_queue to 'failed' so it won't be re-dispatched.

        Returns:
            True if any rows were updated.
        """
        self.connect()
        cursor = self.conn.cursor()

        # Derive base_job_id for grouping across attempt suffixes
        base_job_id = re.sub(r'-r\d+$', '', queue_job_id)

        # Mark latest build_jobs row with 'abandoned' sentinel (group by base_job_id)
        cursor.execute(
            "UPDATE build_jobs SET next_retry_at = 'abandoned' "
            "WHERE id = (SELECT MAX(id) FROM build_jobs WHERE base_job_id = ?)",
            (base_job_id,),
        )
        build_updated = cursor.rowcount > 0

        # Parse source/source_id from base_job_id (strip -rN already done)
        parts = base_job_id.split("-", 2)
        source = None
        source_id = None
        if len(parts) >= 3 and parts[0] == "metroplex" and parts[1] in ("ideaforge", "skylynx", "linear", "academy"):
            source = parts[1]
            source_id = parts[2]
        elif len(parts) == 2 and parts[0] == "metroplex" and parts[1].isdigit():
            source = "ideaforge"
            source_id = parts[1]

        pq_updated = False
        if source and source_id:
            cursor.execute(
                "UPDATE priority_queue SET status = 'failed', completed_at = ? "
                "WHERE source = ? AND source_id = ? AND status != 'completed'",
                (datetime.now().isoformat(), source, source_id),
            )
            pq_updated = cursor.rowcount > 0

        self.conn.commit()
        return build_updated or pq_updated

    # Failure categories that are deterministic — retrying won't help
    NON_RETRYABLE_CATEGORIES = frozenset({
        "dependency_error",  # Missing packages won't appear on retry
        "test_failure",      # Same code = same test failures
        "build_error",       # Syntax/type errors are deterministic
        "quality_rejected",  # Tyrest QA rejection — same code = same result
        "review_failed",     # Automated review check failures are deterministic
        "low_quality",       # Quality score below threshold won't change on retry
    })

    def get_failure_category(self, queue_job_id: str) -> str | None:
        """Get the postmortem failure category for a build, if one exists."""
        self.connect()
        # Also check base_job_id variants (retries use _retry{N} suffix in postmortem)
        base_job_id = re.sub(r'-r\d+$', '', queue_job_id)
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT failure_category FROM build_postmortems "
            "WHERE queue_job_id LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f"{base_job_id}%",),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def is_retryable_failure(self, queue_job_id: str) -> bool:
        """Check if a build failure is worth retrying based on its postmortem category.

        Returns True if the failure is transient (timeout, environment_error, spec_unclear)
        or if no postmortem exists yet.
        Returns False if the failure is deterministic (dependency, test, build errors).
        """
        category = self.get_failure_category(queue_job_id)
        if category is None:
            return True  # No postmortem yet — allow retry
        return category not in self.NON_RETRYABLE_CATEGORIES

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
                AND b.base_job_id NOT IN (
                    SELECT bj2.base_job_id FROM build_jobs bj2
                    INNER JOIN publish_jobs pj ON pj.build_job_id = bj2.queue_job_id
                    WHERE pj.status = 'published'
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
                AND b.base_job_id NOT IN (
                    SELECT bj2.base_job_id FROM build_jobs bj2
                    INNER JOIN publish_jobs pj ON pj.build_job_id = bj2.queue_job_id
                    WHERE pj.status = 'published'
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

    # --- Build Sessions (Paperclip session compaction) ---

    def record_session(
        self,
        base_job_id: str,
        attempt: int,
        session_summary: str,
        input_tokens_total: int = 0,
        output_tokens_total: int = 0,
        session_age_seconds: int = 0,
    ) -> int:
        """Record a session snapshot for retry context."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO build_sessions
            (base_job_id, attempt, session_summary, input_tokens_total, output_tokens_total, session_age_seconds, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            base_job_id, attempt, session_summary,
            input_tokens_total, output_tokens_total, session_age_seconds,
            datetime.now().isoformat(),
        ))
        self.conn.commit()
        return cursor.lastrowid

    def get_latest_session(self, base_job_id: str) -> dict | None:
        """Get the most recent session snapshot for a base job.

        Returns dict with session_summary, input_tokens_total, output_tokens_total,
        session_age_seconds, attempt, created_at. Or None if no session exists.
        """
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM build_sessions
            WHERE base_job_id = ?
            ORDER BY attempt DESC
            LIMIT 1
        """, (base_job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def record_budget_event(
        self,
        event_type: str,
        trigger: str,
        daily_spend: float,
        monthly_spend: float,
        builds_killed: int = 0,
        details: str = "{}",
    ) -> int:
        """Record a budget enforcement event for audit trail."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO budget_events (event_type, trigger, daily_spend, monthly_spend, builds_killed, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (event_type, trigger, daily_spend, monthly_spend, builds_killed, details, datetime.now().isoformat()))
        self.conn.commit()
        return cursor.lastrowid

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

    def update_build_test_ratio(self, queue_job_id: str, ratio: float) -> bool:
        """Set test_ratio on a build job (Phase D2 — test coverage enforcement)."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET test_ratio = ? WHERE queue_job_id = ?",
            (ratio, queue_job_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_published_test_ratios(self) -> list[float]:
        """Get test_ratios for all published builds (deduped by queue_job_id).

        Joins build_jobs with publish_jobs to find published builds that have
        a non-null test_ratio.
        """
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT b.test_ratio
            FROM build_jobs b
            JOIN publish_jobs p ON p.build_job_id = b.queue_job_id AND p.status = 'published'
            WHERE b.test_ratio IS NOT NULL
            AND b.id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
        """)
        return [row[0] for row in cursor.fetchall()]

    # --- Readme Jobs (Gate 4.7) ---

    def has_readme(self, build_job_id: str) -> bool:
        """Check if a build already has a completed readme job."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT 1 FROM readme_jobs WHERE build_job_id = ? AND status = 'completed' LIMIT 1",
            (build_job_id,),
        )
        return cursor.fetchone() is not None

    def get_readme_pending(self) -> list[dict]:
        """Get published builds that haven't had README enhancement yet.

        Returns publish_jobs with status='published' that have no completed
        readme_jobs entry.
        """
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT p.build_job_id, p.title, p.project_dir, p.repo_url
            FROM publish_jobs p
            WHERE p.status = 'published'
            AND p.build_job_id NOT IN (
                SELECT build_job_id FROM readme_jobs WHERE status = 'completed'
            )
        """)
        return [dict(row) for row in cursor.fetchall()]

    def record_readme_job(
        self,
        build_job_id: str,
        repo_url: str,
        status: str,
        error: str | None = None,
    ):
        """Record a readme job result.

        Uses INSERT OR REPLACE keyed on build_job_id to handle retries.
        """
        self.connect()
        cursor = self.conn.cursor()

        completed_at = datetime.now().isoformat() if status == "completed" else None

        cursor.execute("""
            INSERT OR REPLACE INTO readme_jobs
                (build_job_id, repo_url, status, error, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            build_job_id,
            repo_url,
            status,
            error,
            datetime.now().isoformat(),
            completed_at,
        ))

        self.conn.commit()

    # --- Readiness Jobs (Gate 4.9) ---

    def has_readiness(self, build_job_id: str) -> bool:
        """Check if a build already has a completed readiness job."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT 1 FROM readiness_jobs WHERE build_job_id = ? AND status = 'completed' LIMIT 1",
            (build_job_id,),
        )
        return cursor.fetchone() is not None

    def get_readiness_pending(self) -> list[dict]:
        """Get published builds that haven't had readiness checks yet.

        Returns publish_jobs with status='published' that have no completed
        readiness_jobs entry.
        """
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT p.build_job_id, p.title, p.repo_name, p.repo_url, p.project_dir
            FROM publish_jobs p
            WHERE p.status = 'published'
            AND p.build_job_id NOT IN (
                SELECT build_job_id FROM readiness_jobs WHERE status = 'completed'
            )
        """)
        return [dict(row) for row in cursor.fetchall()]

    def record_readiness_job(
        self,
        build_job_id: str | None,
        repo_name: str,
        repo_url: str | None = None,
        status: str = "pending",
        checks_passed: str = "[]",
        checks_failed: str = "[]",
        fixes_applied: str = "[]",
        fixes_failed: str = "[]",
        error: str | None = None,
    ):
        """Record a readiness job result."""
        self.connect()
        cursor = self.conn.cursor()

        completed_at = datetime.now().isoformat() if status in ("completed", "partial") else None

        cursor.execute("""
            INSERT INTO readiness_jobs
                (build_job_id, repo_name, repo_url, status, checks_passed, checks_failed,
                 fixes_applied, fixes_failed, error, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            build_job_id,
            repo_name,
            repo_url,
            status,
            checks_passed,
            checks_failed,
            fixes_applied,
            fixes_failed,
            error,
            datetime.now().isoformat(),
            completed_at,
        ))

        self.conn.commit()

    def get_readiness_stats(self) -> dict:
        """Get readiness job statistics."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT status, COUNT(*) as cnt
            FROM readiness_jobs
            GROUP BY status
        """)
        stats = {row["status"]: row["cnt"] for row in cursor.fetchall()}
        cursor.execute("SELECT COUNT(*) as total FROM readiness_jobs")
        stats["total"] = cursor.fetchone()["total"]
        return stats
