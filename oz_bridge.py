"""
Oz Cloud Bridge
================

Fire-and-forget adapter that submits triaged ideas to Oz cloud agents
for building. Mirrors the um_bridge.py pattern but dispatches to Warp's
cloud infrastructure instead of a local subprocess.

On completion, results are polled via the Oz API and synced back to
Metroplex's state DB.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-import SDK to avoid hard dependency when running in local-only mode
_oz_client = None
_oz_async_client = None


def _get_client():
    """Lazy-initialize the synchronous Oz API client."""
    global _oz_client
    if _oz_client is None:
        try:
            from oz_agent_sdk import OzAPI
            api_key = os.environ.get("WARP_API_KEY", "")
            if not api_key:
                raise ValueError("WARP_API_KEY not set in environment")
            _oz_client = OzAPI(api_key=api_key)
        except ImportError:
            raise ImportError(
                "oz-agent-sdk not installed. Run: pip install oz-agent-sdk"
            )
    return _oz_client


def submit_to_oz(
    idea: dict,
    environment_id: str,
    model_id: str = "claude-sonnet-4-20250514",
    dry_run: bool = False,
) -> Optional[str]:
    """Submit a triaged idea to Oz cloud agent for building.

    Creates a cloud agent run with the idea as prompt context.
    The agent clones the repos configured in the environment,
    generates a spec, and runs the full build pipeline.

    Args:
        idea: Idea dict with keys: id, title, description,
              problem_statement, target_audience, artifact_type
        environment_id: Oz environment ID (e.g. st-metro-builds)
        model_id: LLM model for the cloud agent
        dry_run: If True, log without executing

    Returns:
        run_id (str) if launched, None on error or dry_run
    """
    if dry_run:
        logger.info(
            "[DRY RUN] Would submit to Oz cloud: idea=%s title=%s env=%s",
            idea.get("id"), idea.get("title"), environment_id,
        )
        return None

    prompt = _build_prompt(idea)

    try:
        client = _get_client()
        response = client.agent.run(
            prompt=prompt,
            title=f"Metroplex Build: {idea.get('title', 'unknown')}",
            config={
                "environment_id": environment_id,
                "model_id": model_id,
                "name": f"metroplex-build-{idea.get('id', 'unknown')}",
            },
        )

        run_id = response.run_id
        logger.info(
            "Oz cloud agent launched for idea %s (%s) -- run_id=%s",
            idea.get("id"), idea.get("title"), run_id,
        )
        return run_id

    except Exception as e:
        logger.error(
            "Failed to submit idea %s to Oz cloud: %s",
            idea.get("id"), e,
        )
        return None


def poll_oz_run(run_id: str) -> Optional[dict]:
    """Poll the status of an Oz cloud agent run.

    Args:
        run_id: The Oz run ID returned by submit_to_oz

    Returns:
        Dict with keys: run_id, state, title, session_link, created_at, updated_at
        Returns None on error.
    """
    try:
        client = _get_client()
        run = client.agent.runs.retrieve(run_id)
        return {
            "run_id": run.run_id,
            "state": run.state,  # QUEUED, INPROGRESS, SUCCEEDED, FAILED
            "title": getattr(run, "title", ""),
            "session_link": getattr(run, "session_link", ""),
            "created_at": getattr(run, "created_at", ""),
            "updated_at": getattr(run, "updated_at", ""),
        }
    except Exception as e:
        logger.error("Failed to poll Oz run %s: %s", run_id, e)
        return None


def _build_prompt(idea: dict) -> str:
    """Build the cloud agent prompt from idea data."""
    return f"""You are a build agent for the ST Metro autonomous software ecosystem.

## Task
Build the following project from its specification. Follow the standard
Ultra-Magnus pipeline: generate app spec, scaffold the project, implement
all features, run tests until green, then commit and push to a new branch.

## Idea
- **ID**: {idea.get('id')}
- **Title**: {idea.get('title')}
- **Description**: {idea.get('description', '')}
- **Problem Statement**: {idea.get('problem_statement', '')}
- **Target Audience**: {idea.get('target_audience', '')}
- **Artifact Type**: {idea.get('artifact_type', 'tool')}

## Instructions
1. Read the Ultra-Magnus idea-factory codebase to understand the pipeline
2. Generate an app spec based on the idea above
3. Use the YCE harness to scaffold and build the project
4. Run all tests and fix any failures
5. Create a feature branch and push when green
6. Report the branch name and test results when done

If you encounter errors, debug and retry up to 3 times before reporting failure.
"""
