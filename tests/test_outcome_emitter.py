"""
Tests for OutcomeEmitter (Phase 14a) — outcome record emission at terminal states.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import pytest

from config import Config
from db import StateDB
from audit import AuditLogger
from models import TriageDecision, BuildJob, PublishJob
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import BuildOrchestrator
from orchestrator import CycleOrchestrator
from notifier import LogNotifier

# st-records contracts are imported via sys.path injection inside outcome_emitter.
# In CI runners (or any env without st-records as a sibling project), contracts is
# unavailable. Importing outcome_emitter triggers the sys.path injection; if the
# contracts package still cannot be resolved, skip the whole module cleanly.
import outcome_emitter  # noqa: F401 -- import triggers sys.path injection
pytest.importorskip("contracts.store")


# ---- OutcomeEmitter unit tests ----

@pytest.fixture
def st_records_data(tmp_path):
    """Create a temporary st-records data directory."""
    data_dir = tmp_path / "st_records_data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def emitter(st_records_data):
    """Create an OutcomeEmitter with a temp data dir."""
    from outcome_emitter import OutcomeEmitter
    return OutcomeEmitter(st_records_data_dir=st_records_data)


class TestOutcomeEmitter:
    """Tests for the OutcomeEmitter class."""

    def test_emit_rejected(self, emitter, st_records_data):
        """Emitting a rejected outcome writes to JSONL and SQLite."""
        result = emitter.emit(
            idea_id=42,
            idea_title="Test Idea",
            outcome="rejected",
            overall_score=35.0,
            build_outcome="triage_rejected: below threshold",
            tags=["triage"],
        )
        assert result is True
        assert emitter.emit_count == 1

        # Verify JSONL
        jsonl_path = st_records_data / "outcome_records.jsonl"
        assert jsonl_path.exists()
        line = jsonl_path.read_text().strip()
        record = json.loads(line)
        assert record["idea_id"] == 42
        assert record["outcome"] == "rejected"
        assert record["overall_score"] == 35.0

    def test_emit_published(self, emitter, st_records_data):
        """Emitting a published outcome includes github_url."""
        result = emitter.emit(
            idea_id=99,
            idea_title="Published Tool",
            outcome="published",
            github_url="https://github.com/m2ai-portfolio/published-tool",
            tags=["publish"],
        )
        assert result is True

        jsonl_path = st_records_data / "outcome_records.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        assert record["outcome"] == "published"
        assert "github.com" in record["github_url"]

    def test_emit_build_failed(self, emitter, st_records_data):
        """Emitting a build_failed outcome."""
        result = emitter.emit(
            idea_id=55,
            idea_title="Failed Build",
            outcome="build_failed",
            build_outcome="yce_build_failed: metroplex-ideaforge-55",
            tags=["build"],
        )
        assert result is True

        jsonl_path = st_records_data / "outcome_records.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        assert record["outcome"] == "build_failed"

    def test_emit_deferred(self, emitter):
        """Emitting a deferred outcome."""
        result = emitter.emit(
            idea_id=77,
            idea_title="Deferred Idea",
            outcome="deferred",
            overall_score=55.0,
            build_outcome="max_deferrals_reached (3)",
            tags=["triage"],
        )
        assert result is True
        assert emitter.emit_count == 1

    def test_emit_invalid_outcome_returns_false(self, emitter):
        """Invalid outcome value should return False."""
        result = emitter.emit(
            idea_id=1,
            idea_title="Bad",
            outcome="invalid_state",
        )
        assert result is False
        assert emitter.emit_count == 0

    def test_emit_count_increments(self, emitter):
        """emit_count tracks successful emissions."""
        emitter.emit(idea_id=1, idea_title="A", outcome="rejected")
        emitter.emit(idea_id=2, idea_title="B", outcome="published")
        emitter.emit(idea_id=3, idea_title="C", outcome="invalid")  # fails
        assert emitter.emit_count == 2

    def test_dual_write_sqlite(self, emitter, st_records_data):
        """Outcome is written to both JSONL and SQLite."""
        emitter.emit(
            idea_id=10,
            idea_title="Dual Write Test",
            outcome="published",
            github_url="https://github.com/test/repo",
        )

        # Query SQLite via the store
        records = emitter.store.query_outcomes(idea_id=10)
        assert len(records) == 1
        assert records[0].idea_title == "Dual Write Test"
        assert records[0].outcome.value == "published"

    def test_pipeline_trace(self, emitter, st_records_data):
        """Pipeline trace is correctly serialized."""
        now = datetime.now()
        emitter.emit(
            idea_id=20,
            idea_title="Trace Test",
            outcome="published",
            pipeline_trace=[
                {"stage": "triage", "entered_at": now, "exited_at": now},
                {"stage": "build", "entered_at": now},
            ],
        )

        jsonl_path = st_records_data / "outcome_records.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        assert len(record["pipeline_trace"]) == 2
        assert record["pipeline_trace"][0]["stage"] == "triage"


class TestCreateOutcomeEmitter:
    """Tests for the factory function."""

    def test_returns_emitter_when_available(self, st_records_data):
        from outcome_emitter import create_outcome_emitter
        emitter = create_outcome_emitter(st_records_data_dir=st_records_data)
        assert emitter is not None

    def test_returns_none_when_unavailable(self, tmp_path):
        """Returns None if contracts can't be imported (simulated)."""
        from outcome_emitter import create_outcome_emitter
        with patch("outcome_emitter.OutcomeEmitter.__init__", side_effect=ImportError("no contracts")):
            emitter = create_outcome_emitter()
            assert emitter is None


