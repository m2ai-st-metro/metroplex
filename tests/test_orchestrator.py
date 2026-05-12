"""
Tests for Metroplex Cycle Orchestrator and CLI
"""
import pytest
import subprocess
import sys
import signal
import time
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

from config import Config
from db import StateDB
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import SpecGenerator, BuildOrchestrator
from orchestrator import CycleOrchestrator
from notifier import LogNotifier
from models import TriageDecision, BuildJob, PriorityItem


def make_pq_item(**kwargs) -> PriorityItem:
    """Test helper: construct a PriorityItem with a non-empty idea_data default.

    The enqueue_item guard rejects items with empty idea_data. Orchestrator
    tests don't care about payload contents — this helper supplies a minimal
    valid placeholder.
    """
    kwargs.setdefault("idea_data", '{"test": true}')
    return PriorityItem(**kwargs)


@pytest.fixture
def config():
    """Create test configuration."""
    return Config()


@pytest.fixture
def state_db():
    """Create in-memory state database."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def audit_logger(tmp_path):
    """Create test audit logger."""
    log_path = tmp_path / "test_audit.log"
    return AuditLogger(str(log_path))


@pytest.fixture
def circuit_breaker(state_db):
    """Create test circuit breaker."""
    return CircuitBreaker(threshold=3, state_db=state_db)


@pytest.fixture
def cycle_caps(config):
    """Create test cycle caps."""
    return CycleCaps(config)


@pytest.fixture
def shutdown_handler():
    """Create test shutdown handler."""
    return ShutdownHandler()


@pytest.fixture
def mock_triage_gate():
    """Create mock triage gate."""
    gate = Mock(spec=TriageGate)
    gate.run.return_value = [
        TriageDecision(
            idea_id=1,
            title="Test Idea",
            weighted_score=8.5,
            scaled_score=85.0,
            decision="approve",
            reason="meets approval threshold",
            decided_at=datetime.now()
        )
    ]
    # Mock the ideaforge_reader so orchestrator can look up full idea data
    gate.ideaforge_reader = Mock()
    gate.ideaforge_reader.get_idea_by_id.return_value = {
        "id": 1,
        "title": "Test Idea",
        "description": "A test idea description",
        "problem_statement": "Test problem",
        "target_audience": "Developers",
        "artifact_type": "agent",
        "weighted_score": 8.5,
        "signal_count": 5,
    }
    return gate


@pytest.fixture
def mock_build_orchestrator():
    """Create mock build orchestrator."""
    orch = Mock(spec=BuildOrchestrator)
    build_jobs = [
        BuildJob(
            idea_id=1,
            title="Test Idea",
            spec_path="/tmp/spec_1.txt",
            queue_job_id="metroplex-1",
            status="queued",
            queued_at=datetime.now()
        )
    ]
    orch.run.return_value = build_jobs
    orch.run_from_queue.return_value = build_jobs
    orch.is_runner_active.return_value = False
    orch.poll_and_sync_status.return_value = {
        "running": [], "running_count": 0,
        "completed": [], "failed": [],
        "newly_synced": [],
    }
    return orch


@pytest.fixture
def orchestrator(
    config,
    mock_triage_gate,
    mock_build_orchestrator,
    circuit_breaker,
    cycle_caps,
    shutdown_handler,
    state_db,
    audit_logger
):
    """Create test orchestrator with mocked gates."""
    return CycleOrchestrator(
        config=config,
        triage_gate=mock_triage_gate,
        build_orchestrator=mock_build_orchestrator,
        circuit_breaker=circuit_breaker,
        cycle_caps=cycle_caps,
        shutdown_handler=shutdown_handler,
        state_db=state_db,
        audit_logger=audit_logger,
        cycle_sleep_seconds=1  # Short sleep for tests
    )


class TestCycleOrchestrator:
    """Test CycleOrchestrator class."""

    def test_run_cycle_success(self, orchestrator, mock_triage_gate, mock_build_orchestrator):
        """Test successful cycle execution."""
        result = orchestrator.run_cycle(dry_run=True)

        # Verify all gates were called
        mock_triage_gate.run.assert_called_once_with(dry_run=True)
        mock_build_orchestrator.run_from_queue.assert_called_once()

        # Verify result
        assert result.cycle_id.startswith("cycle-")
        assert result.triage_count == 1
        assert result.build_count == 1
        assert len(result.errors) == 0
        assert result.completed_at is not None

    def test_run_cycle_with_halted_gate(self, orchestrator, circuit_breaker, mock_triage_gate):
        """Test cycle with halted gate."""
        # Halt triage gate
        circuit_breaker.record_failure("triage", "test error")
        circuit_breaker.record_failure("triage", "test error")
        circuit_breaker.record_failure("triage", "test error")

        assert circuit_breaker.is_halted("triage")

        result = orchestrator.run_cycle(dry_run=True)

        # Triage gate should not be called
        mock_triage_gate.run.assert_not_called()

        # Error should be recorded
        assert len(result.errors) > 0
        assert "triage" in result.errors[0].lower()
        assert "halted" in result.errors[0].lower()

    def test_run_cycle_with_gate_failure(self, orchestrator, mock_triage_gate, circuit_breaker):
        """Test cycle with gate failure."""
        # Make triage gate raise exception
        mock_triage_gate.run.side_effect = Exception("Test failure")

        result = orchestrator.run_cycle(dry_run=True)

        # Verify circuit breaker recorded failure
        status = circuit_breaker.state_db.get_gate_status("triage")
        assert status.consecutive_failures == 1
        assert status.last_error == "Gate 1 (triage) failed: Test failure"

        # Verify error in cycle result
        assert len(result.errors) > 0
        assert "triage" in result.errors[0].lower()

    def test_run_continuous_with_max_cycles(self, orchestrator):
        """Test continuous mode with max cycles."""
        results = orchestrator.run_continuous(max_cycles=2, dry_run=True)

        assert len(results) == 2
        assert all(r.completed_at is not None for r in results)

    def test_run_continuous_with_shutdown_signal(self, orchestrator, shutdown_handler):
        """Test continuous mode with shutdown signal."""
        # Trigger shutdown after short delay
        def trigger_shutdown():
            time.sleep(0.5)
            shutdown_handler._stop_event.set()

        import threading
        shutdown_thread = threading.Thread(target=trigger_shutdown)
        shutdown_thread.start()

        results = orchestrator.run_continuous(max_cycles=10, dry_run=True)
        shutdown_thread.join()

        # Should have stopped before reaching 10 cycles
        assert len(results) < 10

    def test_get_status(self, orchestrator, state_db):
        """Test get_status method."""
        # Run a cycle first to populate data
        orchestrator.run_cycle(dry_run=True)

        status = orchestrator.get_status()

        # Verify structure
        assert "gate_statuses" in status
        assert "recent_cycles" in status
        assert "pending_builds" in status

        # Verify gate statuses
        assert len(status["gate_statuses"]) == 3
        gate_names = [gs["gate"] for gs in status["gate_statuses"]]
        assert "triage" in gate_names
        assert "build" in gate_names
        assert "publish" in gate_names

        # Verify recent cycles
        assert len(status["recent_cycles"]) >= 1


class TestCLI:
    """Test CLI commands."""

    METROPLEX_DIR = str(Path(__file__).parent.parent)

    def test_cli_triage_dry_run(self):
        """Test 'metroplex.py triage --dry-run' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "triage", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.METROPLEX_DIR
        )

        # Should succeed (or fail gracefully if no IdeaForge DB)
        assert result.returncode in [0, 1]
        assert "Gate 1" in result.stdout or "Warning" in result.stdout

    def test_cli_status(self):
        """Test 'metroplex.py status' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "status"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.METROPLEX_DIR
        )

        # Should succeed
        assert result.returncode == 0
        assert "METROPLEX STATUS" in result.stdout
        assert "Gate Status:" in result.stdout

    def test_cli_run_all_dry_run(self):
        """Test 'metroplex.py run-all --dry-run' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "run-all", "--dry-run", "--cycles", "1"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=self.METROPLEX_DIR
        )

        # Should succeed (or fail gracefully if no DBs)
        assert result.returncode in [0, 1]
        assert "Gate 1" in result.stdout or "Gate 2" in result.stdout or "Gate 4" in result.stdout or "Warning" in result.stdout

    def test_cli_reset_gate(self):
        """Test 'metroplex.py reset --gate triage' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "reset", "--gate", "triage"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.METROPLEX_DIR
        )

        # Should succeed
        assert result.returncode == 0
        assert "Reset circuit breaker for triage gate" in result.stdout

    def test_cli_reset_all_gates(self):
        """Test 'metroplex.py reset --gate all' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "reset", "--gate", "all"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.METROPLEX_DIR
        )

        # Should succeed
        assert result.returncode == 0
        assert "triage" in result.stdout
        assert "build" in result.stdout
        assert "publish" in result.stdout

    def test_cli_help(self):
        """Test 'metroplex.py --help' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.METROPLEX_DIR
        )

        assert result.returncode == 0
        assert "Metroplex" in result.stdout
        assert "triage" in result.stdout
        assert "build" in result.stdout
        assert "publish" in result.stdout
        assert "run-all" in result.stdout
        assert "status" in result.stdout
        assert "reset" in result.stdout


class TestCircuitBreakerIntegration:
    """Test circuit breaker integration with orchestrator."""

    def test_circuit_breaker_halts_gate_after_threshold(self, orchestrator, circuit_breaker, mock_triage_gate):
        """Test that circuit breaker halts gate after threshold failures."""
        # Make triage gate fail
        mock_triage_gate.run.side_effect = Exception("Persistent failure")

        # Run cycles until gate is halted
        for i in range(5):
            orchestrator.run_cycle(dry_run=True)

        # Verify gate is halted
        assert circuit_breaker.is_halted("triage")

        # Verify subsequent cycles skip halted gate
        mock_triage_gate.run.reset_mock()
        orchestrator.run_cycle(dry_run=True)
        mock_triage_gate.run.assert_not_called()

    def test_circuit_breaker_resets_on_success(self, orchestrator, circuit_breaker, mock_triage_gate):
        """Test that circuit breaker resets on successful execution."""
        # Record some failures
        circuit_breaker.record_failure("triage", "error 1")
        circuit_breaker.record_failure("triage", "error 2")

        status = circuit_breaker.state_db.get_gate_status("triage")
        assert status.consecutive_failures == 2

        # Run successful cycle
        orchestrator.run_cycle(dry_run=True)

        # Verify failures reset
        status = circuit_breaker.state_db.get_gate_status("triage")
        assert status.consecutive_failures == 0
        assert status.last_error is None


class TestShutdownHandlerIntegration:
    """Test shutdown handler integration with orchestrator."""

    def test_shutdown_handler_graceful_stop(self, orchestrator, shutdown_handler):
        """Test graceful shutdown during continuous mode."""
        # Install handler
        shutdown_handler.install()

        # Trigger stop after short delay
        def trigger_stop():
            time.sleep(0.5)
            shutdown_handler._stop_event.set()

        import threading
        stop_thread = threading.Thread(target=trigger_stop)
        stop_thread.start()

        # Run continuous mode
        results = orchestrator.run_continuous(max_cycles=100, dry_run=True)
        stop_thread.join()

        # Should have stopped gracefully before 100 cycles
        assert len(results) < 100
        assert all(r.completed_at is not None for r in results)


class TestScheduleWindows:
    """Test schedule window logic in CycleOrchestrator."""

    def _make_orchestrator(self, config, state_db, audit_logger):
        """Helper to create a minimal orchestrator for schedule tests."""
        mock_triage = Mock(spec=TriageGate)
        mock_triage.run.return_value = []
        mock_build = Mock(spec=BuildOrchestrator)
        mock_build.run_from_queue.return_value = []
        mock_build.is_runner_active.return_value = False
        cb = CircuitBreaker(threshold=3, state_db=state_db)
        cc = CycleCaps(config)
        sh = ShutdownHandler()

        return CycleOrchestrator(
            config=config,
            triage_gate=mock_triage,
            build_orchestrator=mock_build,
            circuit_breaker=cb,
            cycle_caps=cc,
            shutdown_handler=sh,
            state_db=state_db,
            audit_logger=audit_logger,
        )

    def test_always_on_schedule(self, config, state_db, audit_logger):
        """schedule_end=24 means always on."""
        config.schedule_start = 0
        config.schedule_end = 24
        config.active_days = "0,1,2,3,4,5,6"
        orch = self._make_orchestrator(config, state_db, audit_logger)
        assert orch.is_within_schedule() is True

    def test_within_normal_range(self, config, state_db, audit_logger):
        """9am-5pm range, current hour is noon."""
        config.schedule_start = 9
        config.schedule_end = 17
        config.active_days = "0,1,2,3,4,5,6"
        orch = self._make_orchestrator(config, state_db, audit_logger)

        with patch("orchestrator.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 12
            mock_now.weekday.return_value = 2  # Wednesday
            mock_dt.now.return_value = mock_now
            assert orch.is_within_schedule() is True

    def test_outside_normal_range(self, config, state_db, audit_logger):
        """9am-5pm range, current hour is 8am -- outside."""
        config.schedule_start = 9
        config.schedule_end = 17
        config.active_days = "0,1,2,3,4,5,6"
        orch = self._make_orchestrator(config, state_db, audit_logger)

        with patch("orchestrator.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 8
            mock_now.weekday.return_value = 2
            mock_dt.now.return_value = mock_now
            assert orch.is_within_schedule() is False

    def test_overnight_range_evening(self, config, state_db, audit_logger):
        """22:00-06:00 range, current hour is 23 -- inside."""
        config.schedule_start = 22
        config.schedule_end = 6
        config.active_days = "0,1,2,3,4,5,6"
        orch = self._make_orchestrator(config, state_db, audit_logger)

        with patch("orchestrator.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 23
            mock_now.weekday.return_value = 1
            mock_dt.now.return_value = mock_now
            assert orch.is_within_schedule() is True

    def test_overnight_range_morning(self, config, state_db, audit_logger):
        """22:00-06:00 range, current hour is 3am -- inside."""
        config.schedule_start = 22
        config.schedule_end = 6
        config.active_days = "0,1,2,3,4,5,6"
        orch = self._make_orchestrator(config, state_db, audit_logger)

        with patch("orchestrator.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 3
            mock_now.weekday.return_value = 1
            mock_dt.now.return_value = mock_now
            assert orch.is_within_schedule() is True

    def test_overnight_range_outside(self, config, state_db, audit_logger):
        """22:00-06:00 range, current hour is 12 noon -- outside."""
        config.schedule_start = 22
        config.schedule_end = 6
        config.active_days = "0,1,2,3,4,5,6"
        orch = self._make_orchestrator(config, state_db, audit_logger)

        with patch("orchestrator.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 12
            mock_now.weekday.return_value = 1
            mock_dt.now.return_value = mock_now
            assert orch.is_within_schedule() is False

    def test_wrong_day_rejected(self, config, state_db, audit_logger):
        """Weekdays only (Mon-Fri), current is Saturday."""
        config.schedule_start = 0
        config.schedule_end = 24
        config.active_days = "0,1,2,3,4"  # Mon-Fri
        orch = self._make_orchestrator(config, state_db, audit_logger)

        with patch("orchestrator.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 12
            mock_now.weekday.return_value = 5  # Saturday
            mock_dt.now.return_value = mock_now
            assert orch.is_within_schedule() is False

    def test_invalid_active_days_defaults_to_all(self, config, state_db, audit_logger):
        """Invalid active_days string falls back to all days."""
        config.schedule_start = 0
        config.schedule_end = 24
        config.active_days = "invalid"
        orch = self._make_orchestrator(config, state_db, audit_logger)
        # Should not raise, falls back to all days
        assert orch.is_within_schedule() is True


class TestOrchestratorNotifications:
    """Test notification integration in CycleOrchestrator."""

    def test_cycle_notifies_on_triage_approval(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps,
        shutdown_handler, state_db, audit_logger
    ):
        """Test that approved ideas trigger a notification."""
        mock_notifier = Mock()
        mock_notifier.notify.return_value = True

        orch = CycleOrchestrator(
            config=config,
            triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker,
            cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler,
            state_db=state_db,
            audit_logger=audit_logger,
            notifier=mock_notifier,
        )

        orch.run_cycle(dry_run=True)

        # Should have been called with triage approval
        notify_calls = [str(c) for c in mock_notifier.notify.call_args_list]
        approval_notified = any("approved" in c.lower() or "Triage" in c for c in notify_calls)
        assert approval_notified, f"Expected triage approval notification, got: {notify_calls}"

    def test_cycle_notifies_on_build_queued(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps,
        shutdown_handler, state_db, audit_logger
    ):
        """Test that queued builds trigger a notification."""
        mock_notifier = Mock()
        mock_notifier.notify.return_value = True

        orch = CycleOrchestrator(
            config=config,
            triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker,
            cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler,
            state_db=state_db,
            audit_logger=audit_logger,
            notifier=mock_notifier,
        )

        orch.run_cycle(dry_run=True)

        # Check for build queued notification
        notify_calls = [str(c) for c in mock_notifier.notify.call_args_list]
        build_notified = any("Build queued" in c or "built" in c.lower() for c in notify_calls)
        assert build_notified, f"Expected build notification, got: {notify_calls}"

    def test_cycle_notifies_on_error(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps,
        shutdown_handler, state_db, audit_logger
    ):
        """Test that gate failures trigger an error notification."""
        mock_triage_gate.run.side_effect = Exception("DB connection lost")

        mock_notifier = Mock()
        mock_notifier.notify.return_value = True

        orch = CycleOrchestrator(
            config=config,
            triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker,
            cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler,
            state_db=state_db,
            audit_logger=audit_logger,
            notifier=mock_notifier,
        )

        orch.run_cycle(dry_run=True)

        # Should have been called with error notification
        notify_calls = [str(c) for c in mock_notifier.notify.call_args_list]
        error_notified = any("FAIL" in c or "error" in c.lower() for c in notify_calls)
        assert error_notified, f"Expected error notification, got: {notify_calls}"

    def test_cycle_suppresses_empty_cycle_notifications(
        self, config, state_db, audit_logger
    ):
        """Empty cycles (no activity) should not send summary notifications."""
        mock_triage = Mock(spec=TriageGate)
        mock_triage.run.return_value = []  # No ideas
        mock_build = Mock(spec=BuildOrchestrator)
        mock_build.run_from_queue.return_value = []
        mock_build.is_runner_active.return_value = False

        mock_notifier = Mock()
        mock_notifier.notify.return_value = True

        cb = CircuitBreaker(threshold=3, state_db=state_db)
        cc = CycleCaps(config)
        sh = ShutdownHandler()

        orch = CycleOrchestrator(
            config=config,
            triage_gate=mock_triage,
            build_orchestrator=mock_build,
            circuit_breaker=cb,
            cycle_caps=cc,
            shutdown_handler=sh,
            state_db=state_db,
            audit_logger=audit_logger,
            notifier=mock_notifier,
        )

        orch.run_cycle(dry_run=True)

        # No activity, no errors -- should not send summary notification
        # (but the implementation might send nothing at all for empty cycles)
        summary_calls = [
            c for c in mock_notifier.notify.call_args_list
            if "triaged" in str(c).lower() and "built" in str(c).lower()
        ]
        assert len(summary_calls) == 0, "Empty cycles should suppress summary notifications"

    def test_default_notifier_is_log(self, orchestrator):
        """When no notifier is passed, default is LogNotifier."""
        assert isinstance(orchestrator.notifier, LogNotifier)


class TestGetStatusIncludesPriorityQueue:
    """Test that get_status includes priority queue and schedule info."""

    def test_status_includes_priority_queue(self, orchestrator, state_db):
        """get_status should include priority_queue summary."""
        orchestrator.run_cycle(dry_run=True)
        status = orchestrator.get_status()

        assert "priority_queue" in status
        assert "total" in status["priority_queue"]

    def test_status_includes_runner_active(self, orchestrator, state_db):
        """get_status should include runner_active flag."""
        status = orchestrator.get_status()
        assert "runner_active" in status
        assert isinstance(status["runner_active"], bool)

    def test_status_includes_schedule(self, orchestrator, state_db):
        """get_status should include schedule info."""
        status = orchestrator.get_status()
        assert "schedule" in status
        assert "start" in status["schedule"]
        assert "end" in status["schedule"]
        assert "active_days" in status["schedule"]
        assert "currently_in_window" in status["schedule"]


class TestStandaloneStatusPolling:
    """Test unconditional build status polling wired in run_cycle()."""

    def test_run_cycle_calls_poll_and_sync(
        self, orchestrator, mock_build_orchestrator
    ):
        """poll_and_sync_status is called every cycle, independent of run_from_queue."""
        result = orchestrator.run_cycle(dry_run=True)
        assert mock_build_orchestrator.poll_and_sync_status.called

    def test_run_cycle_notifies_completed_builds(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps, shutdown_handler, state_db, audit_logger
    ):
        """Completed builds from polling generate notifications."""
        mock_build_orchestrator.poll_and_sync_status.return_value = {
            "running": [], "running_count": 0,
            "completed": ["metroplex-ideaforge-5"],
            "failed": [],
            "newly_synced": ["metroplex-ideaforge-5"],
        }
        mock_notifier = Mock()
        mock_notifier.notify.return_value = True

        orch = CycleOrchestrator(
            config=config, triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker, cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler, state_db=state_db,
            audit_logger=audit_logger, cycle_sleep_seconds=1,
            notifier=mock_notifier,
        )
        orch.run_cycle(dry_run=True)

        # Check that notifier was called with a "completed" message
        notify_calls = [str(c) for c in mock_notifier.notify.call_args_list]
        assert any("completed" in c.lower() or "Build completed" in c for c in notify_calls)

    def test_run_cycle_poll_failure_nonfatal(
        self, orchestrator, mock_build_orchestrator, audit_logger
    ):
        """A polling failure doesn't prevent the rest of the cycle from running."""
        mock_build_orchestrator.poll_and_sync_status.side_effect = Exception("poll error")

        result = orchestrator.run_cycle(dry_run=True)

        # Cycle should complete without raising
        assert result.completed_at is not None


