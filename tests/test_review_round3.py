"""Regression tests for the round-3 re-review (2026-09-25)."""

from __future__ import annotations

import time

from cos.config import Thresholds
from cos.motion import sweep
from cos.routing import route_pending
from cos.teletraan_client import WorkError
from tests.test_intake_daemon import ScriptedReasoner, button, daemon, text, yes_button
from tests.test_routing import FakeJev, add_task, grant_project, router

T = Thresholds()


def test_r1_failed_rework_on_the_same_contribution_is_routed_again(work, store):
    grant_project(work)
    add_task(work, "t1")
    r, _notes = router(work, store, jev=FakeJev())
    route_pending(r, work["cos"])
    a1 = "t1:created:1:a1"
    work.cmd("runtime", "attempt.started", a1, {"receipt": {"id": "r"}}, kind="attempt")
    work.cmd("worker", "attempt.result", a1, {"result": "v1"}, kind="attempt")
    route_pending(r, work["cos"])
    # Owner asks for rework on the same contribution; the rework fails.
    work.cmd("owner", "attempt.queue", "rw1", {"contributionId": "t1:created:1", "runtime": "api"})
    work.cmd("runtime", "attempt.started", "rw1", {"receipt": {"id": "r2"}}, kind="attempt")
    work.cmd("runtime", "attempt.failed", "rw1", {"observedStopped": True, "error": "boom"}, kind="attempt")
    outs = route_pending(r, work["cos"])
    assert not any(o.decision.startswith("await") for o in outs), "the latest attempt failed; nothing is awaiting review"


def test_r1_overdue_owner_review_is_noticed_once(work, store):
    grant_project(work)
    add_task(work, "t1")
    r, _ = router(work, store, jev=FakeJev())
    route_pending(r, work["cos"])
    a1 = "t1:created:1:a1"
    work.cmd("runtime", "attempt.started", a1, {"receipt": {"id": "r"}}, kind="attempt")
    work.cmd("worker", "attempt.result", a1, {"result": "v1"}, kind="attempt")
    route_pending(r, work["cos"])
    sent = []
    later = time.time() + 25 * 3600
    sweep(work["cos"], store, T, 5, lambda text, b=None: sent.append(text) or "ok", now=later)
    sweep(work["cos"], store, T, 5, lambda text, b=None: sent.append(text) or "ok", now=later + 600)
    assert len(sent) == 1 and "awaiting owner review" in sent[0]


def test_r2_retry_after_rejected_project_create_replays_the_first_attestation(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    yes = yes_button(transport)
    real = work.clients["cos"].command
    def reject_project(op, *a, **k):
        if op == "project.create":
            raise WorkError("AGENT_UNAVAILABLE", command_rejected=True)
        return real(op, *a, **k)
    work.clients["cos"].command = reject_project
    d.handle(button(yes))
    work.clients["cos"].command = real
    card_id = yes.split(":")[1]
    assert store.get_card(card_id)["status"] == "awaiting_yes"
    tap = button(yes)
    tap.message_id = "a-different-message"
    d.handle(tap)
    assert store.get_card(card_id)["status"] == "granted", "same card version, same attestation, replay succeeds"


def test_r2_a_stuck_granting_card_without_a_project_can_be_dropped(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    card_id = yes_button(transport).split(":")[1]
    store.set_card_status(card_id, "granting", "p-never-created")
    d.handle(button(f"drop:{card_id}"))
    assert store.get_card(card_id)["status"] == "dropped"


def test_r3_card_command_accepts_any_known_proposal_id(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    work.cmd("worker", "proposal.file", "q1", {"title": "Idea from another tool", "body": "build the thing", "source": {"type": "quick-capture", "ref": "quick:x"}})
    d.handle(text("card q1"))
    assert transport.sent[-1]["text"].startswith("CARD: ")
    [card] = store.cards_in("awaiting_yes")
    assert card["source"] == "build the thing", "the card was drafted from the proposal, not from the words 'card q1'"


def test_r4_control_commands_do_not_close_the_intake_breaker_and_locked_buttons_are_answered(work, store):
    d, _transport = daemon(work, store, ScriptedReasoner())
    for _ in range(3):
        d.breaker.record_failure("intake")
    d.handle(text("/status"))
    assert d.breaker.is_open("intake")
    answered = []
    d.bot.answer = lambda cid, t: answered.append(t)
    d.handle(button("yes:card-00000000:0000"))
    assert answered and "paused" in answered[0]


def test_r6_granting_card_without_stored_attestation_asks_for_a_new_yes(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    card_id = yes_button(transport).split(":")[1]
    store.set_card_status(card_id, "granting", "p-x")
    d.intake.resume_granting()
    assert store.get_card(card_id)["status"] == "awaiting_yes"
    assert "tap Yes again" in transport.sent[-1]["text"]


def test_q1_a_rejected_first_yes_is_not_replayed_forever(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    yes = yes_button(transport)
    card_id = yes.split(":")[1]
    first = button(yes, who="7001")
    first.from_id = "9999"  # an id Metroplex lists but Teletraan does not
    d.bot.approvers.add("9999")
    d.handle(first)
    assert store.get_card(card_id)["status"] == "awaiting_yes"
    d.handle(button(yes))  # Matthew's real yes
    assert store.get_card(card_id)["status"] == "granted"


def test_q2_control_command_failures_do_not_lock_intake(work, store):
    d, _transport = daemon(work, store, ScriptedReasoner())
    real = work.clients["cos"].wake_pending
    work.clients["cos"].wake_pending = lambda: (_ for _ in ()).throw(WorkError("SOCKET_DOWN"))
    for _ in range(3):
        d.handle(text("/status"))
    work.clients["cos"].wake_pending = real
    assert not d.breaker.is_open("intake")
