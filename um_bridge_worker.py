#!/usr/bin/env python3
"""
UM Bridge Worker
================

Subprocess that runs the Ultra-Magnus pipeline for a single idea.
Invoked by um_bridge.py as a detached process.

1. Imports UM's Repository and PipelineOrchestrator
2. Creates the idea in UM's database
3. Runs the full pipeline with auto-approve for HIL gates
4. Writes the outcome back to IdeaForge's database
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load shared credentials
load_dotenv(Path.home() / ".env.shared")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [um-bridge] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UM Bridge Worker")
    parser.add_argument("--idea-json", required=True, help="JSON-encoded idea dict")
    parser.add_argument("--um-path", required=True, type=Path, help="Path to UM idea-factory")
    return parser.parse_args()


def setup_um_imports(um_path: Path) -> None:
    """Add UM's idea-factory root to sys.path for package imports.

    UM uses relative imports (from ..core.models), so we must import
    via the 'src' package: e.g., from src.pipeline.orchestrator import ...
    """
    if str(um_path) not in sys.path:
        sys.path.insert(0, str(um_path))


def write_back_to_metroplex(idea_id: int, um_idea_id: str, outcome: str) -> None:
    """Write build results back to Metroplex's database.

    Updates both build_jobs (status + project_dir) and priority_queue
    (status + completed_at) so Metroplex's publish gate and status
    reporting reflect the actual build outcome.

    Args:
        idea_id: IdeaForge idea ID
        um_idea_id: UM's UUID for this idea
        outcome: Pipeline outcome ('success', 'failed', 'partial', 'paused')
    """
    metroplex_db = Path(__file__).parent / "data" / "metroplex.db"
    if not metroplex_db.exists():
        logger.warning("Metroplex DB not found at %s", metroplex_db)
        return

    source = "ideaforge"
    queue_job_id = f"metroplex-{source}-{idea_id}"
    build_status = "completed" if outcome in ("success", "partial") else "failed"

    # Find the YCE project directory
    project_dir_str = None
    yce_generations = Path(__file__).parent.parent / "yce-harness" / "generations"
    if yce_generations.is_dir() and um_idea_id:
        uuid_prefix = um_idea_id[:12]
        for entry in yce_generations.iterdir():
            if entry.is_dir() and entry.name.startswith("um-") and uuid_prefix in entry.name:
                project_dir_str = str(entry)
                break

    try:
        conn = sqlite3.connect(str(metroplex_db))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Update build_jobs: status + project_dir
        cursor.execute(
            "UPDATE build_jobs SET status = ?, project_dir = ? WHERE queue_job_id = ? AND status != ?",
            (build_status, project_dir_str, queue_job_id, build_status),
        )
        build_changed = cursor.rowcount > 0

        # Update priority_queue: status + completed_at
        if build_changed:
            now = datetime.now(timezone.utc).isoformat()
            cursor.execute(
                "UPDATE priority_queue SET status = ?, completed_at = ? WHERE source = ? AND source_id = ?",
                (build_status, now, source, str(idea_id)),
            )

        conn.commit()
        conn.close()

        if build_changed:
            logger.info(
                "Metroplex sync: %s → %s (project_dir=%s)",
                queue_job_id, build_status, Path(project_dir_str).name if project_dir_str else "none",
            )
        else:
            logger.warning("No build_job found (or already synced) for %s", queue_job_id)

    except Exception as e:
        logger.error("Metroplex writeback failed: %s", e)


def write_back_to_ideaforge(
    idea_id: int,
    um_idea_id: str,
    outcome: str,
) -> None:
    """Write pipeline result back to IdeaForge's SQLite database.

    Updates the idea's status to 'exported' and records the UM idea ID
    and completion timestamp.
    """
    ideaforge_db = Path(__file__).parent.parent / "ideaforge" / "data" / "ideaforge.db"
    if not ideaforge_db.exists():
        logger.warning("IdeaForge DB not found at %s, skipping writeback", ideaforge_db)
        return

    try:
        conn = sqlite3.connect(str(ideaforge_db))
        conn.execute(
            """
            UPDATE ideas
            SET status = 'exported',
                exported_at = ?,
                ultra_magnus_id = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                um_idea_id,
                idea_id,
            ),
        )
        conn.commit()
        conn.close()
        logger.info("IdeaForge writeback complete: idea %s → UM %s (%s)", idea_id, um_idea_id, outcome)
    except Exception as e:
        logger.error("IdeaForge writeback failed for idea %s: %s", idea_id, e)


