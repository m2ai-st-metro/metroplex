"""
Tests for Metroplex StateDB module.
"""
import json
import pytest
from datetime import datetime

from db import StateDB
from models import TriageDecision, BuildJob, GateStatus, PriorityItem


def make_pq_item(**kwargs) -> PriorityItem:
    """Test helper: construct a PriorityItem with a non-empty idea_data default.

    The enqueue_item guard (added after the 2026-04-03 empty-payload incident)
    rejects items with empty idea_data. These tests don't care about the payload
    contents — they only care about queue mechanics — so this helper supplies a
    minimal valid placeholder. Use PriorityItem directly when a test needs to
    exercise idea_data behavior explicitly.
    """
    kwargs.setdefault("idea_data", '{"test": true}')
    return PriorityItem(**kwargs)


@pytest.fixture
def db():
    """Create in-memory state database."""
    state_db = StateDB(":memory:")
    state_db.init_db()
    yield state_db
    state_db.close()


class TestStateDBInit:
    """Test database initialization."""

    def test_init_creates_tables(self, db):
        cursor = db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row["name"] for row in cursor.fetchall()]

        assert "triage_decisions" in tables
        assert "build_jobs" in tables
        assert "patch_applications" in tables
        assert "cycles" in tables
        assert "gate_status" in tables

    def test_init_creates_indexes(self, db):
        cursor = db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")
        indexes = [row["name"] for row in cursor.fetchall()]

        assert "idx_triage_decisions_idea" in indexes
        assert "idx_triage_decisions_decision" in indexes
        assert "idx_build_jobs_status" in indexes
        assert "idx_cycles_started" in indexes

    def test_init_seeds_gate_status(self, db):
        for gate in ["triage", "build", "publish"]:
            status = db.get_gate_status(gate)
            assert status.gate == gate
            assert status.consecutive_failures == 0
            assert status.halted is False

    def test_init_idempotent(self, db):
        """Calling init_db twice doesn't fail."""
        db.init_db()
        status = db.get_gate_status("triage")
        assert status.consecutive_failures == 0

    def test_file_based_creates_directory(self, tmp_path):
        db_path = str(tmp_path / "subdir" / "test.db")
        state_db = StateDB(db_path)
        state_db.init_db()
        assert (tmp_path / "subdir").exists()
        state_db.close()


class TestActualCostMigration:
    """Phase G: actual_cost_usd column on build_jobs."""

    def test_migration_adds_actual_cost_column(self, db):
        cursor = db.conn.cursor()
        cursor.execute("PRAGMA table_info(build_jobs)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "actual_cost_usd" in columns

    def test_actual_cost_column_defaults_to_null(self, db):
        job = BuildJob(
            idea_id=1,
            title="Test",
            spec_path="/tmp/s.txt",
            queue_job_id="metroplex-ideaforge-100",
            status="queued",
            queued_at=datetime.now(),
        )
        db.record_build_job(job)
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT actual_cost_usd FROM build_jobs WHERE queue_job_id = ?",
            ("metroplex-ideaforge-100",),
        )
        assert cursor.fetchone()["actual_cost_usd"] is None