# ---- Orchestrator integration tests ----

@pytest.fixture
def state_db():
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def mock_emitter():
    """Create a mock OutcomeEmitter."""
    emitter = Mock()
    emitter.emit.return_value = True
    emitter.emit_count = 0

    def _increment_emit(*args, **kwargs):
        emitter.emit_count += 1
        return True

    emitter.emit.side_effect = _increment_emit
    return emitter


@pytest.fixture
def orchestrator_with_emitter(state_db, mock_emitter, tmp_path):
    """Create an orchestrator with a mock outcome emitter."""
    config = Config()
    audit_logger = AuditLogger(str(tmp_path / "audit.log"))

    triage_gate = Mock(spec=TriageGate)
    triage_gate.run.return_value = []
    triage_gate.ideaforge_reader = Mock()

    build_orch = Mock(spec=BuildOrchestrator)
    build_orch.run_from_queue.return_value = []
    build_orch.is_runner_active.return_value = False
    build_orch.poll_and_sync_status.return_value = {
        "running": [], "running_count": 0,
        "completed": [], "failed": [],
        "newly_synced": [],
    }

    circuit_breaker = CircuitBreaker(threshold=3, state_db=state_db)
    cycle_caps = CycleCaps(config)
    shutdown_handler = ShutdownHandler()

    return CycleOrchestrator(
        config=config,
        triage_gate=triage_gate,
        build_orchestrator=build_orch,
        circuit_breaker=circuit_breaker,
        cycle_caps=cycle_caps,
        shutdown_handler=shutdown_handler,
        state_db=state_db,
        audit_logger=audit_logger,
        notifier=LogNotifier(),
        outcome_emitter=mock_emitter,
    )


class TestOrchestratorEmission:
    """Test that the orchestrator emits outcomes at correct terminal states."""

    def test_triage_reject_emits_outcome(self, orchestrator_with_emitter, mock_emitter):
        """Triage rejections emit REJECTED outcomes."""
        orch = orchestrator_with_emitter
        orch.triage_gate.run.return_value = [
            TriageDecision(
                idea_id=10, title="Bad Idea", weighted_score=2.0,
                scaled_score=20.0, decision="reject",
                reason="below rejection threshold", decided_at=datetime.now(),
            ),
        ]

        orch.run_cycle(dry_run=False)

        # Check that emit was called for the rejection
        reject_calls = [
            c for c in mock_emitter.emit.call_args_list
            if c.kwargs.get("outcome") == "rejected"
        ]
        assert len(reject_calls) == 1
        assert reject_calls[0].kwargs["idea_id"] == 10
        assert reject_calls[0].kwargs["idea_title"] == "Bad Idea"

    def test_build_failure_sync_emits_outcome(self, orchestrator_with_emitter, mock_emitter, state_db):
        """Build failures detected during status sync emit BUILD_FAILED outcomes."""
        orch = orchestrator_with_emitter

        # Set up a build job that will be synced as failed
        job = BuildJob(
            idea_id=42, title="Failed Project",
            spec_path="/tmp/spec.txt", queue_job_id="metroplex-ideaforge-42",
            status="queued", queued_at=datetime.now(),
        )
        state_db.record_build_job(job)

        orch.build_orchestrator.poll_and_sync_status.return_value = {
            "running": [], "running_count": 0,
            "completed": [], "failed": ["metroplex-ideaforge-42"],
            "newly_synced": ["metroplex-ideaforge-42"],
        }

        orch.run_cycle(dry_run=False)

        fail_calls = [
            c for c in mock_emitter.emit.call_args_list
            if c.kwargs.get("outcome") == "build_failed"
        ]
        assert len(fail_calls) == 1
        assert fail_calls[0].kwargs["idea_title"] == "Failed Project"

    def test_publish_success_emits_outcome(self, orchestrator_with_emitter, mock_emitter, state_db):
        """Successful publish emits PUBLISHED outcome."""
        orch = orchestrator_with_emitter
        from gates.publish import PublishGate

        pub_gate = Mock(spec=PublishGate)
        pub_job = PublishJob(
            build_job_id="metroplex-ideaforge-99",
            title="Great Tool",
            repo_name="great-tool",
            repo_url="https://github.com/m2ai-portfolio/great-tool",
            status="published",
            project_dir="/tmp/gen/great-tool",
            published_at=datetime.now(),
        )
        pub_gate.run.return_value = [pub_job]
        orch.publish_gate = pub_gate

        # Need a matching build_job so the emitter can look up idea_id
        build = BuildJob(
            idea_id=99, title="Great Tool",
            spec_path="/tmp/spec.txt", queue_job_id="metroplex-ideaforge-99",
            status="completed", queued_at=datetime.now(),
        )
        state_db.record_build_job(build)

        orch.run_cycle(dry_run=False)

        pub_calls = [
            c for c in mock_emitter.emit.call_args_list
            if c.kwargs.get("outcome") == "published"
        ]
        assert len(pub_calls) == 1
        assert pub_calls[0].kwargs["github_url"] == "https://github.com/m2ai-portfolio/great-tool"

    def test_no_emitter_no_crash(self, state_db, tmp_path):
        """Orchestrator works fine when outcome_emitter is None."""
        config = Config()
        audit_logger = AuditLogger(str(tmp_path / "audit.log"))

        triage_gate = Mock(spec=TriageGate)
        triage_gate.run.return_value = [
            TriageDecision(
                idea_id=1, title="Test", weighted_score=2.0,
                scaled_score=20.0, decision="reject",
                reason="test", decided_at=datetime.now(),
            ),
        ]
        triage_gate.ideaforge_reader = Mock()

        build_orch = Mock(spec=BuildOrchestrator)
        build_orch.run_from_queue.return_value = []
        build_orch.is_runner_active.return_value = False
        build_orch.poll_and_sync_status.return_value = {
            "running": [], "running_count": 0,
            "completed": [], "failed": [],
            "newly_synced": [],
        }

        orch = CycleOrchestrator(
            config=config,
            triage_gate=triage_gate,
            build_orchestrator=build_orch,
            circuit_breaker=CircuitBreaker(threshold=3, state_db=state_db),
            cycle_caps=CycleCaps(config),
            shutdown_handler=ShutdownHandler(),
            state_db=state_db,
            audit_logger=audit_logger,
            notifier=LogNotifier(),
            outcome_emitter=None,  # No emitter
        )

        # Should not raise
        result = orch.run_cycle(dry_run=False)
        assert result is not None


