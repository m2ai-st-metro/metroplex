"""
Tests for Phase 5: Continuous Operation
Covers circuit breaker persistence, recovery, per-cycle cap enforcement,
SIGTERM graceful shutdown, and cycle_sleep_seconds config.
"""
import os
import sys
import signal
import time
import subprocess
import tempfile
import pytest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch

from config import Config
from db import StateDB
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import BuildOrchestrator
from gates.patcher import PatchGate
from orchestrator import CycleOrchestrator
from models import TriageDecision, BuildJob, PatchApplication


class TestCircuitBreakerPersistence:
    """Test circuit breaker state survives across DB reconnections (simulating restart)."""

    def test_state_persists_on_file_db(self):
        """Circuit breaker halt state persists when reconnecting to file-based DB."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # First connection: record failures until halted
            db1 = StateDB(db_path)
            db1.init_db()
            breaker1 = CircuitBreaker(threshold=3, state_db=db1)

            breaker1.record_failure("triage", "Error 1")
            breaker1.record_failure("triage", "Error 2")
            breaker1.record_failure("triage", "Error 3")
            assert breaker1.is_halted("triage") is True
            db1.close()

            # Second connection: state should persist (simulates restart)
            db2 = StateDB(db_path)
            db2.init_db()
            breaker2 = CircuitBreaker(threshold=3, state_db=db2)

            assert breaker2.is_halted("triage") is True
            status = db2.get_gate_status("triage")
            assert status.consecutive_failures == 3
            assert status.last_error == "Error 3"
            db2.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_recovery_halt_reset_success(self):
        """Circuit breaker recovery: halt → reset → success clears state."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Halt the gate
            db = StateDB(db_path)
            db.init_db()
            breaker = CircuitBreaker(threshold=3, state_db=db)

            breaker.record_failure("build", "Error 1")
            breaker.record_failure("build", "Error 2")
            breaker.record_failure("build", "Error 3")
            assert breaker.is_halted("build") is True

            # Reset (manual intervention)
            breaker.reset("build")
            assert breaker.is_halted("build") is False

            # Record success
            breaker.record_success("build")
            status = db.get_gate_status("build")
            assert status.consecutive_failures == 0
            assert status.halted is False
            assert status.last_error is None
            db.close()

            # Verify persists after reconnect
            db2 = StateDB(db_path)
            db2.init_db()
            breaker2 = CircuitBreaker(threshold=3, state_db=db2)
            assert breaker2.is_halted("build") is False
            db2.close()
        finally:
            Path(db_path).unlink(missing_ok=True)


class TestTriageGateCapEnforcement:
    """Test that triage gate enforces max_approve_per_cycle cap."""

    def test_max_3_approvals_from_5_high_score_ideas(self):
        """5 high-score ideas should produce 3 approve + 2 defer."""
        config = Config()
        config.max_approve_per_cycle = 3
        config.approve_threshold = 70
        config.reject_threshold = 40

        db = StateDB(":memory:")
        db.init_db()
        audit = AuditLogger(os.devnull)

        # Mock IdeaForge reader returning 5 ideas all above threshold
        mock_reader = Mock()
        mock_reader.get_unprocessed_ideas.return_value = [
            {"id": i, "title": f"Idea {i}", "weighted_score": 8.0}  # 80 scaled > 70 threshold
            for i in range(1, 6)
        ]

        gate = TriageGate(
            config=config,
            state_db=db,
            ideaforge_reader=mock_reader,
            audit_logger=audit
        )

        decisions = gate.run(dry_run=True)

        assert len(decisions) == 5
        approved = [d for d in decisions if d.decision == "approve"]
        deferred = [d for d in decisions if d.decision == "defer"]
        assert len(approved) == 3
        assert len(deferred) == 2
        # Deferred should have "per-cycle cap reached" reason
        for d in deferred:
            assert "cap" in d.reason.lower()

        db.close()