class TestSkyLynxIntake:
    """Tests for Sky-Lynx recommendation intake in CycleOrchestrator."""

    def test_ingest_skylynx_enqueues_recommendations(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps, shutdown_handler, state_db, audit_logger
    ):
        """ingest_skylynx reads pending recs and enqueues them as PriorityItems."""
        mock_reader = Mock()
        mock_reader.get_pending_recommendations.return_value = [
            {
                "id": 1,
                "recommendation_id": "sl-001",
                "session_id": "sky-lynx-2026-02-08",
                "recommendation_type": "pipeline_change",
                "target_system": "pipeline",
                "title": "Fix Session Tracking",
                "priority": "high",
                "scope": "all_personas",
                "target_department": None,
                "status": "pending",
                "emitted_at": "2026-02-08T18:00:00",
                "raw_json": {
                    "description": "Session tracking is broken",
                    "suggested_change": "Add tracking",
                },
            }
        ]
        mock_reader.priority_to_score.return_value = 85.0
        mock_reader.recommendation_to_idea.return_value = {
            "id": "sl-001", "title": "Fix Session Tracking",
            "description": "Session tracking is broken", "artifact_type": "tool",
        }
        mock_reader.mark_dispatched.return_value = None

        orch = CycleOrchestrator(
            config=config, triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker, cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler, state_db=state_db,
            audit_logger=audit_logger, cycle_sleep_seconds=1,
            skylynx_reader=mock_reader,
        )

        count = orch.ingest_skylynx(dry_run=False)

        assert count == 1
        mock_reader.mark_dispatched.assert_called_once_with("sl-001")

        # Verify item was enqueued in the priority queue
        summary = state_db.get_queue_summary()
        assert summary.get("pending", 0) == 1
        assert summary.get("total", 0) == 1

    def test_ingest_skylynx_applies_weight(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps, shutdown_handler, state_db, audit_logger
    ):
        """Priority score is base_score * skylynx_weight."""
        config.skylynx_weight = 1.5

        mock_reader = Mock()
        mock_reader.get_pending_recommendations.return_value = [
            {
                "id": 1, "recommendation_id": "sl-w01",
                "recommendation_type": "pipeline_change", "target_system": "pipeline",
                "title": "Weighted Test", "priority": "high",
                "scope": "", "target_department": None,
                "status": "pending", "emitted_at": "2026-02-08T18:00:00",
                "raw_json": {"description": "Test"},
            }
        ]
        mock_reader.priority_to_score.return_value = 85.0  # high
        mock_reader.recommendation_to_idea.return_value = {"id": "sl-w01", "title": "Weighted Test"}
        mock_reader.mark_dispatched.return_value = None

        orch = CycleOrchestrator(
            config=config, triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker, cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler, state_db=state_db,
            audit_logger=audit_logger, skylynx_reader=mock_reader,
        )
        orch.ingest_skylynx(dry_run=False)

        # Check the enqueued item's priority_score = 85.0 * 1.5 = 127.5
        item = state_db.get_next_pending()
        assert item is not None
        assert item.priority_score == 85.0 * 1.5

    def test_ingest_skylynx_dry_run_no_writes(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps, shutdown_handler, state_db, audit_logger
    ):
        """Dry run counts items but does not enqueue or mark dispatched."""
        mock_reader = Mock()
        mock_reader.get_pending_recommendations.return_value = [
            {
                "id": 1, "recommendation_id": "sl-dry",
                "recommendation_type": "claude_md_update", "target_system": "claude_md",
                "title": "Dry Run Rec", "priority": "medium",
                "scope": "", "target_department": None,
                "status": "pending", "emitted_at": "2026-02-08T18:00:00",
                "raw_json": {"description": "Dry run test"},
            }
        ]
        mock_reader.priority_to_score.return_value = 70.0
        mock_reader.recommendation_to_idea.return_value = {"id": "sl-dry", "title": "Dry Run Rec"}

        orch = CycleOrchestrator(
            config=config, triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker, cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler, state_db=state_db,
            audit_logger=audit_logger, skylynx_reader=mock_reader,
        )
        count = orch.ingest_skylynx(dry_run=True)

        assert count == 1
        mock_reader.mark_dispatched.assert_not_called()
        # Queue should be empty
        summary = state_db.get_queue_summary()
        assert summary.get("total", 0) == 0

    def test_ingest_skylynx_no_reader_returns_zero(self, orchestrator):
        """When skylynx_reader is None, ingest returns 0."""
        assert orchestrator.skylynx_reader is None
        count = orchestrator.ingest_skylynx(dry_run=False)
        assert count == 0

    def test_run_cycle_calls_ingest_skylynx(
        self, config, mock_triage_gate, mock_build_orchestrator,
        circuit_breaker, cycle_caps, shutdown_handler, state_db, audit_logger
    ):
        """run_cycle invokes Sky-Lynx intake before triage."""
        mock_reader = Mock()
        mock_reader.get_pending_recommendations.return_value = []

        orch = CycleOrchestrator(
            config=config, triage_gate=mock_triage_gate,
            build_orchestrator=mock_build_orchestrator,
            circuit_breaker=circuit_breaker, cycle_caps=cycle_caps,
            shutdown_handler=shutdown_handler, state_db=state_db,
            audit_logger=audit_logger, skylynx_reader=mock_reader,
        )
        orch.run_cycle(dry_run=True)

        # Sky-Lynx reader should have been called
        mock_reader.get_pending_recommendations.assert_called_once()


