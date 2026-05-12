#!/usr/bin/env python
"""
Metroplex CLI - Autonomous Build Layer Entry Point
Orchestrates triage and build gates with safety systems.
"""
import sys
import os
import fcntl
import atexit
import argparse
import json
from pathlib import Path

from config import Config
from db import StateDB
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import SpecGenerator, BuildOrchestrator
from gates.publish import PublishGate
from gates.readme import ReadmeGate
from gates.readiness import ReadinessGate
from gates.review import ReviewGate
from orchestrator import CycleOrchestrator
from notifier import create_notifier, FilteredNotifier
from readers.ideaforge_reader import IdeaForgeReader
from readers.skylynx_reader import SkyLynxReader
from dispatcher import create_dispatcher, route_to_worker, build_dispatch_prompt
from outcome_emitter import create_outcome_emitter
from dashboard import compute_funnel_metrics, format_funnel_output
from quality_ratchet import (
    get_quality_threshold,
    get_unchanged_count,
    recalibrate_threshold,
)
from health import run_health_checks, format_report, HealthStatus


def setup_logging(verbose: bool):
    """
    Setup logging configuration.

    Args:
        verbose: If True, set DEBUG level logging
    """
    import logging

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )


def initialize_components(config: Config):
    """
    Initialize all Metroplex components.

    Args:
        config: Metroplex configuration

    Returns:
        Tuple of (orchestrator, state_db, circuit_breaker)
    """
    # Initialize core systems
    state_db = StateDB()
    state_db.init_db()

    audit_logger = AuditLogger()

    # Initialize safety systems
    circuit_breaker = CircuitBreaker(
        threshold=config.circuit_breaker_threshold,
        state_db=state_db
    )
    cycle_caps = CycleCaps(config)
    shutdown_handler = ShutdownHandler()

    # Initialize readers (with error handling for missing DBs)
    try:
        ideaforge_reader = IdeaForgeReader(config.ideaforge_db)
    except FileNotFoundError:
        print(f"Warning: IdeaForge DB not found at {config.ideaforge_db}")
        ideaforge_reader = None

    try:
        skylynx_reader = SkyLynxReader(config.st_records_db)
    except FileNotFoundError:
        print(f"Warning: ST Records DB not found at {config.st_records_db} (Sky-Lynx reader)")
        skylynx_reader = None

    # Initialize gates
    triage_gate = TriageGate(
        config=config,
        state_db=state_db,
        ideaforge_reader=ideaforge_reader,
        audit_logger=audit_logger
    )

    # Initialize spec generator (pass state_db for cost recording)
    template_dir = Path("spec_templates")
    try:
        spec_generator = SpecGenerator(config, template_dir, state_db=state_db)
    except FileNotFoundError:
        print(f"Warning: Template directory not found at {template_dir}")
        spec_generator = None

    # Create build adapter based on config.build_target
    from adapters.factory import create_adapter
    from event_emitter import create_event_emitter as _create_ee
    _adapter_ee = _create_ee()
    build_adapter = create_adapter(config, event_emitter=_adapter_ee)

    build_orchestrator = BuildOrchestrator(
        config=config,
        state_db=state_db,
        spec_generator=spec_generator,
        audit_logger=audit_logger,
        ideaforge_reader=ideaforge_reader,
        adapter=build_adapter,
    )

    publish_gate = PublishGate(
        config=config,
        state_db=state_db,
        audit_logger=audit_logger,
    )

    review_gate = ReviewGate(
        config=config,
        state_db=state_db,
        audit_logger=audit_logger,
    )

    readme_gate = ReadmeGate(
        config=config,
        state_db=state_db,
        audit_logger=audit_logger,
    )

    readiness_gate = ReadinessGate(
        config=config,
        state_db=state_db,
        audit_logger=audit_logger,
    ) if config.readiness_enabled else None

    # Initialize notifier (wrapped with FilteredNotifier for anomaly/summary modes)
    raw_notifier = create_notifier(config.telegram_bot_token, config.telegram_chat_id)
    notifier = FilteredNotifier(raw_notifier, config.notify_mode)

    # Initialize outcome emitter (Phase 14a -- write OutcomeRecords to ST Records)
    outcome_emitter = create_outcome_emitter()

    # Initialize event emitter (Phase F -- Sky-Lynx reactive triggers)
    from event_emitter import create_event_emitter
    event_emitter = create_event_emitter()

    # Initialize dispatcher for non-buildable queue items (Sky-Lynx -> ClaudeClaw)
    dispatcher = create_dispatcher(config.dispatch_db, config.dispatch_chat_id)

    # Initialize A2A server manager (if using A2A or auto dispatch)
    a2a_manager = None
    if config.build_target in ("a2a", "auto"):
        from a2a_lifecycle import A2AServerManager
        a2a_manager = A2AServerManager(
            yce_dir=config.yce_dir,
            server_url=config.a2a_server_url,
        )

    # Initialize orchestrator
    orchestrator = CycleOrchestrator(
        config=config,
        triage_gate=triage_gate,
        build_orchestrator=build_orchestrator,
        circuit_breaker=circuit_breaker,
        cycle_caps=cycle_caps,
        shutdown_handler=shutdown_handler,
        state_db=state_db,
        audit_logger=audit_logger,
        cycle_sleep_seconds=config.cycle_sleep_seconds,
        notifier=notifier,
        skylynx_reader=skylynx_reader,
        publish_gate=publish_gate,
        review_gate=review_gate,
        dispatcher=dispatcher,
        outcome_emitter=outcome_emitter,
        event_emitter=event_emitter,
        readme_gate=readme_gate,
        readiness_gate=readiness_gate,
        a2a_manager=a2a_manager,
    )

    return orchestrator, state_db, circuit_breaker