async def run_um_pipeline(idea_data: dict, um_path: Path) -> dict:
    """Run the full UM pipeline for an idea.

    Returns:
        dict with keys: um_idea_id, outcome, message, duration_seconds
    """
    start_time = time.monotonic()
    setup_um_imports(um_path)

    # Import UM modules (after sys.path setup)
    from src.pipeline.orchestrator import PipelineOrchestrator
    from src.core.models import IdeaInput, ProjectMode, ReviewDecision
    from src.db.repository import Repository

    # Initialize UM's database connection
    db_path = um_path / "data" / "idea-factory.db"
    repo = Repository(db_path)
    await repo.connect()

    try:
        # Create the idea in UM's database using raw SQL to avoid
        # schema mismatches (code may have columns the DB doesn't yet)
        import json as _json
        from uuid import uuid4
        raw_content = _build_raw_content(idea_data)
        um_idea_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        tags = _json.dumps(idea_data.get("source_subreddits", []))

        await repo.db.execute(
            """
            INSERT INTO ideas (id, title, raw_content, tags, current_stage, current_status, submitted_at, updated_at)
            VALUES (?, ?, ?, ?, 'input', 'pending', ?, ?)
            """,
            (um_idea_id, idea_data["title"], raw_content, tags, now, now),
        )
        await repo.db.commit()
        logger.info("Created UM idea: %s (%s)", um_idea_id, idea_data["title"])

        # Run the pipeline
        orchestrator = PipelineOrchestrator(repo)
        auto_approve = os.getenv("METROPLEX_AUTO_APPROVE", "true").lower() in ("true", "1", "yes")

        # Start pipeline (INPUT → ENRICHMENT)
        result = await orchestrator.start_pipeline(um_idea_id)
        if not result.success:
            logger.error("Pipeline start failed: %s", result.message)
            return _result(um_idea_id, "failed", result.message, start_time)

        # Continue until pipeline completes or fails
        max_continuations = 20  # Safety limit
        for i in range(max_continuations):
            result = await orchestrator.continue_pipeline(um_idea_id)

            if not result.success and not result.requires_review:
                logger.error("Pipeline failed at iteration %d: %s", i, result.message)
                return _result(um_idea_id, "failed", result.message, start_time)

            if result.requires_review and auto_approve:
                logger.info("Auto-approving HIL gate at %s", result.stage)
                result = await orchestrator.apply_review(
                    um_idea_id,
                    decision=ReviewDecision.APPROVE,
                    rationale="Auto-approved: Metroplex-sourced, pre-triaged by IdeaForge scoring",
                    reviewer="metroplex-auto",
                )
                if not result.success:
                    logger.error("Auto-approve failed: %s", result.message)
                    return _result(um_idea_id, "failed", result.message, start_time)
                continue

            if result.requires_review and not auto_approve:
                logger.info("Pipeline paused at HIL gate (auto-approve disabled)")
                return _result(um_idea_id, "paused", "Awaiting human review", start_time)

            # Check if pipeline is done
            idea = await repo.get_idea(um_idea_id)
            if idea and idea.current_stage.value in ("completed", "deployment"):
                if idea.current_stage.value == "completed":
                    logger.info("Pipeline completed successfully")
                    return _result(um_idea_id, "success", "Pipeline completed", start_time)
                # If in deployment, continue to let it finish
                continue

        logger.warning("Pipeline hit max continuations (%d)", max_continuations)
        return _result(um_idea_id, "partial", "Max continuations reached", start_time)

    finally:
        await repo.close()


def _build_raw_content(idea_data: dict) -> str:
    """Build UM's raw_content from IdeaForge idea fields."""
    parts = []
    if idea_data.get("description"):
        parts.append(idea_data["description"])
    if idea_data.get("problem_statement"):
        parts.append(f"\n## Problem Statement\n{idea_data['problem_statement']}")
    if idea_data.get("target_audience"):
        parts.append(f"\n## Target Audience\n{idea_data['target_audience']}")
    if idea_data.get("artifact_type"):
        parts.append(f"\n## Artifact Type\n{idea_data['artifact_type']}")
    if idea_data.get("weighted_score"):
        parts.append(f"\n## IdeaForge Score\n{idea_data['weighted_score']}/10")
    return "\n".join(parts) if parts else idea_data.get("title", "No description")