class TestDispatchIntegration:
    """Test dispatcher integration in orchestrator cycle."""

    def _make_orchestrator_with_dispatcher(
        self, config, state_db, audit_logger, dispatcher=None
    ):
        """Helper to create orchestrator with a dispatcher and empty gates."""
        mock_triage = Mock(spec=TriageGate)
        mock_triage.run.return_value = []
        mock_build = Mock(spec=BuildOrchestrator)
        mock_build.run_from_queue.return_value = []
        mock_build.is_runner_active.return_value = False
        mock_build.poll_and_sync_status.return_value = {
            "running": [], "running_count": 0,
            "completed": [], "failed": [], "newly_synced": [],
        }
        cb = CircuitBreaker(threshold=3, state_db=state_db)
        cc = CycleCaps(config)
        sh = ShutdownHandler()

        from dispatcher import LogDispatcher
        return CycleOrchestrator(
            config=config,
            triage_gate=mock_triage,
            build_orchestrator=mock_build,
            circuit_breaker=cb,
            cycle_caps=cc,
            shutdown_handler=sh,
            state_db=state_db,
            audit_logger=audit_logger,
            dispatcher=dispatcher or LogDispatcher(),
        )

    def test_dispatch_skylynx_items_from_queue(self, config, state_db, audit_logger):
        """Sky-Lynx items in the queue get dispatched to ClaudeClaw workers."""
        from models import PriorityItem
        from dispatcher import LogDispatcher

        # Enqueue a skylynx item
        item = make_pq_item(
            source="skylynx",
            source_id="sl-dispatch-001",
            title="Fix Session Tracking",
            description="Session tracking is broken",
            priority_score=85.0,
            idea_data='{"_recommendation_type": "pipeline_change", "description": "Session tracking is broken"}',
        )
        state_db.enqueue_item(item)

        dispatcher = LogDispatcher()
        orch = self._make_orchestrator_with_dispatcher(config, state_db, audit_logger, dispatcher)

        count = orch.dispatch_queue_items(dry_run=False)

        assert count == 1
        assert len(dispatcher.dispatched) == 1
        assert dispatcher.dispatched[0]["worker_type"] == "ravage"

    def test_dispatch_skips_buildable_sources(self, config, state_db, audit_logger):
        """IdeaForge/academy items are NOT dispatched (handled by Gate 2)."""
        from models import PriorityItem
        from dispatcher import LogDispatcher

        for source in ("ideaforge", "academy"):
            item = make_pq_item(
                source=source,
                source_id=f"{source}-001",
                title=f"Test {source}",
                description="Test",
                priority_score=80.0,
            )
            state_db.enqueue_item(item)

        dispatcher = LogDispatcher()
        orch = self._make_orchestrator_with_dispatcher(config, state_db, audit_logger, dispatcher)

        count = orch.dispatch_queue_items(dry_run=False)

        assert count == 0
        assert len(dispatcher.dispatched) == 0

    def test_dispatch_dry_run_no_writes(self, config, state_db, audit_logger):
        """Dry run counts items but doesn't dispatch or update status."""
        from models import PriorityItem
        from dispatcher import LogDispatcher

        item = make_pq_item(
            source="skylynx",
            source_id="sl-dry-001",
            title="Dry Run Dispatch",
            description="Test",
            priority_score=70.0,
        )
        state_db.enqueue_item(item)

        dispatcher = LogDispatcher()
        orch = self._make_orchestrator_with_dispatcher(config, state_db, audit_logger, dispatcher)

        count = orch.dispatch_queue_items(dry_run=True)

        assert count == 1
        assert len(dispatcher.dispatched) == 0  # Not actually dispatched
        # Item should still be pending
        summary = state_db.get_queue_summary()
        assert summary.get("pending", 0) == 1

    def test_dispatch_called_in_run_cycle(self, config, state_db, audit_logger):
        """run_cycle calls dispatch_queue_items."""
        from dispatcher import LogDispatcher

        dispatcher = LogDispatcher()
        orch = self._make_orchestrator_with_dispatcher(config, state_db, audit_logger, dispatcher)

        with patch.object(orch, "dispatch_queue_items", return_value=0) as mock_dispatch:
            orch.run_cycle(dry_run=True)
            mock_dispatch.assert_called_once_with(dry_run=True)

    def test_dispatch_error_does_not_halt_cycle(self, config, state_db, audit_logger):
        """Dispatch errors are non-fatal — cycle continues to Gate 4."""
        mock_dispatcher = Mock()
        mock_dispatcher.dispatch.side_effect = Exception("DB locked")

        from models import PriorityItem
        item = make_pq_item(
            source="skylynx",
            source_id="sl-err-001",
            title="Error Test",
            description="Test",
            priority_score=70.0,
        )
        state_db.enqueue_item(item)

        orch = self._make_orchestrator_with_dispatcher(config, state_db, audit_logger, mock_dispatcher)
        result = orch.run_cycle(dry_run=False)

        # Cycle should complete despite dispatch error
        assert result.completed_at is not None

    def test_dispatch_notifies_on_success(self, config, state_db, audit_logger):
        """Dispatch success triggers a notification."""
        from models import PriorityItem
        from dispatcher import LogDispatcher

        item = make_pq_item(
            source="skylynx",
            source_id="sl-notify-001",
            title="Notify Test",
            description="Test",
            priority_score=70.0,
        )
        state_db.enqueue_item(item)

        mock_notifier = Mock()
        mock_notifier.notify.return_value = True
        dispatcher = LogDispatcher()

        mock_triage = Mock(spec=TriageGate)
        mock_triage.run.return_value = []
        mock_build = Mock(spec=BuildOrchestrator)
        mock_build.run_from_queue.return_value = []
        mock_build.is_runner_active.return_value = False
        mock_build.poll_and_sync_status.return_value = {
            "running": [], "running_count": 0,
            "completed": [], "failed": [], "newly_synced": [],
        }
        orch = CycleOrchestrator(
            config=config,
            triage_gate=mock_triage,
            build_orchestrator=mock_build,
            circuit_breaker=CircuitBreaker(threshold=3, state_db=state_db),
            cycle_caps=CycleCaps(config),
            shutdown_handler=ShutdownHandler(),
            state_db=state_db,
            audit_logger=audit_logger,
            notifier=mock_notifier,
            dispatcher=dispatcher,
        )

        orch.run_cycle(dry_run=False)

        notify_calls = [str(c) for c in mock_notifier.notify.call_args_list]
        dispatched_notified = any("Dispatched" in c or "dispatched" in c for c in notify_calls)
        assert dispatched_notified, f"Expected dispatch notification, got: {notify_calls}"

    def test_dispatch_respects_per_cycle_cap(self, config, state_db, audit_logger):
        """Dispatch respects max_approve_per_cycle cap."""
        from models import PriorityItem
        from dispatcher import LogDispatcher

        config.max_approve_per_cycle = 2

        # Enqueue 5 items
        for i in range(5):
            item = make_pq_item(
                source="skylynx",
                source_id=f"sl-cap-{i:03d}",
                title=f"Cap Test {i}",
                description="Test",
                priority_score=80.0 - i,
            )
            state_db.enqueue_item(item)

        dispatcher = LogDispatcher()
        orch = self._make_orchestrator_with_dispatcher(config, state_db, audit_logger, dispatcher)

        count = orch.dispatch_queue_items(dry_run=False)

        assert count == 2  # Capped at max_approve_per_cycle
        assert len(dispatcher.dispatched) == 2


