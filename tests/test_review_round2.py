"""Regression tests for the round-2 re-review (2026-09-25). Each started as the
reviewer's reproduction of the defect and now asserts the correct behavior."""

from __future__ import annotations

import time

from cos.config import Thresholds
from cos.intake import Intake
from cos.motion import sweep
from cos.routing import route_pending
from cos.teletraan_client import WorkError
from tests.test_intake_daemon import ScriptedReasoner, button, daemon, text, yes_button
from tests.test_routing import FakeJev, add_task, config, grant_project, jev_answer, router

T = Thresholds()


def fail_attempt(work, attempt_id):
    work.cmd("runtime", "attempt.started", attempt_id, {"receipt": {"id": "r"}}, kind="attempt")
    work.cmd("runtime", "attempt.failed", attempt_id, {"observedStopped": True, "error": "boom"}, kind="attempt")


def token_from(notes):
    return next(b["callback_data"] for row in notes[-1][1] for b in row if b["callback_data"].startswith("r:"))[2:]


def test_n1_reserved_task_is_re_asked_and_approvable_after_a_failed_attempt(work, store):
    grant_project(work)
    add_task(work, "ship", reservedAction="publish_push_deploy")
    r, notes = router(work, store, jev=FakeJev())
    intake = Intake(work["cos"], store, ScriptedReasoner(), lambda t, b=None: "sent", config())
    route_pending(r, work["cos"])
    first = store.get(f"cb:{token_from(notes)}")
    assert "Approved" in intake.approve_reserved(first["taskId"], first["scopeRevision"], "7001", "7001", "m1", block_revision=first["blockRevision"])
    route_pending(r, work["cos"])
    fail_attempt(work, "ship:ready:1:a1")
    before = len(notes)
    route_pending(r, work["cos"])
    assert work.get("task", "ship")["status"] == "blocked"
    assert len(notes) == before + 1, "the second block is a new episode and gets its own notice"
    second = store.get(f"cb:{token_from(notes)}")
    assert second["blockRevision"] != first["blockRevision"]
    # The stale first button is refused cleanly; the new one works.
    assert "changed" in intake.approve_reserved(first["taskId"], first["scopeRevision"], "7001", "7001", "m1", block_revision=first["blockRevision"])
    assert "Approved" in intake.approve_reserved(second["taskId"], second["scopeRevision"], "7001", "7001", "m2", block_revision=second["blockRevision"])
    route_pending(r, work["cos"])
    attempts = sorted(a["id"] for a in work["operator"].snapshot()["attempt"] if a["taskId"] == "ship")
    assert len(attempts) == 2 and work.get("task", "ship")["status"] == "active"


def test_n5_a_second_escalation_at_the_same_scope_revision_is_announced(work, store):
    grant_project(work)
    add_task(work, "t1")
    r, notes = router(work, store, jev=FakeJev(jev_answer(choice="none_fit")))
    route_pending(r, work["cos"])
    assert len(notes) == 1
    work.cmd("operator", "task.resume", "t1", {})
    route_pending(r, work["cos"])
    assert work.get("task", "t1")["status"] == "blocked" and len(notes) == 2


def test_n2_work_awaiting_owner_review_is_never_re_dispatched(work, store):
    grant_project(work)
    add_task(work, "t1")
    r, _ = router(work, store, jev=FakeJev())
    route_pending(r, work["cos"])
    a = "t1:created:1:a1"
    work.cmd("runtime", "attempt.started", a, {"receipt": {"id": "r"}}, kind="attempt")
    work.cmd("worker", "attempt.result", a, {"result": {"summary": "please review"}}, kind="attempt")
    route_pending(r, work["cos"])
    found = sweep(work["cos"], store, T, 5, lambda text, b=None: "sent", now=time.time() + 11 * 60)
    assert not [f for f in found if f.check == "M3 unassigned"], "a completed result waits for the owner"
    work.cmd("cos", "wake.raise", "t1", {"reason": "unassigned"}, kind="task")  # even a forced wake
    [out] = route_pending(r, work["cos"])
    assert out.decision.startswith("await: owner")
    assert [x["id"] for x in work["operator"].snapshot()["attempt"]] == [a]


