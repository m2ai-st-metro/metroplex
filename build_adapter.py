"""
Build Adapter Protocol — runtime-agnostic interface for build dispatch.

Inspired by Paperclip's AdapterExecutionContext/AdapterExecutionResult pattern.
Each adapter wraps a specific agent runtime (local YCE, Oz cloud, etc.) behind
a common interface so the build gate doesn't need runtime-specific logic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class BuildAdapterResult(BaseModel):
    """Result of queuing a build via an adapter."""
    job_id: str
    status: str  # "queued" or "failed"
    runtime: str
    error: str | None = None


@runtime_checkable
class BuildAdapter(Protocol):
    """Protocol for build dispatch adapters.

    Implementations must provide:
    - queue(): Submit a build job
    - poll(): Check status of all jobs
    - kill(): Terminate a specific job
    - is_active(): Check if the adapter's runner is alive
    """

    def queue(
        self,
        spec_path: Path,
        job_id: str,
        model: str,
        parallel: bool = False,
        max_workers: int = 2,
    ) -> BuildAdapterResult:
        """Queue a build job. Returns result with status 'queued' or 'failed'."""
        ...

    def poll(self) -> dict:
        """Poll for job statuses. Returns dict with 'jobs' list."""
        ...

    def kill(self, job_id: str) -> bool:
        """Kill a specific running job. Returns True if killed."""
        ...

    def is_active(self) -> bool:
        """Check if the adapter's runner process is active."""
        ...

    def start(self, concurrency: int = 1) -> bool:
        """Start the background runner. Returns True if started."""
        ...