def cmd_triage(args, config: Config):
    """
    Run Gate 1 (triage) only.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error, 2=halted)
    """
    orchestrator, state_db, circuit_breaker = initialize_components(config)

    # Check if gate is halted
    if circuit_breaker.is_halted("triage"):
        print("ERROR: Triage gate is halted by circuit breaker")
        print("Run 'metroplex.py reset --gate triage' to reset")
        return 2

    try:
        print("Running Gate 1 (Triage)...")
        decisions = orchestrator.triage_gate.run(dry_run=args.dry_run)

        print(f"\nCompleted: {len(decisions)} decisions")
        print(f"  Approved: {sum(1 for d in decisions if d.decision == 'approve')}")
        print(f"  Rejected: {sum(1 for d in decisions if d.decision == 'reject')}")
        print(f"  Deferred: {sum(1 for d in decisions if d.decision == 'defer')}")

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_build(args, config: Config):
    """
    Run Gate 2 (build) only.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error, 2=halted)
    """
    orchestrator, state_db, circuit_breaker = initialize_components(config)

    # Check if gate is halted
    if circuit_breaker.is_halted("build"):
        print("ERROR: Build gate is halted by circuit breaker")
        print("Run 'metroplex.py reset --gate build' to reset")
        return 2

    try:
        print("Running Gate 2 (Build)...")

        # Get approved ideas from state DB
        state_db.connect()
        cursor = state_db.conn.cursor()

        if args.idea_id:
            # Build specific idea
            cursor.execute("""
                SELECT idea_id, title
                FROM triage_decisions
                WHERE idea_id = ? AND decision = 'approve'
            """, (args.idea_id,))
            rows = cursor.fetchall()
        else:
            # Build all approved ideas not yet built
            cursor.execute("""
                SELECT idea_id, title
                FROM triage_decisions
                WHERE decision = 'approve'
                AND idea_id NOT IN (SELECT idea_id FROM build_jobs)
            """)
            rows = cursor.fetchall()

        # Look up full idea data from IdeaForge for each approved idea
        try:
            ideaforge_reader = IdeaForgeReader(config.ideaforge_db)
        except FileNotFoundError:
            print(f"Warning: IdeaForge DB not found at {config.ideaforge_db}")
            ideaforge_reader = None

        approved_ideas = []
        for row in rows:
            idea = None
            if ideaforge_reader:
                idea = ideaforge_reader.get_idea_by_id(row["idea_id"])
            if idea:
                approved_ideas.append(idea)
            else:
                # Fallback if IdeaForge lookup fails
                approved_ideas.append({
                    "id": row["idea_id"],
                    "title": row["title"],
                    "description": row["title"],
                    "problem_statement": row["title"],
                    "target_audience": "General",
                    "artifact_type": "tool"
                })

        if not approved_ideas:
            print("No approved ideas to build")
            return 0

        jobs = orchestrator.build_orchestrator.run(approved_ideas, dry_run=args.dry_run)

        print(f"\nCompleted: {len(jobs)} build jobs")
        print(f"  Queued: {sum(1 for j in jobs if j.status == 'queued')}")
        print(f"  Failed: {sum(1 for j in jobs if j.status == 'failed')}")

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_publish(args, config: Config):
    """
    Run Gate 4 (publish) only -- push completed builds to GitHub.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error, 2=halted)
    """
    orchestrator, state_db, circuit_breaker = initialize_components(config)

    # Check if gate is halted
    if circuit_breaker.is_halted("publish"):
        print("ERROR: Publish gate is halted by circuit breaker")
        print("Run 'metroplex.py reset --gate publish' to reset")
        return 2

    try:
        print("Running Gate 4 (Publish)...")
        jobs = orchestrator.publish_gate.run(dry_run=args.dry_run)

        if not jobs:
            print("\nNo unpublished builds found.")
            return 0

        published = sum(1 for j in jobs if j.status == "published")
        failed = sum(1 for j in jobs if j.status == "failed")
        pending = sum(1 for j in jobs if j.status == "pending")

        print(f"\nCompleted: {len(jobs)} processed")
        if published:
            print(f"  Published: {published}")
        if failed:
            print(f"  Failed: {failed}")
        if pending:
            print(f"  Pending (dry-run): {pending}")

        for job in jobs:
            if job.status == "published":
                primary = config.publish_targets[0] if config.publish_targets else "gitlab"
                ns = config.gitlab_namespace if primary == "gitlab" else config.github_org
                print(f"  + {ns}/{job.repo_name} -> {job.repo_url}")
            elif job.status == "failed":
                print(f"  x {job.build_job_id}: {job.error}")

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_run_all(args, config: Config):
    """
    Run full cycle (triage → build → publish).

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    orchestrator, state_db, circuit_breaker = initialize_components(config)

    try:
        if args.cycles == 0:
            # Run continuously
            print("Running continuous mode (Ctrl+C to stop)...")
            results = orchestrator.run_continuous(max_cycles=0, dry_run=args.dry_run)
        else:
            # Run N cycles
            print(f"Running {args.cycles} cycle(s)...")
            results = orchestrator.run_continuous(max_cycles=args.cycles, dry_run=args.dry_run)

        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        print(f"Total cycles: {len(results)}")
        print(f"Total triage decisions: {sum(r.triage_count for r in results)}")
        print(f"Total build jobs: {sum(r.build_count for r in results)}")
        print(f"Total published: {sum(r.publish_count for r in results)}")
        print(f"Total errors: {sum(len(r.errors) for r in results)}")

        return 0
    except KeyboardInterrupt:
        print("\n\nInterrupted by user (Ctrl+C)")
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_queue(args, config: Config):
    """Show priority queue contents."""
    state_db = StateDB()
    state_db.init_db()

    try:
        summary = state_db.get_queue_summary()

        print(f"{'='*60}")
        print("PRIORITY QUEUE")
        print(f"{'='*60}\n")

        print(f"Total items: {summary.get('total', 0)}")
        for s in ("pending", "dispatched", "completed", "failed"):
            if summary.get(s, 0) > 0:
                print(f"  {s.capitalize()}: {summary[s]}")

        # Show pending items
        state_db.connect()
        cursor = state_db.conn.cursor()
        cursor.execute("""
            SELECT id, source, source_id, title, priority_score, status, created_at
            FROM priority_queue
            ORDER BY priority_score DESC
            LIMIT 20
        """)
        rows = cursor.fetchall()

        if rows:
            print(f"\nAll items (by priority):")
            for row in rows:
                status_icon = {"pending": " ", "dispatched": "~", "completed": "+", "failed": "x"}.get(row["status"], "?")
                print(f"  [{status_icon}] #{row['id']} [{row['source']}:{row['source_id']}] score={row['priority_score']:.1f} {row['title']}")

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_status(args, config: Config):
    """
    Show current system status.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    orchestrator, state_db, circuit_breaker = initialize_components(config)

    try:
        status = orchestrator.get_status()

        print(f"{'='*60}")
        print("METROPLEX STATUS")
        print(f"{'='*60}\n")

        # Gate statuses
        print("Gate Status:")
        for gs in status["gate_statuses"]:
            halted_str = "[HALTED]" if gs["halted"] else "[OK]"
            print(f"  {gs['gate']:8s} {halted_str:10s} failures={gs['consecutive_failures']}")
            if gs["last_error"]:
                print(f"           Last error: {gs['last_error'][:60]}")

        # Pending items
        print(f"\nPending Builds: {status['pending_builds']}")

        # Priority queue
        queue = status.get("priority_queue", {})
        if queue:
            print(f"\nPriority Queue:")
            print(f"  Total: {queue.get('total', 0)}")
            for s in ("pending", "dispatched", "completed", "failed"):
                if queue.get(s, 0) > 0:
                    print(f"  {s.capitalize()}: {queue[s]}")

        # Runner status
        runner = status.get("runner_active", False)
        print(f"\nYCE Runner: {'ACTIVE' if runner else 'idle'}")

        # Schedule
        sched = status.get("schedule", {})
        if sched:
            in_window = "YES" if sched.get("currently_in_window") else "NO"
            print(f"\nSchedule: {sched.get('start', 0)}:00-{sched.get('end', 24)}:00, days={sched.get('active_days', 'all')}, in_window={in_window}")

        # Recent cycles
        print(f"\nRecent Cycles ({len(status['recent_cycles'])}):")
        for cycle in status["recent_cycles"]:
            started = cycle["started_at"]
            completed = cycle["completed_at"] or "INCOMPLETE"
            errors = json.loads(cycle["errors"]) if cycle["errors"] else []

            print(f"  {cycle['cycle_id']}")
            print(f"    Started: {started}")
            print(f"    Completed: {completed}")
            print(f"    Triage: {cycle['triage_count']}, Build: {cycle['build_count']}")
            if errors:
                print(f"    Errors: {len(errors)}")

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_builds(args, config: Config):
    """Show build history with output locations and GitHub repos."""
    state_db = StateDB()
    state_db.connect()

    try:
        cursor = state_db.conn.cursor()

        # Get unique builds (deduplicated by queue_job_id, latest entry)
        cursor.execute("""
            SELECT b.queue_job_id, b.idea_id, b.title, b.status, b.project_dir,
                   b.actual_cost_usd,
                   p.repo_url, p.status as pub_status
            FROM build_jobs b
            LEFT JOIN publish_jobs p ON p.build_job_id = b.queue_job_id AND p.status = 'published'
            WHERE b.id IN (
                SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id
            )
            ORDER BY b.id DESC
        """)
        rows = cursor.fetchall()

        print(f"{'='*80}")
        print("METROPLEX BUILD HISTORY")
        print(f"{'='*80}\n")

        for r in rows:
            status_icon = "+" if r["status"] == "completed" else "x"
            pub_icon = "-> " + r["repo_url"] if r["repo_url"] else "   (not published)"
            proj = r["project_dir"] or "(no project dir)"
            actual_cost = r["actual_cost_usd"]
            cost_str = f"${actual_cost:.2f}" if actual_cost is not None else "--"

            print(f"  {status_icon} [{r['status']:>9}] {r['title']}")
            print(f"    ID: {r['queue_job_id']}  Idea: {r['idea_id']}  Cost: {cost_str}")
            print(f"    Dir: {proj}")
            print(f"    {pub_icon}")
            print()

        # Summary
        total = len(rows)
        completed = sum(1 for r in rows if r["status"] == "completed")
        published = sum(1 for r in rows if r["repo_url"])
        print(f"Total: {total} builds, {completed} completed, {published} published to GitHub")

        return 0
    finally:
        state_db.close()


