"""Regression tests for the 2026-09-24 independent review findings (Metroplex side)."""

from __future__ import annotations

import pytest

from cos.config import Thresholds
from cos.intake import Intake
from cos.motion import find, sweep
from cos.routing import route_pending
from cos.spec import validate_card
from cos.teletraan_client import WorkError
from tests.test_intake_daemon import ScriptedReasoner, button, daemon, text, yes_button
from tests.test_motion import NOW, iso, snap, task
from tests.test_routing import FakeJev, FakeReasoner, add_task, grant_project, jev_answer, router

T = Thresholds()


class PartialPlan(ScriptedReasoner):
    """Covers only g1, and returns malformed dependsOn values."""

    def complete_json(self, system, user):
        if system.startswith("Break an approved project"):
            return {"tasks": [{"goalId": "g1", "title": "Code", "objective": "o", "acceptance": "a", "dependsOn": 0},
                              {"goalId": "g1", "title": "More code", "objective": "o", "acceptance": "a", "dependsOn": [0, "x", 9, True]}]}
        return super().complete_json(system, user)


def test_b3_b4_malformed_partial_plan_is_validated_and_every_goal_gets_work(work, store):
    d, transport = daemon(work, store, PartialPlan())
    d.handle(text("build the thing"))
    d.handle(button(yes_button(transport)))
    tasks = sorted(work["operator"].snapshot()["task"], key=lambda t: t["id"])
    assert [t["title"] for t in tasks] == ["Code", "More code", "Write docs"], "g2 was skipped by the model and got a fallback task"
    assert tasks[1]["dependencies"] == [tasks[0]["id"]], "only the valid earlier index survives"
    assert store.cards_in("granted") and not store.cards_in("granting")


