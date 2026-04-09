"""Tests for the Self-Healing build adapter (Phase C Steps 1+2).

Step 1 pinned the Protocol skeleton, status mapping, and state-file replay
against real Phase A dry runs. Step 2 wires dispatch to a file queue consumed
by a long-running Claude Code daemon session, and heartbeat-based liveness.
"""
import json
import os
import time
from pathlib import Path

import pytest

from adapters.self_healing_adapter import SelfHealingAdapter, HEARTBEAT_STALE_SECONDS
from build_adapter import BuildAdapter
from config import Config


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.self_healing_workspace_root = str(tmp_path / "workspaces")
    c.self_healing_queue_root = str(tmp_path / "queue")
    return c


@pytest.fixture
def adapter(config):
    return SelfHealingAdapter(config)


@pytest.fixture
def tmp_spec(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("Build a calculator library. Pure stdlib. 100% pytest.")
    return spec


def _write_state(target_dir: Path, state: dict) -> None:
    state_dir = target_dir / ".self-healing-pipeline"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(state))


# ---------------------------------------------------------------- Protocol
class TestProtocolConformance:
    def test_adapter_satisfies_build_adapter_protocol(self, adapter):
        assert isinstance(adapter, BuildAdapter)

    def test_runtime_is_self_healing(self, adapter):
        assert adapter.runtime == "self_healing"


# ------------------------------------------------------------------- queue
class TestQueue:
    def test_queue_returns_queued_result(self, adapter, tmp_spec):
        """Successful queue returns status='queued' without raising."""
        result = adapter.queue(tmp_spec, "job-1", "opus")
        assert result.status == "queued"
        assert result.job_id == "job-1"
        assert result.runtime == "self_healing"
        assert result.error is None

    def test_queue_prepares_workspace(self, adapter, tmp_spec):
        """Workspace directory is created and spec is copied in."""
        adapter.queue(tmp_spec, "job-prep", "opus")
        target_dir = adapter.workspace_root / "job-prep"
        assert target_dir.is_dir()
        assert (target_dir / "spec.md").read_text() == tmp_spec.read_text()

    def test_queue_tracks_job_internally(self, adapter, tmp_spec):
        """Queued job is tracked so poll() can find its state.json."""
        adapter.queue(tmp_spec, "job-x", "opus")
        assert "job-x" in adapter._jobs
        assert adapter._jobs["job-x"] == adapter.workspace_root / "job-x"

    def test_queue_writes_pending_job_file(self, adapter, tmp_spec):
        """A job file lands in pending/ with the expected schema."""
        adapter.queue(tmp_spec, "job-disp", "opus")
        job_file = adapter.pending_dir / "job-disp.json"
        assert job_file.exists()
        payload = json.loads(job_file.read_text())
        assert payload["job_id"] == "job-disp"
        assert payload["model"] == "opus"
        assert payload["target_dir"] == str(adapter.workspace_root / "job-disp")
        assert payload["spec_path"] == str(
            adapter.workspace_root / "job-disp" / "spec.md"
        )
        assert "queued_at" in payload

    def test_queue_creates_all_queue_subdirs(self, adapter, tmp_spec):
        """pending/, in_flight/worker-1/, completed/, failed/ all exist."""
        adapter.queue(tmp_spec, "job-dirs", "opus")
        assert adapter.pending_dir.is_dir()
        assert adapter.in_flight_dir.is_dir()
        assert adapter.completed_dir.is_dir()
        assert adapter.failed_dir.is_dir()

    def test_queue_writes_job_file_atomically(self, adapter, tmp_spec):
        """No .tmp residue after a successful queue — rename, not write-in-place."""
        adapter.queue(tmp_spec, "job-atomic", "opus")
        tmps = list(adapter.pending_dir.glob("*.tmp"))
        assert tmps == []
        assert (adapter.pending_dir / "job-atomic.json").exists()