def cmd_retry(args, config: Config):
    """Re-dispatch a failed build by resetting its status to pending."""
    state_db = StateDB()
    state_db.connect()

    try:
        queue_job_id = args.build_id

        # Show current state before retry
        cursor = state_db.conn.cursor()
        cursor.execute(
            "SELECT queue_job_id, title, status, project_dir FROM build_jobs "
            "WHERE queue_job_id = ? ORDER BY id DESC LIMIT 1",
            (queue_job_id,),
        )
        row = cursor.fetchone()
        if not row:
            print(f"No build found with ID: {queue_job_id}")
            return 1
        if row["status"] != "failed":
            print(f"Build {queue_job_id} has status '{row['status']}' — only failed builds can be retried")
            return 1

        print(f"Retrying build: {row['title']}")
        print(f"  ID: {queue_job_id}")
        print(f"  Current status: {row['status']}")

        if state_db.reset_build_for_retry(queue_job_id):
            print(f"  Reset to: queued (priority_queue → pending)")
            print(f"\nBuild will be re-dispatched on next cycle, or run 'metroplex.py build' manually.")
            return 0
        else:
            print("  Failed to reset — build may not be in 'failed' state")
            return 1
    finally:
        state_db.close()


def cmd_cost(args, config: Config):
    """Show cost tracking summary."""
    state_db = StateDB()
    state_db.init_db()

    try:
        daily = state_db.get_daily_spend()
        monthly = state_db.get_monthly_spend()

        print(f"{'='*60}")
        print("METROPLEX COST TRACKER")
        print(f"{'='*60}\n")

        print(f"Today's spend:       ${daily:.2f} / ${config.daily_cost_limit:.2f}")
        print(f"This month's spend:  ${monthly:.2f} / ${config.monthly_cost_limit:.2f}")
        print(f"Alert threshold:     {config.cost_alert_threshold*100:.0f}%")
        print(f"Build cost estimate: ${config.build_cost_estimate:.2f}\n")

        breakdown = state_db.get_cost_breakdown(days=args.days)
        if breakdown:
            print(f"Daily breakdown (last {args.days} days):")
            for entry in breakdown:
                print(f"  {entry['date']}  ${entry['total_cost']:.2f}  ({entry['entry_count']} entries)")
        else:
            print("No cost data recorded yet.")

        by_source = state_db.get_cost_by_source(days=args.days)
        if by_source:
            print(f"\nSpend by source (last {args.days} days):")
            for entry in by_source:
                print(f"  {entry['source']:30s}  ${entry['total_cost']:7.2f}  ({entry['entry_count']:>3} entries, {entry['pct_of_total']:>5.1f}%)")

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_postmortems(args, config: Config):
    """Show failure category summary from build post-mortems."""
    from postmortem import get_postmortem_summary, get_failure_patterns

    state_db = StateDB()
    state_db.init_db()

    try:
        summary = get_postmortem_summary(state_db)
        if not summary:
            print("No build post-mortems recorded yet.")
            return 0

        print(f"{'='*60}")
        print("BUILD FAILURE POST-MORTEMS")
        print(f"{'='*60}\n")
        print(f"{'Category':<20} {'Count':>6} {'Avg Score':>10}")
        print("-" * 40)
        for row in summary:
            avg = f"{row['avg_score']:.1f}" if row["avg_score"] is not None else "-"
            print(f"{row['category']:<20} {row['count']:>6} {avg:>10}")

        patterns = get_failure_patterns(state_db, min_count=2)
        if patterns:
            print(f"\n{'='*60}")
            print("RECURRING PATTERNS (2+ occurrences)")
            print(f"{'='*60}\n")
            for p in patterns:
                print(f"  {p['category']} / {p['stage']}: {p['count']} occurrences")
                for sig in p["sample_signatures"][:2]:
                    if sig:
                        print(f"    -> {sig[:80]}...")

        return 0
    finally:
        state_db.close()


