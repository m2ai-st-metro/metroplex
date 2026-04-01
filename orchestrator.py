"""
Metroplex Cycle Orchestrator
Sequences all four gates into cycles with safety systems integration.
Includes notifications, schedule windows, and priority queue dispatch.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Protocol

from config import Config
from models import CycleResult, PriorityItem, TriageDecision
from db import StateDB
from audit import AuditLogger
from safety import BudgetEnforcer, CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import BuildOrchestrator
from gates.patcher import PatchGate
from gates.publish import PublishGate
from gates.readme import ReadmeGate
from gates.review import ReviewGate
from gates.tyrest import TyrestGate
from notifier import Notifier, LogNotifier
from dispatcher import Dispatcher, LogDispatcher, route_to_worker, build_dispatch_prompt
from readers.academy_reader import AcademyReader
from oz_bridge import poll_oz_run
from readers.skylynx_reader import SkyLynxReader
from readers.linear_reader import LinearReader
from outcome_emitter import OutcomeEmitter
from gates.quality_scorer import score_project
from quality_ratchet import evaluate_ratchet, evaluate_test_ratchet
from postmortem import capture_postmortem, get_failure_patterns
from feasibility_scorer import resolve_prediction, adjust_feature_weights
from readers.ideaforge_writer import IdeaForgeWriter

import logging

logger = logging.getLogger(__name__)


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
        review_gate: ReviewGate | None = None,
        tyrest_gate: TyrestGate | None = None,
        dispatcher: Dispatcher | None = None,
        outcome_emitter: OutcomeEmitter | None = None,
        readme_gate: ReadmeGate | None = None,
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
            review_gate: ReviewGate instance (optional, enables Gate 4.5 code review)
            tyrest_gate: TyrestGate instance (optional, enables Gate 4.25 LLM QA review)
            dispatcher: Dispatcher for routing non-buildable items to ClaudeClaw workers
            outcome_emitter: OutcomeEmitter for writing terminal-state outcomes to ST Records
            readme_gate: ReadmeGate instance (optional, enables Gate 4.7 README enhancement)
        """
        self.config = config
        self.triage_gate = triage_gate
        self.build_orchestrator = build_orchestrator
        self.patch_gate = patch_gate
        self.publish_gate = publish_gate
        self.review_gate = review_gate
        self.tyrest_gate = tyrest_gate
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
        self.dispatcher = dispatcher or LogDispatcher()
        self.outcome_emitter = outcome_emitter
        self.readme_gate = readme_gate
        # IdeaForge writer for build outcome feedback (L5 B3)
        try:
            self.ideaforge_writer = IdeaForgeWriter(config.ideaforge_db)
        except Exception:
            self.ideaforge_writer = None
        # Budget enforcer: kills running builds when spend exceeds limits
        self.budget_enforcer = BudgetEnforcer(config, state_db)
        # Track which gates have already sent a halted notification.
        # Prevents spamming Telegram every cycle while a breaker is tripped.
        self._halted_notified: set[str] = set()

    def _write_ideaforge_outcome(self, idea_id: int, outcome: str) -> None:
        """Write a build outcome back to IdeaForge for scoring weight feedback (L5 B3).

        Best-effort: never raises exceptions that would block the cycle.
        """
        if self.ideaforge_writer is None:
            import logging
            logging.getLogger(__name__).warning(
                "IdeaForge writer not initialized, cannot write outcome for idea %d",
                idea_id,
            )
            return
        try:
            self.ideaforge_writer.write_build_outcome(idea_id, outcome)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to write IdeaForge outcome for idea %d: %s", idea_id, e,
            )

    def _backfill_ideaforge_outcomes(self) -> int:
        """Sweep for builds with terminal outcomes that were never written to IdeaForge.

        Maps build terminal states to IdeaForge outcome values:
          - status='failed' + next_retry_at='abandoned' -> 'build_failed'
          - status='failed' (non-retryable) -> 'build_failed'
          - review_status='review_failed' -> 'review_failed'
          - review_status='tyrest_rejected' -> 'tyrest_rejected'
          - status='published' or publish confirmed -> 'published'

        Returns the number of outcomes backfilled.
        """
        if self.ideaforge_writer is None:
            return 0

        import logging
        log = logging.getLogger(__name__)

        try:
            self.state_db.connect()
            # Get all idea_ids that already have outcomes in IdeaForge
            ifw_conn = self.ideaforge_writer.conn
            if ifw_conn is None:
                self.ideaforge_writer._connect()
                ifw_conn = self.ideaforge_writer.conn
            existing = set(
                r[0] for r in ifw_conn.execute(
                    "SELECT id FROM ideas WHERE build_outcome IS NOT NULL"
                ).fetchall()
            )

            # Terminal state mapping: query builds that should have outcomes
            # Priority order matters — later outcomes override earlier ones
            terminal_builds = self.state_db.conn.execute("""
                SELECT DISTINCT idea_id, status, review_status, next_retry_at
                FROM build_jobs
                WHERE status IN ('completed', 'failed')
                  AND idea_id IS NOT NULL
                  AND typeof(idea_id) != 'text' OR (typeof(idea_id) = 'text' AND idea_id GLOB '[0-9]*')
            """).fetchall()

            backfilled = 0
            # Track best outcome per idea_id (published > review_failed > build_failed)
            outcome_map: dict[int, str] = {}
            for row in terminal_builds:
                try:
                    idea_id = int(row[0])
                except (ValueError, TypeError):
                    continue

                if idea_id in existing:
                    continue

                status = row[1]
                review_status = row[2]
                next_retry_at = row[3]

                if review_status == "tyrest_rejected":
                    outcome_map[idea_id] = "tyrest_rejected"
                elif review_status == "review_failed":
                    # Don't override tyrest_rejected
                    if outcome_map.get(idea_id) != "tyrest_rejected":
                        outcome_map[idea_id] = "review_failed"
                elif status == "failed":
                    # Only set build_failed if no better outcome exists
                    if idea_id not in outcome_map:
                        outcome_map[idea_id] = "build_failed"

            for idea_id, outcome in outcome_map.items():
                try:
                    self.ideaforge_writer.write_build_outcome(idea_id, outcome)
                    backfilled += 1
                except Exception as e:
                    log.warning("Backfill failed for idea %d: %s", idea_id, e)

            return backfilled

        except Exception as e:
            log.warning("Backfill sweep error: %s", e)
            return 0

    def check_budget(self) -> tuple[bool, str]:
        """Check if spending is within daily and monthly budget limits.

        Returns:
            Tuple of (can_proceed, message).
            can_proceed is False if either limit is exceeded.
        """
        daily = self.state_db.get_daily_spend()
        monthly = self.state_db.get_monthly_spend()

        daily_limit = self.config.daily_cost_limit
        monthly_limit = self.config.monthly_cost_limit
        alert_pct = self.config.cost_alert_threshold

        # Hard limits
        if daily >= daily_limit:
            return False, f"Daily cost limit reached: ${daily:.2f} / ${daily_limit:.2f}"
        if monthly >= monthly_limit:
            return False, f"Monthly cost limit reached: ${monthly:.2f} / ${monthly_limit:.2f}"

        # Alert thresholds (warning, not blocking)
        if daily >= daily_limit * alert_pct:
            self.notifier.notify(
                f"Budget warning: daily spend ${daily:.2f} ({daily/daily_limit*100:.0f}% of ${daily_limit:.2f} limit)",
                "warning",
            )
        if monthly >= monthly_limit * alert_pct:
            self.notifier.notify(
                f"Budget warning: monthly spend ${monthly:.2f} ({monthly/monthly_limit*100:.0f}% of ${monthly_limit:.2f} limit)",
                "warning",
            )

        return True, ""

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
                    # Mark as dispatched in ST Records DB
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

    def _reset_priority_queue_for_retry(self, queue_job_id: str):
        """Reset the priority_queue item to 'pending' for a retried build.

        Called by the orchestrator after mark_build_for_retry succeeds and
        the backoff timer has expired. This is the ONLY place where
        priority_queue is reset for retries — mark_build_for_retry no longer
        does this directly, preventing the dual-path infinite loop bug.
        """
        base_id = re.sub(r'-r\d+$', '', queue_job_id)
        parts = base_id.split("-", 2)
        source = None
        source_id = None
        if len(parts) >= 3 and parts[0] == "metroplex" and parts[1] in ("ideaforge", "skylynx", "linear", "academy"):
            source = parts[1]
            source_id = parts[2]
        elif len(parts) == 2 and parts[0] == "metroplex" and parts[1].isdigit():
            source = "ideaforge"
            source_id = parts[1]

        if source and source_id:
            self.state_db.connect()
            self.state_db.conn.execute(
                "UPDATE priority_queue SET status = 'pending', completed_at = NULL "
                "WHERE source = ? AND source_id = ? AND status IN ('failed', 'dispatched')",
                (source, source_id),
            )
            self.state_db.conn.commit()

    def dispatch_queue_items(self, dry_run: bool = False) -> int:
        """
        Dispatch non-buildable priority queue items to ClaudeClaw workers.

        Buildable items (ideaforge/linear/academy) are handled by Gate 2 (build).
        Non-buildable items (skylynx) route to ClaudeClaw workers via the dispatcher.

        Args:
            dry_run: If True, log but don't actually dispatch

        Returns:
            Number of items dispatched
        """
        dispatch_sources = ("skylynx",)
        dispatched = 0
        max_dispatch = self.config.max_approve_per_cycle
        seen_ids: set[int] = set()

        for _ in range(max_dispatch):
            if dry_run:
                item = self.state_db.get_next_pending(sources=dispatch_sources)
            else:
                item = self.state_db.claim_next_pending(
                    claimer_id=f"dispatch-{os.getpid()}", sources=dispatch_sources
                )
            if item is None or item.id in seen_ids:
                break
            seen_ids.add(item.id)

            idea_data = {}
            if item.idea_data:
                try:
                    idea_data = json.loads(item.idea_data)
                except (json.JSONDecodeError, TypeError):
                    pass

            worker = route_to_worker(
                item.source,
                idea_data.get("_recommendation_type", ""),
            )
            item_dict = {
                "source": item.source,
                "source_id": item.source_id,
                "title": item.title,
                "description": item.description,
                "priority_score": item.priority_score,
                "idea_data": item.idea_data,
            }
            prompt = build_dispatch_prompt(item_dict)

            if dry_run:
                print(f"  [DRY RUN] Would dispatch #{item.id} [{item.source}:{item.source_id}] -> {worker}")
                dispatched += 1
            else:
                try:
                    task_id = self.dispatcher.dispatch(prompt, worker)
                    # Item already claimed as 'dispatched' by claim_next_pending()
                    self.state_db.set_dispatch_task_id(item.id, task_id)
                    self.audit_logger.log_decision(
                        "dispatch", "dispatched",
                        {"source_id": item.source_id, "worker": worker, "task_id": task_id[:8]}
                    )
                    dispatched += 1
                except Exception as e:
                    self.audit_logger.log_error(
                        "dispatch",
                        f"Failed to dispatch #{item.id} [{item.source}:{item.source_id}]: {e}"
                    )
                    self.state_db.update_item_status(item.id, "failed", "completed_at")

        return dispatched

    def sync_dispatch_status(self) -> dict:
        """
        Poll ClaudeClaw's dispatch_queue for completed/failed tasks and update
        the corresponding priority_queue entries.

        Returns:
            Dict with 'synced' count and lists of 'completed' and 'failed' source_ids.
        """
        result = {"synced": 0, "completed": [], "failed": []}

        dispatched_items = self.state_db.get_dispatched_items(sources=("skylynx",))
        if not dispatched_items:
            return result

        for item in dispatched_items:
            try:
                task = self.dispatcher.check_result(item["dispatch_task_id"])
            except Exception as e:
                self.audit_logger.log_error(
                    "dispatch_sync",
                    f"Failed to check task {item['dispatch_task_id']}: {e}",
                )
                continue

            if task is None:
                continue

            status = task.get("status", "")
            if status in ("completed", "done"):
                self.state_db.update_item_status(item["id"], "completed", "completed_at")
                result["completed"].append(item["source_id"])
                result["synced"] += 1
            elif status in ("failed", "error"):
                self.state_db.update_item_status(item["id"], "failed", "completed_at")
                result["failed"].append(item["source_id"])
                result["synced"] += 1

        return result


    def poll_oz_builds(self) -> dict:
        """Poll Oz cloud agent runs and sync terminal states back to DB.

        Checks all build_jobs with queue_job_id starting with 'oz-' that
        are still in 'queued' or 'running' status.

        Returns:
            dict with keys: checked, completed, failed, still_running
        """
        result = {"checked": 0, "completed": [], "failed": [], "still_running": []}

        # Get active Oz build jobs from DB
        self.state_db.connect()
        cursor = self.state_db.conn.cursor()
        cursor.execute("""
            SELECT queue_job_id, idea_id, title
            FROM build_jobs
            WHERE queue_job_id LIKE 'oz-%'
            AND status IN ('queued', 'running')
        """)
        oz_jobs = [dict(row) for row in cursor.fetchall()]

        if not oz_jobs:
            return result

        for job in oz_jobs:
            # Extract run_id from job_id (oz-<first12chars>)
            run_id_prefix = job["queue_job_id"][3:]  # strip 'oz-'
            # We need the full run_id; for now poll with prefix
            # The SDK should handle partial matching or we store full run_id
            run_status = poll_oz_run(run_id_prefix)
            result["checked"] += 1

            if run_status is None:
                continue

            state = run_status.get("state", "")

            if state == "SUCCEEDED":
                self.state_db.update_build_job_status(job["queue_job_id"], "completed")
                result["completed"].append(job["queue_job_id"])
                self.notifier.notify(
                    f"Oz cloud build completed: {job['title']}",
                )
            elif state == "FAILED":
                self.state_db.update_build_job_status(job["queue_job_id"], "failed")
                result["failed"].append(job["queue_job_id"])
                self.notifier.notify(
                    f"Oz cloud build FAILED: {job['title']}",
                    "error",
                )
            elif state in ("QUEUED", "INPROGRESS"):
                result["still_running"].append(job["queue_job_id"])

        return result

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

        # Force WAL checkpoint to ensure fresh snapshot (prevents stale reads
        # when other processes have written to the DB between cycles)
        self.state_db.connect()
        self.state_db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

        # Start cycle
        cycle_result = self.state_db.start_cycle(cycle_id)
        self.audit_logger.log_cycle_start(cycle_id)

        triage_count = 0
        build_count = 0
        patch_count = 0
        errors = []
        outcome_count_before = self.outcome_emitter.emit_count if self.outcome_emitter else 0

        # Budget hard-stop check at cycle start (kills running builds if over limit)
        if not dry_run and self.budget_enforcer.check_and_enforce():
            msg = "Budget hard-stop triggered — running builds killed"
            errors.append(msg)
            print(f"! {msg}")
            self.notifier.notify(f"BUDGET HARD-STOP: {msg}", "error")

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

                # Emit outcomes for terminal triage decisions (Phase 14a)
                if self.outcome_emitter:
                    for d in decisions:
                        if d.decision == "reject":
                            self.outcome_emitter.emit(
                                idea_id=d.idea_id,
                                idea_title=d.title,
                                outcome="rejected",
                                overall_score=d.scaled_score,
                                build_outcome=f"triage_rejected: {d.reason}",
                                tags=["triage"],
                            )
                        elif d.decision == "defer":
                            deferral_count = self.state_db.get_deferral_count(d.idea_id)
                            if deferral_count >= self.config.max_deferrals:
                                # Record a reject decision so get_triaged_idea_ids
                                # filters this idea out of future triage cycles
                                reject_decision = TriageDecision(
                                    idea_id=d.idea_id,
                                    title=d.title,
                                    weighted_score=d.weighted_score,
                                    scaled_score=d.scaled_score,
                                    decision="reject",
                                    reason=f"exceeded_max_deferrals ({deferral_count})",
                                    decided_at=d.decided_at,
                                )
                                self.state_db.record_triage_decision(reject_decision)
                                self.outcome_emitter.emit(
                                    idea_id=d.idea_id,
                                    idea_title=d.title,
                                    outcome="rejected",
                                    overall_score=d.scaled_score,
                                    build_outcome=f"max_deferrals_reached ({deferral_count})",
                                    tags=["triage"],
                                )

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

        # Budget check before build dispatch
        budget_ok, budget_msg = self.check_budget()
        if not budget_ok:
            errors.append(f"Budget exceeded: {budget_msg}")
            print(f"! {budget_msg} — skipping builds this cycle")
            self.notifier.notify(f"BUDGET EXCEEDED: {budget_msg}", "error")

        # Gate 2: Build (pulls from priority queue, dispatches to YCE Harness)
        if not budget_ok:
            pass  # Skip build gate when over budget
        elif self.circuit_breaker.is_halted("build"):
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

                # Capture structured postmortems for failed builds (L5 B1)
                if failed:
                    for job_id in failed:
                        build = self.state_db.get_build_by_queue_job_id(job_id)
                        if build:
                            # Find log file for this build
                            log_path = None
                            build_log_dir = Path("data/build_logs")
                            if build_log_dir.exists():
                                # Look for log files matching the job ID
                                for log_file in build_log_dir.glob(f"*{job_id}*"):
                                    log_path = str(log_file)
                                    break

                            capture_postmortem(
                                state_db=self.state_db,
                                queue_job_id=job_id,
                                idea_id=int(build["idea_id"]) if str(build["idea_id"]).isdigit() else 0,
                                title=build["title"],
                                log_path=log_path,
                                spec_path=build.get("spec_path"),
                                idea_score=build.get("quality_score"),
                                artifact_type=None,
                                retry_count=build.get("retry_count"),
                                review_status=build.get("review_status"),
                                quality_score=build.get("quality_score"),
                            )

                # Resolve feasibility predictions for terminal builds (L5 B2)
                for job_id in completed:
                    resolve_prediction(self.state_db, job_id, "completed")
                for job_id in failed:
                    resolve_prediction(self.state_db, job_id, "failed")

                # Write build outcomes back to IdeaForge (L5 B3)
                for job_id in failed:
                    build = self.state_db.get_build_by_queue_job_id(job_id)
                    if build and str(build["idea_id"]).isdigit():
                        self._write_ideaforge_outcome(int(build["idea_id"]), "build_failed")

                # Adjust feasibility feature weights if enough data (L5 B2)
                try:
                    adjust_feature_weights(self.state_db)
                except Exception as e:
                    self.audit_logger.log_error("build", f"Feasibility weight adjustment failed: {e}")

                # Emit outcomes for newly failed builds (Phase 14a)
                if self.outcome_emitter and failed:
                    for job_id in failed:
                        build = self.state_db.get_build_by_queue_job_id(job_id)
                        if build:
                            self.outcome_emitter.emit(
                                idea_id=int(build["idea_id"]) if str(build["idea_id"]).isdigit() else 0,
                                idea_title=build["title"],
                                outcome="build_failed",
                                build_outcome=f"yce_build_failed: {job_id}",
                                tags=["build"],
                            )
        except Exception as e:
            # Non-fatal -- log and continue to publish gate
            self.audit_logger.log_error("build", f"Status poll failed: {e}")

        # Budget hard-stop check after sync (new cost data may have arrived)
        if not dry_run and self.budget_enforcer.check_and_enforce():
            msg = "Budget hard-stop triggered after build sync"
            errors.append(msg)
            print(f"! {msg}")
            self.notifier.notify(f"BUDGET HARD-STOP: {msg}", "error")

        # Oz Cloud Build Status Sync
        if self.config.build_target in ("cloud", "auto") and self.config.oz_environment_id:
            try:
                oz_sync = self.poll_oz_builds()
                if oz_sync["completed"] or oz_sync["failed"]:
                    self.audit_logger.log_decision(
                        "build", "oz_sync",
                        {"completed": oz_sync["completed"], "failed": oz_sync["failed"]},
                    )
            except Exception as e:
                self.audit_logger.log_error("build", f"Oz build poll failed: {e}")

        # Auto-retry failed builds (Phase 13f, hardened against infinite loops)
        try:
            # 1. Abandon builds that have exhausted all retries
            exhausted = self.state_db.get_exhausted_builds()
            for build in exhausted:
                queue_job_id = build["queue_job_id"]
                if self.state_db.mark_build_abandoned(queue_job_id):
                    print(f"  ABANDONED (max {self.state_db.MAX_RETRIES} retries): {build['title']} ({queue_job_id})")
                    self.audit_logger.log_decision(
                        "build", "max_retries_exceeded",
                        {
                            "queue_job_id": queue_job_id,
                            "title": build["title"],
                            "max_retries": self.state_db.MAX_RETRIES,
                        },
                    )
                    self.notifier.notify(
                        f"Build ABANDONED after {self.state_db.MAX_RETRIES} retries: {build['title']}",
                        "error",
                    )
                    if self.outcome_emitter:
                        self.outcome_emitter.emit(
                            idea_id=int(build["idea_id"]) if str(build["idea_id"]).isdigit() else 0,
                            idea_title=build["title"],
                            outcome="build_failed",
                            build_outcome=f"max_retries_exceeded: {queue_job_id}",
                            tags=["build", "abandoned"],
                        )
                    # Close feedback loops for abandoned builds (L5 B2+B3)
                    try:
                        resolve_prediction(self.state_db, queue_job_id, "failure")
                        idea_id = int(build["idea_id"]) if str(build["idea_id"]).isdigit() else None
                        if idea_id:
                            self._write_ideaforge_outcome(idea_id, "build_failed")
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Failed to close feedback loop for abandoned build %s: %s",
                            queue_job_id, e,
                        )

            # 2. Retry builds that haven't exhausted retries and whose backoff has expired
            #    Skip deterministic failures (dependency, test, build errors) — retrying won't help
            retryable = self.state_db.get_retryable_builds()
            for build in retryable:
                queue_job_id = build["queue_job_id"]
                if not self.state_db.is_retryable_failure(queue_job_id):
                    category = self.state_db.get_failure_category(queue_job_id)
                    print(f"  Skip retry (deterministic {category}): {build['title']} ({queue_job_id})")
                    self.audit_logger.log_decision(
                        "build", "skip_retry_deterministic",
                        {"queue_job_id": queue_job_id, "failure_category": category}
                    )
                    self.state_db.mark_build_abandoned(queue_job_id)
                    continue
                if self.state_db.mark_build_for_retry(queue_job_id):
                    retry_num = (build.get("retry_count") or 0) + 1
                    print(f"  Auto-retry #{retry_num}: {build['title']} ({queue_job_id})")
                    self.audit_logger.log_decision(
                        "build", "auto_retry",
                        {"queue_job_id": queue_job_id, "retry_count": retry_num}
                    )
                    self.notifier.notify(
                        f"Build auto-retry #{retry_num}: {build['title']}"
                    )
                    # Reset priority_queue to 'pending' so run_from_queue
                    # re-dispatches on the next cycle (backoff already enforced
                    # by get_retryable_builds checking next_retry_at).
                    self._reset_priority_queue_for_retry(queue_job_id)
        except Exception as e:
            self.audit_logger.log_error("build", f"Auto-retry check failed: {e}")

        # Dispatch: route non-buildable queue items to ClaudeClaw workers
        dispatch_count = 0
        try:
            dispatch_count = self.dispatch_queue_items(dry_run=dry_run)
            if dispatch_count > 0:
                print(f"+ Dispatch: {dispatch_count} items sent to ClaudeClaw workers")
                self.notifier.notify(f"Dispatched {dispatch_count} items to ClaudeClaw workers")
        except Exception as e:
            self.audit_logger.log_error("dispatch", f"Dispatch failed: {e}")

        # Dispatch Status Sync (poll ClaudeClaw for completed/failed dispatched tasks)
        try:
            sync_result = self.sync_dispatch_status()
            if sync_result["synced"] > 0:
                completed = sync_result["completed"]
                failed = sync_result["failed"]
                if completed:
                    self.notifier.notify(f"Dispatch completed: {', '.join(completed)}")
                if failed:
                    self.notifier.notify(f"Dispatch failed: {', '.join(failed)}", "error")
        except Exception as e:
            # Non-fatal -- log and continue
            self.audit_logger.log_error("dispatch_sync", f"Dispatch sync failed: {e}")

        # Gate 4.5: Review (automated quality checks on completed builds)
        review_count = 0
        if self.review_gate is not None:
            try:
                print(f"Running Gate 4.5 (review)...")
                review_results = self.review_gate.run(dry_run=dry_run)
                review_count = len(review_results)
                passed = sum(1 for r in review_results if r.verdict == "pass")
                failed = sum(1 for r in review_results if r.verdict == "fail")

                if review_count > 0:
                    print(f"+ Gate 4.5 completed: {review_count} reviewed, {passed} passed, {failed} failed")
                    if failed > 0:
                        failed_titles = [r.title for r in review_results if r.verdict == "fail"]
                        self.notifier.notify(
                            f"Review gate: {failed} builds failed checks: {', '.join(failed_titles)}",
                            "warning",
                        )
                        # Resolve feasibility predictions for review failures (L5 B2)
                        for r in review_results:
                            if r.verdict == "fail":
                                resolve_prediction(self.state_db, r.queue_job_id, "review_failed")
                                # Write review_failed outcome to IdeaForge (L5 B3)
                                build = self.state_db.get_build_by_queue_job_id(r.queue_job_id)
                                if build and str(build["idea_id"]).isdigit():
                                    self._write_ideaforge_outcome(int(build["idea_id"]), "review_failed")
                        # Emit outcomes for review failures (Phase 14a)
                        if self.outcome_emitter:
                            for r in review_results:
                                if r.verdict == "fail":
                                    build = self.state_db.get_build_by_queue_job_id(r.queue_job_id)
                                    self.outcome_emitter.emit(
                                        idea_id=int(build["idea_id"]) if build and str(build["idea_id"]).isdigit() else 0,
                                        idea_title=r.title,
                                        outcome="build_failed",
                                        build_outcome=f"review_failed: {', '.join(r.checks_failed)}",
                                        tags=["review"],
                                    )
                    if passed > 0:
                        self.notifier.notify(f"Review gate: {passed} builds passed, ready to publish")
            except Exception as e:
                self.audit_logger.log_error("review", f"Review gate failed: {e}")

        # Gate 4.25: Tyrest LLM QA review (on builds that passed ReviewGate)
        if self.tyrest_gate is not None and review_count > 0:
            try:
                passed_reviews = [r for r in review_results if r.verdict == "pass"]
                tyrest_count = 0
                for review in passed_reviews:
                    # Get spec path and project_dir for this build
                    build = self.state_db.get_build_by_queue_job_id(review.queue_job_id)
                    if not build:
                        continue
                    spec_path = build.get("spec_path", "")
                    project_dir = build.get("project_dir", "")
                    if not project_dir:
                        continue

                    spec_text = ""
                    if spec_path and Path(spec_path).is_file():
                        spec_text = Path(spec_path).read_text(encoding="utf-8")

                    if dry_run:
                        print(f"  [DRY RUN] Would Tyrest-review: {review.title}")
                        tyrest_count += 1
                        continue

                    tyrest_result = self.tyrest_gate.review_build(
                        Path(project_dir), spec_text, idea_title=review.title,
                    )
                    tyrest_count += 1

                    if tyrest_result.rejected:
                        # Downgrade review_status so publish gate skips it
                        self.state_db.update_build_review_status(
                            review.queue_job_id, "tyrest_rejected",
                        )
                        self.notifier.notify(
                            f"Tyrest REJECTED: {review.title} — {tyrest_result.reasoning}",
                            "warning",
                        )
                        # Resolve feasibility prediction for Tyrest rejection (L5 B2)
                        resolve_prediction(self.state_db, review.queue_job_id, "tyrest_rejected")
                        # Write tyrest_rejected outcome to IdeaForge (L5 B3)
                        build = self.state_db.get_build_by_queue_job_id(review.queue_job_id)
                        if build and str(build["idea_id"]).isdigit():
                            self._write_ideaforge_outcome(int(build["idea_id"]), "tyrest_rejected")
                        # Emit outcome for Tyrest rejection (Phase 14a)
                        if self.outcome_emitter:
                            build = self.state_db.get_build_by_queue_job_id(review.queue_job_id)
                            self.outcome_emitter.emit(
                                idea_id=int(build["idea_id"]) if build and str(build["idea_id"]).isdigit() else 0,
                                idea_title=review.title,
                                outcome="rejected",
                                build_outcome=f"tyrest_rejected: {tyrest_result.reasoning}",
                                tags=["tyrest"],
                            )
                    else:
                        self.audit_logger.log_decision(
                            gate="tyrest",
                            action=tyrest_result.verdict.lower(),
                            details={
                                "queue_job_id": review.queue_job_id,
                                "title": review.title,
                                "overall": tyrest_result.overall,
                                "confidence": tyrest_result.confidence,
                            },
                        )

                if tyrest_count > 0:
                    print(f"+ Gate 4.25 completed: {tyrest_count} Tyrest-reviewed")
            except Exception as e:
                self.audit_logger.log_error("tyrest", f"Tyrest gate failed: {e}")

        # Quality scoring (Phase 14b) — score builds that passed review
        if review_count > 0:
            try:
                scored_builds = 0
                for r in review_results:
                    if r.verdict != "pass":
                        continue
                    build = self.state_db.get_build_by_queue_job_id(r.queue_job_id)
                    if not build or not build.get("project_dir"):
                        continue
                    project_dir = Path(build["project_dir"])
                    if not project_dir.is_dir():
                        continue

                    # Use Tyrest overall score if this build was Tyrest-reviewed
                    tyrest_overall = None
                    if self.tyrest_gate is not None and build.get("review_status") not in ("tyrest_rejected",):
                        # Check if we have a Tyrest result from this cycle
                        # The tyrest_result is only available for the current iteration;
                        # for already-reviewed builds, we don't re-score Tyrest
                        pass  # Tyrest score will be None for now; 14c can enhance

                    breakdown = score_project(project_dir, tyrest_overall=tyrest_overall)
                    if not dry_run:
                        self.state_db.update_build_quality_score(
                            r.queue_job_id, breakdown.total_score,
                        )
                    scored_builds += 1
                    self.audit_logger.log_decision(
                        gate="quality",
                        action="scored",
                        details={
                            "queue_job_id": r.queue_job_id,
                            "title": r.title,
                            "quality_score": breakdown.total_score,
                            "static_score": breakdown.static_score,
                            "source_files": breakdown.source_file_count,
                            "test_files": breakdown.test_file_count,
                        },
                    )

                if scored_builds > 0:
                    print(f"+ Quality scored: {scored_builds} builds")
            except Exception as e:
                self.audit_logger.log_error("quality", f"Quality scoring failed: {e}")

        # Quality ratchet evaluation (Phase 14e)
        try:
            ratchet_result = evaluate_ratchet(self.state_db)
            if ratchet_result["activated"]:
                if ratchet_result["tightened"]:
                    print(f"+ Quality ratchet: {ratchet_result['reason']}")
                    self.notifier.notify(
                        f"Quality ratchet tightened: {ratchet_result['reason']}",
                    )
                self.audit_logger.log_decision(
                    gate="quality_ratchet",
                    action="tightened" if ratchet_result["tightened"] else "unchanged",
                    details=ratchet_result,
                )
        except Exception as e:
            self.audit_logger.log_error("quality_ratchet", f"Ratchet evaluation failed: {e}")

        # Test coverage ratchet evaluation (Phase D2)
        try:
            test_ratchet_result = evaluate_test_ratchet(self.state_db)
            if test_ratchet_result["activated"]:
                if test_ratchet_result["tightened"]:
                    print(f"+ Test coverage ratchet: {test_ratchet_result['reason']}")
                    self.notifier.notify(
                        f"Test coverage ratchet tightened: {test_ratchet_result['reason']}",
                    )
                self.audit_logger.log_decision(
                    gate="test_coverage_ratchet",
                    action="tightened" if test_ratchet_result["tightened"] else "unchanged",
                    details=test_ratchet_result,
                )
        except Exception as e:
            self.audit_logger.log_error("test_coverage_ratchet", f"Test ratchet evaluation failed: {e}")

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
                            # Resolve feasibility prediction as success (L5 B2)
                            resolve_prediction(self.state_db, job.build_job_id, "published")
                            # Write published outcome to IdeaForge (L5 B3)
                            build = self.state_db.get_build_by_queue_job_id(job.build_job_id)
                            if build and str(build["idea_id"]).isdigit():
                                self._write_ideaforge_outcome(int(build["idea_id"]), "published")
                            self.notifier.notify(
                                f"Published: {self.config.github_org}/{job.repo_name} ({job.title})"
                            )
                            # Emit outcome for published builds (Phase 14a)
                            if self.outcome_emitter:
                                build = self.state_db.get_build_by_queue_job_id(job.build_job_id)
                                quality = build.get("quality_score") if build else None
                                self.outcome_emitter.emit(
                                    idea_id=int(build["idea_id"]) if build and str(build["idea_id"]).isdigit() else 0,
                                    idea_title=job.title,
                                    outcome="published",
                                    overall_score=quality,
                                    build_outcome="published_to_github",
                                    github_url=job.repo_url,
                                    tags=["publish"],
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

        # Gate 4.7: README Enhancement
        if self.readme_gate is not None and self.publish_gate is not None:
            # Collect published jobs from this cycle (pub_jobs defined in Gate 4 block above)
            try:
                published_jobs = [j for j in pub_jobs if j.status == "published"] if pub_jobs else []
            except NameError:
                published_jobs = []

            if published_jobs:
                try:
                    print(f"Running Gate 4.7 (readme)...")
                    readme_results = self.readme_gate.run(published_jobs=published_jobs, dry_run=dry_run)
                    readme_count = sum(1 for r in readme_results if r.get("status") == "completed")
                    print(f"+ Gate 4.7 completed: {len(readme_results)} processed, {readme_count} enhanced")
                except Exception as e:
                    print(f"x Gate 4.7 (readme) failed: {e}")
                    self.audit_logger.log_error("readme", str(e))

        # Gate 4.8: Swindle (storefront listing)
        swindle_script = Path.home() / "projects" / "swindle" / "swindle.py"
        if swindle_script.is_file() and self.publish_gate is not None:
            try:
                published_for_swindle = [j for j in pub_jobs if j.status == "published"] if pub_jobs else []
            except NameError:
                published_for_swindle = []

            for job in published_for_swindle:
                # Skip if already staged in Swindle
                build = self.state_db.get_build_by_queue_job_id(job.build_job_id)
                spec_path = build.get("spec_path", "") if build else ""
                try:
                    print(f"Running Gate 4.8 (swindle): {job.title}...")
                    cmd = [
                        "python3", str(swindle_script), "prepare",
                        job.repo_url or "",
                        "--title", job.title,
                        "--project-dir", job.project_dir,
                    ]
                    if spec_path:
                        cmd.extend(["--spec-path", spec_path])
                    if dry_run:
                        cmd.append("--dry-run")
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=300,
                        cwd=str(swindle_script.parent),
                    )
                    if result.returncode == 0:
                        print(f"+ Gate 4.8 (swindle): staged listing for {job.title}")
                    else:
                        print(f"x Gate 4.8 (swindle) failed: {result.stderr[:200]}")
                except Exception as e:
                    print(f"x Gate 4.8 (swindle) error: {e}")

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

        # Log outcome emission count for this cycle
        if self.outcome_emitter:
            cycle_outcomes = self.outcome_emitter.emit_count - outcome_count_before
            if cycle_outcomes > 0:
                print(f"+ Outcomes emitted: {cycle_outcomes}")

        # IdeaForge outcome backfill sweep (L5 B3)
        # Catches builds that reached terminal state but whose write-back was
        # missed due to crashes, writer init failures, or code-path gaps.
        try:
            backfilled = self._backfill_ideaforge_outcomes()
            if backfilled > 0:
                import logging
                logging.getLogger(__name__).info(
                    "Backfilled %d IdeaForge outcomes", backfilled,
                )
                print(f"+ IdeaForge outcome backfill: {backfilled} written")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "IdeaForge outcome backfill failed: %s", e,
            )

        # End cycle
        self.state_db.end_cycle(cycle_id, triage_count, build_count, patch_count, errors, publish_count)
        self.audit_logger.log_cycle_end(cycle_id, triage_count, build_count, patch_count, errors)

        # Only notify on cycles with actual activity or new errors.
        # Suppress summary when the only "errors" are halted-gate messages (already notified once).
        halted_only = all("halted by circuit breaker" in e for e in errors)
        has_activity = (
            triage_count > 0 or build_count > 0 or publish_count > 0
            or dispatch_count > 0 or patch_count > 0
        )
        if has_activity or (errors and not halted_only):
            error_text = f", {len(errors)} errors" if errors else ""
            pub_text = f", {publish_count} published" if publish_count > 0 else ""
            disp_text = f", {dispatch_count} dispatched" if dispatch_count > 0 else ""
            summary_level = "warning" if errors else "info"
            self.notifier.notify(
                f"Metroplex: {triage_count} triaged, {build_count} built{pub_text}{disp_text}, {patch_count} patched{error_text}",
                summary_level,
            )

        # Anomaly detection (Phase D) — runs after all gates, before return
        try:
            from anomaly_detector import AnomalyDetector
            detector = AnomalyDetector(self.config.state_db_path, self.notifier)
            anomalies = detector.run_all()
            if anomalies:
                logger.warning("Anomalies detected: %s", anomalies)
        except Exception as e:
            logger.debug("Anomaly detection skipped: %s", e)

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