class TestCLIQueue:
    """Test CLI queue command."""

    def test_cli_queue_command(self):
        """Test 'metroplex.py queue' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "queue"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode == 0
        assert "PRIORITY QUEUE" in result.stdout
        assert "Total items:" in result.stdout


class TestQualityScoringForwardsRubric:
    """R-A item 3 (2026-05-12): the quality-scoring step reads
    build_jobs.scoring_rubric and forwards it to score_project."""

    @staticmethod
    def _seed_build(state_db, queue_job_id, project_dir, scoring_rubric=None):
        """Insert a build_jobs row with the given rubric and project_dir."""
        job = BuildJob(
            idea_id=str(queue_job_id),
            title=f"T-{queue_job_id}",
            spec_path="/tmp/spec.txt",
            queue_job_id=queue_job_id,
            status="queued",
            queued_at=datetime.now(),
            scoring_rubric=scoring_rubric,
        )
        state_db.record_build_job(job)
        # Now mark it completed + reviewed + attach project_dir
        state_db.update_build_job_status(queue_job_id, "completed")
        state_db.update_build_job_project_dir(queue_job_id, str(project_dir))
        state_db.update_build_review_status(queue_job_id, "reviewed")

    @staticmethod
    def _review_pass_result(queue_job_id, title):
        """Stub mimicking ReviewResult — duck-typed."""
        r = MagicMock()
        r.verdict = "pass"
        r.queue_job_id = queue_job_id
        r.title = title
        return r

    def test_orchestrator_forwards_life_domain_rubric(
        self, orchestrator, state_db, tmp_path,
    ):
        """C3a: when build_jobs.scoring_rubric='life_domain', score_project
        receives scoring_rubric='life_domain'."""
        # Project dir: no agent shape -> life_domain gate would fire if real.
        # We patch score_project so we only assert the kwarg.
        project_dir = tmp_path / "proj_life"
        project_dir.mkdir()
        (project_dir / "README.md").write_text("hello")

        self._seed_build(state_db, "build-life-1", project_dir,
                         scoring_rubric="life_domain")

        review_results = [self._review_pass_result("build-life-1", "T-life")]

        with patch("orchestrator.score_project") as mock_score:
            mock_breakdown = MagicMock()
            mock_breakdown.total_score = 0.0
            mock_breakdown.static_score = 0.0
            mock_breakdown.source_file_count = 0
            mock_breakdown.test_file_count = 0
            mock_breakdown.category_failed = True
            mock_breakdown.category_failure_reason = "missing_agent_yaml"
            mock_score.return_value = mock_breakdown

            scored = orchestrator._score_review_pass_builds(
                review_results, dry_run=False,
            )

        assert scored == 1
        assert mock_score.call_count == 1
        # Assert via kwargs so positional/keyword equivalence is irrelevant.
        _, kwargs = mock_score.call_args
        assert kwargs.get("scoring_rubric") == "life_domain"

    def test_orchestrator_forwards_none_rubric_when_null(
        self, orchestrator, state_db, tmp_path,
    ):
        """C3b: NULL scoring_rubric -> score_project called with rubric=None
        (backward-compat: pre-rubric rows must not be gated)."""
        project_dir = tmp_path / "proj_null"
        project_dir.mkdir()
        (project_dir / "README.md").write_text("hi")
        (project_dir / "main.py").write_text("print('hi')\n")

        self._seed_build(state_db, "build-null-1", project_dir,
                         scoring_rubric=None)

        review_results = [self._review_pass_result("build-null-1", "T-null")]

        with patch("orchestrator.score_project") as mock_score:
            mock_breakdown = MagicMock()
            mock_breakdown.total_score = 24.0
            mock_breakdown.static_score = 24.0
            mock_breakdown.source_file_count = 1
            mock_breakdown.test_file_count = 0
            mock_breakdown.category_failed = False
            mock_breakdown.category_failure_reason = None
            mock_score.return_value = mock_breakdown

            scored = orchestrator._score_review_pass_builds(
                review_results, dry_run=False,
            )

        assert scored == 1
        assert mock_score.call_count == 1
        _, kwargs = mock_score.call_args
        assert kwargs.get("scoring_rubric") is None

    def test_orchestrator_skips_when_project_dir_missing(
        self, orchestrator, state_db, tmp_path,
    ):
        """C3d: rows where project_dir is missing or non-dir are skipped
        with no score_project call and no DB update."""
        # Seed a row whose project_dir points at a non-existent path.
        bogus = tmp_path / "does_not_exist"
        # We seed without calling update_build_job_project_dir to leave NULL.
        job = BuildJob(
            idea_id="404",
            title="Phantom",
            spec_path="/tmp/spec.txt",
            queue_job_id="build-phantom",
            status="queued",
            queued_at=datetime.now(),
            scoring_rubric="life_domain",
        )
        state_db.record_build_job(job)
        state_db.update_build_job_status("build-phantom", "completed")

        review_results = [self._review_pass_result("build-phantom", "T-phantom")]

        with patch("orchestrator.score_project") as mock_score:
            scored = orchestrator._score_review_pass_builds(
                review_results, dry_run=False,
            )

        assert scored == 0
        assert mock_score.call_count == 0

    def test_orchestrator_dry_run_skips_db_write_but_still_calls_score(
        self, orchestrator, state_db, tmp_path,
    ):
        """dry_run=True: score_project still invoked (so we can observe the
        gate via audit log), but update_build_quality_score is NOT called."""
        project_dir = tmp_path / "proj_dry"
        project_dir.mkdir()
        (project_dir / "README.md").write_text("x")

        self._seed_build(state_db, "build-dry-1", project_dir,
                         scoring_rubric="life_domain")

        review_results = [self._review_pass_result("build-dry-1", "T-dry")]

        with patch("orchestrator.score_project") as mock_score, \
             patch.object(state_db, "update_build_quality_score") as mock_update:
            mock_breakdown = MagicMock()
            mock_breakdown.total_score = 0.0
            mock_breakdown.static_score = 0.0
            mock_breakdown.source_file_count = 0
            mock_breakdown.test_file_count = 0
            mock_breakdown.category_failed = True
            mock_breakdown.category_failure_reason = "missing_agent_yaml"
            mock_score.return_value = mock_breakdown
            # Re-bind the state_db on orchestrator to the patched one
            orchestrator.state_db = state_db

            scored = orchestrator._score_review_pass_builds(
                review_results, dry_run=True,
            )

        assert scored == 1
        assert mock_score.call_count == 1
        mock_update.assert_not_called()