def _result(um_idea_id: str, outcome: str, message: str, start_time: float) -> dict:
    return {
        "um_idea_id": um_idea_id,
        "outcome": outcome,
        "message": message,
        "duration_seconds": round(time.monotonic() - start_time, 1),
    }


def emit_outcome_record(idea_data: dict, result: dict) -> None:
    """Write a structured outcome record to ST Factory's persona_metrics.db.

    This is the feedback loop closure: it links IdeaForge scores and triage data
    to the actual build outcome, enabling Sky-Lynx to correlate upstream decision
    quality with downstream results.
    """
    stfactory_db = Path(os.getenv(
        "STFACTORY_DB_PATH",
        str(Path(__file__).parent.parent / "st-factory" / "data" / "persona_metrics.db"),
    ))
    if not stfactory_db.exists():
        logger.warning("ST Factory DB not found at %s, skipping outcome record", stfactory_db)
        return

    outcome_data = {
        "ideaforge_id": idea_data.get("id"),
        "ideaforge_scores": {
            "weighted_score": idea_data.get("weighted_score"),
            "opportunity_score": idea_data.get("opportunity_score"),
            "problem_score": idea_data.get("problem_score"),
            "feasibility_score": idea_data.get("feasibility_score"),
            "why_now_score": idea_data.get("why_now_score"),
            "competition_score": idea_data.get("competition_score"),
        },
        "artifact_type": idea_data.get("artifact_type"),
        "signal_count": idea_data.get("signal_count"),
        "um_idea_id": result.get("um_idea_id"),
        "pipeline_message": result.get("message"),
        "source": "metroplex_um_bridge",
    }

    try:
        conn = sqlite3.connect(str(stfactory_db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """INSERT INTO outcome_records
               (idea_id, idea_title, outcome, overall_score, recommendation,
                capabilities_fit, build_outcome, artifact_count, tech_stack,
                total_duration_seconds, tags, github_url, emitted_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                idea_data.get("id"),
                idea_data.get("title", ""),
                result.get("outcome", "unknown"),
                idea_data.get("weighted_score"),
                idea_data.get("artifact_type", ""),
                "",  # capabilities_fit (populated later by UM evaluation)
                result.get("outcome", "unknown"),
                0,  # artifact_count (populated later)
                json.dumps(idea_data.get("source_subreddits", [])),
                result.get("duration_seconds", 0),
                json.dumps([]),  # tags
                "",  # github_url (populated by publish gate)
                datetime.now(timezone.utc).isoformat(),
                json.dumps(outcome_data),
            ),
        )
        conn.commit()
        conn.close()
        logger.info(
            "Outcome record emitted to ST Factory: idea=%s outcome=%s score=%s",
            idea_data.get("id"), result.get("outcome"), idea_data.get("weighted_score"),
        )
    except Exception as e:
        logger.error("Failed to emit outcome record: %s", e)


def main() -> int:
    args = parse_args()

    try:
        idea_data = json.loads(args.idea_json)
    except json.JSONDecodeError as e:
        logger.error("Invalid idea JSON: %s", e)
        return 1

    idea_id = idea_data.get("id")
    logger.info("Starting UM pipeline for IdeaForge idea %s: %s", idea_id, idea_data.get("title"))

    try:
        result = asyncio.run(run_um_pipeline(idea_data, args.um_path))

        # Write back to IdeaForge and Metroplex
        if result.get("um_idea_id"):
            write_back_to_ideaforge(
                idea_id=idea_id,
                um_idea_id=result["um_idea_id"],
                outcome=result["outcome"],
            )
            write_back_to_metroplex(
                idea_id=idea_id,
                um_idea_id=result["um_idea_id"],
                outcome=result["outcome"],
            )

        # Emit structured outcome record to ST Factory for feedback loop
        emit_outcome_record(idea_data, result)

        logger.info(
            "UM pipeline complete: idea=%s outcome=%s duration=%ss message=%s",
            idea_id, result["outcome"], result["duration_seconds"], result["message"],
        )
        return 0 if result["outcome"] == "success" else 1

    except Exception as e:
        logger.error("UM pipeline crashed for idea %s: %s", idea_id, e)
        traceback.print_exc()

        # Still try to write back failure to both databases
        write_back_to_ideaforge(idea_id=idea_id, um_idea_id="", outcome="failed")
        write_back_to_metroplex(idea_id=idea_id, um_idea_id="", outcome="failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