class TestPatchGateCapEnforcement:
    """Test that patch gate enforces max_patches_per_cycle cap."""

    def test_max_5_patches_from_8_available(self):
        """8 available patches should result in only 5 processed."""
        config = Config()
        config.max_patches_per_cycle = 5

        db = StateDB(":memory:")
        db.init_db()
        audit = AuditLogger(os.devnull)

        # Mock ST Factory reader returning 8 patches
        mock_reader = Mock()
        mock_reader.get_proposed_patches.return_value = [
            {
                "patch_id": f"patch-{i}",
                "persona_id": f"persona-{i}",
                "from_version": "1.0",
                "to_version": "1.1",
                "raw_json": {"operations": []},
            }
            for i in range(1, 9)
        ]

        gate = PatchGate(
            config=config,
            state_db=db,
            stfactory_reader=mock_reader,
            audit_logger=audit
        )

        patches = gate.run(dry_run=True)

        # Only 5 should be processed (cap enforced by slicing in PatchGate.run)
        assert len(patches) <= 5

        db.close()


class TestSIGTERMGracefulShutdown:
    """Test SIGTERM graceful shutdown of metroplex.py subprocess."""

    METROPLEX_DIR = str(Path(__file__).parent.parent)

    def test_sigterm_stops_run_all_gracefully(self):
        """SIGTERM stops 'metroplex.py run-all --cycles 0 --dry-run' subprocess gracefully."""
        proc = subprocess.Popen(
            [sys.executable, "metroplex.py", "run-all", "--cycles", "0", "--dry-run"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.METROPLEX_DIR,
        )

        # Give the process time to start and enter continuous mode
        time.sleep(2)

        # Send SIGTERM
        proc.send_signal(signal.SIGTERM)

        # Wait for clean exit (should not take more than 10s)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("Process did not exit within 10s after SIGTERM")

        # Should exit cleanly (0)
        assert proc.returncode == 0
        # Should show shutdown message or summary
        assert "Shutdown signal received" in stdout or "SUMMARY" in stdout or "Completed" in stdout


class TestCycleSleepSecondsConfig:
    """Test cycle_sleep_seconds configuration field."""

    def test_default_is_60(self):
        """Default cycle_sleep_seconds should be 60."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METROPLEX_CYCLE_SLEEP_SECONDS", None)
            config = Config()
            assert config.cycle_sleep_seconds == 60

    def test_env_var_override(self):
        """METROPLEX_CYCLE_SLEEP_SECONDS env var overrides default."""
        with patch.dict(os.environ, {"METROPLEX_CYCLE_SLEEP_SECONDS": "120"}):
            config = Config()
            assert config.cycle_sleep_seconds == 120

    def test_invalid_env_var_keeps_default(self):
        """Invalid METROPLEX_CYCLE_SLEEP_SECONDS preserves default."""
        with patch.dict(os.environ, {"METROPLEX_CYCLE_SLEEP_SECONDS": "not_a_number"}):
            config = Config()
            assert config.cycle_sleep_seconds == 60

    def test_validation_warning_below_10(self):
        """cycle_sleep_seconds < 10 produces a validation warning."""
        config = Config()
        config.cycle_sleep_seconds = 5
        warnings = config.validate()
        assert any("cycle_sleep_seconds" in w for w in warnings)

    def test_no_warning_at_10(self):
        """cycle_sleep_seconds = 10 should not produce a warning."""
        config = Config()
        config.cycle_sleep_seconds = 10
        warnings = config.validate()
        assert not any("cycle_sleep_seconds" in w for w in warnings)

    def test_threaded_to_orchestrator(self):
        """cycle_sleep_seconds from config is passed through to CycleOrchestrator."""
        config = Config()
        config.cycle_sleep_seconds = 30

        db = StateDB(":memory:")
        db.init_db()
        audit = AuditLogger(os.devnull)

        breaker = CircuitBreaker(threshold=3, state_db=db)
        caps = CycleCaps(config)
        handler = ShutdownHandler()

        mock_triage = Mock(spec=TriageGate)
        mock_build = Mock(spec=BuildOrchestrator)
        mock_patch = Mock(spec=PatchGate)

        orch = CycleOrchestrator(
            config=config,
            triage_gate=mock_triage,
            build_orchestrator=mock_build,
            patch_gate=mock_patch,
            circuit_breaker=breaker,
            cycle_caps=caps,
            shutdown_handler=handler,
            state_db=db,
            audit_logger=audit,
            cycle_sleep_seconds=config.cycle_sleep_seconds
        )

        assert orch.cycle_sleep_seconds == 30
        db.close()
