"""
Metroplex Safety Systems
Circuit breaker, cycle caps, budget enforcement, and graceful shutdown handler
to prevent runaway autonomous behavior.
"""
import json
import logging
import os
import signal
import time
import threading
from pathlib import Path
from typing import Literal

from config import Config
from db import StateDB
from models import GateStatus

logger = logging.getLogger(__name__)


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

    def record_success(self, gate: Literal["triage", "build", "publish"]) -> None:
        """
        Record successful gate execution.
        Resets consecutive failures to 0.

        Args:
            gate: Gate name (triage, build, publish)
        """
        if self.state_db is None:
            return

        status = self.state_db.get_gate_status(gate)
        status.consecutive_failures = 0
        status.last_error = None
        self.state_db.update_gate_status(status)

    def record_failure(self, gate: Literal["triage", "build", "publish"], error: str) -> None:
        """
        Record failed gate execution.
        Increments consecutive failures. If >= threshold, halts the gate.

        Args:
            gate: Gate name (triage, build, publish)
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

    def is_halted(self, gate: Literal["triage", "build", "publish"]) -> bool:
        """
        Check if a gate is halted.

        Args:
            gate: Gate name (triage, build, publish)

        Returns:
            True if gate is halted, False otherwise
        """
        if self.state_db is None:
            return False

        status = self.state_db.get_gate_status(gate)
        return status.halted

    def reset(self, gate: Literal["triage", "build", "publish"]) -> None:
        """
        Manually reset a gate (for CLI reset command).
        Clears consecutive failures, unhalt, and clear error.

        Args:
            gate: Gate name (triage, build, publish)
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
            List of GateStatus for triage, build, and publish gates
        """
        if self.state_db is None:
            return []

        return [
            self.state_db.get_gate_status("triage"),
            self.state_db.get_gate_status("build"),
            self.state_db.get_gate_status("publish"),
        ]


class CycleCaps:
    """Per-cycle operation caps to prevent runaway behavior."""

    def __init__(self, config: Config):
        """
        Initialize cycle caps from config.

        Args:
            config: Metroplex config with max_approve_per_cycle
        """
        self.max_approve = config.max_approve_per_cycle

    def check_approve_cap(self, current_count: int) -> bool:
        """
        Check if approve cap has been reached.

        Args:
            current_count: Number of approvals so far in cycle

        Returns:
            True if under cap (can approve more), False if at/over cap
        """
        return current_count < self.max_approve


class BudgetEnforcer:
    """Kill running builds when spending exceeds budget limits.

    Inspired by Paperclip's budget hard-stop pattern. Checks daily and monthly
    spend against limits and sends SIGTERM to the YCE queue runner if exceeded.
    Feature-flagged via config.budget_hard_stop.
    """

    RUNNER_PID_FILE = Path(__file__).parent / "data" / "runner.pid"
    KILL_GRACE_SECONDS = 15

    def __init__(self, config: Config, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self._already_enforced = False  # Prevent repeated kills in same cycle

    def check_and_enforce(self) -> bool:
        """Check budget and kill running builds if exceeded.

        Returns True if enforcement was triggered.
        """
        if not self.config.budget_hard_stop:
            return False

        daily = self.state_db.get_daily_spend()
        monthly = self.state_db.get_monthly_spend()
        daily_limit = self.config.daily_cost_limit * self.config.budget_grace_percent
        monthly_limit = self.config.monthly_cost_limit * self.config.budget_grace_percent

        exceeded_daily = daily >= daily_limit
        exceeded_monthly = monthly >= monthly_limit

        if not exceeded_daily and not exceeded_monthly:
            self._already_enforced = False
            return False

        if self._already_enforced:
            return True  # Already killed this cycle, don't re-trigger

        trigger = []
        if exceeded_daily:
            trigger.append(f"daily=${daily:.2f}>=${daily_limit:.2f}")
        if exceeded_monthly:
            trigger.append(f"monthly=${monthly:.2f}>=${monthly_limit:.2f}")
        trigger_str = ", ".join(trigger)

        builds_killed = self._kill_running_builds()

        self.state_db.record_budget_event(
            event_type="hard_stop",
            trigger=trigger_str,
            daily_spend=daily,
            monthly_spend=monthly,
            builds_killed=builds_killed,
            details=json.dumps({
                "daily_limit": self.config.daily_cost_limit,
                "monthly_limit": self.config.monthly_cost_limit,
                "grace_percent": self.config.budget_grace_percent,
            }),
        )

        logger.warning(
            "BUDGET HARD-STOP: %s — killed %d build(s)",
            trigger_str, builds_killed,
        )

        self._already_enforced = True
        return True

    def _kill_running_builds(self) -> int:
        """Send SIGTERM to the YCE queue runner, wait, then SIGKILL if needed.

        Returns number of processes killed (0 or 1).
        """
        if not self.RUNNER_PID_FILE.exists():
            return 0

        try:
            pid = int(self.RUNNER_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            return 0

        # Check if process is alive
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            self.RUNNER_PID_FILE.unlink(missing_ok=True)
            return 0

        # SIGTERM → grace period → SIGKILL
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to runner PID %d", pid)
        except (ProcessLookupError, PermissionError):
            return 0

        time.sleep(self.KILL_GRACE_SECONDS)

        try:
            os.kill(pid, 0)  # Check if still alive
            os.kill(pid, signal.SIGKILL)
            logger.warning("Runner PID %d did not exit after SIGTERM, sent SIGKILL", pid)
        except (ProcessLookupError, PermissionError):
            pass  # Already dead, good

        self.RUNNER_PID_FILE.unlink(missing_ok=True)
        return 1


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