# -------------------------------------------------------------------- poll
class TestPollStatusMapping:
    @pytest.mark.parametrize(
        "self_healing_status,expected_metroplex_status",
        [
            ("planning", "running"),
            ("building", "running"),
            ("judging", "running"),
            ("passed", "completed"),
            ("escalated", "failed"),
            ("unknown_future_state", "pending"),
            (None, "pending"),
        ],
    )
    def test_status_map(self, self_healing_status, expected_metroplex_status):
        assert (
            SelfHealingAdapter._map_status(self_healing_status)
            == expected_metroplex_status
        )

    def test_poll_empty_when_no_jobs(self, adapter):
        assert adapter.poll() == {"jobs": []}

    def test_poll_pending_when_state_file_missing(self, adapter, tmp_path):
        target = tmp_path / "job-a"
        target.mkdir()
        adapter._jobs["job-a"] = target
        result = adapter.poll()
        assert len(result["jobs"]) == 1
        job = result["jobs"][0]
        assert job["id"] == "job-a"
        assert job["job_id"] == "job-a"
        assert job["status"] == "pending"

    def test_poll_pending_when_state_file_corrupt(self, adapter, tmp_path):
        target = tmp_path / "job-corrupt"
        target.mkdir()
        state_dir = target / ".self-healing-pipeline"
        state_dir.mkdir()
        (state_dir / "state.json").write_text("{not valid json")
        adapter._jobs["job-corrupt"] = target
        result = adapter.poll()
        assert result["jobs"][0]["status"] == "pending"

    def test_poll_running_during_planning(self, adapter, tmp_path):
        target = tmp_path / "job-plan"
        target.mkdir()
        _write_state(target, {"status": "planning", "attempt": 1})
        adapter._jobs["job-plan"] = target
        job = adapter.poll()["jobs"][0]
        assert job["status"] == "running"
        assert job["self_healing_state"] == "planning"
        assert job["attempt"] == 1

    def test_poll_running_during_building(self, adapter, tmp_path):
        target = tmp_path / "job-build"
        target.mkdir()
        _write_state(target, {"status": "building", "attempt": 2})
        adapter._jobs["job-build"] = target
        assert adapter.poll()["jobs"][0]["status"] == "running"

    def test_poll_running_during_judging(self, adapter, tmp_path):
        target = tmp_path / "job-judge"
        target.mkdir()
        _write_state(target, {"status": "judging", "attempt": 2})
        adapter._jobs["job-judge"] = target
        assert adapter.poll()["jobs"][0]["status"] == "running"

    def test_poll_completed_on_passed(self, adapter, tmp_path):
        target = tmp_path / "job-pass"
        target.mkdir()
        _write_state(
            target,
            {
                "status": "passed",
                "attempt": 1,
                "judge_verdict": "pass",
            },
        )
        adapter._jobs["job-pass"] = target
        job = adapter.poll()["jobs"][0]
        assert job["status"] == "completed"
        assert job["judge_verdict"] == "pass"
        assert job["project_dir"] == str(target)

    def test_poll_failed_on_escalated(self, adapter, tmp_path):
        target = tmp_path / "job-esc"
        target.mkdir()
        _write_state(
            target,
            {
                "status": "escalated",
                "attempt": 3,
                "judge_verdict": "escalate",
                "escalation_reason": "ambiguous spec",
            },
        )
        adapter._jobs["job-esc"] = target
        job = adapter.poll()["jobs"][0]
        assert job["status"] == "failed"
        assert job["escalation_reason"] == "ambiguous spec"


class TestPollMultipleJobs:
    def test_poll_returns_all_tracked_jobs(self, adapter, tmp_path):
        for jid, status in [
            ("j-plan", "planning"),
            ("j-pass", "passed"),
            ("j-esc", "escalated"),
        ]:
            target = tmp_path / jid
            target.mkdir()
            _write_state(target, {"status": status, "attempt": 1})
            adapter._jobs[jid] = target

        result = adapter.poll()
        by_id = {j["id"]: j for j in result["jobs"]}
        assert by_id["j-plan"]["status"] == "running"
        assert by_id["j-pass"]["status"] == "completed"
        assert by_id["j-esc"]["status"] == "failed"


