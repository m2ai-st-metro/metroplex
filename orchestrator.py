"""
Metroplex Cycle Orchestrator
Sequences all three gates into cycles with safety systems integration.
"""
import time
from datetime import datetime
from typing import Optional

from config import Config
from models import CycleResult
from db import StateDB
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import BuildOrchestrator
from gates.patcher import PatchGate


class CycleOrchestrator:
    """Orchestrates full Metroplex cycles (triage → build → patch)."""

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
        cycle_sleep_seconds: int = 60
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

    def run_cycle(self, dry_run: bool = False) -> CycleResult:
        """
        Run a single Metroplex cycle: triage → build → patch.

        Process:
        1. Log cycle start
        2. Run Gate 1 (triage) - skip if circuit breaker halted
        3. Run Gate 2 (build) - skip if circuit breaker halted, feed approved ideas from step 2
        4. Run Gate 3 (patch) - skip if circuit breaker halted
        5. For each gate: on success → circuit_breaker.record_success(), on exception → circuit_breaker.record_failure()
        6. Log cycle end with summary
        7. Return CycleResult

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

        triage_count = 0
        build_count = 0
        patch_count = 0
        errors = []
        approved_ideas = []

        # Gate 1: Triage
        if self.circuit_breaker.is_halted("triage"):
            error_msg = "Gate 1 (triage) halted by circuit breaker"
            errors.append(error_msg)
            print(f"⚠ {error_msg}")
        else:
            try:
                print(f"Running Gate 1 (triage)...")
                decisions = self.triage_gate.run(dry_run=dry_run)
                triage_count = len(decisions)

                # Extract approved ideas for build gate (look up full data from IdeaForge)
                approved_ideas = []
                for d in decisions:
                    if d.decision == "approve":
                        if self.triage_gate.ideaforge_reader:
                            idea = self.triage_gate.ideaforge_reader.get_idea_by_id(d.idea_id)
                            if idea:
                                approved_ideas.append(idea)
                        else:
                            # Fallback if reader unavailable (shouldn't happen if triage succeeded)
                            approved_ideas.append({
                                "id": d.idea_id,
                                "title": d.title,
                                "description": d.title,
                                "problem_statement": d.title,
                                "target_audience": "General",
                                "artifact_type": "tool"
                            })

                self.circuit_breaker.record_success("triage")
                print(f"✓ Gate 1 completed: {triage_count} decisions, {len(approved_ideas)} approved")
            except Exception as e:
                error_msg = f"Gate 1 (triage) failed: {str(e)}"
                errors.append(error_msg)
                print(f"✗ {error_msg}")
                self.circuit_breaker.record_failure("triage", error_msg)
                self.audit_logger.log_error("triage", error_msg)

        # Gate 2: Build
        if self.circuit_breaker.is_halted("build"):
            error_msg = "Gate 2 (build) halted by circuit breaker"
            errors.append(error_msg)
            print(f"⚠ {error_msg}")
        else:
            try:
                print(f"Running Gate 2 (build) with {len(approved_ideas)} approved ideas...")
                jobs = self.build_orchestrator.run(approved_ideas, dry_run=dry_run)
                build_count = len(jobs)

                self.circuit_breaker.record_success("build")
                print(f"✓ Gate 2 completed: {build_count} build jobs")
            except Exception as e:
                error_msg = f"Gate 2 (build) failed: {str(e)}"
                errors.append(error_msg)
                print(f"✗ {error_msg}")
                self.circuit_breaker.record_failure("build", error_msg)
                self.audit_logger.log_error("build", error_msg)

        # Gate 3: Patch
        if self.circuit_breaker.is_halted("patch"):
            error_msg = "Gate 3 (patch) halted by circuit breaker"
            errors.append(error_msg)
            print(f"⚠ {error_msg}")
        else:
            try:
                print(f"Running Gate 3 (patch)...")
                patches = self.patch_gate.run(dry_run=dry_run)
                patch_count = len(patches)

                self.circuit_breaker.record_success("patch")
                print(f"✓ Gate 3 completed: {patch_count} patches")
            except Exception as e:
                error_msg = f"Gate 3 (patch) failed: {str(e)}"
                errors.append(error_msg)
                print(f"✗ {error_msg}")
                self.circuit_breaker.record_failure("patch", error_msg)
                self.audit_logger.log_error("patch", error_msg)

        # End cycle
        self.state_db.end_cycle(cycle_id, triage_count, build_count, patch_count, errors)
        self.audit_logger.log_cycle_end(cycle_id, triage_count, build_count, patch_count, errors)

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

        Process:
        1. Install shutdown handler
        2. Loop: run_cycle, check shutdown_handler.should_stop(), sleep between cycles
        3. If max_cycles > 0, stop after N cycles
        4. Return all CycleResults

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
        Get full system status.

        Returns:
            Dictionary with gate statuses, recent cycles, and pending items
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
            "pending_builds": pending_builds
        }
