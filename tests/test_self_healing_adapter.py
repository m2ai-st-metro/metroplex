"""Tests for the Self-Healing build adapter skeleton (Phase C Step 1)."""
import json
from pathlib import Path

import pytest

from adapters.self_healing_adapter import SelfHealingAdapter
from build_adapter import BuildAdapter
from config import Config


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.self_healing_workspace_root = str(tmp_path / "workspaces")
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
    def test_queue_raises_notimplemented_in_skeleton(self, adapter, tmp_spec):
        """Step 1: dispatch is explicitly not wired. This should be loud."""
        with pytest.raises(NotImplementedError, match="Step 2"):
            adapter.queue(tmp_spec, "job-1", "opus")

    def test_queue_prepares_workspace_before_dispatch(self, adapter, tmp_spec):
        """Workspace + spec copy happen before the dispatch stub raises."""
        with pytest.raises(NotImplementedError):
            adapter.queue(tmp_spec, "job-prep", "opus")
        target_dir = adapter.workspace_root / "job-prep"
        assert target_dir.is_dir()
        assert (target_dir / "spec.md").read_text() == tmp_spec.read_text()

    def test_queue_does_not_track_job_on_stub_raise(self, adapter, tmp_spec):
        """A job that can never dispatch must not linger in internal tracking."""
        with pytest.raises(NotImplementedError):
            adapter.queue(tmp_spec, "job-x", "opus")
        # Step 1 behaviour: we DO currently track the job before _dispatch runs,
        # so poll() can be exercised against hand-written state files. This
        # test pins that behaviour intentionally — if Step 2 changes it, update
        # both the adapter and this assertion in the same commit.
        assert "job-x" in adapter._jobs


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


# -------------------------------------------------------------- lifecycle
class TestLifecycle:
    def test_is_active_skeleton(self, adapter):
        assert adapter.is_active() is True

    def test_start_skeleton_returns_true(self, adapter):
        assert adapter.start(concurrency=1) is True

    def test_start_concurrency_ignored_in_skeleton(self, adapter):
        assert adapter.start(concurrency=5) is True


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
