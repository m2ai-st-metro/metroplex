"""
Oz Cloud Build Adapter — wraps oz_bridge.py for cloud-based builds.

Routes builds to Oz/Warp cloud agents instead of local YCE Harness.
"""
import logging
from pathlib import Path

from build_adapter import BuildAdapter, BuildAdapterResult
from config import Config

logger = logging.getLogger(__name__)


class OzAdapter:
    """Build adapter that dispatches to Oz cloud agents."""

    runtime = "oz"

    def __init__(self, config: Config):
        self.config = config

    def queue(
        self,
        spec_path: Path,
        job_id: str,
        model: str,
        parallel: bool = False,
        max_workers: int = 2,
    ) -> BuildAdapterResult:
        """Queue a build via Oz cloud API."""
        try:
            from oz_bridge import submit_to_oz
            run_id = submit_to_oz(
                spec_path=str(spec_path),
                job_id=job_id,
                model=model,
                environment_id=self.config.oz_environment_id,
            )
            if run_id:
                return BuildAdapterResult(
                    job_id=job_id, status="queued", runtime=self.runtime
                )
            return BuildAdapterResult(
                job_id=job_id, status="failed", runtime=self.runtime,
                error="oz_bridge.submit_to_oz returned None",
            )
        except Exception as e:
            return BuildAdapterResult(
                job_id=job_id, status="failed", runtime=self.runtime,
                error=str(e),
            )

    def poll(self) -> dict:
        """Poll Oz cloud for job statuses."""
        try:
            from oz_bridge import poll_oz_run
            # Oz polling is handled differently — returns per-run status
            # The orchestrator handles this via poll_oz_builds()
            return {"jobs": []}
        except Exception:
            return {"jobs": []}

    def kill(self, job_id: str) -> bool:
        """Oz cloud builds cannot be killed from the client side."""
        logger.warning("Cannot kill Oz cloud build %s — not supported", job_id)
        return False

    def is_active(self) -> bool:
        """Oz adapter is always 'active' (cloud-based, no local process)."""
        return True

    def start(self, concurrency: int = 1) -> bool:
        """No-op for cloud adapter — no local runner to start."""
        return True
