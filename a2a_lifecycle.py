"""
A2A Server Lifecycle Manager

Spawns and monitors the YCE A2A wrapper server as a subprocess.
Handles start, stop, health checks, and crash loop protection.
"""
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MAX_RESTARTS_PER_HOUR = 5
HEALTH_TIMEOUT = 5
SHUTDOWN_GRACE = 5


class A2AServerManager:
    """Manages the YCE A2A wrapper server subprocess lifecycle."""

    def __init__(self, yce_dir: str, server_url: str = "http://127.0.0.1:18900"):
        self.yce_dir = Path(yce_dir)
        self.server_url = server_url
        self.yce_python = self.yce_dir / "venv" / "bin" / "python"
        self.server_script = self.yce_dir / "a2a_server.py"
        self.data_dir = Path(__file__).parent / "data"
        self.pid_file = self.data_dir / "a2a_server.pid"
        self.log_file = self.data_dir / "a2a_server.log"
        self._restart_times: list[float] = []
        self._process: subprocess.Popen | None = None

    def start(self) -> bool:
        """Start the A2A server subprocess. Returns True if started."""
        if self.is_healthy():
            logger.info("A2A server already healthy, skipping start")
            return True

        if not self.server_script.exists():
            logger.error("A2A server script not found: %s", self.server_script)
            return False

        if not self.yce_python.exists():
            logger.error("YCE venv python not found: %s", self.yce_python)
            return False

        # Crash loop guard
        now = time.time()
        self._restart_times = [t for t in self._restart_times if now - t < 3600]
        if len(self._restart_times) >= MAX_RESTARTS_PER_HOUR:
            logger.error(
                "Crash loop guard: %d restarts in the last hour, refusing to start",
                len(self._restart_times),
            )
            return False

        self.data_dir.mkdir(parents=True, exist_ok=True)
        log_fh = open(self.log_file, "a")

        try:
            proc = subprocess.Popen(
                [str(self.yce_python), str(self.server_script)],
                cwd=str(self.yce_dir),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._process = proc
            self.pid_file.write_text(str(proc.pid))
            self._restart_times.append(now)
            logger.info("Started A2A server (PID %d)", proc.pid)
            return True
        except Exception as e:
            logger.error("Failed to start A2A server: %s", e)
            return False

    def stop(self) -> bool:
        """Stop the A2A server subprocess gracefully."""
        pid = self._read_pid()
        if pid is None:
            return True

        try:
            os.kill(pid, signal.SIGTERM)
            # Wait for grace period
            for _ in range(SHUTDOWN_GRACE):
                time.sleep(1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                # Still alive after grace period -- force kill
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            logger.info("Stopped A2A server (PID %d)", pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("No permission to kill A2A server (PID %d)", pid)
            return False

        self.pid_file.unlink(missing_ok=True)
        self._process = None
        return True

    def is_healthy(self) -> bool:
        """Check if A2A server process is alive and responding."""
        pid = self._read_pid()
        if pid is None:
            return False

        # Check process alive
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            self.pid_file.unlink(missing_ok=True)
            return False

        # Check HTTP health via agent card
        try:
            with httpx.Client(timeout=HEALTH_TIMEOUT) as client:
                resp = client.get(f"{self.server_url}/.well-known/agent.json")
                return resp.status_code == 200
        except (httpx.HTTPError, Exception):
            return False

    def ensure_running(self) -> bool:
        """Ensure the server is running. Start if not. Returns health status."""
        if self.is_healthy():
            return True
        # Clean up stale PID
        self.pid_file.unlink(missing_ok=True)
        return self.start()

    def _read_pid(self) -> int | None:
        if not self.pid_file.exists():
            return None
        try:
            return int(self.pid_file.read_text().strip())
        except (ValueError, OSError):
            self.pid_file.unlink(missing_ok=True)
            return None