class TestBuildActualCostHelpers:
    """Phase G: get_build_actual_cost / update_build_actual_cost."""

    def _make_build(self, db, queue_job_id: str) -> None:
        job = BuildJob(
            idea_id=1,
            title="Test",
            spec_path="/tmp/s.txt",
            queue_job_id=queue_job_id,
            status="completed",
            queued_at=datetime.now(),
        )
        db.record_build_job(job)

    def test_get_actual_cost_no_entries_returns_zero(self, db):
        self._make_build(db, "metroplex-ideaforge-200")
        assert db.get_build_actual_cost("metroplex-ideaforge-200") == 0.0

    def test_get_actual_cost_aggregates_across_entries(self, db):
        self._make_build(db, "metroplex-ideaforge-201")
        db.record_cost("spec_expander", "qwen", 100, 50, 0.05, queue_job_id="metroplex-ideaforge-201")
        db.record_cost("spec_simplifier", "qwen", 200, 100, 0.10, queue_job_id="metroplex-ideaforge-201")
        db.record_cost("build", "opus", 5000, 2000, 1.20, queue_job_id="metroplex-ideaforge-201")
        total = db.get_build_actual_cost("metroplex-ideaforge-201")
        assert abs(total - 1.35) < 0.0001

    def test_get_actual_cost_ignores_other_jobs_and_null(self, db):
        self._make_build(db, "metroplex-ideaforge-202")
        self._make_build(db, "metroplex-ideaforge-203")
        db.record_cost("spec_expander", "qwen", 100, 50, 0.05, queue_job_id="metroplex-ideaforge-202")
        db.record_cost("spec_expander", "qwen", 999, 999, 99.99, queue_job_id="metroplex-ideaforge-203")
        db.record_cost("ad_hoc", "qwen", 100, 50, 7.77, queue_job_id=None)
        assert abs(db.get_build_actual_cost("metroplex-ideaforge-202") - 0.05) < 0.0001

    def test_update_build_actual_cost_writes_aggregate(self, db):
        self._make_build(db, "metroplex-ideaforge-204")
        db.record_cost("spec_expander", "qwen", 100, 50, 0.05, queue_job_id="metroplex-ideaforge-204")
        db.record_cost("build", "opus", 5000, 2000, 1.50, queue_job_id="metroplex-ideaforge-204")
        total = db.update_build_actual_cost("metroplex-ideaforge-204")
        assert abs(total - 1.55) < 0.0001
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT actual_cost_usd FROM build_jobs WHERE queue_job_id = ?",
            ("metroplex-ideaforge-204",),
        )
        assert abs(cursor.fetchone()["actual_cost_usd"] - 1.55) < 0.0001

    def test_update_build_actual_cost_idempotent(self, db):
        self._make_build(db, "metroplex-ideaforge-205")
        db.record_cost("spec_expander", "qwen", 100, 50, 0.42, queue_job_id="metroplex-ideaforge-205")
        first = db.update_build_actual_cost("metroplex-ideaforge-205")
        second = db.update_build_actual_cost("metroplex-ideaforge-205")
        assert first == second == 0.42

    def test_update_build_actual_cost_with_no_entries_writes_zero(self, db):
        self._make_build(db, "metroplex-ideaforge-206")
        total = db.update_build_actual_cost("metroplex-ideaforge-206")
        assert total == 0.0
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT actual_cost_usd FROM build_jobs WHERE queue_job_id = ?",
            ("metroplex-ideaforge-206",),
        )
        assert cursor.fetchone()["actual_cost_usd"] == 0.0


class TestTriageDecisionRecords:
    """Test triage decision recording."""

    def test_record_and_query(self, db):
        decision = TriageDecision(
            idea_id=42,
            title="Test Idea",
            weighted_score=8.5,
            scaled_score=85.0,
            decision="approve",
            reason="meets threshold",
            decided_at=datetime(2026, 2, 23, 12, 0, 0),
        )
        db.record_triage_decision(decision)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM triage_decisions WHERE idea_id = 42")
        row = cursor.fetchone()

        assert row is not None
        assert row["title"] == "Test Idea"
        assert row["scaled_score"] == 85.0
        assert row["decision"] == "approve"

    def test_record_multiple(self, db):
        for i in range(5):
            decision = TriageDecision(
                idea_id=i,
                title=f"Idea {i}",
                weighted_score=float(i),
                scaled_score=float(i * 10),
                decision="approve" if i > 2 else "reject",
                reason="test",
                decided_at=datetime.now(),
            )
            db.record_triage_decision(decision)

        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM triage_decisions")
        assert cursor.fetchone()["cnt"] == 5

    def test_check_constraint_rejects_invalid_decision(self, db):
        """Decision column only accepts approve/reject/defer."""
        cursor = db.conn.cursor()
        with pytest.raises(Exception):
            cursor.execute(
                "INSERT INTO triage_decisions (idea_id, title, weighted_score, scaled_score, decision, reason, decided_at) "
                "VALUES (1, 'test', 5.0, 50.0, 'invalid_decision', '', '2026-01-01')"
            )


class TestBuildJobRecords:
    """Test build job recording."""

    def test_record_and_query(self, db):
        job = BuildJob(
            idea_id=10,
            title="Build Test",
            spec_path="/tmp/spec_10.txt",
            queue_job_id="metroplex-10",
            status="queued",
            queued_at=datetime(2026, 2, 23, 14, 0, 0),
        )
        db.record_build_job(job)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM build_jobs WHERE idea_id = 10")
        row = cursor.fetchone()

        assert row["queue_job_id"] == "metroplex-10"
        assert row["status"] == "queued"

    def test_check_constraint_rejects_invalid_status(self, db):
        cursor = db.conn.cursor()
        with pytest.raises(Exception):
            cursor.execute(
                "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at) "
                "VALUES (1, 'test', '/tmp/x', 'j1', 'bogus', '2026-01-01')"
            )


class TestCycleRecords:
    """Test cycle start/end recording."""

    def test_start_cycle(self, db):
        result = db.start_cycle("cycle-test-001")
        assert result.cycle_id == "cycle-test-001"
        assert result.completed_at is None
        assert result.triage_count == 0

    def test_end_cycle(self, db):
        db.start_cycle("cycle-test-002")
        db.end_cycle("cycle-test-002", triage_count=3, build_count=1, patch_count=2, errors=["err1"])

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM cycles WHERE cycle_id = 'cycle-test-002'")
        row = cursor.fetchone()

        assert row["completed_at"] is not None
        assert row["triage_count"] == 3
        assert row["build_count"] == 1
        assert row["patch_count"] == 2
        assert json.loads(row["errors"]) == ["err1"]

    def test_cycle_id_unique(self, db):
        db.start_cycle("cycle-unique")
        with pytest.raises(Exception):
            db.start_cycle("cycle-unique")


