"""
Self-Healing Build Adapter — wraps the /self-healing-pipeline Planner/Builder/Judge
loop behind the BuildAdapter Protocol.

Phase C Step 1: SKELETON ONLY.

This adapter provides:
  - Protocol conformance (queue, poll, kill, is_active, start)
  - Per-job workspace directories under `self_healing_workspace_root`
  - State tracking via `.self-healing-pipeline/state.json` polling
  - Status mapping from self-healing loop states to Metroplex build statuses

What is NOT wired in Step 1:
  - The actual dispatch of the P/B/J loop (`_dispatch` raises NotImplementedError).
    This is the single swap point for Step 2, where a long-running Claude Code
    session daemon will receive dispatch requests via IPC and invoke the
    /self-healing-pipeline skill. Keeping the loop in a persistent interactive
    session avoids the ~87k-token headless boot tax measured 2026-04-08 and
    keeps billing on Max OAuth rather than reopening the disabled API key.

State mapping (self-healing → Metroplex):
  - planning, building, judging → "running"
  - passed                      → "completed"
  - escalated                   → "failed"
  - (missing / corrupt state)   → "pending"
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from build_adapter import BuildAdapter, BuildAdapterResult
from config import Config

logger = logging.getLogger(__name__)


# Self-healing loop status (from .self-healing-pipeline/state.json) → Metroplex build-job status
_STATUS_MAP: dict[str, str] = {
    "planning": "running",
    "building": "running",
    "judging": "running",
    "passed": "completed",
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
            str(Path(__file__).parent.parent / "data" / "self_healing_workspaces"),
        )
        self.workspace_root = Path(workspace_root)
        # Internal tracking: job_id -> target workspace directory
        self._jobs: dict[str, Path] = {}

    # ------------------------------------------------------------------ queue
    def queue(
        self,
        spec_path: Path,
        job_id: str,
        model: str,
        parallel: bool = False,
        max_workers: int = 2,
    ) -> BuildAdapterResult:
        """Create a workspace for this job, copy the spec in, and dispatch.

        In Step 1 `_dispatch` is a stub — calling `queue()` will raise until
        Step 2 wires it to the daemon. The skeleton still creates the workspace
        and spec copy so poll/kill/status-mapping logic can be exercised by
        tests with hand-written state files.
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
        except NotImplementedError:
            # Expected in Step 1 skeleton. Re-raise so callers see the clear
            # "not wired yet" signal instead of a silent queued status.
            raise
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
        """SWAP POINT for Step 2.

        Step 2 will replace this stub with an IPC call to a long-running
        Claude Code session daemon that invokes `/self-healing-pipeline` with
        the prepared spec and target_dir. The daemon will write the
        `.self-healing-pipeline/state.json` that `poll()` reads.
        """
        raise NotImplementedError(
            "SelfHealingAdapter dispatch is not yet wired. Step 2 will connect "
            "this to a persistent Claude Code daemon (Path D from the Phase C "
            "billing review) to avoid headless boot-tax overhead."
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
        """Mark a tracked job as escalated so the next poll reports failed.

        Step 1: writes `status: "escalated"` into the job's state.json as a
        best-effort signal. Step 2 will additionally signal the daemon to stop
        any in-flight attempt.
        """
        if job_id not in self._jobs:
            return False
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
        """Step 1: skeleton is always "active" (no background runner yet).

        Step 2 will check whether the Claude Code daemon session is alive.
        """
        return True

    def start(self, concurrency: int = 1) -> bool:
        """Step 1: no-op, returns True.

        Step 2 will boot the persistent Claude Code daemon session here.
        """
        logger.info(
            "SelfHealingAdapter.start called (skeleton — daemon not yet wired)"
        )
        return True
