"""
Integration test for the BuildGate -> SelfHealingAdapter -> poll chain.

Exercises the full dispatch-and-sync path end-to-end WITHOUT running the real
self-healing daemon, the /self-healing-pipeline skill, or any Claude Code
subprocess. The only thing faked is the daemon: we hand-write
`<target_dir>/.self-healing-pipeline/state.json` to simulate the P/B/J loop
having reached a particular state, then let the adapter's real `poll()` read
it back.

Why this shape works: `SelfHealingAdapter.poll()` ONLY looks at
`<target_dir>/.self-healing-pipeline/state.json` on disk. It does not care
whether the job file is still in pending/, has moved to in_flight/, or has
been routed to completed/. That makes writing a terminal state.json entirely
sufficient to drive the full chain through `BuildOrchestrator.check_status`
-> `poll_and_sync_status` -> `StateDB.update_build_job_status`.

Coverage:
  1. Happy path (passed)    — queue, workspace + pending file present,
                              DB row at queued, flip state.json to passed,
                              first poll syncs to completed, second poll is
                              idempotent.
  2. Escalated path         — same shape, terminal state=escalated, row lands
                              at failed. We deliberately do NOT assert
                              escalation_reason is persisted because the
                              Metroplex build_jobs schema doesn't store it.
  3. Running state          — state.json=building, row goes to started, and
                              polling a second time without any state.json
                              change is a no-op (started -> started).
  4. Guard/heartbeat        — stale heartbeat causes queue_build to skip
                              dispatch entirely, leaves no DB row, no pending
                              file, and logs a warning.

Notes:
  - No `time.sleep()` calls. The 120s heartbeat window is huge vs test runtime.
  - The adapter instance is shared between BuildGate and the test fixtures so
    both sides see the same `_jobs` dict.
  - The idea dict only needs `id`, `title`, and `_source` — queue_build reads
    those and passes the rest through.
"""
import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adapters.self_healing_adapter import SelfHealingAdapter, HEARTBEAT_STALE_SECONDS
from audit import AuditLogger
from config import Config
from db import StateDB
from gates.build import BuildOrchestrator, SpecGenerator


@pytest.fixture
def state_db():
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def shc_config(tmp_path):
    c = Config()
    c.build_target = "self_healing"
    c.self_healing_workspace_root = str(tmp_path / "workspaces")
    c.self_healing_queue_root = str(tmp_path / "queue")
    return c


@pytest.fixture
def adapter(shc_config):
    return SelfHealingAdapter(shc_config)


@pytest.fixture
def fresh_heartbeat(adapter):
    adapter.queue_root.mkdir(parents=True, exist_ok=True)
    hb = adapter.queue_root / "heartbeat-worker-1.txt"
    hb.touch()
    return hb


@pytest.fixture
def stale_heartbeat(adapter):
    adapter.queue_root.mkdir(parents=True, exist_ok=True)
    hb = adapter.queue_root / "heartbeat-worker-1.txt"
    hb.touch()
    old = time.time() - (HEARTBEAT_STALE_SECONDS + 60)
    os.utime(hb, (old, old))
    return hb


@pytest.fixture
def spec_file(tmp_path):
    spec = tmp_path / "approved_spec.md"
    spec.write_text("# Test spec\nBuild a calculator. Stdlib only. 100% pytest coverage.\n")
    return spec


@pytest.fixture
def orchestrator(shc_config, state_db, adapter, tmp_path):
    audit_log = tmp_path / "audit.log"
    audit_logger = AuditLogger(str(audit_log))
    spec_generator = MagicMock(spec=SpecGenerator)  # never called in these tests
    return BuildOrchestrator(
        config=shc_config,
        state_db=state_db,
        spec_generator=spec_generator,
        audit_logger=audit_logger,
        adapter=adapter,
    )


def _write_state(target_dir: Path, state: dict) -> None:
    state_dir = target_dir / ".self-healing-pipeline"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(state))


def _idea(idea_id: int = 1, title: str = "Calc tool") -> dict:
    return {
        "id": idea_id,
        "title": title,
        "_source": "ideaforge",
    }