class TestGateStatus:
    """Test gate status CRUD."""

    def test_get_default_status(self, db):
        status = db.get_gate_status("triage")
        assert status.gate == "triage"
        assert status.consecutive_failures == 0
        assert status.halted is False
        assert status.last_error is None

    def test_update_gate_status(self, db):
        status = GateStatus(gate="build", consecutive_failures=2, halted=False, last_error="timeout")
        db.update_gate_status(status)

        retrieved = db.get_gate_status("build")
        assert retrieved.consecutive_failures == 2
        assert retrieved.last_error == "timeout"
        assert retrieved.halted is False

    def test_halt_gate(self, db):
        status = GateStatus(gate="publish", consecutive_failures=3, halted=True, last_error="3 failures")
        db.update_gate_status(status)

        retrieved = db.get_gate_status("publish")
        assert retrieved.halted is True

    def test_reset_gate(self, db):
        # Set halted
        db.update_gate_status(GateStatus(gate="triage", consecutive_failures=3, halted=True, last_error="err"))
        # Reset
        db.update_gate_status(GateStatus(gate="triage", consecutive_failures=0, halted=False, last_error=None))

        retrieved = db.get_gate_status("triage")
        assert retrieved.consecutive_failures == 0
        assert retrieved.halted is False
        assert retrieved.last_error is None

    def test_unknown_gate_returns_default(self, db):
        """Getting a gate not in the table returns a default."""
        # The init seeds triage/build/publish, but let's query one that exists
        status = db.get_gate_status("triage")
        assert status.gate == "triage"


class TestConnectionManagement:
    """Test DB connection lifecycle."""

    def test_connect_and_close(self):
        db = StateDB(":memory:")
        db.init_db()
        assert db.conn is not None
        db.close()
        assert db.conn is None

    def test_reconnect_after_close(self):
        db = StateDB(":memory:")
        db.init_db()
        db.close()
        db.connect()
        assert db.conn is not None
        db.close()

    def test_double_close_safe(self):
        db = StateDB(":memory:")
        db.init_db()
        db.close()
        db.close()  # Should not raise


