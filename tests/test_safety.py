"""
Tests for Metroplex Safety Systems
Tests circuit breaker, cycle caps, and graceful shutdown handler.
"""
import pytest
import signal
import os
import time
import threading

from config import Config
from db import StateDB
from safety import CircuitBreaker, CycleCaps, ShutdownHandler


class TestCircuitBreaker:
    """Test CircuitBreaker class."""

    def test_initial_state(self, in_memory_db):
        """Test that gates start in non-halted state."""
        breaker = CircuitBreaker(threshold=3, state_db=in_memory_db)

        assert breaker.is_halted("triage") is False
        assert breaker.is_halted("build") is False
        assert breaker.is_halted("patch") is False

    def test_record_failures_below_threshold(self, in_memory_db):
        """Test recording failures below threshold does not halt."""
        breaker = CircuitBreaker(threshold=3, state_db=in_memory_db)

        # Record 2 failures
        breaker.record_failure("triage", "Error 1")
        assert breaker.is_halted("triage") is False

        breaker.record_failure("triage", "Error 2")
        assert breaker.is_halted("triage") is False

        # Verify consecutive failures count
        status = in_memory_db.get_gate_status("triage")
        assert status.consecutive_failures == 2
        assert status.last_error == "Error 2"

    def test_record_failures_at_threshold_halts(self, in_memory_db):
        """Test recording failures at threshold halts the gate."""
        breaker = CircuitBreaker(threshold=3, state_db=in_memory_db)

        # Record 3 failures
        breaker.record_failure("triage", "Error 1")
        breaker.record_failure("triage", "Error 2")
        breaker.record_failure("triage", "Error 3")

        # Should now be halted
        assert breaker.is_halted("triage") is True

        # Verify status
        status = in_memory_db.get_gate_status("triage")
        assert status.consecutive_failures == 3
        assert status.halted is True
        assert status.last_error == "Error 3"

    def test_reset_after_halt(self, in_memory_db):
        """Test reset() clears halt and failures."""
        breaker = CircuitBreaker(threshold=3, state_db=in_memory_db)

        # Halt the gate
        breaker.record_failure("build", "Error 1")
        breaker.record_failure("build", "Error 2")
        breaker.record_failure("build", "Error 3")
        assert breaker.is_halted("build") is True

        # Reset
        breaker.reset("build")

        # Should be un-halted
        assert breaker.is_halted("build") is False

        # Verify status cleared
        status = in_memory_db.get_gate_status("build")
        assert status.consecutive_failures == 0
        assert status.halted is False
        assert status.last_error is None

    def test_success_resets_consecutive_failures(self, in_memory_db):
        """Test record_success() resets consecutive failures to 0."""
        breaker = CircuitBreaker(threshold=3, state_db=in_memory_db)

        # Record 2 failures
        breaker.record_failure("patch", "Error 1")
        breaker.record_failure("patch", "Error 2")

        # Verify 2 failures
        status = in_memory_db.get_gate_status("patch")
        assert status.consecutive_failures == 2

        # Record success
        breaker.record_success("patch")

        # Should reset to 0
        status = in_memory_db.get_gate_status("patch")
        assert status.consecutive_failures == 0
        assert status.last_error is None
        assert status.halted is False

    def test_gates_independent(self, in_memory_db):
        """Test that gates have independent failure tracking."""
        breaker = CircuitBreaker(threshold=3, state_db=in_memory_db)

        # Halt triage
        breaker.record_failure("triage", "Error 1")
        breaker.record_failure("triage", "Error 2")
        breaker.record_failure("triage", "Error 3")

        # Other gates should still be un-halted
        assert breaker.is_halted("triage") is True
        assert breaker.is_halted("build") is False
        assert breaker.is_halted("patch") is False

    def test_get_status_all_gates(self, in_memory_db):
        """Test get_status() returns all gate statuses."""
        breaker = CircuitBreaker(threshold=3, state_db=in_memory_db)

        # Set different states for each gate
        breaker.record_failure("triage", "Triage error")
        breaker.record_failure("build", "Build error")
        breaker.record_failure("build", "Build error 2")

        statuses = breaker.get_status()

        assert len(statuses) == 4
        gates = {s.gate for s in statuses}
        assert gates == {"triage", "build", "publish", "patch"}

        # Find specific gate statuses
        triage = next(s for s in statuses if s.gate == "triage")
        build = next(s for s in statuses if s.gate == "build")
        patch = next(s for s in statuses if s.gate == "patch")

        assert triage.consecutive_failures == 1
        assert build.consecutive_failures == 2
        assert patch.consecutive_failures == 0

    def test_custom_threshold(self, in_memory_db):
        """Test circuit breaker with custom threshold."""
        breaker = CircuitBreaker(threshold=2, state_db=in_memory_db)

        # Should halt after 2 failures
        breaker.record_failure("triage", "Error 1")
        assert breaker.is_halted("triage") is False

        breaker.record_failure("triage", "Error 2")
        assert breaker.is_halted("triage") is True

    def test_no_state_db(self):
        """Test circuit breaker with no state_db (graceful no-op)."""
        breaker = CircuitBreaker(threshold=3, state_db=None)

        # Should not raise errors
        breaker.record_failure("triage", "Error")
        breaker.record_success("triage")
        breaker.reset("triage")

        assert breaker.is_halted("triage") is False
        assert breaker.get_status() == []


