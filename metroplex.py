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
from orchestrator import CycleOrchestrator
from notifier import create_notifier
from readers.ideaforge_reader import IdeaForgeReader
from readers.stfactory_reader import STFactoryReader


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

    # Initialize gates
    triage_gate = TriageGate(
        config=config,
        state_db=state_db,
        ideaforge_reader=ideaforge_reader,
        audit_logger=audit_logger
    )

    # Initialize spec generator
    template_dir = Path("spec_templates")
    try:
        spec_generator = SpecGenerator(config, template_dir)
    except FileNotFoundError:
        print(f"Warning: Template directory not found at {template_dir}")
        spec_generator = None

    build_orchestrator = BuildOrchestrator(
        config=config,
        state_db=state_db,
        spec_generator=spec_generator,
        audit_logger=audit_logger
    )

    patch_gate = PatchGate(
        config=config,
        state_db=state_db,
        stfactory_reader=stfactory_reader,
        audit_logger=audit_logger
    )

    # Initialize notifier
    notifier = create_notifier(config.telegram_bot_token, config.telegram_chat_id)

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
        notifier=notifier
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


def cmd_run_all(args, config: Config):
    """
    Run full cycle (triage → build → patch).

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
            gates = ["triage", "build", "patch"]
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

    # patch command
    patch_parser = subparsers.add_parser("patch", help="Run Gate 3 (patch) only")
    patch_parser.add_argument("--dry-run", action="store_true", help="Print patches without applying")

    # run-all command
    run_all_parser = subparsers.add_parser("run-all", help="Run full cycle (triage → build → patch)")
    run_all_parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode")
    run_all_parser.add_argument("--cycles", type=int, default=1, help="Number of cycles (0=infinite)")

    # queue command
    queue_parser = subparsers.add_parser("queue", help="Show priority queue contents")

    # status command
    status_parser = subparsers.add_parser("status", help="Show current system status")

    # reset command
    reset_parser = subparsers.add_parser("reset", help="Reset circuit breaker")
    reset_parser.add_argument("--gate", required=True, choices=["triage", "build", "patch", "all"], help="Gate to reset")

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
    elif args.command == "patch":
        sys.exit(cmd_patch(args, config))
    elif args.command == "run-all":
        sys.exit(cmd_run_all(args, config))
    elif args.command == "queue":
        sys.exit(cmd_queue(args, config))
    elif args.command == "status":
        sys.exit(cmd_status(args, config))
    elif args.command == "reset":
        sys.exit(cmd_reset(args, config))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
