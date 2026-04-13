"""
Self-Healing Build Adapter — wraps the /self-healing-pipeline Planner/Builder/Judge
loop behind the BuildAdapter Protocol.

Phase C Step 2: dispatch via file queue to a long-running Claude Code daemon
session running the `self-healing-daemon` skill. The daemon reads job files
from `<queue_root>/pending/`, invokes `/self-healing-pipeline` once per job,
and writes terminal state back to `<target_dir>/.self-healing-pipeline/state.json`
where this adapter's `poll()` reads it.

Why a file-queue + persistent interactive session instead of a subprocess per
build or an Agent SDK consumer:

  - `claude -p` headless pays ~87k tokens boot tax per invocation (measured
    2026-04-08) and would bleed ~$4/build on boot alone.
  - Agent SDK `query()` draws from extra usage credits, not Max base
    subscription (effective 2026-04-04). We deliberately stay off that path.
  - An interactive Claude Code session pays boot tax once at startup and
    processes many builds on Max base subscription before needing restart.

The trade-off is that this adapter cannot auto-start the daemon (no TTY from
a subprocess). The operator starts one `claude` session per day inside
`/home/apexaipc/projects/metroplex/` and types `/self-healing-daemon start`.
The adapter detects liveness via heartbeat-*.txt files that the daemon
touches each loop iteration.

State mapping (self-healing → Metroplex):
  - planning, building, judging → "running"
  - passed                      → "completed"
  - escalated                   → "failed"
  - (missing / corrupt state)   → "pending"
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_adapter import BuildAdapter, BuildAdapterResult
from config import Config

logger = logging.getLogger(__name__)

# Heartbeat freshness window. The daemon loop sleeps 60s when idle, so any
# interval >120s would catch one missed heartbeat plus normal jitter.
HEARTBEAT_STALE_SECONDS = 360  # 6 min: Planner phase can take 3-5 min on complex specs


# Self-healing loop status (from .self-healing-pipeline/state.json) → Metroplex build-job status
_STATUS_MAP: dict[str, str] = {
    "planning": "running",
    "building": "running",
    "judging": "running",
    "passed": "completed",
    "review_rejected": "failed",
    "escalated": "failed",
}


class SelfHealingAdapter:
    """Build adapter that dispatches to the /self-healing-pipeline P/B/J loop.

    Step 1 skeleton: queue creates a per-job workspace and tracks it; poll reads
    `.self-healing-pipeline/state.json` from each workspace and maps to Metroplex
    build statuses. The actual P/B/J dispatch is stubbed at `_dispatch` and will
    be wired to a persistent Claude Code daemon in Step 2.
    """

    runtime = "self_healing"

    def __init__(self, config: Config):
        self.config = config
        workspace_root = getattr(
            config,
            "self_healing_workspace_root",
            "",
        ) or str(Path(__file__).parent.parent / "data" / "self_healing_workspaces")
        self.workspace_root = Path(workspace_root)
        queue_root = getattr(
            config,
            "self_healing_queue_root",
            "",
        ) or str(Path(__file__).parent.parent / "data" / "self_healing_queue")
        self.queue_root = Path(queue_root)
        # Internal tracking: job_id -> target workspace directory
        self._jobs: dict[str, Path] = {}

    # -------------------------------------------------------- queue layout
    @property
    def pending_dir(self) -> Path:
        return self.queue_root / "pending"

    @property
    def in_flight_dir(self) -> Path:
        return self.queue_root / "in_flight" / "worker-1"

    @property
    def completed_dir(self) -> Path:
        return self.queue_root / "completed"

    @property
    def failed_dir(self) -> Path:
        return self.queue_root / "failed"

    def _ensure_queue_dirs(self) -> None:
        for d in (
            self.pending_dir,
            self.in_flight_dir,
            self.completed_dir,
            self.failed_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ queue
    def queue(
        self,
        spec_path: Path,
        job_id: str,
        model: str,
        parallel: bool = False,
        max_workers: int = 2,
    ) -> BuildAdapterResult:
        """Create a workspace for this job, copy the spec in, and write a job
        file to the pending queue for the daemon to pick up.

        This does NOT wait for the daemon to consume the job. Metroplex's
        build gate polls via `poll()` on subsequent cycles.
        """
        try:
            target_dir = self._prepare_workspace(spec_path, job_id)
            self._jobs[job_id] = target_dir
            self._dispatch(
                spec_path=target_dir / "spec.md",
                job_id=job_id,
                target_dir=target_dir,
                model=model,
                parallel=parallel,
                max_workers=max_workers,
            )
            return BuildAdapterResult(
                job_id=job_id, status="queued", runtime=self.runtime
            )
        except Exception as e:
            logger.error("SelfHealingAdapter.queue failed for %s: %s", job_id, e)
            return BuildAdapterResult(
                job_id=job_id,
                status="failed",
                runtime=self.runtime,
                error=str(e),
            )

    def _prepare_workspace(self, spec_path: Path, job_id: str) -> Path:
        """Create `<workspace_root>/<job_id>/` and copy the spec into it."""
        target_dir = self.workspace_root / job_id
        target_dir.mkdir(parents=True, exist_ok=True)
        spec_dest = target_dir / "spec.md"
        spec_dest.write_text(spec_path.read_text())
        return target_dir

    def _dispatch(
        self,
        spec_path: Path,
        job_id: str,
        target_dir: Path,
        model: str,
        parallel: bool,
        max_workers: int,
    ) -> None:
        """Write a job file to `<queue_root>/pending/<job_id>.json`.

        The daemon loop in the `self-healing-daemon` skill reads this file,
        moves it to `in_flight/worker-1/`, invokes `/self-healing-pipeline`,
        and routes the finished job to `completed/` or `failed/`.

        Written atomically via tmp + rename so the daemon never reads a
        half-written file.
        """
        self._ensure_queue_dirs()
        payload = {
            "job_id": job_id,
            "target_dir": str(target_dir),
            "spec_path": str(spec_path),
            "model": model,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        job_file = self.pending_dir / f"{job_id}.json"
        tmp_file = job_file.with_suffix(".json.tmp")
        tmp_file.write_text(json.dumps(payload, indent=2))
        tmp_file.rename(job_file)
        logger.info(
            "SelfHealingAdapter: dispatched %s to %s", job_id, job_file
        )

    # ------------------------------------------------------------------- poll
    def poll(self) -> dict[str, Any]:
        """Read each tracked job's state file and return a Metroplex-shaped dict.

        Return shape matches what `gates.build.BuildGate.poll_and_sync_status`
        consumes: `{"jobs": [{"id": ..., "job_id": ..., "status": ..., ...}]}`.
        Both `id` and `job_id` are populated because the existing adapters are
        inconsistent about which key they emit.
        """
        jobs: list[dict[str, Any]] = []
        for job_id, target_dir in list(self._jobs.items()):
            jobs.append(self._job_status(job_id, target_dir))
        return {"jobs": jobs}

    def _job_status(self, job_id: str, target_dir: Path) -> dict[str, Any]:
        """Build a single job_data dict for one tracked job."""
        state_file = target_dir / ".self-healing-pipeline" / "state.json"
        base: dict[str, Any] = {
            "id": job_id,
            "job_id": job_id,
            "status": "pending",
            "project_dir": str(target_dir),
        }
        if not state_file.exists():
            return base
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "SelfHealingAdapter: unreadable state.json for %s: %s", job_id, e
            )
            return base

        base["status"] = self._map_status(state.get("status"))
        base["self_healing_state"] = state.get("status")
        base["attempt"] = state.get("attempt")
        base["judge_verdict"] = state.get("judge_verdict")
        base["escalation_reason"] = state.get("escalation_reason")
        base["review_verdict"] = state.get("review_verdict")
        base["review_critical_count"] = state.get("review_critical_count")
        return base

    @staticmethod
    def _map_status(self_healing_status: str | None) -> str:
        """Translate a self-healing loop status to a Metroplex build-job status.

        Unknown or missing statuses map to "pending" — this is conservative;
        the build gate treats pending as "still running, not yet synced."
        """
        if self_healing_status is None:
            return "pending"
        return _STATUS_MAP.get(self_healing_status, "pending")

    # ------------------------------------------------------------------- kill
    def kill(self, job_id: str) -> bool:
        """Remove the job from the pending queue (if not yet claimed) and
        mark its state.json as escalated.

        If the daemon has already claimed the job (moved it to in_flight/),
        this adapter cannot directly interrupt the running P/B/J loop. It
        writes the escalated state as a best-effort signal; the Judge in the
        current attempt will see its test contract as failing and route to
        an escalate verdict on its own schedule. For immediate termination
        the operator must restart the daemon session.
        """
        if job_id not in self._jobs:
            return False

        # Remove from pending queue if the daemon has not picked it up yet.
        pending_file = self.pending_dir / f"{job_id}.json"
        if pending_file.exists():
            try:
                pending_file.unlink()
                logger.info(
                    "SelfHealingAdapter.kill: removed %s from pending queue",
                    job_id,
                )
            except OSError as e:
                logger.warning(
                    "SelfHealingAdapter.kill: could not remove %s from pending: %s",
                    job_id,
                    e,
                )

        target_dir = self._jobs[job_id]
        state_file = target_dir / ".self-healing-pipeline" / "state.json"
        try:
            if state_file.exists():
                state = json.loads(state_file.read_text())
            else:
                state = {}
            state["status"] = "escalated"
            state["escalation_reason"] = "killed by SelfHealingAdapter"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state))
            return True
        except Exception as e:
            logger.error("SelfHealingAdapter.kill failed for %s: %s", job_id, e)
            return False

    # -------------------------------------------------------------- lifecycle
    def is_active(self) -> bool:
        """True if at least one daemon heartbeat file was touched within the
        last HEARTBEAT_STALE_SECONDS (default 120s).

        The daemon loop touches `heartbeat-worker-1.txt` at the top of every
        iteration and again after each build completes. A stale heartbeat
        means the daemon crashed, was killed, or the operator hasn't started
        it yet today.
        """
        if not self.queue_root.exists():
            return False
        now = time.time()
        for heartbeat in self.queue_root.glob("heartbeat-*.txt"):
            try:
                age = now - heartbeat.stat().st_mtime
            except OSError:
                continue
            if age <= HEARTBEAT_STALE_SECONDS:
                return True
        return False

    def start(self, concurrency: int = 1) -> bool:
        """Cannot auto-start an interactive Claude Code session from a
        subprocess — there is no TTY available. Instead, verify that a
        daemon is already running via heartbeat, and if not, log a clear
        message telling the operator how to start one manually.

        Returns True if a daemon is live, False otherwise.
        """
        if self.is_active():
            logger.info("SelfHealingAdapter: daemon already active")
            return True
        logger.warning(
            "SelfHealingAdapter: no fresh heartbeat at %s. The self-healing "
            "daemon cannot be auto-started from a subprocess (requires a TTY "
            "for the interactive Claude Code session). Open a terminal in "
            "%s and run `claude`, then type `/self-healing-daemon start`.",
            self.queue_root,
            Path(__file__).parent.parent,
        )
        return False
