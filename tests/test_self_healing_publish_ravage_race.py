"""Red tests for the publish-vs-Ravage race in SelfHealingAdapter.

Build 352 (2026-05-04) was published to GitHub with quality_score=60 at
15:44:59 while the daemon's Step 10.5 Ravage review wrote
review_rejected/review_critical_count=2 ten seconds later at 15:45:09.
Same silent-failure-hunter that rejected build 322 also rejected 352,
but 352 won the race because the adapter's `_STATUS_MAP` advances
`"passed"` -> `"completed"` as soon as the pipeline Judge passes, with no
check for whether Ravage has finished.

These tests exercise the `_job_status` -> `_map_status` seam directly
through `adapter.poll()`, so the test contract does not lock in the
`_map_status` signature. The fix is free to pass the full state dict
or to pass review_verdict as a second argument.

Both tests must FAIL against the unmodified adapter on this branch
and PASS after the fix.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.self_healing_adapter import SelfHealingAdapter
from config import Config


@pytest.fixture
def adapter(tmp_path: Path) -> SelfHealingAdapter:
    c = Config()
    c.self_healing_workspace_root = str(tmp_path / "workspaces")
    c.self_healing_queue_root = str(tmp_path / "queue")
    return SelfHealingAdapter(c)


def _write_state(target_dir: Path, state: dict) -> None:
    """Write `state` to `<target_dir>/.self-healing-pipeline/state.json`."""
    state_dir = target_dir / ".self-healing-pipeline"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(state))


def test_passed_without_review_verdict_returns_running(
    adapter: SelfHealingAdapter, tmp_path: Path
) -> None:
    """R1: The race window itself.

    The pipeline Judge has just written `status="passed"`. The daemon's
    Step 10.5 Ravage review has not yet started (or is in flight) so
    `review_verdict` is absent from state.json. The adapter MUST report
    "running" so that Metroplex's publish gate does not pick the build
    up. Reporting "completed" here is the bug that shipped build 352.
    """
    target = tmp_path / "job-race"
    target.mkdir()
    _write_state(
        target,
        {
            "status": "passed",
            "attempt": 1,
            "judge_verdict": "pass",
            # NOTE: review_verdict deliberately absent — Ravage hasn't run yet.
        },
    )
    adapter._jobs["job-race"] = target

    job = adapter.poll()["jobs"][0]

    assert job["status"] == "running", (
        "BUG: adapter reports 'completed' for a build that has not yet been "
        "reviewed by Ravage. Expected 'running' until review_verdict is set. "
        f"Got status={job['status']!r}."
    )


def test_passed_with_review_rejected_verdict_returns_failed(
    adapter: SelfHealingAdapter, tmp_path: Path
) -> None:
    """R2: Defense in depth against ordering ambiguity.

    Ravage has just written `review_verdict="rejected"` and
    `review_critical_count=2`, but has not yet (or has already crashed
    before) flipping `state.status` from "passed" to "review_rejected"
    per daemon Step 10.5(f). The adapter MUST NOT trust that the daemon
    will always complete the status flip in lockstep with the verdict
    write. A non-None rejected verdict on a "passed" status must short-
    circuit to "failed".

    This is the exact scenario from build 352: the verdict was written
    but the publish gate had already polled "completed" 10 seconds
    earlier. Even if the daemon's status flip eventually arrived, the
    adapter would have already returned "completed" once and the build
    would already be on GitHub.
    """
    target = tmp_path / "job-rejected-pre-flip"
    target.mkdir()
    _write_state(
        target,
        {
            "status": "passed",
            "attempt": 1,
            "judge_verdict": "pass",
            "review_verdict": "rejected",
            "review_critical_count": 2,
        },
    )
    adapter._jobs["job-rejected-pre-flip"] = target

    job = adapter.poll()["jobs"][0]

    assert job["status"] == "failed", (
        "BUG: adapter reports 'completed' for a build Ravage has rejected. "
        "Even before the daemon flips state.status to 'review_rejected', "
        "a non-None review_verdict='rejected' must short-circuit to 'failed'. "
        f"Got status={job['status']!r}."
    )