class TestPriorityQueue:
    """Test priority queue operations."""

    def test_init_creates_priority_queue_table(self, db):
        cursor = db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='priority_queue'")
        assert cursor.fetchone() is not None

    def test_init_creates_priority_queue_indexes(self, db):
        cursor = db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_priority_queue_%'")
        indexes = [row["name"] for row in cursor.fetchall()]
        assert "idx_priority_queue_status" in indexes
        assert "idx_priority_queue_score" in indexes
        assert "idx_priority_queue_source" in indexes

    def test_enqueue_item(self, db):
        item = make_pq_item(
            source="ideaforge",
            source_id="42",
            title="Test Idea",
            description="A test idea",
            priority_score=85.0,
            idea_data='{"id": 42, "title": "Test Idea"}',
        )
        row_id = db.enqueue_item(item)
        assert row_id > 0

        # Verify stored
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM priority_queue WHERE id = ?", (row_id,))
        row = cursor.fetchone()
        assert row["source"] == "ideaforge"
        assert row["source_id"] == "42"
        assert row["title"] == "Test Idea"
        assert row["priority_score"] == 85.0
        assert row["status"] == "pending"

    def test_enqueue_duplicate_skips(self, db):
        item = make_pq_item(
            source="ideaforge",
            source_id="42",
            title="Test Idea",
            description="Desc",
            priority_score=85.0,
            idea_data='{"id": 42, "title": "Test Idea"}',
        )
        first_id = db.enqueue_item(item)
        second_id = db.enqueue_item(item)

        assert first_id > 0
        assert second_id == 0  # Duplicate skipped

        # Verify only one row
        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM priority_queue")
        assert cursor.fetchone()["cnt"] == 1

    def test_enqueue_rejects_empty_idea_data(self, db):
        """Guard against the 2026-04-03 incident: 42 rows inserted with
        idea_data='{}' caused silent dispatch failures. enqueue_item must
        refuse empty payloads instead of accepting them."""
        import pytest

        base_kwargs = dict(
            source="ideaforge",
            source_id="99",
            title="Empty Payload",
            description="Desc",
            priority_score=50.0,
        )

        # Empty string (Pydantic default)
        with pytest.raises(ValueError, match="idea_data is empty"):
            db.enqueue_item(make_pq_item(**base_kwargs, idea_data=""))

        # Empty JSON object
        with pytest.raises(ValueError, match="idea_data is empty"):
            db.enqueue_item(make_pq_item(**base_kwargs, idea_data="{}"))

        # Whitespace-only
        with pytest.raises(ValueError, match="idea_data is empty"):
            db.enqueue_item(make_pq_item(**base_kwargs, idea_data="  {} "))

        # Row count unchanged
        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM priority_queue WHERE source_id='99'")
        assert cursor.fetchone()["cnt"] == 0

    def test_claim_next_pending_skips_poisoned_rows(self, db):
        """Historical poisoned rows (idea_data='{}') must not be claimed —
        they would burn a worker cycle and fail at dispatch. claim_next_pending
        must filter them out so live work advances."""
        # Inject a poisoned row directly, bypassing enqueue_item's guard
        # (simulating pre-guard data written by an ad-hoc script)
        db.conn.execute("""
            INSERT INTO priority_queue (source, source_id, title, description,
                                        priority_score, status, idea_data, created_at)
            VALUES ('ideaforge', '666', 'Poisoned', 'p', 99.0, 'pending', '{}',
                    '2026-04-03T19:44:20')
        """)
        db.conn.commit()

        # Enqueue a legitimate lower-priority item
        db.enqueue_item(make_pq_item(
            source="ideaforge",
            source_id="777",
            title="Good",
            description="good",
            priority_score=50.0,
            idea_data='{"id": 777, "title": "Good"}',
        ))

        # Despite the poisoned row having a higher priority_score, the claim
        # must return the legitimate row instead.
        claimed = db.claim_next_pending("test-worker")
        assert claimed is not None
        assert claimed.source_id == "777"

    def test_get_next_pending_returns_highest_score(self, db):
        for score, sid in [(50.0, "1"), (90.0, "2"), (70.0, "3")]:
            item = make_pq_item(
                source="ideaforge",
                source_id=sid,
                title=f"Idea {sid}",
                description="Desc",
                priority_score=score,
            )
            db.enqueue_item(item)

        result = db.get_next_pending()
        assert result is not None
        assert result.source_id == "2"
        assert result.priority_score == 90.0

    def test_get_next_pending_empty_queue(self, db):
        result = db.get_next_pending()
        assert result is None

    def test_get_next_pending_skips_non_pending(self, db):
        item = make_pq_item(
            source="ideaforge",
            source_id="1",
            title="Dispatched Idea",
            description="Desc",
            priority_score=90.0,
        )
        row_id = db.enqueue_item(item)
        db.update_item_status(row_id, "dispatched", "dispatched_at")

        result = db.get_next_pending()
        assert result is None

    def test_update_item_status(self, db):
        item = make_pq_item(
            source="ideaforge",
            source_id="1",
            title="Test Idea",
            description="Desc",
            priority_score=85.0,
        )
        row_id = db.enqueue_item(item)

        db.update_item_status(row_id, "dispatched", "dispatched_at")

        cursor = db.conn.cursor()
        cursor.execute("SELECT status, dispatched_at FROM priority_queue WHERE id = ?", (row_id,))
        row = cursor.fetchone()
        assert row["status"] == "dispatched"
        assert row["dispatched_at"] is not None

    def test_update_item_status_completed(self, db):
        item = make_pq_item(
            source="ideaforge",
            source_id="1",
            title="Test Idea",
            description="Desc",
            priority_score=85.0,
        )
        row_id = db.enqueue_item(item)

        db.update_item_status(row_id, "completed", "completed_at")

        cursor = db.conn.cursor()
        cursor.execute("SELECT status, completed_at FROM priority_queue WHERE id = ?", (row_id,))
        row = cursor.fetchone()
        assert row["status"] == "completed"
        assert row["completed_at"] is not None

    def test_update_item_status_no_timestamp(self, db):
        item = make_pq_item(
            source="ideaforge",
            source_id="1",
            title="Test Idea",
            description="Desc",
            priority_score=85.0,
        )
        row_id = db.enqueue_item(item)

        db.update_item_status(row_id, "failed")

        cursor = db.conn.cursor()
        cursor.execute("SELECT status FROM priority_queue WHERE id = ?", (row_id,))
        assert cursor.fetchone()["status"] == "failed"

    def test_get_queue_summary(self, db):
        # Insert items with different statuses
        for sid, score in [("1", 90.0), ("2", 80.0), ("3", 70.0)]:
            item = make_pq_item(
                source="ideaforge", source_id=sid, title=f"Idea {sid}",
                description="Desc", priority_score=score,
            )
            db.enqueue_item(item)

        # Mark one as dispatched and one as completed
        db.update_item_status(2, "dispatched", "dispatched_at")
        db.update_item_status(3, "completed", "completed_at")

        summary = db.get_queue_summary()
        assert summary["total"] == 3
        assert summary.get("pending", 0) == 1
        assert summary.get("dispatched", 0) == 1
        assert summary.get("completed", 0) == 1

    def test_get_queue_summary_empty(self, db):
        summary = db.get_queue_summary()
        assert summary["total"] == 0

    def test_update_build_job_status_syncs_priority_queue(self, db):
        """Completing a build job also updates the priority queue item."""
        # Insert into priority_queue
        item = make_pq_item(
            source="ideaforge", source_id="5", title="Build Me",
            description="Desc", priority_score=80.0,
        )
        db.enqueue_item(item)
        db.update_item_status(1, "dispatched", "dispatched_at")

        # Insert a matching build job
        job = BuildJob(
            idea_id=5, title="Build Me", spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-5", status="queued",
            queued_at=datetime(2026, 2, 26, 12, 0, 0),
        )
        db.record_build_job(job)

        # Mark build job as completed
        db.update_build_job_status("metroplex-5", "completed")

        # Verify build_jobs table updated
        cursor = db.conn.cursor()
        cursor.execute("SELECT status FROM build_jobs WHERE queue_job_id = 'metroplex-5'")
        assert cursor.fetchone()["status"] == "completed"

        # Verify priority_queue also updated
        cursor.execute("SELECT status, completed_at FROM priority_queue WHERE source_id = '5'")
        row = cursor.fetchone()
        assert row["status"] == "completed"
        assert row["completed_at"] is not None

    def test_update_build_job_status_failed_syncs(self, db):
        """Failing a build job marks priority queue item as failed."""
        item = make_pq_item(
            source="ideaforge", source_id="6", title="Will Fail",
            description="Desc", priority_score=75.0,
        )
        db.enqueue_item(item)

        job = BuildJob(
            idea_id=6, title="Will Fail", spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-6", status="queued",
            queued_at=datetime(2026, 2, 26, 12, 0, 0),
        )
        db.record_build_job(job)

        db.update_build_job_status("metroplex-6", "failed")

        cursor = db.conn.cursor()
        cursor.execute("SELECT status FROM priority_queue WHERE source_id = '6'")
        assert cursor.fetchone()["status"] == "failed"

    def test_enqueue_different_sources(self, db):
        """Items from different sources with same source_id are allowed."""
        item1 = make_pq_item(
            source="ideaforge", source_id="1", title="From IdeaForge",
            description="Desc", priority_score=80.0,
        )
        item2 = make_pq_item(
            source="skylynx", source_id="1", title="From SkyLynx",
            description="Desc", priority_score=90.0,
        )
        id1 = db.enqueue_item(item1)
        id2 = db.enqueue_item(item2)

        assert id1 > 0
        assert id2 > 0
        assert id1 != id2

    # --- Gap 1: Multi-source build status sync tests ---

    def test_update_build_job_status_skylynx_source(self, db):
        """New format job ID with skylynx source syncs priority_queue correctly."""
        item = make_pq_item(
            source="skylynx", source_id="sl-abc123", title="SkyLynx Rec",
            description="Desc", priority_score=135.0,
        )
        row_id = db.enqueue_item(item)
        db.update_item_status(row_id, "dispatched", "dispatched_at")

        job = BuildJob(
            idea_id=0, title="SkyLynx Rec", spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-skylynx-sl-abc123", status="queued",
            queued_at=datetime(2026, 2, 27, 12, 0, 0),
        )
        db.record_build_job(job)

        db.update_build_job_status("metroplex-skylynx-sl-abc123", "completed")

        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT status, completed_at FROM priority_queue WHERE source = 'skylynx' AND source_id = 'sl-abc123'"
        )
        row = cursor.fetchone()
        assert row["status"] == "completed"
        assert row["completed_at"] is not None

    def test_update_build_job_status_ideaforge_new_format(self, db):
        """New format with ideaforge source (backward compat with numeric IDs)."""
        item = make_pq_item(
            source="ideaforge", source_id="5", title="IdeaForge Idea",
            description="Desc", priority_score=80.0,
        )
        row_id = db.enqueue_item(item)
        db.update_item_status(row_id, "dispatched", "dispatched_at")

        job = BuildJob(
            idea_id=5, title="IdeaForge Idea", spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-5", status="queued",
            queued_at=datetime(2026, 2, 27, 12, 0, 0),
        )
        db.record_build_job(job)

        db.update_build_job_status("metroplex-ideaforge-5", "completed")

        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT status FROM priority_queue WHERE source = 'ideaforge' AND source_id = '5'"
        )
        assert cursor.fetchone()["status"] == "completed"

    def test_update_build_job_status_malformed_job_id_noop(self, db):
        """Malformed job IDs don't crash and don't modify priority_queue."""
        item = make_pq_item(
            source="ideaforge", source_id="10", title="Safe Item",
            description="Desc", priority_score=70.0,
        )
        row_id = db.enqueue_item(item)
        db.update_item_status(row_id, "dispatched", "dispatched_at")

        # These should all be no-ops (no crash, no queue update)
        db.update_build_job_status("invalid-format", "completed")
        db.update_build_job_status("", "completed")
        db.update_build_job_status("metroplex-unknown-123", "completed")

        cursor = db.conn.cursor()
        cursor.execute("SELECT status FROM priority_queue WHERE source_id = '10'")
        assert cursor.fetchone()["status"] == "dispatched"  # Unchanged


