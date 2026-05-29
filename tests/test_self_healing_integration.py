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

    # 5. FAKE THE DAEMON — hand-write terminal state.
    # Post publish-vs-Ravage-race fix (2026-05-04): a "passed" status without
    # a Ravage review_verdict now maps to "running", not "completed", because
    # the pipeline Judge writes "passed" before the daemon's Step 10.5 review
    # runs. We must emit review_verdict="approved" to simulate Ravage having
    # finished its review.
    _write_state(
        target_dir,
        {
            "status": "passed",
            "attempt": 1,
            "judge_verdict": "pass",
            "review_verdict": "approved",
        },
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


def test_poll_sets_review_rejected_on_ravage_reject(
    orchestrator, adapter, state_db, spec_file, fresh_heartbeat
):
    """When the daemon's poll dict carries review_verdict='rejected' (Ravage
    rejected on safety-class findings), the failed-branch of poll_and_sync_status
    must persist review_status='review_rejected' so the postmortem classifier
    can label it correctly instead of falling through to spec_unclear.

    Drives the REAL adapter.poll() via a hand-written state.json with
    status='passed' + review_verdict='rejected', which _resolve_metroplex_status
    maps to 'failed' while still surfacing review_verdict in the poll dict.
    """
    idea = _idea(idea_id=5, title="Elder-care companion")
    job = orchestrator.queue_build(idea, spec_file, dry_run=False)
    assert job is not None
    assert job.queue_job_id == "metroplex-ideaforge-5"

    target_dir = adapter.workspace_root / "metroplex-ideaforge-5"

    # Ravage finished and REJECTED — Judge had written passed, review_verdict
    # flips it to a failed metroplex status (defense-in-depth path).
    _write_state(
        target_dir,
        {
            "status": "passed",
            "attempt": 1,
            "judge_verdict": "pass",
            "review_verdict": "rejected",
            "review_critical_count": 2,
        },
    )

    poll_result = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-5" in poll_result["failed"]

    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-5")
    assert row["status"] == "failed"
    # The fix: the Ravage rejection verdict is preserved, not lost.
    assert row["review_status"] == "review_rejected"


def test_poll_review_rejected_via_loop_status_escalated(
    orchestrator, adapter, state_db, spec_file, fresh_heartbeat
):
    """self_healing_state in ('review_rejected','escalated') also triggers the
    review_rejected tag, even without an explicit review_verdict on the dict."""
    idea = _idea(idea_id=6, title="Escalated build")
    job = orchestrator.queue_build(idea, spec_file, dry_run=False)
    assert job is not None

    target_dir = adapter.workspace_root / "metroplex-ideaforge-6"
    _write_state(
        target_dir,
        {
            "status": "review_rejected",
            "attempt": 3,
            "review_critical_count": 1,
        },
    )

    poll_result = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-6" in poll_result["failed"]

    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-6")
    assert row["status"] == "failed"
    assert row["review_status"] == "review_rejected"


def test_poll_non_self_healing_dict_does_not_crash(
    orchestrator, adapter, state_db, spec_file, fresh_heartbeat
):
    """Defensive: a poll dict lacking review_verdict/self_healing_state keys
    (non-self-healing adapter shape, e.g. OzAdapter) must not raise and must
    NOT tag review_status. Mocks check_status directly to produce the bare
    failed-job shape that has none of the self-healing review keys."""
    idea = _idea(idea_id=7, title="Plain failure")
    job = orchestrator.queue_build(idea, spec_file, dry_run=False)
    assert job is not None

    # Bare poll dict: a failed job with NO review_verdict / self_healing_state.
    orchestrator.check_status = MagicMock(return_value={
        "jobs": [{"id": "metroplex-ideaforge-7", "status": "failed"}]
    })

    poll_result = orchestrator.poll_and_sync_status()
    assert "metroplex-ideaforge-7" in poll_result["failed"]

    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-7")
    assert row["status"] == "failed"
    # No review verdict on the dict → review_status left untouched.
    assert row["review_status"] is None


def test_ravage_findings_reach_retry_planner_context(
    orchestrator, adapter, state_db, spec_file, fresh_heartbeat, tmp_path
):
    """Part 2b: assert the Ravage review findings actually reach the retry
    Planner. _record_build_session captures review_verdict + review-report.md +
    structured review-findings.json into a session record; _inject_session_context
    then appends that summary to the retry's spec file. This is the end-to-end
    Ravage->Planner feedback channel (already wired); the test guards it.
    """
    idea = _idea(idea_id=8, title="Safety-class build")
    job = orchestrator.queue_build(idea, spec_file, dry_run=False)
    assert job is not None
    base_job_id = "metroplex-ideaforge-8"

    target_dir = adapter.workspace_root / base_job_id
    state_dir = target_dir / ".self-healing-pipeline"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Daemon artifacts: terminal state + Ravage review report + structured findings.
    (state_dir / "state.json").write_text(json.dumps({
        "status": "passed",
        "attempt": 1,
        "judge_verdict": "pass",
        "review_verdict": "rejected",
        "review_critical_count": 1,
    }))
    (state_dir / "review-report.md").write_text(
        "# Ravage Review\nCRITICAL: unbounded medication-dose input accepted "
        "without validation — safety hazard for elder-care dosing."
    )
    (state_dir / "review-findings.json").write_text(json.dumps({
        "findings": [
            {
                "finding_id": "F-01",
                "claim_class": "FAILURE",
                "title": "Medication dose not validated",
                "input_shape": "negative_dose",
                "expected_behavior": "reject negative or out-of-range doses",
                "observed_behavior": "accepts any float",
                "severity": "critical",
                "source": "ravage",
                "confidence": 0.95,
            }
        ]
    }))

    # Poll: failed branch records the session snapshot (and tags review_rejected).
    orchestrator.poll_and_sync_status()

    # The session record must contain the Ravage findings text.
    session = state_db.get_latest_session(base_job_id)
    assert session is not None, "session snapshot was not recorded"
    summary = session["session_summary"]
    assert "verdict=rejected" in summary
    assert "Ravage" in summary or "review" in summary.lower()
    # Structured findings injected as the auto-extracted claims table.
    assert "Prior-review-derived claims" in summary
    assert "reject negative or out-of-range doses" in summary
    assert "F-01" in summary

    # Now simulate a retry dispatch: _inject_session_context must append the
    # captured Ravage context (incl. findings) into the retry's spec file.
    retry_spec = tmp_path / "retry_spec.md"
    retry_spec.write_text("# Retry spec\nBuild the elder-care companion.\n")
    orchestrator._inject_session_context(base_job_id, attempt=1, spec_path=retry_spec)

    injected = retry_spec.read_text()
    assert "Prior Build Attempts" in injected
    assert "reject negative or out-of-range doses" in injected, (
        "Ravage findings did not reach the retry Planner's spec input"
    )