def test_b3_interrupted_decomposition_is_resumed_without_duplicates(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    yes = yes_button(transport)
    real = work.clients["cos"].command
    calls = {"n": 0}
    def flaky(op, *a, **k):
        if op == "task.create":
            calls["n"] += 1
            if calls["n"] == 2:
                raise WorkError("SOCKET_DROPPED")
        return real(op, *a, **k)
    work.clients["cos"].command = flaky
    d.handle(button(yes))
    assert "did not finish" in transport.sent[-1]["text"]
    assert len(store.cards_in("granting")) == 1 and len(work["operator"].snapshot()["task"]) == 1
    work.clients["cos"].command = real
    d.handle(button(yes))  # Matthew taps Yes again
    assert sorted(t["title"] for t in work["operator"].snapshot()["task"]) == ["Code", "Docs"]
    assert len(work["operator"].snapshot()["project"]) == 1 and store.cards_in("granted")


def test_b3_hourly_resume_finishes_granting_cards(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    card_id = yes_button(transport).split(":")[1]
    real = work.clients["cos"].command
    def no_tasks(op, *a, **k):
        if op == "task.create":
            raise WorkError("SOCKET_DROPPED")
        return real(op, *a, **k)
    work.clients["cos"].command = no_tasks
    d.handle(button(yes_button(transport)))
    work.clients["cos"].command = real
    assert d.intake.resume_granting() == [card_id]
    assert len(work["operator"].snapshot()["task"]) == 2 and store.get_card(card_id)["status"] == "granted"


def test_b5_failed_escalation_notice_is_resent_by_the_sweep(work, store):
    grant_project(work)
    add_task(work, "t1")
    r, notes = router(work, store, jev=FakeJev(jev_answer(choice="none_fit")))
    r.notify = lambda text, buttons=None: None  # Telegram down: send fails
    route_pending(r, work["cos"])
    assert work.get("task", "t1")["status"] == "blocked"
    sent = []
    sweep(work["cos"], store, T, 10, lambda text, b=None: sent.append(text) or "ok")
    sweep(work["cos"], store, T, 10, lambda text, b=None: sent.append(text) or "ok")
    assert len(sent) == 1 and "Needs you (intent)" in sent[0], "re-sent once, then remembered"


def test_b6_reasoning_breaker_is_separate_and_loud_and_turn_breaker_resets(work, store):
    grant_project(work)
    for i in range(3):
        add_task(work, f"t{i}")
    r, notes = router(work, store, jev=FakeJev(error="JEV_HTTP_503"), reasoner=FakeReasoner(None))
    route_pending(r, work["cos"])
    assert r.breaker.is_open("reasoning") and not r.breaker.is_open("turn")
    assert sum("local Qwen reasoning failed 3 times" in n[0] for n in notes) == 1
    r.breaker.record_failure("turn")
    r.breaker.record_failure("turn")
    route_pending(r, work["cos"])
    assert r.breaker.store.get_breaker("turn")[0] == 0, "a clean pass resets the consecutive count"


def test_b6_capacity_limit_waits_instead_of_failing(work, store):
    grant_project(work)
    add_task(work, "t1")
    snap_before = work["operator"].snapshot()
    project = snap_before["project"][0]
    r, _ = router(work, store)
    project_limited = {**project, "concurrencyLimit": 0}
    wake = next(w for w in work["cos"].wake_pending()["wakes"])
    out = r.handle(wake, {**work["cos"].snapshot(), "project": [project_limited]})
    assert out.decision == "wait: project at its concurrency limit"


def test_b7_active_task_with_no_live_attempt_is_unassigned_and_stuck_queue_is_noticed():
    acked = {"id": "w", "taskId": "t1", "reason": "stopped", "cycle": 1, "acknowledged": True, "acknowledgedAt": iso(11 * 60)}
    stopped = {"id": "a1", "taskId": "t1", "status": "failed", "heartbeatAt": iso(20 * 60)}
    [f] = find(snap([task(status="active")], [stopped], [acked]), T, NOW)
    assert f.check == "M3 unassigned"
    queued = {"id": "a2", "taskId": "t2", "status": "queued", "queuedAt": iso(16 * 60), "runtime": "api"}
    [q] = find(snap([task("t2", status="active")], [queued]), T, NOW)
    assert q.check == "M6 queued" and q.reason is None


def test_b10_orphan_notices_are_batched_into_one_message(store):
    wakes = [{"id": f"p:t{i}:created", "taskId": f"t{i}", "reason": "created", "cycle": 1, "acknowledged": False, "raisedAt": iso(20 * 60)} for i in range(12)]
    s = snap([task(f"t{i}") for i in range(12)], wakes=wakes)

    class C:
        def snapshot(self):
            return s
    sent = []
    sweep(C(), store, T, 10, lambda text, b=None: sent.append(text) or "ok", now=NOW)
    sweep(C(), store, T, 10, lambda text, b=None: sent.append(text) or "ok", now=NOW)
    assert len(sent) == 1 and "12 stuck wakes" in sent[0] and "and 2 more" in sent[0]


def test_b8_card_screen_shows_every_hash_bound_field_and_refuses_unknown_ones():
    card = {"title": "T", "objective": "O", "doneWhen": "D", "owner": "owner", "goals": [{"goalId": "g1", "statement": "S", "doneWhen": "G"}],
            "scopeIn": ["the repo"], "scopeOut": ["publishing"], "reservedActions": ["publish_push_deploy"], "kill": "K", "hostedAllowed": True, "humanOnly": False, "specTemplate": "v1"}
    screen = Intake.render(card)
    for needle in ("the repo", "publishing", "Hosted routing via Jev (TypeSafe): yes", "Human-only (never routed): no", "Spec template: v1", "Kill: K"):
        assert needle in screen
    with pytest.raises(ValueError, match="does not show"):
        Intake.render({**card, "surprise": "x"})


def test_b9_card_command_turns_a_proposal_into_a_card(work, store):
    d, transport = daemon(work, store, ScriptedReasoner(label="note"))
    d.handle(text("maybe someday: a thing"))
    pid = work["operator"].snapshot()["proposal"][0]["id"]
    d.intake.reasoner = ScriptedReasoner()
    d.handle(text(f"card {pid}", mid="2"))
    assert transport.sent[-1]["text"].startswith("CARD: ")
    d.handle(text("card Q-nope", mid="3"))
    assert "No card or proposal named Q-nope" in transport.sent[-1]["text"]


def test_jev_is_not_paid_twice_after_a_crash_between_call_and_record(work, store):
    grant_project(work)
    add_task(work, "t1")
    jev = FakeJev()
    r, _ = router(work, store, jev=jev, reasoner=FakeReasoner("worker"))
    real = r.client.command
    def crash_on_record(op, *a, **k):
        if op == "judgment.record":
            raise SystemExit("killed after the paid call")
        return real(op, *a, **k)
    r.client.command = crash_on_record
    with pytest.raises(SystemExit):
        route_pending(r, work["cos"])
    r.client.command = real
    [out] = route_pending(r, work["cos"])
    assert len(jev.calls) == 1, "the replay records 'outcome unknown' instead of paying again"
    assert work.get("judgment", "route:p:t1:created:1")["error"] == "JEV_OUTCOME_UNKNOWN_AFTER_RESTART"
    assert out.decision.startswith("assign: worker (reasoning turn")


def test_hash_safety_is_checked_before_a_card_is_shown():
    base = {"title": "T", "objective": "O", "doneWhen": "D", "owner": "o", "goals": [{"goalId": "g1", "statement": "S", "doneWhen": "G"}], "reservedActions": []}
    assert validate_card(base) == []
    assert any("hash parity" in p for p in validate_card({**base, "weight": 1.5}))
    assert any("hash parity" in p for p in validate_card({**base, "extra": {"10": "x"}}))
