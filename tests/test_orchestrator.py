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
from gates.patcher import PatchGate
from orchestrator import CycleOrchestrator
from models import TriageDecision, BuildJob, PatchApplication


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
    return orch


@pytest.fixture
def mock_patch_gate():
    """Create mock patch gate."""
    gate = Mock(spec=PatchGate)
    gate.run.return_value = [
        PatchApplication(
            patch_id="patch-1",
            persona_id="persona-1",
            from_version="1.0",
            to_version="1.1",
            status="applied",
            reason="patch applied successfully",
            applied_at=datetime.now()
        )
    ]
    return gate


@pytest.fixture
def orchestrator(
    config,
    mock_triage_gate,
    mock_build_orchestrator,
    mock_patch_gate,
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
        patch_gate=mock_patch_gate,
        circuit_breaker=circuit_breaker,
        cycle_caps=cycle_caps,
        shutdown_handler=shutdown_handler,
        state_db=state_db,
        audit_logger=audit_logger,
        cycle_sleep_seconds=1  # Short sleep for tests
    )


class TestCycleOrchestrator:
    """Test CycleOrchestrator class."""

    def test_run_cycle_success(self, orchestrator, mock_triage_gate, mock_build_orchestrator, mock_patch_gate):
        """Test successful cycle execution."""
        result = orchestrator.run_cycle(dry_run=True)

        # Verify all gates were called
        mock_triage_gate.run.assert_called_once_with(dry_run=True)
        mock_build_orchestrator.run_from_queue.assert_called_once()
        mock_patch_gate.run.assert_called_once_with(dry_run=True)

        # Verify result
        assert result.cycle_id.startswith("cycle-")
        assert result.triage_count == 1
        assert result.build_count == 1
        assert result.patch_count == 1
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
        assert "patch" in gate_names

        # Verify recent cycles
        assert len(status["recent_cycles"]) >= 1


class TestCLI:
    """Test CLI commands."""

    def test_cli_triage_dry_run(self):
        """Test 'metroplex.py triage --dry-run' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "triage", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30
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
            timeout=30
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
            timeout=60
        )

        # Should succeed (or fail gracefully if no DBs)
        assert result.returncode in [0, 1]
        assert "Gate 1" in result.stdout or "Gate 2" in result.stdout or "Gate 3" in result.stdout or "Warning" in result.stdout

    def test_cli_reset_gate(self):
        """Test 'metroplex.py reset --gate triage' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "reset", "--gate", "triage"],
            capture_output=True,
            text=True,
            timeout=30
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
            timeout=30
        )

        # Should succeed
        assert result.returncode == 0
        assert "triage" in result.stdout
        assert "build" in result.stdout
        assert "patch" in result.stdout

    def test_cli_help(self):
        """Test 'metroplex.py --help' command."""
        result = subprocess.run(
            [sys.executable, "metroplex.py", "--help"],
            capture_output=True,
            text=True,
            timeout=30
        )

        assert result.returncode == 0
        assert "Metroplex" in result.stdout
        assert "triage" in result.stdout
        assert "build" in result.stdout
        assert "patch" in result.stdout
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