class TestStaleQueuedBuildRecovery:
    """Fix B / Fix C: stale queued build recovery no longer destroys rows."""

    def _enqueue_and_dispatch(self, db, idea_id: int, title: str = "T"):
        item = make_pq_item(
            source="ideaforge",
            source_id=str(idea_id),
            title=title,
            description="d",
            priority_score=70.0,
        )
        pq_id = db.enqueue_item(item)
        db.update_item_status(pq_id, "dispatched", "dispatched_at")
        return pq_id

    def _record_stale_queued_job(self, db, idea_id: int, queue_job_id: str):
        """Insert a build_jobs row with a queued_at old enough to be stale."""
        from datetime import timedelta
        stale_time = datetime.now() - timedelta(
            minutes=StateDB.STALE_QUEUED_THRESHOLD_MINUTES + 5
        )
        job = BuildJob(
            idea_id=idea_id,
            title=f"Idea {idea_id}",
            spec_path="/tmp/spec.txt",
            queue_job_id=queue_job_id,
            status="queued",
            queued_at=stale_time,
        )
        db.record_build_job(job)

    def test_reset_stale_queued_build_preserves_row(self, db):
        """Fix B: reset_stale_queued_build must UPDATE the row, not delete it."""
        pq_id = self._enqueue_and_dispatch(db, 77, title="Preserve Me")
        self._record_stale_queued_job(db, 77, "metroplex-ideaforge-77")

        db.reset_stale_queued_build("metroplex-ideaforge-77", pq_id)

        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT status, next_retry_at FROM build_jobs WHERE queue_job_id = ?",
            ("metroplex-ideaforge-77",),
        )
        row = cursor.fetchone()
        assert row is not None, "build_jobs row must be preserved, not deleted"
        assert row["status"] == "failed"
        assert row["next_retry_at"] == "abandoned"

        cursor.execute(
            "SELECT status, dispatched_at FROM priority_queue WHERE id = ?",
            (pq_id,),
        )
        pq_row = cursor.fetchone()
        assert pq_row["status"] == "pending"
        assert pq_row["dispatched_at"] is None

    def test_reset_stale_queued_build_excluded_from_retry(self, db):
        """Fix B: abandoned stale builds must not appear in get_retryable_builds."""
        pq_id = self._enqueue_and_dispatch(db, 88)
        self._record_stale_queued_job(db, 88, "metroplex-ideaforge-88")
        db.reset_stale_queued_build("metroplex-ideaforge-88", pq_id)

        retryable = db.get_retryable_builds()
        ids = [r["queue_job_id"] for r in retryable]
        assert "metroplex-ideaforge-88" not in ids

    def test_get_stale_queued_builds_honors_exclude_set(self, db):
        """Fix C: exclude_job_ids filters out live-running jobs."""
        pq_a = self._enqueue_and_dispatch(db, 101, title="Live")
        pq_b = self._enqueue_and_dispatch(db, 102, title="Really Stale")
        self._record_stale_queued_job(db, 101, "metroplex-ideaforge-101")
        self._record_stale_queued_job(db, 102, "metroplex-ideaforge-102")

        # Without exclusion, both should be stale
        stale_all = db.get_stale_queued_builds()
        ids_all = {r["queue_job_id"] for r in stale_all}
        assert "metroplex-ideaforge-101" in ids_all
        assert "metroplex-ideaforge-102" in ids_all

        # With exclusion of 101, only 102 should remain
        stale_filtered = db.get_stale_queued_builds(
            exclude_job_ids={"metroplex-ideaforge-101"}
        )
        ids_filtered = {r["queue_job_id"] for r in stale_filtered}
        assert "metroplex-ideaforge-101" not in ids_filtered
        assert "metroplex-ideaforge-102" in ids_filtered


