"""
Ultra-Magnus Bridge
====================

Fire-and-forget adapter that submits triaged ideas from Metroplex
to Ultra-Magnus's pipeline. Runs the UM pipeline in a subprocess
to isolate DB lifecycle and avoid blocking Metroplex's gate cycle.

On completion (success or failure), writes results back to
IdeaForge's DB so Metroplex can track outcomes on its next cycle.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve UM path from env or default sibling directory
ULTRA_MAGNUS_PATH = Path(
    os.getenv(
        "ULTRA_MAGNUS_PATH",
        str(Path(__file__).parent.parent / "ultra-magnus" / "idea-factory"),
    )
)

# Worker script lives alongside this module
_WORKER_SCRIPT = Path(__file__).parent / "um_bridge_worker.py"

# Wall-clock timeout for UM bridge builds (seconds). Default 90 minutes.
BUILD_TIMEOUT = int(os.getenv("METROPLEX_BUILD_TIMEOUT_SECONDS", "5400"))


def submit_to_um(idea: dict, dry_run: bool = False) -> bool:
    """Submit a triaged idea to Ultra-Magnus pipeline (fire-and-forget).

    Spawns a detached subprocess that:
    1. Creates the idea in UM's database
    2. Runs the full pipeline (enrichment → evaluation → scaffolding → build)
    3. Writes the result back to IdeaForge's DB

    Args:
        idea: Idea dict with keys: id, title, description, problem_statement,
              target_audience, artifact_type, weighted_score, source_subreddits
        dry_run: If True, log the command without executing

    Returns:
        True if subprocess was launched, False on error
    """
    if not ULTRA_MAGNUS_PATH.exists():
        logger.error("Ultra-Magnus not found at %s", ULTRA_MAGNUS_PATH)
        return False

    if not _WORKER_SCRIPT.exists():
        logger.error("Bridge worker script not found at %s", _WORKER_SCRIPT)
        return False

    # Serialize idea data for subprocess
    idea_json = json.dumps(idea)

    # Use UM's venv Python to get access to its dependencies (pydantic, aiosqlite, etc.)
    um_python = ULTRA_MAGNUS_PATH / ".venv" / "bin" / "python"
    if not um_python.exists():
        um_python = ULTRA_MAGNUS_PATH / "venv" / "bin" / "python"
    if not um_python.exists():
        logger.error("UM Python venv not found at %s", ULTRA_MAGNUS_PATH)
        return False

    command = [
        str(um_python),
        str(_WORKER_SCRIPT),
        "--idea-json", idea_json,
        "--um-path", str(ULTRA_MAGNUS_PATH),
    ]

    if dry_run:
        logger.info("[DRY RUN] Would execute UM bridge: idea=%s title=%s", idea["id"], idea["title"])
        return True

    try:
        log_dir = Path(__file__).parent / "data" / "um_bridge_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"um_bridge_{idea['id']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"

        with open(log_file, "w") as lf:
            proc = subprocess.Popen(
                command,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # Detach from parent
            )

        logger.info(
            "UM bridge launched for idea %s (%s) — PID %d, log: %s, timeout: %ds",
            idea["id"], idea["title"], proc.pid, log_file, BUILD_TIMEOUT,
        )

        # Launch a watchdog thread that kills the subprocess on timeout
        _start_timeout_watchdog(proc, idea["id"], idea["title"], log_file)

        return True

    except Exception as e:
        logger.error("Failed to launch UM bridge for idea %s: %s", idea["id"], e)
        return False


def _start_timeout_watchdog(proc: subprocess.Popen, idea_id, title: str, log_file: Path) -> None:
    """Background thread that kills a build subprocess if it exceeds BUILD_TIMEOUT."""

    def _watchdog():
        try:
            exit_code = proc.wait(timeout=BUILD_TIMEOUT)
            if exit_code != 0:
                logger.warning(
                    "UM bridge for idea %s (%s) exited with code %d",
                    idea_id, title, exit_code,
                )
        except subprocess.TimeoutExpired:
            logger.error(
                "UM bridge TIMEOUT for idea %s (%s) after %ds — killing PID %d",
                idea_id, title, BUILD_TIMEOUT, proc.pid,
            )
            # Kill the entire process group (start_new_session=True created a new group)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            # Append timeout notice to log
            try:
                with open(log_file, "a") as lf:
                    lf.write(f"\n\n=== KILLED BY TIMEOUT ({BUILD_TIMEOUT}s) ===\n")
            except OSError:
                pass

    t = threading.Thread(target=_watchdog, daemon=True, name=f"um-bridge-watchdog-{idea_id}")
    t.start()
