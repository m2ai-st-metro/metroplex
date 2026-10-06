"""A rework on a reserved-action task needs Matthew's fresh approval (one per attempt).
The owner's `ttn rework` raises a "rework" wake; Metroplex asks with the same Approve
card as first routing and never routes a worker itself (2026-10-06, sidewalk 9b04 t3)."""

from __future__ import annotations

from cos.intake import Intake
from cos.routing import route_pending
from tests.test_intake_daemon import ScriptedReasoner
from tests.test_review_round2 import fail_attempt, token_from
from tests.test_routing import FakeJev, add_task, config, decisions, finish, grant_project, router

A1 = "ship:ready:1:a1"


def approve(intake, store, notes, message_id):
    ref = store.get(f"cb:{token_from(notes)}")
    return intake.approve_reserved(ref["taskId"], ref["scopeRevision"], "7001", "7001", message_id, block_revision=ref["blockRevision"])


def reviewed_reserved_task(work, store):
    """A reserved task routed after its first approval, whose attempt has a result."""
    grant_project(work)
    add_task(work, "ship", reservedAction="publish_push_deploy")
    r, notes = router(work, store, jev=FakeJev())
    intake = Intake(work["cos"], store, ScriptedReasoner(), lambda t, b=None: "sent", config())
    route_pending(r, work["cos"])
    assert "Approved" in approve(intake, store, notes, "m1")
    route_pending(r, work["cos"])
    finish(work, A1, worker="worker")
    route_pending(r, work["cos"])  # the result wake: owner reviews
    return r, notes, intake


def attempts(work):
    return sorted(a["id"] for a in work["operator"].snapshot()["attempt"] if a["taskId"] == "ship")


def test_rework_wake_asks_once_and_the_tap_lets_the_owner_queue_exactly_one_rework(work, store):
    r, notes, intake = reviewed_reserved_task(work, store)
    before = len(notes)
    work.cmd("owner", "wake.raise", "ship", {"reason": "rework"}, kind="task")
    [out] = route_pending(r, work["cos"])
    assert out.decision.startswith("escalate: reserved") and "rework" in out.decision
    assert work.get("task", "ship")["status"] == "blocked"
    assert len(notes) == before + 1 and "rework" in notes[-1][0]
    assert "Approved" in approve(intake, store, notes, "m2")
    outs = route_pending(r, work["cos"])
    assert any(d.startswith("await") for d in decisions(outs)), decisions(outs)
    assert attempts(work) == [A1], "Metroplex routes no worker after the tap"
    work.cmd("owner", "attempt.queue", f"{A1}:rework:2", {"contributionId": "ship:ready:1", "runtime": "api"}, kind="attempt")
    used = [a for a in work["operator"].snapshot()["approval"] if a.get("taskId") == "ship" and a.get("usedBy") == f"{A1}:rework:2"]
    assert len(used) == 1, "the fresh approval covers exactly the owner's rework"


def test_rework_wake_with_an_unused_approval_sends_no_card(work, store):
    r, notes, intake = reviewed_reserved_task(work, store)
    work.cmd("owner", "wake.raise", "ship", {"reason": "rework"}, kind="task")
    route_pending(r, work["cos"])
    assert "Approved" in approve(intake, store, notes, "m2")
    route_pending(r, work["cos"])
    before = len(notes)
    work.cmd("owner", "wake.raise", "ship", {"reason": "rework"}, kind="task")
    [out] = route_pending(r, work["cos"])
    assert out.decision == "noop: rework approval already recorded" and len(notes) == before
    assert work.get("task", "ship")["status"] != "blocked"


def test_rework_wake_on_a_task_without_a_reserved_action_is_a_noop(work, store):
    grant_project(work)
    add_task(work, "ship")
    r, notes = router(work, store, jev=FakeJev())
    route_pending(r, work["cos"])
    finish(work, "ship:created:1:a1", worker="worker")
    route_pending(r, work["cos"])
    work.cmd("owner", "wake.raise", "ship", {"reason": "rework"}, kind="task")
    [out] = route_pending(r, work["cos"])
    assert out.decision == "noop: rework needs no reserved approval" and notes == []


def test_rework_wake_with_nothing_awaiting_review_is_a_noop(work, store):
    grant_project(work)
    add_task(work, "ship", reservedAction="publish_push_deploy")
    r, notes = router(work, store, jev=FakeJev())
    intake = Intake(work["cos"], store, ScriptedReasoner(), lambda t, b=None: "sent", config())
    route_pending(r, work["cos"])
    assert "Approved" in approve(intake, store, notes, "m1")
    route_pending(r, work["cos"])
    fail_attempt(work, A1)  # failed, so no result awaits review
    work.cmd("owner", "wake.raise", "ship", {"reason": "rework"}, kind="task")
    outs = route_pending(r, work["cos"])
    assert "noop: nothing awaiting review to rework" in decisions(outs), decisions(outs)