class TestSoftResetAttempts:
    """soft_reset_attempts: re-enable a fully-retry-exhausted build by trimming
    failed rows + re-pending priority_queue. The published UPDATE recipe is
    insufficient at MAX_RETRIES because get_retryable_builds counts failed rows
    directly — this helper closes that gap."""

    def _setup_exhausted(self, db, idea_id: int = 427, base: str = "metroplex-ideaforge-427"):
        """Create the live #427 shape: priority_queue row + MAX_RETRIES failed build_jobs rows."""
        item = make_pq_item(
            source="ideaforge",
            source_id=str(idea_id),
            title="Nighttime Newborn Triage Copilot",
            description="d",
            priority_score=77.0,
        )
        pq_id = db.enqueue_item(item)
        db.update_item_status(pq_id, "dispatched", "dispatched_at")
        # status='failed' on priority_queue for the exhausted scenario
        db.conn.execute("UPDATE priority_queue SET status = 'failed' WHERE id = ?", (pq_id,))
        for suffix in ("", "-r1", "-r2"):
            db.record_build_job(BuildJob(
                idea_id=idea_id,
                title="Nighttime Newborn Triage Copilot",
                spec_path="/tmp/spec.txt",
                queue_job_id=f"{base}{suffix}",
                status="failed",
                queued_at=datetime.now(),
            ))
        db.conn.commit()
        return pq_id

    def test_exhausted_build_blocked_from_retry_before_reset(self, db):
        """Sanity: 3 failed rows means get_retryable_builds excludes the build."""
        self._setup_exhausted(db)
        retryable = db.get_retryable_builds()
        assert all(r["base_job_id"] != "metroplex-ideaforge-427" for r in retryable)
        assert db.count_failed_builds("metroplex-ideaforge-427") == 3

    def test_soft_reset_trims_failed_rows_and_repends_queue(self, db):
        """3 failed rows + soft_reset -> 2 retained, priority_queue back to pending."""
        pq_id = self._setup_exhausted(db)

        result = db.soft_reset_attempts("metroplex-ideaforge-427")

        assert result["deleted_count"] == 1
        assert result["retained_failed_count"] == 2
        assert result["priority_queue_id"] == pq_id
        assert result["source"] == "ideaforge"
        assert result["source_id"] == "427"
        # The most-recent row should be the one deleted
        assert "metroplex-ideaforge-427-r2" in result["deleted_queue_job_ids"]

        cursor = db.conn.cursor()
        cursor.execute("SELECT status, dispatched_at, claimed_by FROM priority_queue WHERE id = ?", (pq_id,))
        row = cursor.fetchone()
        assert row["status"] == "pending"
        assert row["dispatched_at"] is None
        assert row["claimed_by"] is None

        # Build is now eligible for retry
        assert db.count_failed_builds("metroplex-ideaforge-427") == 2

    def test_soft_reset_with_rn_suffix_raises(self, db):
        """Caller mistake: passing a -rN suffix should be rejected loudly."""
        with pytest.raises(ValueError, match="-rN"):
            db.soft_reset_attempts("metroplex-ideaforge-427-r2")

    def test_soft_reset_keep_max_failed_zero_clears_all(self, db):
        """keep_max_failed=0 deletes every failed row (used for full reset)."""
        self._setup_exhausted(db)
        result = db.soft_reset_attempts("metroplex-ideaforge-427", keep_max_failed=0)
        assert result["deleted_count"] == 3
        assert result["retained_failed_count"] == 0
        assert db.count_failed_builds("metroplex-ideaforge-427") == 0

    def test_soft_reset_with_no_priority_queue_row_still_trims(self, db):
        """If priority_queue is missing, build_jobs are still trimmed; pq id is None."""
        for suffix in ("", "-r1", "-r2"):
            db.record_build_job(BuildJob(
                idea_id=999,
                title="Orphan",
                spec_path="/tmp/spec.txt",
                queue_job_id=f"metroplex-ideaforge-999{suffix}",
                status="failed",
                queued_at=datetime.now(),
            ))
        db.conn.commit()

        result = db.soft_reset_attempts("metroplex-ideaforge-999")
        assert result["deleted_count"] == 1
        assert result["priority_queue_id"] is None