# ---------------------------------------------- Phase A dry-run state replay
class TestPhaseAStateFileReplay:
    """Replay real sandbox state.json files from the N=6 Phase A dry runs.

    These fixtures are the actual state files written by the validated P/B/J
    loop on real Metroplex failing specs. If the adapter's mapping breaks,
    these tests fail before any live build is attempted.
    """

    SANDBOX_RUNS = [
        ("self-healing-test-199", "passed", "completed"),
        ("self-healing-test-208", "passed", "completed"),
        ("self-healing-test-73", "passed", "completed"),
        ("self-healing-test-73-retry", "passed", "completed"),
        ("self-healing-test-38", "passed", "completed"),
        ("self-healing-test-tsmcp", "passed", "completed"),
    ]

    @pytest.mark.parametrize("sandbox_dir,loop_status,expected", SANDBOX_RUNS)
    def test_replay_sandbox_state(
        self, adapter, tmp_path, sandbox_dir, loop_status, expected
    ):
        src = Path("/home/apexaipc/sandbox") / sandbox_dir / ".self-healing-pipeline" / "state.json"
        if not src.exists():
            pytest.skip(f"Sandbox fixture missing: {src}")

        target = tmp_path / sandbox_dir
        (target / ".self-healing-pipeline").mkdir(parents=True)
        (target / ".self-healing-pipeline" / "state.json").write_text(
            src.read_text()
        )
        adapter._jobs[sandbox_dir] = target

        job = adapter.poll()["jobs"][0]
        assert job["self_healing_state"] == loop_status
        assert job["status"] == expected


# -------------------------------------------------------------------- kill
class TestKill:
    def test_kill_unknown_job_returns_false(self, adapter):
        assert adapter.kill("nope") is False

    def test_kill_writes_escalated_state(self, adapter, tmp_path):
        target = tmp_path / "job-k"
        target.mkdir()
        _write_state(target, {"status": "building", "attempt": 2})
        adapter._jobs["job-k"] = target

        assert adapter.kill("job-k") is True

        state = json.loads(
            (target / ".self-healing-pipeline" / "state.json").read_text()
        )
        assert state["status"] == "escalated"
        assert "killed by SelfHealingAdapter" in state["escalation_reason"]

    def test_kill_then_poll_reports_failed(self, adapter, tmp_path):
        target = tmp_path / "job-k2"
        target.mkdir()
        _write_state(target, {"status": "building", "attempt": 1})
        adapter._jobs["job-k2"] = target

        adapter.kill("job-k2")
        assert adapter.poll()["jobs"][0]["status"] == "failed"

    def test_kill_creates_state_dir_if_missing(self, adapter, tmp_path):
        target = tmp_path / "job-bare"
        target.mkdir()
        adapter._jobs["job-bare"] = target

        assert adapter.kill("job-bare") is True
        state_file = target / ".self-healing-pipeline" / "state.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text())["status"] == "escalated"

    def test_kill_removes_pending_job_file(self, adapter, tmp_spec):
        """If the daemon hasn't picked up the job, kill removes it from pending."""
        adapter.queue(tmp_spec, "job-cancel", "opus")
        pending_file = adapter.pending_dir / "job-cancel.json"
        assert pending_file.exists()

        assert adapter.kill("job-cancel") is True
        assert not pending_file.exists()

    def test_kill_escalates_state_even_if_already_claimed(self, adapter, tmp_spec):
        """If the daemon already moved the job to in_flight, state is still escalated."""
        adapter.queue(tmp_spec, "job-inflight", "opus")
        pending_file = adapter.pending_dir / "job-inflight.json"
        in_flight_file = adapter.in_flight_dir / "job-inflight.json"
        # Simulate the daemon claiming the job
        pending_file.rename(in_flight_file)

        assert adapter.kill("job-inflight") is True
        assert not pending_file.exists()
        assert in_flight_file.exists()  # kill does not touch in_flight files

        state_file = adapter._jobs["job-inflight"] / ".self-healing-pipeline" / "state.json"
        assert json.loads(state_file.read_text())["status"] == "escalated"


