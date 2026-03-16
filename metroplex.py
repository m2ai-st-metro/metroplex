#!/usr/bin/env python
"""
Metroplex CLI - Autonomous Build Layer Entry Point
Orchestrates triage, build, and patch gates with safety systems.
"""
import sys
import argparse
import json
from pathlib import Path

from config import Config
from db import StateDB
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from gates.triage import TriageGate
from gates.build import SpecGenerator, BuildOrchestrator
from gates.patcher import PatchGate
from gates.publish import PublishGate
from gates.review import ReviewGate
from gates.tyrest import TyrestGate
from orchestrator import CycleOrchestrator
from notifier import create_notifier, FilteredNotifier
from readers.ideaforge_reader import IdeaForgeReader
from readers.linear_reader import LinearReader
from readers.academy_reader import AcademyReader
from readers.skylynx_reader import SkyLynxReader
from readers.stfactory_reader import STFactoryReader
from dispatcher import create_dispatcher, route_to_worker, build_dispatch_prompt
from outcome_emitter import create_outcome_emitter


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
        stfactory_reader = STFactoryReader(config.stfactory_db)
    except FileNotFoundError:
        print(f"Warning: ST Factory DB not found at {config.stfactory_db}")
        stfactory_reader = None

    try:
        skylynx_reader = SkyLynxReader(config.stfactory_db)
    except FileNotFoundError:
        print(f"Warning: ST Factory DB not found at {config.stfactory_db} (Sky-Lynx reader)")
        skylynx_reader = None

    # Initialize Linear reader (requires ARCADE_API_KEY)
    import os
    arcade_key = os.environ.get("ARCADE_API_KEY", "")
    if arcade_key and config.linear_team:
        try:
            linear_reader = LinearReader(
                arcade_api_key=arcade_key,
                arcade_user_id=os.environ.get("ARCADE_USER_ID", "agent@local"),
                team=config.linear_team,
                label_filter=config.linear_label_filter,
                poll_states=config.linear_poll_states,
            )
        except (ValueError, Exception) as e:
            print(f"Warning: Linear reader init failed: {e}")
            linear_reader = None
    else:
        linear_reader = None
        if not arcade_key:
            pass  # Silent -- Arcade key not configured
        elif not config.linear_team:
            pass  # Silent -- no team configured

    # Initialize Academy reader (reads from promotions JSONL file)
    academy_reader = AcademyReader(
        promotions_path=config.academy_promotions_path,
        academy_dir=config.academy_dir,
    )

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

    # Tyrest gate init (moved earlier so build_orchestrator can use it)
    tyrest_gate = None
    if config.tyrest_enabled:
        try:
            tyrest_gate = TyrestGate(
                enabled=True,
                model=config.tyrest_model,
                approve_confidence=config.tyrest_approve_confidence,
                reject_confidence=config.tyrest_reject_confidence,
            )
        except ValueError as e:
            print(f"Warning: Tyrest gate disabled — {e}")

    build_orchestrator = BuildOrchestrator(
        config=config,
        state_db=state_db,
        spec_generator=spec_generator,
        audit_logger=audit_logger,
        tyrest_gate=tyrest_gate,
    )

    patch_gate = PatchGate(
        config=config,
        state_db=state_db,
        stfactory_reader=stfactory_reader,
        audit_logger=audit_logger
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

    # Initialize notifier (wrapped with FilteredNotifier for anomaly/summary modes)
    raw_notifier = create_notifier(config.telegram_bot_token, config.telegram_chat_id)
    notifier = FilteredNotifier(raw_notifier, config.notify_mode)

    # Initialize outcome emitter (Phase 14a — write OutcomeRecords to ST Factory)
    outcome_emitter = create_outcome_emitter()

    # Initialize dispatcher for non-buildable queue items (Sky-Lynx -> ClaudeClaw)
    dispatcher = create_dispatcher(config.dispatch_db, config.dispatch_chat_id)

    # Initialize orchestrator
    orchestrator = CycleOrchestrator(
        config=config,
        triage_gate=triage_gate,
        build_orchestrator=build_orchestrator,
        patch_gate=patch_gate,
        circuit_breaker=circuit_breaker,
        cycle_caps=cycle_caps,
        shutdown_handler=shutdown_handler,
        state_db=state_db,
        audit_logger=audit_logger,
        cycle_sleep_seconds=config.cycle_sleep_seconds,
        notifier=notifier,
        skylynx_reader=skylynx_reader,
        linear_reader=linear_reader,
        academy_reader=academy_reader,
        publish_gate=publish_gate,
        review_gate=review_gate,
        tyrest_gate=tyrest_gate,
        dispatcher=dispatcher,
        outcome_emitter=outcome_emitter,
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


def cmd_patch(args, config: Config):
    """
    Run Gate 3 (patch) only.

    Args:
        args: Parsed command-line arguments
        config: Metroplex configuration

    Returns:
        Exit code (0=success, 1=error, 2=halted)
    """
    orchestrator, state_db, circuit_breaker = initialize_components(config)

    # Check if gate is halted
    if circuit_breaker.is_halted("patch"):
        print("ERROR: Patch gate is halted by circuit breaker")
        print("Run 'metroplex.py reset --gate patch' to reset")
        return 2

    try:
        print("Running Gate 3 (Patch)...")
        patches = orchestrator.patch_gate.run(dry_run=args.dry_run)

        print(f"\nCompleted: {len(patches)} patches")
        print(f"  Applied: {sum(1 for p in patches if p.status == 'applied')}")
        print(f"  Failed: {sum(1 for p in patches if p.status == 'failed')}")
        print(f"  Skipped: {sum(1 for p in patches if p.status == 'skipped')}")

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
                print(f"  + {config.github_org}/{job.repo_name} -> {job.repo_url}")
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
    Run full cycle (triage → build → publish → patch).

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
        print(f"Total patches: {sum(r.patch_count for r in results)}")
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
            print(f"    Triage: {cycle['triage_count']}, Build: {cycle['build_count']}, Patch: {cycle['patch_count']}")
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

            print(f"  {status_icon} [{r['status']:>9}] {r['title']}")
            print(f"    ID: {r['queue_job_id']}  Idea: {r['idea_id']}")
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

        return 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
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
            gates = ["triage", "build", "publish", "patch"]
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

    # patch command
    patch_parser = subparsers.add_parser("patch", help="Run Gate 3 (patch) only")
    patch_parser.add_argument("--dry-run", action="store_true", help="Print patches without applying")

    # run-all command
    run_all_parser = subparsers.add_parser("run-all", help="Run full cycle (triage → build → publish → patch)")
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

    # reset command
    reset_parser = subparsers.add_parser("reset", help="Reset circuit breaker")
    reset_parser.add_argument("--gate", required=True, choices=["triage", "build", "publish", "patch", "all"], help="Gate to reset")

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
    elif args.command == "patch":
        sys.exit(cmd_patch(args, config))
    elif args.command == "run-all":
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
    elif args.command == "reset":
        sys.exit(cmd_reset(args, config))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
