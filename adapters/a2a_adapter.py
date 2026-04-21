"""
A2A Build Adapter -- dispatches builds to YCE via A2A protocol (JSON-RPC).

Uses httpx directly against A2A JSON-RPC endpoints. The A2AClient class
in a2a.client is deprecated in v0.3.25; raw httpx is more stable.
"""
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

import httpx

from build_adapter import BuildAdapter, BuildAdapterResult
from config import Config

logger = logging.getLogger(__name__)

# Map A2A TaskState values to Metroplex build statuses
_STATE_MAP = {
    "submitted": "started",
    "working": "started",
    "input-required": "started",
    "completed": "completed",
    "failed": "failed",
    "canceled": "failed",
    "rejected": "failed",
    "auth-required": "failed",
    "unknown": "started",
}

MAX_CONSECUTIVE_FAILURES = 3
MAX_TASK_MAP_SIZE = 100


class A2AAdapter:
    """Build adapter that dispatches to YCE via A2A JSON-RPC protocol."""

    runtime = "a2a"

    def __init__(self, config: Config, event_emitter=None):
        self.config = config
        self.server_url = getattr(config, "a2a_server_url", "http://127.0.0.1:18900")
        self.event_emitter = event_emitter
        self._task_map: dict[str, str] = {}  # a2a_task_id -> job_id
        self._job_to_task: dict[str, str] = {}  # job_id -> a2a_task_id
        self._consecutive_failures = 0
        self._last_states: dict[str, str] = {}  # task_id -> last known state

    def queue(
        self,
        spec_path: Path,
        job_id: str,
        model: str,
        parallel: bool = False,
        max_workers: int = 2,
    ) -> BuildAdapterResult:
        """Queue a build via A2A message/send."""
        spec_content = spec_path.read_text()
        params_json = json.dumps({
            "spec_content": spec_content,
            "job_id": job_id,
            "model": model,
            "parallel": parallel,
            "max_workers": max_workers,
        })

        message_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": params_json}],
                    "message_id": message_id,
                },
            },
        }

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(self.server_url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            if "error" in data:
                error_msg = data["error"].get("message", "A2A RPC error")
                error_code = data["error"].get("code", 0)
                self._record_failure()
                logger.error(
                    "A2A server error for %s: code=%s msg=%s",
                    job_id, error_code, error_msg,
                )
                if self.event_emitter:
                    self.event_emitter.emit("a2a_dispatch_failed", {
                        "job_id": job_id, "error": error_msg,
                        "error_code": error_code,
                    }, correlation_id=job_id)
                return BuildAdapterResult(
                    job_id=job_id, status="failed", runtime=self.runtime,
                    error=f"A2A server error (code={error_code}): {error_msg}",
                )

            # Extract task_id from response
            result = data.get("result", {})
            task_id = result.get("id") if isinstance(result, dict) else None
            if task_id:
                self._track_task(task_id, job_id)

            self._consecutive_failures = 0
            return BuildAdapterResult(job_id=job_id, status="queued", runtime=self.runtime)

        except httpx.ReadTimeout:
            # The A2A server accepted the request (TCP connected, headers
            # sent) but is still processing. The server's execute() runs
            # queue_runner add + start, which takes longer than the HTTP
            # timeout. Treat as "queued" -- the poll loop will pick up
            # the actual result later.
            logger.info(
                "A2A dispatch for %s timed out waiting for response -- "
                "treating as queued (server is processing)",
                job_id,
            )
            self._consecutive_failures = 0
            return BuildAdapterResult(job_id=job_id, status="queued", runtime=self.runtime)

        except (httpx.HTTPError, json.JSONDecodeError, Exception) as e:
            self._record_failure()
            error_msg = str(e)
            if self.event_emitter:
                self.event_emitter.emit("a2a_dispatch_failed", {
                    "job_id": job_id, "error": error_msg,
                }, correlation_id=job_id)
            return BuildAdapterResult(
                job_id=job_id, status="failed", runtime=self.runtime, error=error_msg,
            )

    def poll(self) -> dict:
        """Poll for build status updates.

        Two-tier polling:
        1. A2A task_map: query the A2A server for tracked tasks (works when
           the adapter dispatched and received a task_id in the same process).
        2. queue.json fallback: read YCE's queue file directly for any
           metroplex-* jobs. This handles ReadTimeout dispatches (no task_id)
           and adapter restarts (task_map lost).
        """
        jobs = []
        seen_job_ids = set()

        # Tier 1: A2A task_map polling (existing behavior)
        for task_id, job_id in list(self._task_map.items()):
            payload = {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "tasks/get",
                "params": {"id": task_id},
            }
            try:
                with httpx.Client(timeout=15) as client:
                    resp = client.post(self.server_url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()

                if "error" in data:
                    continue

                result = data.get("result", {})
                a2a_state = result.get("status", {}).get("state", "unknown")
                metroplex_status = _STATE_MAP.get(a2a_state, "started")

                # Emit state change events
                old_state = self._last_states.get(task_id)
                if old_state != a2a_state and self.event_emitter:
                    self.event_emitter.emit("a2a_state_change", {
                        "job_id": job_id,
                        "task_id": task_id,
                        "old_state": old_state,
                        "new_state": a2a_state,
                    }, correlation_id=job_id)
                self._last_states[task_id] = a2a_state

                job_info = {"job_id": job_id, "status": metroplex_status, "a2a_task_id": task_id}

                # Extract project_dir from artifacts if completed
                if metroplex_status == "completed":
                    artifacts = result.get("artifacts", [])
                    for artifact in (artifacts or []):
                        for part in artifact.get("parts", []):
                            if part.get("kind") == "text":
                                try:
                                    art_data = json.loads(part["text"])
                                    if "project_dir" in art_data:
                                        job_info["project_dir"] = art_data["project_dir"]
                                except (json.JSONDecodeError, KeyError):
                                    pass

                jobs.append(job_info)
                seen_job_ids.add(job_id)

                # Clean up terminal tasks
                if metroplex_status in ("completed", "failed"):
                    del self._task_map[task_id]
                    self._job_to_task.pop(job_id, None)
                    self._last_states.pop(task_id, None)

                self._consecutive_failures = 0

            except (httpx.HTTPError, Exception) as e:
                logger.warning("Failed to poll task %s: %s", task_id, e)

        # Tier 2: queue.json fallback for jobs not tracked via A2A task_map.
        # Covers ReadTimeout dispatches and adapter restarts.
        queue_file = Path(self.config.yce_dir) / "data" / "queue.json"
        try:
            if queue_file.exists():
                queue_data = json.loads(queue_file.read_text())
                for job_data in queue_data.get("jobs", []):
                    job_id = job_data.get("id", "")
                    if not job_id.startswith("metroplex-"):
                        continue
                    if job_id in seen_job_ids:
                        continue
                    yce_status = job_data.get("status", "")
                    if yce_status in ("completed", "failed"):
                        job_info = {
                            "job_id": job_id,
                            "status": yce_status,
                            "project_dir": job_data.get("project_dir", ""),
                        }
                        jobs.append(job_info)
                    elif yce_status in ("running", "pending"):
                        jobs.append({"job_id": job_id, "status": "running"})
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read queue.json fallback: %s", e)

        return {"jobs": jobs}

    def kill(self, job_id: str) -> bool:
        """Cancel a task via A2A tasks/cancel."""
        task_id = self._job_to_task.get(job_id)
        if not task_id:
            return False

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/cancel",
            "params": {"id": task_id},
        }
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(self.server_url, json=payload)
                resp.raise_for_status()
            self._task_map.pop(task_id, None)
            self._job_to_task.pop(job_id, None)
            self._last_states.pop(task_id, None)
            return True
        except (httpx.HTTPError, Exception) as e:
            logger.warning("Failed to cancel task %s: %s", task_id, e)
            return False

    def is_active(self) -> bool:
        """Check if A2A server is reachable via agent card endpoint."""
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            if self.event_emitter:
                self.event_emitter.emit("a2a_fallback_triggered", {
                    "consecutive_failures": self._consecutive_failures,
                })
            return False
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self.server_url}/.well-known/agent.json")
                return resp.status_code == 200
        except (httpx.HTTPError, Exception):
            return False

    def start(self, concurrency: int = 1) -> bool:
        """No-op -- lifecycle managed by A2AServerManager."""
        return True

    def _track_task(self, task_id: str, job_id: str):
        """Track an A2A task_id <-> job_id mapping, with cap."""
        if len(self._task_map) >= MAX_TASK_MAP_SIZE:
            # Evict oldest entry
            oldest = next(iter(self._task_map))
            old_job = self._task_map.pop(oldest)
            self._job_to_task.pop(old_job, None)
            self._last_states.pop(oldest, None)
        self._task_map[task_id] = job_id
        self._job_to_task[job_id] = task_id

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error(
                "A2A adapter circuit breaker tripped after %d failures",
                self._consecutive_failures,
            )
