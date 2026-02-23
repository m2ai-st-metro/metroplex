"""
Metroplex Safety Systems
Circuit breaker, cycle caps, and graceful shutdown handler to prevent runaway autonomous behavior.
"""
import signal
import threading
from typing import Literal

from config import Config
from db import StateDB
from models import GateStatus


class CircuitBreaker:
    """Circuit breaker to halt gates after consecutive failures."""

    def __init__(self, threshold: int = 3, state_db: StateDB = None):
        """
        Initialize circuit breaker.

        Args:
            threshold: Number of consecutive failures before halting gate
            state_db: StateDB instance for persisting gate status
        """
        self.threshold = threshold
        self.state_db = state_db

    def record_success(self, gate: Literal["triage", "build", "patch"]) -> None:
        """
        Record successful gate execution.
        Resets consecutive failures to 0.

        Args:
            gate: Gate name (triage, build, patch)
        """
        if self.state_db is None:
            return

        status = self.state_db.get_gate_status(gate)
        status.consecutive_failures = 0
        status.last_error = None
        self.state_db.update_gate_status(status)

    def record_failure(self, gate: Literal["triage", "build", "patch"], error: str) -> None:
        """
        Record failed gate execution.
        Increments consecutive failures. If >= threshold, halts the gate.

        Args:
            gate: Gate name (triage, build, patch)
            error: Error message/description
        """
        if self.state_db is None:
            return

        status = self.state_db.get_gate_status(gate)
        status.consecutive_failures += 1
        status.last_error = error

        # Halt if threshold reached
        if status.consecutive_failures >= self.threshold:
            status.halted = True

        self.state_db.update_gate_status(status)

    def is_halted(self, gate: Literal["triage", "build", "patch"]) -> bool:
        """
        Check if a gate is halted.

        Args:
            gate: Gate name (triage, build, patch)

        Returns:
            True if gate is halted, False otherwise
        """
        if self.state_db is None:
            return False

        status = self.state_db.get_gate_status(gate)
        return status.halted

    def reset(self, gate: Literal["triage", "build", "patch"]) -> None:
        """
        Manually reset a gate (for CLI reset command).
        Clears consecutive failures, unhalt, and clear error.

        Args:
            gate: Gate name (triage, build, patch)
        """
        if self.state_db is None:
            return

        status = self.state_db.get_gate_status(gate)
        status.consecutive_failures = 0
        status.halted = False
        status.last_error = None
        self.state_db.update_gate_status(status)

    def get_status(self) -> list[GateStatus]:
        """
        Get status of all gates.

        Returns:
            List of GateStatus for triage, build, and patch gates
        """
        if self.state_db is None:
            return []

        return [
            self.state_db.get_gate_status("triage"),
            self.state_db.get_gate_status("build"),
            self.state_db.get_gate_status("patch"),
        ]


class CycleCaps:
    """Per-cycle operation caps to prevent runaway behavior."""

    def __init__(self, config: Config):
        """
        Initialize cycle caps from config.

        Args:
            config: Metroplex config with max_approve_per_cycle and max_patches_per_cycle
        """
        self.max_approve = config.max_approve_per_cycle
        self.max_patches = config.max_patches_per_cycle

    def check_approve_cap(self, current_count: int) -> bool:
        """
        Check if approve cap has been reached.

        Args:
            current_count: Number of approvals so far in cycle

        Returns:
            True if under cap (can approve more), False if at/over cap
        """
        return current_count < self.max_approve

    def check_patch_cap(self, current_count: int) -> bool:
        """
        Check if patch cap has been reached.

        Args:
            current_count: Number of patches applied so far in cycle

        Returns:
            True if under cap (can apply more), False if at/over cap
        """
        return current_count < self.max_patches


class ShutdownHandler:
    """Graceful shutdown handler for SIGTERM and SIGINT signals."""

    def __init__(self):
        """Initialize shutdown handler."""
        self._stop_event = threading.Event()
        self._original_sigterm = None
        self._original_sigint = None

    def install(self) -> None:
        """
        Install signal handlers for SIGTERM and SIGINT.
        Must be called from main thread.
        """
        self._original_sigterm = signal.signal(signal.SIGTERM, self._handle_signal)
        self._original_sigint = signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame) -> None:
        """
        Signal handler that sets stop event.

        Args:
            signum: Signal number
            frame: Current stack frame
        """
        self._stop_event.set()

    def should_stop(self) -> bool:
        """
        Check if shutdown signal was received.

        Returns:
            True if SIGTERM or SIGINT received, False otherwise
        """
        return self._stop_event.is_set()

    def wait_for_completion(self, timeout: float = 30.0) -> None:
        """
        Wait for current operation to complete or timeout.
        Blocks until stop event is set or timeout expires.

        Args:
            timeout: Maximum seconds to wait (default 30.0)
        """
        self._stop_event.wait(timeout=timeout)