# ---- Backfill tests ----

class TestBackfillOutcomes:
    """Tests for the backfill-outcomes CLI command."""

    def test_backfill_emits_for_rejected_ideas(self, state_db, st_records_data):
        """Backfill emits outcomes for triage rejects."""
        from outcome_emitter import OutcomeEmitter

        # Insert a triage rejection
        state_db.record_triage_decision(TriageDecision(
            idea_id=5, title="Rejected Idea", weighted_score=2.0,
            scaled_score=20.0, decision="reject",
            reason="below threshold", decided_at=datetime.now(),
        ))

        emitter = OutcomeEmitter(st_records_data_dir=st_records_data)

        # Simulate backfill logic (extracted from cmd_backfill_outcomes)
        state_db.connect()
        cursor = state_db.conn.cursor()
        cursor.execute(
            "SELECT idea_id, title, scaled_score, reason FROM triage_decisions WHERE decision = 'reject'"
        )
        for row in cursor.fetchall():
            emitter.emit(
                idea_id=row["idea_id"],
                idea_title=row["title"],
                outcome="rejected",
                overall_score=row["scaled_score"],
                build_outcome=f"triage_rejected: {row['reason']}",
                tags=["triage", "backfill"],
            )

        assert emitter.emit_count == 1
        records = emitter.store.query_outcomes(outcome="rejected")
        assert len(records) == 1
        assert records[0].idea_id == 5
        assert "backfill" in records[0].tags

    def test_backfill_skips_existing(self, state_db, st_records_data):
        """Backfill doesn't duplicate existing outcome records."""
        from outcome_emitter import OutcomeEmitter

        emitter = OutcomeEmitter(st_records_data_dir=st_records_data)

        # Pre-emit an outcome
        emitter.emit(idea_id=5, idea_title="Already Tracked", outcome="rejected")

        # Check that reading back shows the ID
        existing_ids = {rec.idea_id for rec in emitter.store.read_outcomes(limit=10000)}
        assert 5 in existing_ids

        # Insert same idea in triage
        state_db.record_triage_decision(TriageDecision(
            idea_id=5, title="Already Tracked", weighted_score=2.0,
            scaled_score=20.0, decision="reject",
            reason="test", decided_at=datetime.now(),
        ))

        # Backfill should skip it
        state_db.connect()
        cursor = state_db.conn.cursor()
        cursor.execute(
            "SELECT idea_id FROM triage_decisions WHERE decision = 'reject'"
        )
        skipped = 0
        for row in cursor.fetchall():
            if row["idea_id"] in existing_ids:
                skipped += 1

        assert skipped == 1