class DepsFailOnce(ScriptedReasoner):
    pass


def test_n3_tasks_of_a_granting_project_are_held_and_deps_exist_before_routing(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    real = work.clients["cos"].command
    def fail_deps(op, *a, **k):
        if op == "task.dependencies":
            raise WorkError("SOCKET_DROPPED")
        return real(op, *a, **k)
    work.clients["cos"].command = fail_deps
    d.handle(button(yes_button(transport)))
    work.clients["cos"].command = real
    assert store.cards_in("granting")
    outs = route_pending(d.router, work["cos"])
    assert outs and all(o.decision == "hold: project setup not finished" for o in outs)
    assert work["operator"].snapshot()["attempt"] == []
    d.intake.resume_granting()
    tasks = sorted(work["operator"].snapshot()["task"], key=lambda t: t["id"])
    assert tasks[1]["dependencies"] == [tasks[0]["id"]]
    outs = route_pending(d.router, work["cos"])
    t2_decision = next(o.decision for o in outs if o.commands and "dependencies" in o.decision or o.decision == "wait: dependencies not done")
    assert t2_decision == "wait: dependencies not done", "t2 waits for t1 once setup is finished"
    assert not [a for a in work["operator"].snapshot()["attempt"] if a["taskId"] == tasks[1]["id"]]


def test_n4_card_command_never_rewrites_a_granting_or_granted_card(work, store):
    d, transport = daemon(work, store, ScriptedReasoner(label="note"))
    d.handle(text("maybe someday: a thing"))
    pid = work["operator"].snapshot()["proposal"][0]["id"]
    d.intake.reasoner = ScriptedReasoner()
    d.handle(text(f"card {pid}", mid="2"))
    d.handle(button(yes_button(transport)))
    [card] = store.cards_in("granted")
    d.handle(text(f"card {pid}", mid="3"))
    assert store.get_card(card["card_id"])["status"] == "granted"
    assert "already granted" in transport.sent[-1]["text"]


def test_n9_card_command_only_matches_real_ids(work, store):
    d, transport = daemon(work, store, ScriptedReasoner(label="note"))
    d.handle(text("card games night with friends"))
    d.handle(text("card games"))
    assert all("No card or proposal" not in m["text"] for m in transport.sent), "'card games' is an idea, not a command"


def test_codex_window_project_created_before_granting_is_resumed_hourly(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    yes = yes_button(transport)
    real = work.clients["cos"].command
    def die_after_project(op, *a, **k):
        result = real(op, *a, **k)
        if op == "project.create":
            raise SystemExit("killed after project.create committed")
        return result
    work.clients["cos"].command = die_after_project
    try:
        d.handle(button(yes))
    except SystemExit:
        pass
    work.clients["cos"].command = real
    assert store.cards_in("granting"), "granting is written before any command"
    assert d.intake.resume_granting()
    assert len(work["operator"].snapshot()["project"]) == 1 and len(work["operator"].snapshot()["task"]) == 2


def test_n10_conflict_errors_do_not_promise_a_safe_retry(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    real = work.clients["cos"].command
    def conflict(op, *a, **k):
        raise WorkError("COMMAND_ID_CONFLICT", command_rejected=True)
    d.handle(text("build the thing"))
    work.clients["cos"].command = conflict
    d.handle(button(yes_button(transport)))
    work.clients["cos"].command = real
    assert "nothing is duplicated" not in transport.sent[-1]["text"] and "conflicts" in transport.sent[-1]["text"]


def test_n11_open_intake_breaker_is_honored(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    for _ in range(3):
        d.breaker.record_failure("intake")
    d.handle(text("build the thing"))
    assert "intake is paused" in transport.sent[-1]["text"]
    assert not store.cards_in("awaiting_yes")
    d.handle(text("/status"))
    assert "Open breakers: intake" in transport.sent[-1]["text"], "control commands still work"