def test_self_healing_happy_path_end_to_end(
    orchestrator, adapter, state_db, spec_file, fresh_heartbeat
):
    # 1. Liveness precondition
    assert adapter.is_active() is True

    # 2. Queue through BuildGate
    idea = _idea(idea_id=1, title="Calc tool")
    job = orchestrator.queue_build(idea, spec_file, dry_run=False)
    assert job is not None  # guard does NOT fire — heartbeat fresh
    assert job.queue_job_id == "metroplex-ideaforge-1"
    assert job.status == "queued"

    # 3. Adapter side-effects
    pending_file = adapter.pending_dir / "metroplex-ideaforge-1.json"
    assert pending_file.exists()
    payload = json.loads(pending_file.read_text())
    for key in ("job_id", "target_dir", "spec_path", "model", "queued_at"):
        assert key in payload

    target_dir = adapter.workspace_root / "metroplex-ideaforge-1"
    assert (target_dir / "spec.md").exists()
    assert (target_dir / "spec.md").read_text() == spec_file.read_text()
    assert "metroplex-ideaforge-1" in adapter._jobs

    # 4. DB row recorded at queued
    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-1")
    assert row is not None
    assert row["status"] == "queued"
    assert row["idea_id"] == 1
    assert row["title"] == "Calc tool"
    assert row["retry_count"] == 0
    assert row["completed_at"] is None

    # 5. FAKE THE DAEMON — hand-write terminal state
    _write_state(
        target_dir,
        {"status": "passed", "attempt": 1, "judge_verdict": "pass"},
    )

    # 6. First poll syncs the row
    poll_result = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-1" in poll_result["completed"]
    assert "metroplex-ideaforge-1" in poll_result["newly_synced"]

    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-1")
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["retry_count"] == 0
    assert row["next_retry_at"] is None

    # 7. Second poll is idempotent — not in newly_synced
    poll_result_2 = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-1" not in poll_result_2["newly_synced"]


def test_self_healing_escalated_path_end_to_end(
    orchestrator, adapter, state_db, spec_file, fresh_heartbeat
):
    assert adapter.is_active() is True

    idea = _idea(idea_id=2, title="Flaky tool")
    job = orchestrator.queue_build(idea, spec_file, dry_run=False)
    assert job is not None
    assert job.queue_job_id == "metroplex-ideaforge-2"
    assert job.status == "queued"

    target_dir = adapter.workspace_root / "metroplex-ideaforge-2"
    assert (target_dir / "spec.md").exists()

    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-2")
    assert row is not None
    assert row["status"] == "queued"
    assert row["retry_count"] == 0
    assert row["completed_at"] is None

    _write_state(
        target_dir,
        {
            "status": "escalated",
            "attempt": 2,
            "escalation_reason": "Judge rejected after 2 attempts",
        },
    )

    poll_result = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-2" in poll_result["failed"]
    assert "metroplex-ideaforge-2" in poll_result["newly_synced"]

    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-2")
    assert row["status"] == "failed"
    assert row["completed_at"] is not None
    assert row["retry_count"] == 0
    # escalation_reason is NOT persisted in Metroplex DB — do not assert on it.


def test_self_healing_running_state_no_terminal_transition(
    orchestrator, adapter, state_db, spec_file, fresh_heartbeat
):
    assert adapter.is_active() is True

    idea = _idea(idea_id=3, title="Long runner")
    job = orchestrator.queue_build(idea, spec_file, dry_run=False)
    assert job is not None
    assert job.queue_job_id == "metroplex-ideaforge-3"

    target_dir = adapter.workspace_root / "metroplex-ideaforge-3"

    _write_state(target_dir, {"status": "building", "attempt": 1})

    poll_result = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-3" in poll_result["running"]
    assert "metroplex-ideaforge-3" in poll_result["newly_synced"]

    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-3")
    assert row["status"] == "started"
    assert row["completed_at"] is None

    # Second poll without changing state.json: started -> started is a no-op.
    poll_result_2 = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-3" not in poll_result_2["newly_synced"]


def test_self_healing_guard_skips_when_heartbeat_stale(
    orchestrator, adapter, state_db, spec_file, stale_heartbeat, caplog
):
    assert adapter.is_active() is False

    with caplog.at_level(logging.WARNING):
        idea = _idea(idea_id=4, title="Should not dispatch")
        job = orchestrator.queue_build(idea, spec_file, dry_run=False)

    assert job is None
    assert state_db.get_build_by_queue_job_id("metroplex-ideaforge-4") is None
    assert not (adapter.pending_dir / "metroplex-ideaforge-4.json").exists()
    assert "metroplex-ideaforge-4" not in adapter._jobs
    assert any(
        "self-healing daemon" in rec.message.lower()
        for rec in caplog.records
    )