def cmd_reset(args, config: Config):
    """
    Reset circuit breaker for gate(s).

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    orchestrator, state_db, circuit_breaker = initialize_components(config)

    try:
        if args.gate == "all":
            gates = ["triage", "build", "publish"]
        else:
            gates = [args.gate]

        for gate in gates:
            circuit_breaker.reset(gate)
            print(f"Reset circuit breaker for {gate} gate")

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_dispatch(args, config: Config):
    """
    Dispatch pending queue items to EA-Claude workers.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    state_db = StateDB()
    state_db.init_db()

    dispatcher = create_dispatcher(config.dispatch_db, config.dispatch_chat_id)

    try:
        state_db.connect()
        cursor = state_db.conn.cursor()

        if args.item_id:
            # Dispatch specific item
            cursor.execute(
                "SELECT * FROM priority_queue WHERE id = ? AND status = 'pending'",
                (args.item_id,)
            )
        else:
            # Dispatch next N pending items by priority
            cursor.execute("""
                SELECT * FROM priority_queue
                WHERE status = 'pending'
                ORDER BY priority_score DESC
                LIMIT ?
            """, (args.count,))

        rows = cursor.fetchall()

        if not rows:
            print("No pending items to dispatch")
            return 0

        dispatched = 0
        for row in rows:
            item = dict(row)
            idea_data = {}
            if item.get("idea_data"):
                try:
                    idea_data = json.loads(item["idea_data"])
                except (json.JSONDecodeError, TypeError):
                    pass

            worker = args.worker or route_to_worker(
                item["source"],
                idea_data.get("_recommendation_type", "")
            )
            prompt = build_dispatch_prompt(item)

            if args.dry_run:
                print(f"  [DRY RUN] #{item['id']} [{item['source']}:{item['source_id']}] -> {worker}")
                print(f"    {item['title']}")
                dispatched += 1
            else:
                try:
                    task_id = dispatcher.dispatch(prompt, worker)
                    # Mark as dispatched in priority queue
                    cursor.execute(
                        "UPDATE priority_queue SET status = 'dispatched', dispatched_at = ? WHERE id = ?",
                        (json.dumps(None), item["id"])  # dispatched_at handled by the DB
                    )
                    state_db.conn.commit()
                    print(f"  #{item['id']} [{item['source']}:{item['source_id']}] -> {worker} (task={task_id[:8]}...)")
                    dispatched += 1
                except Exception as e:
                    print(f"  ERROR dispatching #{item['id']}: {e}")

        print(f"\nDispatched: {dispatched}/{len(rows)}")
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_backfill_outcomes(args, config: Config):
    """
    Backfill OutcomeRecords for all terminal-state ideas missing outcomes.

    Scans triage_decisions, build_jobs, and publish_jobs to find terminal
    states that were never emitted as OutcomeRecords.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    from outcome_emitter import create_outcome_emitter

    emitter = create_outcome_emitter()
    if emitter is None:
        print("ERROR: OutcomeEmitter unavailable (st-records not found)")
        return 1

    state_db = StateDB()
    state_db.init_db()

    # Load existing outcome idea_ids to avoid duplicates
    try:
        existing_ids = set()
        for rec in emitter.store.read_outcomes(limit=10000):
            existing_ids.add(rec.idea_id)
        print(f"Existing outcome records: {len(existing_ids)}")
    except Exception as e:
        print(f"Warning: Could not read existing outcomes: {e}")
        existing_ids = set()

    emitted = 0
    skipped = 0

    try:
        state_db.connect()
        cursor = state_db.conn.cursor()

        # 1. Triage rejects
        cursor.execute(
            "SELECT idea_id, title, scaled_score, reason FROM triage_decisions WHERE decision = 'reject'"
        )
        for row in cursor.fetchall():
            if row["idea_id"] in existing_ids:
                skipped += 1
                continue
            if args.dry_run:
                print(f"  [DRY RUN] Would emit REJECTED: {row['title']} (idea {row['idea_id']})")
            else:
                emitter.emit(
                    idea_id=row["idea_id"],
                    idea_title=row["title"],
                    outcome="rejected",
                    overall_score=row["scaled_score"],
                    build_outcome=f"triage_rejected: {row['reason']}",
                    tags=["triage", "backfill"],
                )
            existing_ids.add(row["idea_id"])
            emitted += 1

        # 2. Failed builds (latest status per queue_job_id)
        cursor.execute("""
            SELECT b.idea_id, b.title, b.queue_job_id, b.status, b.review_status
            FROM build_jobs b
            WHERE b.id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
            AND b.status = 'failed'
        """)
        for row in cursor.fetchall():
            idea_id = int(row["idea_id"]) if str(row["idea_id"]).isdigit() else 0
            if idea_id in existing_ids:
                skipped += 1
                continue
            if args.dry_run:
                print(f"  [DRY RUN] Would emit BUILD_FAILED: {row['title']} ({row['queue_job_id']})")
            else:
                emitter.emit(
                    idea_id=idea_id,
                    idea_title=row["title"],
                    outcome="build_failed",
                    build_outcome=f"build_failed: {row['queue_job_id']}",
                    tags=["build", "backfill"],
                )
            existing_ids.add(idea_id)
            emitted += 1

        # 3. Review-failed builds
        cursor.execute("""
            SELECT b.idea_id, b.title, b.queue_job_id
            FROM build_jobs b
            WHERE b.id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
            AND b.status = 'completed'
            AND b.review_status IN ('review_failed', 'tyrest_rejected')
        """)
        for row in cursor.fetchall():
            idea_id = int(row["idea_id"]) if str(row["idea_id"]).isdigit() else 0
            if idea_id in existing_ids:
                skipped += 1
                continue
            if args.dry_run:
                print(f"  [DRY RUN] Would emit BUILD_FAILED (review): {row['title']}")
            else:
                emitter.emit(
                    idea_id=idea_id,
                    idea_title=row["title"],
                    outcome="build_failed",
                    build_outcome=f"review_or_tyrest_failed: {row['queue_job_id']}",
                    tags=["review", "backfill"],
                )
            existing_ids.add(idea_id)
            emitted += 1

        # 4. Published builds
        cursor.execute("""
            SELECT p.build_job_id, p.title, p.repo_url, b.idea_id
            FROM publish_jobs p
            JOIN build_jobs b ON b.queue_job_id = p.build_job_id
            WHERE p.status = 'published'
            AND b.id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
        """)
        for row in cursor.fetchall():
            idea_id = int(row["idea_id"]) if str(row["idea_id"]).isdigit() else 0
            if idea_id in existing_ids:
                skipped += 1
                continue
            if args.dry_run:
                print(f"  [DRY RUN] Would emit PUBLISHED: {row['title']} -> {row['repo_url']}")
            else:
                emitter.emit(
                    idea_id=idea_id,
                    idea_title=row["title"],
                    outcome="published",
                    build_outcome="published_to_github",
                    github_url=row["repo_url"],
                    tags=["publish", "backfill"],
                )
            existing_ids.add(idea_id)
            emitted += 1

        action = "Would emit" if args.dry_run else "Emitted"
        print(f"\n{action}: {emitted} outcome records ({skipped} already existed)")
        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        emitter.close()
        state_db.close()


def cmd_score_builds(args, config: Config):
    """
    Score completed builds that don't have a quality_score yet.

    Scans build_jobs for completed builds with NULL quality_score,
    resolves their project directories, and runs the structural scorer.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    from gates.quality_scorer import score_project

    state_db = StateDB()
    state_db.init_db()

    try:
        state_db.connect()
        cursor = state_db.conn.cursor()

        # R-A item 3: select scoring_rubric so we can forward it to score_project.
        # NULL rows -> rubric=None -> backward-compat (no category gate applied).
        cursor.execute("""
            SELECT DISTINCT b.queue_job_id, b.title, b.project_dir, b.quality_score, b.scoring_rubric
            FROM build_jobs b
            WHERE b.status = 'completed'
            AND b.id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
            AND (b.quality_score IS NULL)
        """)
        rows = cursor.fetchall()

        if not rows:
            print("No unscored completed builds found.")
            return 0

        scored = 0
        skipped = 0

        for row in rows:
            project_dir = row["project_dir"]
            if not project_dir or not Path(project_dir).is_dir():
                skipped += 1
                if args.verbose:
                    print(f"  Skip (no dir): {row['title']}")
                continue

            rubric = row["scoring_rubric"]
            breakdown = score_project(Path(project_dir), scoring_rubric=rubric)

            # When the life_domain category gate fired, surface the reason
            # in the operator-visible output so the 0/100 is not opaque.
            gate_hint = ""
            if breakdown.category_failed:
                gate_hint = f", CATEGORY_FAILED={breakdown.category_failure_reason}"

            if args.dry_run:
                print(f"  [DRY RUN] {row['title']}: {breakdown.total_score}/100 "
                      f"(static={breakdown.static_score}, src={breakdown.source_file_count}, "
                      f"tests={breakdown.test_file_count}{gate_hint})")
            else:
                state_db.update_build_quality_score(row["queue_job_id"], breakdown.total_score)
                print(f"  {row['title']}: {breakdown.total_score}/100 "
                      f"(static={breakdown.static_score}, src={breakdown.source_file_count}, "
                      f"tests={breakdown.test_file_count}{gate_hint})")
            scored += 1

        action = "Would score" if args.dry_run else "Scored"
        print(f"\n{action}: {scored} builds ({skipped} skipped — no project dir)")
        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_quality_digest(args, config: Config):
    """
    Print a concise quality digest for inclusion in daily reports.

    Outputs: outcome counts, quality score stats by terminal state,
    suggested threshold, and recent builds. Designed for Telegram consumption.

    Returns:
        Exit code (0=success, 1=error)
    """
    state_db = StateDB()
    state_db.init_db()

    try:
        state_db.connect()
        cursor = state_db.conn.cursor()

        lines = ["Build Quality Digest"]
        lines.append("=" * 30)

        # Outcome summary (from st-records)
        try:
            from outcome_emitter import create_outcome_emitter
            emitter = create_outcome_emitter()
            if emitter:
                records = emitter.store.read_outcomes(limit=1000)
                from collections import Counter
                outcomes = Counter(r.outcome.value for r in records)
                total = len(records)
                lines.append(f"\nOutcomes ({total} total):")
                for outcome, count in outcomes.most_common():
                    lines.append(f"  {outcome}: {count}")
                emitter.close()
        except Exception:
            pass

        # Quality scores by terminal state
        cursor.execute("""
            SELECT
                b.queue_job_id, b.title, b.status, b.review_status, b.quality_score,
                p.status as pub_status
            FROM build_jobs b
            LEFT JOIN publish_jobs p ON p.build_job_id = b.queue_job_id AND p.status = 'published'
            WHERE b.quality_score IS NOT NULL
            AND b.id IN (SELECT MAX(id) FROM build_jobs GROUP BY queue_job_id)
            ORDER BY b.quality_score DESC
        """)
        rows = cursor.fetchall()

        if rows:
            # Group by state
            groups: dict[str, list[float]] = {}
            for r in rows:
                if r["review_status"] in ("review_failed", "tyrest_rejected"):
                    state = r["review_status"]
                elif r["status"] == "failed":
                    state = "build_failed"
                elif r["pub_status"] == "published":
                    state = "published"
                else:
                    state = "other"
                groups.setdefault(state, []).append(r["quality_score"])

            lines.append(f"\nQuality Scores ({len(rows)} builds):")
            for state, scores in sorted(groups.items(), key=lambda x: -sum(x[1])/len(x[1])):
                avg = sum(scores) / len(scores)
                lines.append(f"  {state}: avg {avg:.0f}/100 (n={len(scores)})")

            all_scores = [r["quality_score"] for r in rows]
            avg_all = sum(all_scores) / len(all_scores)
            lines.append(f"  overall: avg {avg_all:.0f}/100")

            # Threshold suggestion
            pub_scores = groups.get("published", [])
            fail_scores = (
                groups.get("build_failed", []) +
                groups.get("review_failed", []) +
                groups.get("tyrest_rejected", [])
            )
            if pub_scores and fail_scores:
                pub_avg = sum(pub_scores) / len(pub_scores)
                fail_avg = sum(fail_scores) / len(fail_scores)
                threshold = (pub_avg + fail_avg) / 2
                lines.append(f"\nSuggested threshold: {threshold:.0f}/100")
                lines.append(f"  (published avg {pub_avg:.0f} vs failed avg {fail_avg:.0f})")

            # Top and bottom builds
            lines.append(f"\nTop builds:")
            for r in rows[:3]:
                lines.append(f"  {r['quality_score']:.0f} {r['title'][:50]}")
            if len(rows) > 3:
                bottom = sorted(rows, key=lambda r: r["quality_score"])[:3]
                lines.append(f"Bottom builds:")
                for r in bottom:
                    lines.append(f"  {r['quality_score']:.0f} {r['title'][:50]}")
        else:
            lines.append("\nNo scored builds yet.")

        # Recent cycle activity
        cursor.execute("""
            SELECT COUNT(*) as cnt FROM cycles
            WHERE started_at >= datetime('now', '-24 hours')
        """)
        recent_cycles = cursor.fetchone()["cnt"]
        lines.append(f"\nLast 24h: {recent_cycles} cycles")

        print("\n".join(lines))
        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_funnel(args, config: Config):
    """Show pipeline funnel conversion metrics.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    try:
        metroplex_db = str(Path("data/metroplex.db").resolve())
        ideaforge_db = config.ideaforge_db

        metrics = compute_funnel_metrics(
            metroplex_db_path=metroplex_db,
            ideaforge_db_path=ideaforge_db,
            days=args.days,
        )

        output = format_funnel_output(metrics, as_json=args.json)
        print(output)
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1


def cmd_recalibrate(args, config: Config):
    """
    Force-recalibrate the quality ratchet threshold to the current proposed value.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error)
    """
    state_db = StateDB()
    state_db.init_db()

    try:
        current = get_quality_threshold(state_db)
        unchanged = get_unchanged_count(state_db)

        if current is None:
            print("No quality threshold has been set yet. Nothing to recalibrate.")
            return 1

        # Compute proposed without applying
        from quality_ratchet import evaluate_ratchet
        eval_result = evaluate_ratchet(state_db, allow_recalibrate=True)
        proposed = eval_result.get("proposed_threshold")

        if proposed is None:
            print(f"Cannot compute proposed threshold: {eval_result.get('reason', 'unknown')}")
            return 1

        print("Quality Ratchet Recalibration")
        print("=" * 40)
        print(f"  Current threshold:    {current}")
        print(f"  Proposed threshold:   {proposed}")
        print(f"  Unchanged cycles:     {unchanged}")
        print(f"  Direction:            {'tighten' if proposed > current else 'loosen'}")
        print()

        if proposed == current:
            print("Proposed equals current. No change needed.")
            return 0

        if args.yes:
            confirmed = True
        else:
            answer = input(f"Reset threshold from {current} to {proposed}? [y/N] ")
            confirmed = answer.strip().lower() in ("y", "yes")

        if not confirmed:
            print("Aborted.")
            return 0

        # Apply the recalibration
        result = recalibrate_threshold(state_db)

        if not result["success"]:
            print(f"Failed: {result['reason']}")
            return 1

        # Log to audit
        from audit import AuditLogger
        audit = AuditLogger()
        audit.log(
            gate="quality_ratchet",
            action="recalibrated",
            details={
                "old_threshold": result["old_threshold"],
                "new_threshold": result["new_threshold"],
                "triggered_by": "manual_cli",
                "stats": result["stats"],
            },
        )

        print(f"Recalibrated: {result['old_threshold']} -> {result['new_threshold']}")
        print("Unchanged cycle counter reset to 0.")
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_health(args, config: Config):
    """
    Run pipeline health checks and print a report.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code: 0=OK, 1=WARN, 2=CRIT
    """
    db_path = "data/metroplex.db"
    report = run_health_checks(
        db_path=db_path,
        daily_cost_limit=config.daily_cost_limit,
    )
    print(format_report(report))
    return int(report.overall_status)


def cmd_ego(args, config: Config):
    """EGO learning system: show status or run an experiment."""
    from db import StateDB
    state_db = StateDB("data/metroplex.db")
    state_db.connect()
    state_db.init_db()

    if args.run:
        from learning.ego_runner import run_ego_cycle
        from event_emitter import create_event_emitter

        emitter = create_event_emitter()
        result = run_ego_cycle(
            state_db=state_db,
            event_emitter=emitter,
            dry_run=args.dry_run,
        )
        if result:
            print(f"\nExperiment result:")
            print(f"  Baseline score: {result.baseline_score:.1f}")
            print(f"  Variant score:  {result.variant_score:.1f}")
            print(f"  Improvement:    {result.improvement_pct:.1%}")
            print(f"  Winner:         {result.is_winner}")
            print(f"  Reason:         {result.reason}")
        else:
            print("No experiment ran (preconditions not met or rollback triggered)")
    else:
        from learning.ego_runner import ego_status
        print(ego_status(state_db))

    state_db.close()
    return 0


_SINGLE_INSTANCE_LOCK_FH = None


def _acquire_single_instance_lock():
    """Acquire an exclusive flock to prevent concurrent run-all instances.

    Fix D1: two run-all processes racing on the same metroplex.db cause
    interleaved dispatches and duplicate polls that corrupt build state.
    We use a POSIX flock on data/metroplex.lock; if a second process starts
    while one already holds it, the second exits with code 2.
    """
    global _SINGLE_INSTANCE_LOCK_FH
    lock_dir = Path(__file__).parent / "data"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "metroplex.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            f"ERROR: another metroplex run-all is already holding {lock_path}. Exiting.",
            file=sys.stderr,
        )
        fh.close()
        sys.exit(2)
    fh.write(str(os.getpid()))
    fh.flush()
    _SINGLE_INSTANCE_LOCK_FH = fh

    def _release():
        try:
            if _SINGLE_INSTANCE_LOCK_FH is not None:
                fcntl.flock(_SINGLE_INSTANCE_LOCK_FH.fileno(), fcntl.LOCK_UN)
                _SINGLE_INSTANCE_LOCK_FH.close()
        except Exception:
            pass

    atexit.register(_release)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Metroplex - Autonomous Build Layer",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Global flags
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # triage command
    triage_parser = subparsers.add_parser("triage", help="Run Gate 1 (triage) only")
    triage_parser.add_argument("--dry-run", action="store_true", help="Print decisions without writing to DB")

    # build command
    build_parser = subparsers.add_parser("build", help="Run Gate 2 (build) only")
    build_parser.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    build_parser.add_argument("--idea-id", type=int, help="Build specific idea ID")

    # publish command
    publish_parser = subparsers.add_parser("publish", help="Run Gate 4 (publish) -- push builds to GitHub")
    publish_parser.add_argument("--dry-run", action="store_true", help="Show what would be published without creating repos")

    # run-all command
    run_all_parser = subparsers.add_parser("run-all", help="Run full cycle (triage → build → publish)")
    run_all_parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode")
    run_all_parser.add_argument("--cycles", type=int, default=1, help="Number of cycles (0=infinite)")

    # queue command
    queue_parser = subparsers.add_parser("queue", help="Show priority queue contents")

    # status command
    status_parser = subparsers.add_parser("status", help="Show current system status")

    # dispatch command
    dispatch_parser = subparsers.add_parser("dispatch", help="Dispatch queue items to EA-Claude workers")
    dispatch_parser.add_argument("--dry-run", action="store_true", help="Show what would be dispatched")
    dispatch_parser.add_argument("--item-id", type=int, help="Dispatch specific queue item by ID")
    dispatch_parser.add_argument("--count", type=int, default=1, help="Number of items to dispatch (default: 1)")
    dispatch_parser.add_argument("--worker", choices=["starscream", "ravage", "soundwave", "astrotrain", "default"],
                                help="Override auto-routed worker type")

    # builds command
    builds_parser = subparsers.add_parser("builds", help="Show build history and output locations")

    # retry command
    retry_parser = subparsers.add_parser("retry", help="Re-dispatch a failed build")
    retry_parser.add_argument("build_id", help="Queue job ID to retry (e.g. metroplex-ideaforge-79)")

    # cost command
    cost_parser = subparsers.add_parser("cost", help="Show cost tracking summary")
    cost_parser.add_argument("--days", type=int, default=7, help="Number of days for breakdown (default: 7)")

    # backfill-outcomes command
    backfill_parser = subparsers.add_parser("backfill-outcomes", help="Backfill OutcomeRecords for terminal-state ideas")
    backfill_parser.add_argument("--dry-run", action="store_true", help="Show what would be emitted without writing")

    # score-builds command
    score_parser = subparsers.add_parser("score-builds", help="Score completed builds for structural quality (Phase 14b)")
    score_parser.add_argument("--dry-run", action="store_true", help="Show scores without writing to DB")

    # quality-digest command
    digest_parser = subparsers.add_parser("quality-digest", help="Print quality digest for daily reports (Phase 14d)")

    # funnel command
    funnel_parser = subparsers.add_parser("funnel", help="Show pipeline funnel conversion metrics (Phase D1)")
    funnel_parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7, max: 90)")
    funnel_parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted table")

    # postmortems command
    postmortems_parser = subparsers.add_parser("postmortems", help="Show build failure post-mortem summary")

    # reset command
    reset_parser = subparsers.add_parser("reset", help="Reset circuit breaker")
    reset_parser.add_argument("--gate", required=True, choices=["triage", "build", "publish", "all"], help="Gate to reset")

    # recalibrate command
    recalibrate_parser = subparsers.add_parser("recalibrate", help="Force-reset quality ratchet threshold to current proposed value")
    recalibrate_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # health command
    health_parser = subparsers.add_parser("health", help="Run pipeline health checks (Phase D)")

    # readiness command (Gate 4.9)
    readiness_parser = subparsers.add_parser("readiness", help="Run Gate 4.9 (readiness checks) on published builds")
    readiness_parser.add_argument("--dry-run", action="store_true", help="Show checks without fixing")

    # readiness-fix command (batch mode)
    readiness_fix_parser = subparsers.add_parser("readiness-fix", help="Run readiness checks + fixes on specific repos")
    readiness_fix_parser.add_argument("--repo", type=str, help="Single repo name to check/fix")
    readiness_fix_parser.add_argument("--all", action="store_true", dest="all_repos", help="Run on all 16 known repos")
    readiness_fix_parser.add_argument("--dry-run", action="store_true", help="Show checks without fixing")

    # ego command (Phase F learning loop)
    ego_parser = subparsers.add_parser("ego", help="EGO learning system status and manual experiment run")
    ego_parser.add_argument("--run", action="store_true", help="Run one EGO experiment cycle now")
    ego_parser.add_argument("--dry-run", action="store_true", help="Evaluate but don't apply winners")

    # Parse arguments
    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)

    # Load configuration
    config = Config()
    warnings = config.validate()
    if warnings:
        print("Configuration warnings:")
        for warning in warnings:
            print(f"  - {warning}")
        print()

    # Execute command
    if args.command == "triage":
        sys.exit(cmd_triage(args, config))
    elif args.command == "build":
        sys.exit(cmd_build(args, config))
    elif args.command == "publish":
        sys.exit(cmd_publish(args, config))
    elif args.command == "run-all":
        # Single-instance lock: prevent dual metroplex daemons writing to
        # the same SQLite DB (Bug 2 — April 3 orphan process).
        # Dry runs are exempt — they don't modify DB state and tests
        # expect to run them alongside a live daemon.
        if not getattr(args, "dry_run", False):
            _acquire_single_instance_lock()
        sys.exit(cmd_run_all(args, config))
    elif args.command == "queue":
        sys.exit(cmd_queue(args, config))
    elif args.command == "status":
        sys.exit(cmd_status(args, config))
    elif args.command == "dispatch":
        sys.exit(cmd_dispatch(args, config))
    elif args.command == "builds":
        sys.exit(cmd_builds(args, config))
    elif args.command == "retry":
        sys.exit(cmd_retry(args, config))
    elif args.command == "cost":
        sys.exit(cmd_cost(args, config))
    elif args.command == "postmortems":
        sys.exit(cmd_postmortems(args, config))
    elif args.command == "reset":
        sys.exit(cmd_reset(args, config))
    elif args.command == "recalibrate":
        sys.exit(cmd_recalibrate(args, config))
    elif args.command == "backfill-outcomes":
        sys.exit(cmd_backfill_outcomes(args, config))
    elif args.command == "score-builds":
        sys.exit(cmd_score_builds(args, config))
    elif args.command == "quality-digest":
        sys.exit(cmd_quality_digest(args, config))
    elif args.command == "funnel":
        sys.exit(cmd_funnel(args, config))
    elif args.command == "health":
        sys.exit(cmd_health(args, config))
    elif args.command == "readiness":
        sys.exit(cmd_readiness(args, config))
    elif args.command == "readiness-fix":
        sys.exit(cmd_readiness_fix(args, config))
    elif args.command == "ego":
        sys.exit(cmd_ego(args, config))
    else:
        parser.print_help()
        sys.exit(1)


# --- Readiness Commands (Gate 4.9) ---

BATCH_FIX_REPOS = [
    "agenticstarter",
    "hipaa-compliant-mcp-security-proxy-for-healthcare-ai-agents",
    "landingflow-audit",
    "local-ai-coding-agent-optimizer",
    "march-2026-ai-coding-tool-power-rankings-update",
    "mcp-healthcare",
    "mcpick",
    "microservice-contract-validator",
    "personalokr",
    "self-hosted-mcp-server-for-local-ai-agent-orchestration",
    "semantiguard",
    "suprlogs",
    "timebill",
    "workflowmcp",
]


def cmd_readiness(args, config: Config):
    """Run Gate 4.9 (readiness) on published builds missing readiness checks."""
    state_db = StateDB()
    state_db.init_db()
    audit_logger = AuditLogger()

    gate = ReadinessGate(config=config, state_db=state_db, audit_logger=audit_logger)

    try:
        print("Running Gate 4.9 (Readiness)...")
        results = gate.run(dry_run=args.dry_run)
        ok_count = sum(1 for r in results if r.get("status") == "completed")
        print(f"\nReadiness: {len(results)} processed, {ok_count} fully ready")
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


def cmd_readiness_fix(args, config: Config):
    """Run readiness checks + fixes on specific repos (batch mode)."""
    state_db = StateDB()
    state_db.init_db()
    audit_logger = AuditLogger()

    gate = ReadinessGate(config=config, state_db=state_db, audit_logger=audit_logger)

    if args.repo:
        repo_names = [args.repo]
    elif args.all_repos:
        repo_names = BATCH_FIX_REPOS
    else:
        print("ERROR: Specify --repo <name> or --all")
        return 1

    try:
        print(f"Running readiness fix on {len(repo_names)} repo(s)...")
        results = gate.run_batch(repo_names=repo_names, dry_run=args.dry_run)

        # Summary table
        print(f"\n{'Repo':<60} {'Status':<10} {'Fixed':<8} {'Remaining':<10}")
        print("-" * 88)
        for r in results:
            name = r.get("repo_name", "?")
            status = r.get("status", "?")
            fixed = len(r.get("fixes_applied", []))
            remaining = len(r.get("fixes_failed", []))
            print(f"{name:<60} {status:<10} {fixed:<8} {remaining:<10}")

        ok_count = sum(1 for r in results if r.get("status") == "completed")
        print(f"\nTotal: {len(results)} repos, {ok_count} fully ready")
        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        state_db.close()


if __name__ == "__main__":
    main()