class TestCycleCaps:
    """Test CycleCaps class."""

    def test_approve_cap_under_limit(self, test_config):
        """Test approve cap returns True when under limit."""
        # Default max_approve_per_cycle is 3
        caps = CycleCaps(test_config)

        assert caps.check_approve_cap(0) is True
        assert caps.check_approve_cap(1) is True
        assert caps.check_approve_cap(2) is True

    def test_approve_cap_at_limit(self, test_config):
        """Test approve cap returns False at/over limit."""
        # Default max_approve_per_cycle is 3
        caps = CycleCaps(test_config)

        assert caps.check_approve_cap(3) is False
        assert caps.check_approve_cap(4) is False
        assert caps.check_approve_cap(100) is False

    def test_patch_cap_under_limit(self, test_config):
        """Test patch cap returns True when under limit."""
        # Default max_patches_per_cycle is 5
        caps = CycleCaps(test_config)

        assert caps.check_patch_cap(0) is True
        assert caps.check_patch_cap(1) is True
        assert caps.check_patch_cap(4) is True

    def test_patch_cap_at_limit(self, test_config):
        """Test patch cap returns False at/over limit."""
        # Default max_patches_per_cycle is 5
        caps = CycleCaps(test_config)

        assert caps.check_patch_cap(5) is False
        assert caps.check_patch_cap(6) is False
        assert caps.check_patch_cap(100) is False

    def test_custom_caps(self):
        """Test cycle caps with custom config values."""
        config = Config()
        config.max_approve_per_cycle = 2
        config.max_patches_per_cycle = 10

        caps = CycleCaps(config)

        # Approve cap at 2
        assert caps.check_approve_cap(1) is True
        assert caps.check_approve_cap(2) is False

        # Patch cap at 10
        assert caps.check_patch_cap(9) is True
        assert caps.check_patch_cap(10) is False


class TestShutdownHandler:
    """Test ShutdownHandler class."""

    def test_initial_state(self):
        """Test handler starts in non-stopped state."""
        handler = ShutdownHandler()
        assert handler.should_stop() is False

    def test_sigterm_sets_stop(self):
        """Test SIGTERM signal sets stop flag."""
        handler = ShutdownHandler()
        handler.install()

        # Send SIGTERM to self
        os.kill(os.getpid(), signal.SIGTERM)

        # Give signal time to be handled
        time.sleep(0.1)

        assert handler.should_stop() is True

    def test_sigint_sets_stop(self):
        """Test SIGINT signal sets stop flag."""
        handler = ShutdownHandler()
        handler.install()

        # Send SIGINT to self
        os.kill(os.getpid(), signal.SIGINT)

        # Give signal time to be handled
        time.sleep(0.1)

        assert handler.should_stop() is True

    def test_wait_for_completion_immediate(self):
        """Test wait_for_completion() returns immediately when stop is set."""
        handler = ShutdownHandler()
        handler.install()

        # Set stop flag
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.1)

        # Should return immediately
        start = time.time()
        handler.wait_for_completion(timeout=5.0)
        elapsed = time.time() - start

        assert elapsed < 1.0  # Should be nearly instant

    def test_wait_for_completion_timeout(self):
        """Test wait_for_completion() respects timeout."""
        handler = ShutdownHandler()

        # Don't send signal, should timeout
        start = time.time()
        handler.wait_for_completion(timeout=0.5)
        elapsed = time.time() - start

        # Should wait approximately 0.5 seconds
        assert 0.4 <= elapsed <= 0.7

    def test_multiple_handlers(self):
        """Test multiple ShutdownHandler instances have independent event flags."""
        handler1 = ShutdownHandler()
        handler2 = ShutdownHandler()

        # Only install handler1
        handler1.install()

        # Send signal
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.1)

        # handler1 should be stopped (received signal)
        assert handler1.should_stop() is True

        # handler2 should NOT be stopped (independent event flag)
        assert handler2.should_stop() is False

    def test_signal_handler_thread_safety(self):
        """Test shutdown handler is thread-safe."""
        handler = ShutdownHandler()
        handler.install()

        results = []

        def check_stop():
            time.sleep(0.1)
            results.append(handler.should_stop())

        # Start multiple threads
        threads = [threading.Thread(target=check_stop) for _ in range(5)]
        for t in threads:
            t.start()

        # Send signal while threads running
        os.kill(os.getpid(), signal.SIGTERM)

        # Wait for threads
        for t in threads:
            t.join()

        # All threads should see stop=True
        assert all(results)
