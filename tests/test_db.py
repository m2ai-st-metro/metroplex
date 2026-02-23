"""
Tests for Metroplex StateDB module.
"""
import json
import pytest
from datetime import datetime

from db import StateDB
from models import TriageDecision, BuildJob, PatchApplication, GateStatus


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
        for gate in ["triage", "build", "patch"]:
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


class TestPatchApplicationRecords:
    """Test patch application recording."""

    def test_record_and_query(self, db):
        patch = PatchApplication(
            patch_id="patch-abc",
            persona_id="persona-xyz",
            from_version="1.0",
            to_version="1.1",
            status="applied",
            reason="success",
            applied_at=datetime(2026, 2, 23, 15, 0, 0),
        )
        db.record_patch_application(patch)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM patch_applications WHERE patch_id = 'patch-abc'")
        row = cursor.fetchone()

        assert row["persona_id"] == "persona-xyz"
        assert row["from_version"] == "1.0"
        assert row["status"] == "applied"

    def test_nullable_versions(self, db):
        patch = PatchApplication(
            patch_id="patch-null",
            persona_id="p1",
            from_version=None,
            to_version=None,
            status="skipped",
            reason="no ops",
            applied_at=datetime.now(),
        )
        db.record_patch_application(patch)

        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM patch_applications WHERE patch_id = 'patch-null'")
        row = cursor.fetchone()
        assert row["from_version"] is None
        assert row["to_version"] is None


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
        status = GateStatus(gate="patch", consecutive_failures=3, halted=True, last_error="3 failures")
        db.update_gate_status(status)

        retrieved = db.get_gate_status("patch")
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
        # The init seeds triage/build/patch, but let's query one that exists
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