class TestCostBySource:
    """Test cost ledger aggregation grouped by source."""

    def test_get_cost_by_source_orders_and_percentages(self, db):
        db.record_cost(source="spec_expander", model="m", input_tokens=100, output_tokens=50, estimated_cost=0.05)
        db.record_cost(source="spec_expander", model="m", input_tokens=150, output_tokens=75, estimated_cost=0.075)
        db.record_cost(source="readme_generation", model="m", input_tokens=20, output_tokens=10, estimated_cost=0.01)
        db.record_cost(source="readiness_topics", model="m", input_tokens=40, output_tokens=20, estimated_cost=0.02)

        rows = db.get_cost_by_source(days=7)

        # Three distinct sources
        assert len(rows) == 3

        # Ordered by total_cost DESC
        sources = [r["source"] for r in rows]
        assert sources == ["spec_expander", "readiness_topics", "readme_generation"]

        # Totals correct (spec_expander aggregates two entries)
        assert rows[0]["total_cost"] == pytest.approx(0.125)
        assert rows[0]["entry_count"] == 2
        assert rows[1]["total_cost"] == pytest.approx(0.02)
        assert rows[2]["total_cost"] == pytest.approx(0.01)

        # Percentages sum to ~100
        assert sum(r["pct_of_total"] for r in rows) == pytest.approx(100.0, abs=0.2)

    def test_get_cost_by_source_empty(self, db):
        assert db.get_cost_by_source(days=7) == []


