"""
Idea Context Helper

Looks up IdeaForge context (problem_statement, target_audience, plain description)
for a given build_job_id so downstream publish-phase gates (README, Readiness)
can use the original plain-speak framing instead of re-deriving it from code.

Returns None for builds that did not originate in IdeaForge (e.g. Sky-Lynx)
-- callers fall back to their prior behavior.
"""
import logging
from pathlib import Path

from db import StateDB
from readers.ideaforge_reader import IdeaForgeReader

logger = logging.getLogger(__name__)


def load_idea_context(
    state_db: StateDB,
    build_job_id: str,
    ideaforge_db_path: str,
) -> dict | None:
    """
    Resolve the IdeaForge idea behind a build and return publish-relevant fields.

    Args:
        state_db: Metroplex state DB (for build_jobs.idea_id lookup)
        build_job_id: queue_job_id on build_jobs
        ideaforge_db_path: Path to ideaforge.db

    Returns:
        Dict with keys {description, problem_statement, target_audience} (all
        stripped strings; missing fields become empty strings), or None if the
        idea cannot be resolved.
    """
    build = state_db.get_build_by_queue_job_id(build_job_id)
    if not build:
        return None

    idea_id = build.get("idea_id")
    if not idea_id:
        return None

    if not Path(ideaforge_db_path).exists():
        logger.warning(f"IdeaForge DB not found at {ideaforge_db_path} -- skipping context lookup")
        return None

    try:
        reader = IdeaForgeReader(ideaforge_db_path)
        idea = reader.get_idea_by_id(int(idea_id))
        reader.close()
    except Exception as e:
        logger.warning(f"Failed to load idea {idea_id} from IdeaForge: {e}")
        return None

    if not idea:
        return None

    return {
        "description": (idea.get("description") or "").strip(),
        "problem_statement": (idea.get("problem_statement") or "").strip(),
        "target_audience": (idea.get("target_audience") or "").strip(),
        "struggling_user": (idea.get("struggling_user") or "").strip(),
    }
