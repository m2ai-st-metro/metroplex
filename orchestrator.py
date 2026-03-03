"""
Metroplex Cycle Orchestrator
Sequences all four gates into cycles with safety systems integration.
Includes notifications, schedule windows, and priority queue dispatch.
"""
import json
import time
from datetime import datetime
from typing import Optional, Protocol

from config import Config
from models import CycleResult, PriorityItem
from db import StateDB
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import BuildOrchestrator
from gates.patcher import PatchGate
from gates.publish import PublishGate
from notifier import Notifier, LogNotifier
from readers.academy_reader import AcademyReader
from readers.skylynx_reader import SkyLynxReader
from readers.linear_reader import LinearReader


class CycleOrchestrator:
    """Orchestrates full Metroplex cycles (triage -> build -> publish -> patch)."""

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
        notifier: Notifier | None = None,
        skylynx_reader: SkyLynxReader | None = None,
        linear_reader: LinearReader | None = None,
        academy_reader: AcademyReader | None = None,
        publish_gate: PublishGate | None = None,
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
            skylynx_reader: SkyLynxReader instance (optional, enables Sky-Lynx intake)
            linear_reader: LinearReader instance (optional, enables Linear intake)
            academy_reader: AcademyReader instance (optional, enables Academy promotion intake)
            publish_gate: PublishGate instance (optional, enables Gate 4)
        """
        self.config = config
        self.triage_gate = triage_gate
        self.build_orchestrator = build_orchestrator
        self.patch_gate = patch_gate
        self.publish_gate = publish_gate
        self.circuit_breaker = circuit_breaker
        self.cycle_caps = cycle_caps
        self.shutdown_handler = shutdown_handler
        self.state_db = state_db
        self.audit_logger = audit_logger
        self.cycle_sleep_seconds = cycle_sleep_seconds
        self.notifier = notifier or LogNotifier()
        self.skylynx_reader = skylynx_reader
        self.linear_reader = linear_reader
        self.academy_reader = academy_reader
        # Track which gates have already sent a halted notification.
        # Prevents spamming Telegram every cycle while a breaker is tripped.
        self._halted_notified: set[str] = set()

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

    def ingest_skylynx(self, dry_run: bool = False) -> int:
        """
        Ingest pending Sky-Lynx recommendations into the priority queue.

        Sky-Lynx recommendations bypass triage (they are already analyzed)
        and enqueue directly with skylynx_weight applied.

        Args:
            dry_run: If True, count items but don't write to DB

        Returns:
            Number of recommendations enqueued
        """
        if self.skylynx_reader is None:
            return 0

        try:
            recs = self.skylynx_reader.get_pending_recommendations()
        except Exception as e:
            self.audit_logger.log_error("skylynx_intake", f"Failed to read recommendations: {e}")
            return 0

        if not recs:
            return 0

        count = 0
        for rec in recs:
            base_score = self.skylynx_reader.priority_to_score(rec.get("priority", "medium"))
            priority_score = base_score * self.config.skylynx_weight
            idea = self.skylynx_reader.recommendation_to_idea(rec)

            item = PriorityItem(
                source="skylynx",
                source_id=rec["recommendation_id"],
                title=rec["title"],
                description=rec.get("raw_json", {}).get("description", rec["title"]),
                priority_score=priority_score,
                idea_data=json.dumps(idea, default=str),
            )

            if dry_run:
                print(f"  [DRY RUN] Would enqueue Sky-Lynx: {rec['title']} (score={priority_score:.1f})")
                count += 1
            else:
                row_id = self.state_db.enqueue_item(item)
                if row_id > 0:
                    # Mark as dispatched in ST Factory DB
                    try:
                        self.skylynx_reader.mark_dispatched(rec["recommendation_id"])
                    except Exception as e:
                        self.audit_logger.log_error(
                            "skylynx_intake",
                            f"Failed to mark {rec['recommendation_id']} dispatched: {e}"
                        )
                    count += 1

        return count

    def ingest_linear(self, dry_run: bool = False) -> int:
        """
        Ingest issues from Linear into the priority queue.

        Linear issues bypass triage (they are already triaged in Linear)
        and enqueue directly with linear_weight applied.

        Args:
            dry_run: If True, count items but don't write to DB

        Returns:
            Number of issues enqueued
        """
        if self.linear_reader is None:
            return 0

        try:
            issues = self.linear_reader.get_issues()
        except Exception as e:
            self.audit_logger.log_error("linear_intake", f"Failed to read issues: {e}")
            return 0

        if not issues:
            return 0

        count = 0
        for issue in issues:
            base_score = self.linear_reader.priority_to_score(issue.get("priority", 0))
            priority_score = base_score * self.config.linear_weight
            idea = self.linear_reader.issue_to_idea(issue)

            item = PriorityItem(
                source="linear",
                source_id=issue["identifier"],
                title=issue["title"],
                description=issue.get("description", issue["title"]) or issue["title"],
                priority_score=priority_score,
                idea_data=json.dumps(idea, default=str),
            )

            if dry_run:
                print(f"  [DRY RUN] Would enqueue Linear: {issue['identifier']} {issue['title']} (score={priority_score:.1f})")
                count += 1
            else:
                row_id = self.state_db.enqueue_item(item)
                if row_id > 0:
                    count += 1  # Linear has no write-back (no "mark dispatched")

        return count

    def ingest_academy(self, dry_run: bool = False) -> int:
        """
        Ingest pending Academy persona promotions into the priority queue.

        Academy promotions bypass triage (they are pre-validated by graduation
        gates) and enqueue directly with academy_weight applied.

        Args:
            dry_run: If True, count items but don't write to DB

        Returns:
            Number of promotions enqueued
        """
        if self.academy_reader is None:
            return 0

        try:
            promotions = self.academy_reader.get_pending_promotions()
        except Exception as e:
            self.audit_logger.log_error("academy_intake", f"Failed to read promotions: {e}")
            return 0

        if not promotions:
            return 0

        count = 0
        for promo in promotions:
            base_score = self.academy_reader.priority_to_score(promo.get("priority", "high"))
            priority_score = base_score * self.config.academy_weight
            idea = self.academy_reader.promotion_to_idea(promo)

            item = PriorityItem(
                source="academy",
                source_id=promo.get("promotion_id", f"promo-{promo.get('persona_id', 'unknown')}"),
                title=f"{promo.get('persona_name', promo.get('persona_id', 'unknown'))} - Agent Build",
                description=idea["description"],
                priority_score=priority_score,
                idea_data=json.dumps(idea, default=str),
            )

            if dry_run:
                print(f"  [DRY RUN] Would enqueue Academy: {item.title} (score={priority_score:.1f})")
                count += 1
            else:
                row_id = self.state_db.enqueue_item(item)
                if row_id > 0:
                    # Mark as dispatched in promotions JSONL
                    try:
                        self.academy_reader.mark_dispatched(promo.get("promotion_id", ""))
                    except Exception as e:
                        self.audit_logger.log_error(
                            "academy_intake",
                            f"Failed to mark {promo.get('promotion_id')} dispatched: {e}"
                        )
                    count += 1

        return count

    def run_cycle(self, dry_run: bool = False) -> CycleResult:
        """
        Run a single Metroplex cycle: intake -> triage -> build -> patch.

        Intake: Sky-Lynx + Linear (bypass triage, direct to queue).
        Build gate pulls from the priority queue (populated by triage + intake).

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

        # Sky-Lynx Intake (enqueue recommendations directly into priority queue)
        skylynx_count = self.ingest_skylynx(dry_run=dry_run)
        if skylynx_count > 0:
            print(f"+ Sky-Lynx intake: {skylynx_count} recommendations enqueued")
            self.notifier.notify(f"Sky-Lynx: {skylynx_count} recommendations enqueued")

        # Linear Intake (enqueue issues directly into priority queue)
        linear_count = self.ingest_linear(dry_run=dry_run)
        if linear_count > 0:
            print(f"+ Linear intake: {linear_count} issues enqueued")
            self.notifier.notify(f"Linear: {linear_count} issues enqueued")

        # Academy Intake (enqueue persona promotions directly into priority queue)
        academy_count = self.ingest_academy(dry_run=dry_run)
        if academy_count > 0:
            print(f"+ Academy intake: {academy_count} promotions enqueued")
            self.notifier.notify(f"Academy: {academy_count} persona promotions enqueued")

        # Gate 1: Triage (scores ideas, enqueues approved into priority_queue)
        if self.circuit_breaker.is_halted("triage"):
            error_msg = "Gate 1 (triage) halted by circuit breaker"
            errors.append(error_msg)
            print(f"! {error_msg}")
            if "triage" not in self._halted_notified:
                self.notifier.notify(f"ALERT: triage gate halted by circuit breaker", "warning")
                self._halted_notified.add("triage")
        else:
            self._halted_notified.discard("triage")
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
            if "build" not in self._halted_notified:
                self.notifier.notify(f"ALERT: build gate halted by circuit breaker", "warning")
                self._halted_notified.add("build")
        else:
            self._halted_notified.discard("build")
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

        # Build Status Sync (unconditional -- syncs completed/failed builds every cycle)
        try:
            sync_result = self.build_orchestrator.poll_and_sync_status()
            newly_synced = sync_result.get("newly_synced", [])
            if newly_synced:
                completed = [jid for jid in newly_synced if jid in sync_result.get("completed", [])]
                failed = [jid for jid in newly_synced if jid in sync_result.get("failed", [])]
                if completed:
                    self.notifier.notify(f"Build completed: {', '.join(completed)}")
                if failed:
                    self.notifier.notify(f"Build failed: {', '.join(failed)}", "error")
        except Exception as e:
            # Non-fatal -- log and continue to publish gate
            self.audit_logger.log_error("build", f"Status poll failed: {e}")

        # Gate 4: Publish (push completed builds to GitHub)
        publish_count = 0
        if self.publish_gate is not None:
            if self.circuit_breaker.is_halted("publish"):
                error_msg = "Gate 4 (publish) halted by circuit breaker"
                errors.append(error_msg)
                print(f"! {error_msg}")
                if "publish" not in self._halted_notified:
                    self.notifier.notify(f"ALERT: publish gate halted by circuit breaker", "warning")
                    self._halted_notified.add("publish")
            else:
                self._halted_notified.discard("publish")
                try:
                    print(f"Running Gate 4 (publish)...")
                    pub_jobs = self.publish_gate.run(dry_run=dry_run)
                    publish_count = sum(1 for j in pub_jobs if j.status == "published")

                    self.circuit_breaker.record_success("publish")
                    print(f"+ Gate 4 completed: {len(pub_jobs)} processed, {publish_count} published")

                    for job in pub_jobs:
                        if job.status == "published":
                            self.notifier.notify(
                                f"Published: {self.config.github_org}/{job.repo_name} ({job.title})"
                            )
                        elif job.status == "failed":
                            self.notifier.notify(f"Publish FAILED: {job.title} -- {job.error}", "error")
                except Exception as e:
                    error_msg = f"Gate 4 (publish) failed: {str(e)}"
                    errors.append(error_msg)
                    print(f"x {error_msg}")
                    self.circuit_breaker.record_failure("publish", error_msg)
                    self.audit_logger.log_error("publish", error_msg)
                    self.notifier.notify(f"Gate 4 (publish) FAILED: {str(e)}", "error")

        # Gate 3: Patch
        if self.circuit_breaker.is_halted("patch"):
            error_msg = "Gate 3 (patch) halted by circuit breaker"
            errors.append(error_msg)
            print(f"! {error_msg}")
            if "patch" not in self._halted_notified:
                self.notifier.notify(f"ALERT: patch gate halted by circuit breaker", "warning")
                self._halted_notified.add("patch")
        else:
            self._halted_notified.discard("patch")
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

        # Only notify on cycles with actual activity or new errors.
        # Suppress summary when the only "errors" are halted-gate messages (already notified once).
        halted_only = all("halted by circuit breaker" in e for e in errors)
        has_activity = triage_count > 0 or build_count > 0 or publish_count > 0 or patch_count > 0
        if has_activity or (errors and not halted_only):
            error_text = f", {len(errors)} errors" if errors else ""
            pub_text = f", {publish_count} published" if publish_count > 0 else ""
            self.notifier.notify(
                f"Metroplex: {triage_count} triaged, {build_count} built{pub_text}, {patch_count} patched{error_text}"
            )

        # Update cycle result
        cycle_result.completed_at = datetime.now()
        cycle_result.triage_count = triage_count
        cycle_result.build_count = build_count
        cycle_result.publish_count = publish_count
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
            print(f"  Publish: {cycle_result.publish_count}")
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
