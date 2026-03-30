"""
Local Build Adapter — wraps YCE Harness queue_runner.py subprocess calls.

This is the default adapter, extracted from BuildOrchestrator's inline
subprocess logic for the Paperclip adapter pattern integration.
"""
import json
import logging
import os
import subprocess
from pathlib import Path

from build_adapter import BuildAdapter, BuildAdapterResult
from config import Config

logger = logging.getLogger(__name__)

RUNNER_PID_FILE = Path(__file__).parent.parent / "data" / "runner.pid"


class LocalAdapter:
    """Build adapter that dispatches to local YCE Harness via subprocess."""

    runtime = "local"

    def __init__(self, config: Config):
        self.config = config
        self.queue_runner_path = Path(config.yce_dir) / "queue_runner.py"
        self.yce_python = Path(config.yce_dir) / "venv" / "bin" / "python"

    def queue(
        self,
        spec_path: Path,
        job_id: str,
        model: str,
        parallel: bool = False,
        max_workers: int = 2,
    ) -> BuildAdapterResult:
        """Queue a build via queue_runner.py add."""
        command = [
            str(self.yce_python),
            str(self.queue_runner_path),
            "add",
            str(spec_path.resolve()),
            "--id",
            job_id,
            "--model",
            model,
        ]
        if parallel:
            command.append("--parallel")
            command.extend(["--max-workers", str(max_workers)])

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return BuildAdapterResult(
                    job_id=job_id, status="queued", runtime=self.runtime
                )
            else:
                error = result.stderr.strip() or result.stdout.strip() or "Unknown error"
                return BuildAdapterResult(
                    job_id=job_id, status="failed", runtime=self.runtime, error=error
                )
        except subprocess.TimeoutExpired:
            return BuildAdapterResult(
                job_id=job_id, status="failed", runtime=self.runtime,
                error="queue command timed out after 30s",
            )
        except Exception as e:
            return BuildAdapterResult(
                job_id=job_id, status="failed", runtime=self.runtime,
                error=str(e),
            )

    def poll(self) -> dict:
        """Poll queue_runner.py status --json."""
        command = [
            str(self.yce_python),
            str(self.queue_runner_path),
            "status",
            "--json",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            return {}
        except Exception:
            return {}

    def kill(self, job_id: str) -> bool:
        """Kill the runner process (kills all jobs, not individual ones)."""
        if not RUNNER_PID_FILE.exists():
            return False
        try:
            import signal
            pid = int(RUNNER_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            RUNNER_PID_FILE.unlink(missing_ok=True)
            return True
        except (ValueError, ProcessLookupError, PermissionError):
            RUNNER_PID_FILE.unlink(missing_ok=True)
            return False

    def is_active(self) -> bool:
        """Check if queue_runner process is still running."""
        if not RUNNER_PID_FILE.exists():
            return False
        try:
            pid = int(RUNNER_PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return True
        except (ValueError, ProcessLookupError, PermissionError):
            RUNNER_PID_FILE.unlink(missing_ok=True)
            return False

    def start(self, concurrency: int = 1) -> bool:
        """Start queue_runner.py as a background process."""
        if self.is_active():
            logger.info("Queue runner already active, skipping start")
            return True

        command = [
            str(self.yce_python),
            str(self.queue_runner_path),
            "start",
            "--concurrency",
            str(concurrency),
        ]

        try:
            log_path = Path(__file__).parent.parent / "data" / "runner.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "a")

            proc = subprocess.Popen(
                command,
                cwd=str(Path(self.config.yce_dir)),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            RUNNER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            RUNNER_PID_FILE.write_text(str(proc.pid))
            logger.info("Started queue runner (PID %d, concurrency %d)", proc.pid, concurrency)
            return True
        except Exception as e:
            logger.error("Failed to start queue runner: %s", e)
            return False
