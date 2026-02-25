"""
Metroplex Cycle Orchestrator
Sequences all three gates into cycles with safety systems integration.
Includes notifications, schedule windows, and priority queue dispatch.
"""
import time
from datetime import datetime
from typing import Optional, Protocol

from config import Config
from models import CycleResult
from db import StateDB
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import BuildOrchestrator
from gates.patcher import PatchGate
from notifier import Notifier, LogNotifier


class CycleOrchestrator:
    """Orchestrates full Metroplex cycles (triage -> build -> patch)."""

    def __init__(
        self,
        config: Config,
        triage_gate: TriageGate,
        build_orchestrator: BuildOrchestrator,
        patch_gate: PatchGate,
        circuit_breaker: CircuitBreaker,
        cycle_caps: CycleCaps,
        shutdown_handler: ShutdownHandler,
        state_db: StateDB,
        audit_logger: AuditLogger,
        cycle_sleep_seconds: int = 60,
        notifier: Notifier | None = None
    ):
        """
        Initialize Cycle Orchestrator.

        Args:
            config: Metroplex configuration
            triage_gate: TriageGate instance
            build_orchestrator: BuildOrchestrator instance
            patch_gate: PatchGate instance
            circuit_breaker: CircuitBreaker instance
            cycle_caps: CycleCaps instance
            shutdown_handler: ShutdownHandler instance
            state_db: StateDB instance
            audit_logger: AuditLogger instance
            cycle_sleep_seconds: Sleep duration between cycles (default 60)
            notifier: Notification backend (defaults to LogNotifier)
        """
        self.config = config
        self.triage_gate = triage_gate
        self.build_orchestrator = build_orchestrator
        self.patch_gate = patch_gate
        self.circuit_breaker = circuit_breaker
        self.cycle_caps = cycle_caps
        self.shutdown_handler = shutdown_handler
        self.state_db = state_db
        self.audit_logger = audit_logger
        self.cycle_sleep_seconds = cycle_sleep_seconds
        self.notifier = notifier or LogNotifier()

    def is_within_schedule(self) -> bool:
        """Check if current time is within the configured schedule window."""
        now = datetime.now()
        current_hour = now.hour
        current_day = now.weekday()  # 0=Mon, 6=Sun

        # Parse active days
        try:
            active_days = [int(d.strip()) for d in self.config.active_days.split(",")]
        except ValueError:
            active_days = list(range(7))  # Default: all days

        if current_day not in active_days:
            return False

        # Handle schedule window
        start = self.config.schedule_start
        end = self.config.schedule_end

        if end == 24:
            # Always on
            return True

        if start <= end:
            # Normal range: e.g. 9-17
            return start <= current_hour < end
        else:
            # Overnight range: e.g. 22-6
            return current_hour >= start or current_hour < end

    def run_cycle(self, dry_run: bool = False) -> CycleResult:
        """
        Run a single Metroplex cycle: triage -> build -> patch.

        Build gate now pulls from the priority queue (populated by triage)
        instead of receiving approved_ideas directly.

        Args:
            dry_run: If True, run gates in dry-run mode (no DB writes)

        Returns:
            CycleResult with cycle metrics
        """
        # Generate cycle ID with microseconds to ensure uniqueness
        now = datetime.now()
        cycle_id = f"cycle-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond:06d}"

        # Start cycle
        cycle_result = self.state_db.start_cycle(cycle_id)
        self.audit_logger.log_cycle_start(cycle_id)
        self.notifier.notify(f"Metroplex cycle {cycle_id} started")

        triage_count = 0
        build_count = 0
        patch_count = 0
        errors = []

        # Gate 1: Triage (scores ideas, enqueues approved into priority_queue)
        if self.circuit_breaker.is_halted("triage"):
            error_msg = "Gate 1 (triage) halted by circuit breaker"
            errors.append(error_msg)
            print(f"! {error_msg}")
            self.notifier.notify(f"ALERT: triage gate halted by circuit breaker", "warning")
        else:
            try:
                print(f"Running Gate 1 (triage)...")
                decisions = self.triage_gate.run(dry_run=dry_run)
                triage_count = len(decisions)

                approved_count = sum(1 for d in decisions if d.decision == "approve")
                self.circuit_breaker.record_success("triage")
                print(f"+ Gate 1 completed: {triage_count} decisions, {approved_count} approved")

                if approved_count > 0:
                    titles = [d.title for d in decisions if d.decision == "approve"]
                    self.notifier.notify(
                        f"Triage approved {approved_count}: {', '.join(titles)}"
                    )
            except Exception as e:
                error_msg = f"Gate 1 (triage) failed: {str(e)}"
                errors.append(error_msg)
                print(f"x {error_msg}")
                self.circuit_breaker.record_failure("triage", error_msg)
                self.audit_logger.log_error("triage", error_msg)
                self.notifier.notify(f"Gate 1 (triage) FAILED: {str(e)}", "error")

        # Gate 2: Build (pulls from priority queue, dispatches to YCE Harness)
        if self.circuit_breaker.is_halted("build"):
            error_msg = "Gate 2 (build) halted by circuit breaker"
            errors.append(error_msg)
            print(f"! {error_msg}")
            self.notifier.notify(f"ALERT: build gate halted by circuit breaker", "warning")
        else:
            try:
                print(f"Running Gate 2 (build) from priority queue...")
                jobs = self.build_orchestrator.run_from_queue(self.state_db, dry_run=dry_run)
                build_count = len(jobs)

                self.circuit_breaker.record_success("build")
                print(f"+ Gate 2 completed: {build_count} build jobs")

                for job in jobs:
                    if job.status == "queued":
                        self.notifier.notify(f"Build queued: {job.title} (job {job.queue_job_id})")
                    elif job.status == "failed":
                        self.notifier.notify(f"Build FAILED: {job.title}", "error")
            except Exception as e:
                error_msg = f"Gate 2 (build) failed: {str(e)}"
                errors.append(error_msg)
                print(f"x {error_msg}")
                self.circuit_breaker.record_failure("build", error_msg)
                self.audit_logger.log_error("build", error_msg)
                self.notifier.notify(f"Gate 2 (build) FAILED: {str(e)}", "error")

        # Gate 3: Patch
        if self.circuit_breaker.is_halted("patch"):
            error_msg = "Gate 3 (patch) halted by circuit breaker"
            errors.append(error_msg)
            print(f"! {error_msg}")
        else:
            try:
                print(f"Running Gate 3 (patch)...")
                patches = self.patch_gate.run(dry_run=dry_run)
                patch_count = len(patches)

                self.circuit_breaker.record_success("patch")
                print(f"+ Gate 3 completed: {patch_count} patches")
            except Exception as e:
                error_msg = f"Gate 3 (patch) failed: {str(e)}"
                errors.append(error_msg)
                print(f"x {error_msg}")
                self.circuit_breaker.record_failure("patch", error_msg)
                self.audit_logger.log_error("patch", error_msg)

        # End cycle
        self.state_db.end_cycle(cycle_id, triage_count, build_count, patch_count, errors)
        self.audit_logger.log_cycle_end(cycle_id, triage_count, build_count, patch_count, errors)

        # Cycle summary notification
        error_text = f", {len(errors)} errors" if errors else ""
        self.notifier.notify(
            f"Cycle {cycle_id} complete: {triage_count} triaged, {build_count} built, {patch_count} patched{error_text}"
        )

        # Update cycle result
        cycle_result.completed_at = datetime.now()
        cycle_result.triage_count = triage_count
        cycle_result.build_count = build_count
        cycle_result.patch_count = patch_count
        cycle_result.errors = errors

        return cycle_result

    def run_continuous(self, max_cycles: int = 0, dry_run: bool = False) -> list[CycleResult]:
        """
        Run cycles continuously until max_cycles reached or shutdown signal received.
        Respects schedule windows -- sleeps when outside configured hours.

        Args:
            max_cycles: Maximum number of cycles (0 = infinite)
            dry_run: If True, run gates in dry-run mode

        Returns:
            List of CycleResult objects
        """
        # Install shutdown handler
        self.shutdown_handler.install()

        results = []
        cycle_count = 0

        print(f"Starting continuous mode (max_cycles={max_cycles}, dry_run={dry_run})")
        print(f"Schedule: {self.config.schedule_start}:00-{self.config.schedule_end}:00, days={self.config.active_days}")
        print(f"Press Ctrl+C (SIGINT) or send SIGTERM to stop gracefully\n")

        while True:
            # Check shutdown signal
            if self.shutdown_handler.should_stop():
                print("\nShutdown signal received, stopping gracefully...")
                break

            # Check max cycles
            if max_cycles > 0 and cycle_count >= max_cycles:
                print(f"\nReached max_cycles limit ({max_cycles}), stopping...")
                break

            # Check schedule window
            if not self.is_within_schedule():
                print(f"Outside schedule window, sleeping 60s...")
                for _ in range(60):
                    if self.shutdown_handler.should_stop():
                        return results
                    time.sleep(1)
                continue

            # Run cycle
            cycle_result = self.run_cycle(dry_run=dry_run)
            results.append(cycle_result)
            cycle_count += 1

            print(f"\nCycle {cycle_count} completed")
            print(f"  Triage: {cycle_result.triage_count}")
            print(f"  Build: {cycle_result.build_count}")
            print(f"  Patch: {cycle_result.patch_count}")
            print(f"  Errors: {len(cycle_result.errors)}")

            # Check if should continue
            if max_cycles > 0 and cycle_count >= max_cycles:
                break

            if self.shutdown_handler.should_stop():
                break

            # Sleep between cycles
            print(f"\nSleeping {self.cycle_sleep_seconds}s before next cycle...")
            for _ in range(self.cycle_sleep_seconds):
                if self.shutdown_handler.should_stop():
                    print("\nShutdown signal received during sleep, stopping...")
                    return results
                time.sleep(1)

        print(f"\nCompleted {cycle_count} cycles")
        return results

    def get_status(self) -> dict:
        """
        Get full system status including priority queue.

        Returns:
            Dictionary with gate statuses, recent cycles, pending items, and queue summary
        """
        gate_statuses = self.circuit_breaker.get_status()

        # Get recent cycles from DB
        self.state_db.connect()
        cursor = self.state_db.conn.cursor()
        cursor.execute("""
            SELECT cycle_id, started_at, completed_at, triage_count, build_count, patch_count, errors
            FROM cycles
            ORDER BY started_at DESC
            LIMIT 10
        """)
        recent_cycles = [dict(row) for row in cursor.fetchall()]

        # Get pending approved ideas (approved but not built)
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM triage_decisions
            WHERE decision = 'approve'
            AND idea_id NOT IN (SELECT idea_id FROM build_jobs)
        """)
        pending_builds = cursor.fetchone()["count"]

        # Priority queue summary
        queue_summary = self.state_db.get_queue_summary()

        # Runner status
        runner_active = self.build_orchestrator.is_runner_active()

        return {
            "gate_statuses": [
                {
                    "gate": gs.gate,
                    "consecutive_failures": gs.consecutive_failures,
                    "halted": gs.halted,
                    "last_error": gs.last_error
                }
                for gs in gate_statuses
            ],
            "recent_cycles": recent_cycles,
            "pending_builds": pending_builds,
            "priority_queue": queue_summary,
            "runner_active": runner_active,
            "schedule": {
                "start": self.config.schedule_start,
                "end": self.config.schedule_end,
                "active_days": self.config.active_days,
                "currently_in_window": self.is_within_schedule()
            }
        }