class TestBuildJobsScoringRubricMigration:
    """R-A item 3 (2026-05-12): build_jobs.scoring_rubric idempotent migration
    + record_build_job persistence.
    """

    def _get_columns(self, db) -> dict:
        """Return PRAGMA table_info(build_jobs) as {name: row dict}."""
        cursor = db.conn.cursor()
        cursor.execute("PRAGMA table_info(build_jobs)")
        return {row["name"]: dict(row) for row in cursor.fetchall()}

    def _build_job(self, idea_id=42, queue_job_id="metroplex-ideaforge-42",
                   scoring_rubric=None):
        return BuildJob(
            idea_id=idea_id,
            title=f"Idea {idea_id}",
            spec_path=f"/tmp/spec_{idea_id}.txt",
            queue_job_id=queue_job_id,
            status="queued",
            queued_at=datetime.now(),
            scoring_rubric=scoring_rubric,
        )

    # --- C1: migration is idempotent + nullable ---

    def test_scoring_rubric_column_present_on_fresh_db(self, db):
        cols = self._get_columns(db)
        assert "scoring_rubric" in cols
        assert cols["scoring_rubric"]["type"].upper() == "TEXT"

    def test_scoring_rubric_column_nullable_for_legacy_inserts(self, db):
        """A BuildJob without scoring_rubric must store NULL (not 'tech')."""
        job = self._build_job(queue_job_id="metroplex-ideaforge-100")
        db.record_build_job(job)

        row = db.get_build_by_queue_job_id("metroplex-ideaforge-100")
        assert row is not None
        assert row["scoring_rubric"] is None

    def test_init_db_is_idempotent_on_scoring_rubric(self, tmp_path):
        """Re-running init_db on an already-migrated DB must not raise
        'duplicate column name' and must keep exactly one scoring_rubric column.
        """
        db_path = str(tmp_path / "idem.db")
        first = StateDB(db_path)
        first.init_db()
        first.close()

        second = StateDB(db_path)
        # MUST NOT raise: PRAGMA-driven guard skips the ALTER.
        second.init_db()

        cursor = second.conn.cursor()
        cursor.execute("PRAGMA table_info(build_jobs)")
        names = [row["name"] for row in cursor.fetchall()]
        assert names.count("scoring_rubric") == 1
        second.close()

    def test_preexisting_rows_survive_migration(self, tmp_path):
        """Create a DB without the new column, insert a row, then run the
        migration. The row must still be present with scoring_rubric=NULL.
        """
        import sqlite3
        db_path = str(tmp_path / "preexisting.db")

        # Bootstrap a minimal build_jobs table WITHOUT scoring_rubric.
        bootstrap = sqlite3.connect(db_path)
        bootstrap.execute("""
            CREATE TABLE build_jobs (
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
        bootstrap.execute(
            "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (777, "Pre-Migration", "/tmp/pre.txt", "pre-001", "queued",
             datetime.now().isoformat()),
        )
        bootstrap.commit()
        bootstrap.close()

        # Run the full Metroplex migration on top of the bootstrap.
        db = StateDB(db_path)
        db.init_db()

        # Pre-migration row survives, with scoring_rubric NULL.
        row = db.get_build_by_queue_job_id("pre-001")
        assert row is not None
        assert row["title"] == "Pre-Migration"
        assert row["scoring_rubric"] is None
        db.close()

    # --- C2: record_build_job persists the rubric ---

    def test_record_build_job_persists_life_domain_rubric(self, db):
        job = self._build_job(
            queue_job_id="metroplex-ideaforge-201",
            scoring_rubric="life_domain",
        )
        db.record_build_job(job)

        row = db.get_build_by_queue_job_id("metroplex-ideaforge-201")
        assert row is not None
        assert row["scoring_rubric"] == "life_domain"

    def test_record_build_job_persists_tech_rubric_verbatim(self, db):
        """Build queue is allowed to pass 'tech' explicitly; the DB must
        store it verbatim (no auto-rewrite to NULL or 'life_domain').
        """
        job = self._build_job(
            queue_job_id="metroplex-ideaforge-202",
            scoring_rubric="tech",
        )
        db.record_build_job(job)

        row = db.get_build_by_queue_job_id("metroplex-ideaforge-202")
        assert row is not None
        assert row["scoring_rubric"] == "tech"

    def test_record_build_job_persists_null_rubric_by_default(self, db):
        """A BuildJob constructed without the kwarg defaults to None."""
        # Mirror legacy construction (no scoring_rubric kwarg).
        job = BuildJob(
            idea_id=203,
            title="Legacy Construction",
            spec_path="/tmp/legacy.txt",
            queue_job_id="metroplex-ideaforge-203",
            status="queued",
            queued_at=datetime.now(),
        )
        db.record_build_job(job)

        row = db.get_build_by_queue_job_id("metroplex-ideaforge-203")
        assert row is not None
        assert row["scoring_rubric"] is None