# ----------------------------------------------------- heartbeat liveness
class TestHeartbeatLiveness:
    def test_is_active_false_when_queue_dir_missing(self, adapter):
        assert not adapter.queue_root.exists()
        assert adapter.is_active() is False

    def test_is_active_false_when_no_heartbeat_files(self, adapter):
        adapter._ensure_queue_dirs()
        assert adapter.is_active() is False

    def test_is_active_true_when_heartbeat_fresh(self, adapter):
        adapter._ensure_queue_dirs()
        heartbeat = adapter.queue_root / "heartbeat-worker-1.txt"
        heartbeat.write_text(str(time.time()))
        assert adapter.is_active() is True

    def test_is_active_false_when_heartbeat_stale(self, adapter):
        adapter._ensure_queue_dirs()
        heartbeat = adapter.queue_root / "heartbeat-worker-1.txt"
        heartbeat.write_text("stale")
        # Force mtime to just outside the freshness window
        stale_mtime = time.time() - (HEARTBEAT_STALE_SECONDS + 10)
        os.utime(heartbeat, (stale_mtime, stale_mtime))
        assert adapter.is_active() is False

    def test_is_active_picks_up_any_worker_heartbeat(self, adapter):
        """Multi-daemon readiness: any fresh heartbeat-*.txt satisfies is_active."""
        adapter._ensure_queue_dirs()
        (adapter.queue_root / "heartbeat-worker-3.txt").write_text(str(time.time()))
        assert adapter.is_active() is True

    def test_is_active_ignores_stale_when_fresh_exists(self, adapter):
        adapter._ensure_queue_dirs()
        stale = adapter.queue_root / "heartbeat-worker-1.txt"
        stale.write_text("stale")
        os.utime(stale, (time.time() - 3600, time.time() - 3600))
        fresh = adapter.queue_root / "heartbeat-worker-2.txt"
        fresh.write_text("fresh")
        assert adapter.is_active() is True


# -------------------------------------------------------------- lifecycle
class TestLifecycle:
    def test_start_returns_false_without_heartbeat(self, adapter, caplog):
        """No daemon running → start() returns False and logs a clear message."""
        import logging
        with caplog.at_level(logging.WARNING):
            assert adapter.start(concurrency=1) is False
        assert any(
            "self-healing daemon" in rec.message.lower()
            for rec in caplog.records
        )

    def test_start_returns_true_with_fresh_heartbeat(self, adapter):
        adapter._ensure_queue_dirs()
        (adapter.queue_root / "heartbeat-worker-1.txt").write_text(str(time.time()))
        assert adapter.start(concurrency=1) is True

    def test_start_returns_false_with_stale_heartbeat(self, adapter):
        adapter._ensure_queue_dirs()
        hb = adapter.queue_root / "heartbeat-worker-1.txt"
        hb.write_text("stale")
        os.utime(hb, (time.time() - 3600, time.time() - 3600))
        assert adapter.start(concurrency=1) is False


# ----------------------------------------------------------------- factory
class TestFactoryIntegration:
    def test_factory_routes_self_healing(self, tmp_path):
        from adapters.factory import create_adapter

        c = Config()
        c.build_target = "self_healing"
        c.self_healing_workspace_root = str(tmp_path / "workspaces")
        adapter = create_adapter(c)
        assert isinstance(adapter, SelfHealingAdapter)
        assert adapter.runtime == "self_healing"

    def test_config_accepts_self_healing_build_target(self, monkeypatch, tmp_path):
        monkeypatch.setenv("METROPLEX_BUILD_TARGET", "self_healing")
        monkeypatch.setenv(
            "METROPLEX_SELF_HEALING_WORKSPACE_ROOT", str(tmp_path / "ws")
        )
        c = Config()
        assert c.build_target == "self_healing"
        assert c.self_healing_workspace_root == str(tmp_path / "ws")
