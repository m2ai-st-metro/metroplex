"""Tests for the A2A build adapter and lifecycle manager."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adapters.a2a_adapter import A2AAdapter, MAX_CONSECUTIVE_FAILURES
from a2a_lifecycle import A2AServerManager
from config import Config


@pytest.fixture
def config():
    c = Config()
    c.a2a_server_url = "http://127.0.0.1:18900"
    return c


@pytest.fixture
def event_emitter():
    return MagicMock()


@pytest.fixture
def adapter(config, event_emitter):
    return A2AAdapter(config, event_emitter=event_emitter)


@pytest.fixture
def tmp_spec(tmp_path):
    spec = tmp_path / "test_spec.txt"
    spec.write_text("Build a calculator app")
    return spec


class TestA2AAdapterQueue:
    def test_queue_success(self, adapter, tmp_spec):
        """Successful queue returns status='queued'."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {
                "id": "task-abc",
                "kind": "task",
                "status": {"state": "submitted"},
                "context_id": "ctx-1",
            },
        }

        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.queue(tmp_spec, "job-1", "haiku")

        assert result.status == "queued"
        assert result.job_id == "job-1"
        assert result.runtime == "a2a"
        assert adapter._consecutive_failures == 0
        assert "task-abc" in adapter._task_map

    def test_queue_rpc_error(self, adapter, tmp_spec, event_emitter):
        """RPC error returns status='failed' and emits event."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "error": {"code": -32000, "message": "Server busy"},
        }

        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.queue(tmp_spec, "job-2", "haiku")

        assert result.status == "failed"
        assert "Server busy" in result.error
        event_emitter.emit.assert_called_once()
        assert event_emitter.emit.call_args[0][0] == "a2a_dispatch_failed"

    def test_queue_network_error(self, adapter, tmp_spec, event_emitter):
        """Network error returns failed and increments circuit breaker."""
        import httpx
        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_client_cls.return_value = mock_client

            result = adapter.queue(tmp_spec, "job-3", "haiku")

        assert result.status == "failed"
        assert adapter._consecutive_failures == 1

    def test_queue_read_timeout_treated_as_queued(self, adapter, tmp_spec):
        """ReadTimeout means server accepted but is still processing -- treat as queued."""
        import httpx
        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ReadTimeout("timed out")
            mock_client_cls.return_value = mock_client

            result = adapter.queue(tmp_spec, "job-timeout", "haiku")

        assert result.status == "queued"
        assert adapter._consecutive_failures == 0  # Not a failure


class TestA2AAdapterPoll:
    def test_poll_returns_jobs(self, adapter):
        """Poll returns mapped job statuses."""
        adapter._task_map = {"task-1": "job-1"}
        adapter._job_to_task = {"job-1": "task-1"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {
                "id": "task-1",
                "kind": "task",
                "status": {"state": "working"},
                "context_id": "ctx-1",
            },
        }

        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls, \
             patch("adapters.a2a_adapter.Path.exists", return_value=False):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.poll()

        assert len(result["jobs"]) == 1
        assert result["jobs"][0]["status"] == "started"  # working -> started
        assert result["jobs"][0]["job_id"] == "job-1"

    def test_poll_completed_cleans_up(self, adapter, event_emitter):
        """Completed tasks are removed from tracking."""
        adapter._task_map = {"task-2": "job-2"}
        adapter._job_to_task = {"job-2": "task-2"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {
                "id": "task-2",
                "kind": "task",
                "status": {"state": "completed"},
                "context_id": "ctx-1",
                "artifacts": [{
                    "artifact_id": "art-1",
                    "parts": [{"kind": "text", "text": json.dumps({"project_dir": "/tmp/build"})}],
                }],
            },
        }

        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.poll()

        assert result["jobs"][0]["status"] == "completed"
        assert result["jobs"][0]["project_dir"] == "/tmp/build"
        assert "task-2" not in adapter._task_map

    def test_poll_emits_state_change(self, adapter, event_emitter):
        """State transitions emit a2a_state_change events."""
        adapter._task_map = {"task-3": "job-3"}
        adapter._job_to_task = {"job-3": "task-3"}
        adapter._last_states = {"task-3": "submitted"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {
                "id": "task-3",
                "kind": "task",
                "status": {"state": "working"},
                "context_id": "ctx-1",
            },
        }

        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            adapter.poll()

        event_emitter.emit.assert_called_once_with("a2a_state_change", {
            "job_id": "job-3",
            "task_id": "task-3",
            "old_state": "submitted",
            "new_state": "working",
        }, correlation_id="job-3")


class TestA2AAdapterCircuitBreaker:
    def test_circuit_breaker_trips(self, adapter, event_emitter):
        """After MAX_CONSECUTIVE_FAILURES, is_active returns False."""
        adapter._consecutive_failures = MAX_CONSECUTIVE_FAILURES

        result = adapter.is_active()

        assert result is False
        event_emitter.emit.assert_called_once_with("a2a_fallback_triggered", {
            "consecutive_failures": MAX_CONSECUTIVE_FAILURES,
        })

    def test_circuit_breaker_resets_on_success(self, adapter, tmp_spec):
        """Successful queue resets the failure counter."""
        adapter._consecutive_failures = 2

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"id": "task-99", "status": {"state": "submitted"}, "context_id": "c", "kind": "task"},
        }

        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            adapter.queue(tmp_spec, "job-reset", "haiku")

        assert adapter._consecutive_failures == 0


class TestA2AAdapterKill:
    def test_kill_known_task(self, adapter):
        """Kill sends cancel for a known task."""
        adapter._task_map = {"task-kill": "job-kill"}
        adapter._job_to_task = {"job-kill": "task-kill"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("adapters.a2a_adapter.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.kill("job-kill")

        assert result is True
        assert "task-kill" not in adapter._task_map

    def test_kill_unknown_task(self, adapter):
        """Kill returns False for unknown job_id."""
        assert adapter.kill("nonexistent") is False


class TestA2AAdapterTaskMap:
    def test_task_map_cap(self, adapter, tmp_spec):
        """Task map evicts oldest when full."""
        # Fill to max
        for i in range(100):
            adapter._task_map[f"task-{i}"] = f"job-{i}"
            adapter._job_to_task[f"job-{i}"] = f"task-{i}"

        adapter._track_task("task-new", "job-new")
        assert len(adapter._task_map) == 100
        assert "task-0" not in adapter._task_map  # Oldest evicted
        assert "task-new" in adapter._task_map


class TestA2AServerManager:
    def test_is_healthy_no_pid(self, config, tmp_path):
        mgr = A2AServerManager(yce_dir=str(tmp_path))
        mgr.pid_file = tmp_path / "no_pid.pid"
        assert mgr.is_healthy() is False

    def test_stop_no_pid(self, config, tmp_path):
        mgr = A2AServerManager(yce_dir=str(tmp_path))
        mgr.pid_file = tmp_path / "no_pid.pid"
        assert mgr.stop() is True

    def test_ensure_running_starts_when_unhealthy(self, config, tmp_path):
        mgr = A2AServerManager(yce_dir=str(tmp_path))
        mgr.pid_file = tmp_path / "no_pid.pid"
        mgr.is_healthy = MagicMock(return_value=False)
        mgr.start = MagicMock(return_value=True)
        assert mgr.ensure_running() is True
        mgr.start.assert_called_once()

    def test_ensure_running_skips_when_healthy(self, config, tmp_path):
        mgr = A2AServerManager(yce_dir=str(tmp_path))
        mgr.is_healthy = MagicMock(return_value=True)
        mgr.start = MagicMock()
        assert mgr.ensure_running() is True
        mgr.start.assert_not_called()

    def test_crash_loop_guard(self, config, tmp_path):
        """After MAX_RESTARTS_PER_HOUR, start() refuses."""
        import time
        mgr = A2AServerManager(yce_dir=str(tmp_path))
        mgr._restart_times = [time.time()] * 5  # Already at limit
        mgr.pid_file = tmp_path / "no_pid.pid"
        # Create dummy files so the existence checks pass
        (tmp_path / "a2a_server.py").write_text("")
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("")
        mgr.server_script = tmp_path / "a2a_server.py"
        mgr.yce_python = venv_bin / "python"

        result = mgr.start()
        assert result is False
