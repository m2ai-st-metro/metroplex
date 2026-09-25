"""Routing turns against a real Teletraan work service (J1-J3, J5, AE5, AE6/K5, C4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cos.config import Config
from cos.reasoning import NoReasoner, ReasoningUnavailable
from cos.routing import Router, route_pending
from cos.safety import CircuitBreaker, CycleCaps
from cos.spec import card_digest
from tests.conftest import MATTHEW


def config(**overrides) -> Config:
    base = dict(data_dir=Path("/tmp"), work_socket=Path("/tmp/x"), cos_token_file=Path("/tmp/x"), bot_token=None, approver_ids=(MATTHEW,), chat_id=MATTHEW,
                typesafe_api_key="test-key", jev_model="jev-latest", local_base_url=None, local_model=None,
                env="pilot", wake_poll_s=60, sweep_s=300)
    base.update(overrides)
    return Config(**base)


def card(hosted=True, reserved=("publish_push_deploy",)):
    return {"title": "Pilot", "objective": "Deliver the pilot", "doneWhen": "Reviewer runs it", "owner": "owner",
            "goals": [{"goalId": "g1", "statement": "Build", "doneWhen": "Tests pass"}], "reservedActions": list(reserved), "hostedAllowed": hosted, "humanOnly": False}


def spec(**o):
    s = {"cardGoalId": "g1", "doneWhen": "tests attached", "checkpoints": [{"name": "result", "evidenceExpected": "diff"}], "qualityChecks": [], "reservedAction": None}
    s.update(o)
    return s


def grant_project(work, pid="p", hosted=True):
    c = card(hosted=hosted)
    work.cmd("cos", "approval.record", f"ap-{pid}", {"scope": "project", "cardHash": card_digest(c), "chatId": MATTHEW, "fromId": MATTHEW, "messageId": "1"})
    work.cmd("cos", "project.create", pid, {"card": c, "approvalId": f"ap-{pid}"})
    work.cmd("owner", "project.member", pid, {"agentId": "worker"})


def add_task(work, tid, pid="p", **spec_overrides):
    work.cmd("cos", "task.create", tid, {"projectId": pid, "title": tid, "objective": f"do {tid}", "acceptance": "evidence", "owner": "owner", "spec": spec(**spec_overrides)})


def jev_answer(choice="worker", confidence=0.9, ready=0.9, labels=("owner", "worker", "wait", "none_fit")):
    rest = (1 - confidence) / (len(labels) - 1)
    return {"model": "jev-latest", "answers": {
        "route": {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": {label: (confidence if label == choice else rest) for label in labels}},
        "ready": {"type": "noul", "noul": ready}}, "usage": {"input_tokens": 100}}


class FakeJev:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __call__(self, key, request):
        self.calls.append(request)
        if self.error:
            raise RuntimeError(self.error)
        labels = tuple(request["questions"]["route"]["criteria"])
        r = self.response or jev_answer(labels=labels)
        if set(r["answers"]["route"]["probabilities"]) != set(labels):
            r = jev_answer(choice=r["answers"]["route"]["choice"], confidence=r["answers"]["route"]["confidence"], ready=r["answers"]["ready"]["noul"], labels=labels)
        return r


class FakeReasoner:
    def __init__(self, choice="worker"):
        self.choice, self.calls = choice, 0

    def complete_json(self, system, user):
        self.calls += 1
        if self.choice is None:
            raise ReasoningUnavailable("down")
        return {"choice": self.choice, "reason": "fits"}


def router(work, store, jev=None, reasoner=None, **cfg):
    notes = []
    r = Router(work["cos"], config(**cfg), store, CircuitBreaker(store), CycleCaps(store, 900), notify=lambda text, buttons=None: notes.append((text, buttons)) or "sent",
               jev_post=jev or FakeJev(), reasoner_factory=lambda c, hosted: reasoner or NoReasoner())
    r.hosted_seen = []
    return r, notes


def test_j1_confident_jev_assigns_a_contributor_and_links_the_judgment(work, store):
    grant_project(work)
    add_task(work, "t1")
    jev = FakeJev()
    r, _ = router(work, store, jev=jev)
    [out] = route_pending(r, work["cos"])
    assert out.decision.startswith("assign: worker")
    wake = work.get("wake", "p:t1:created")
    assert wake["acknowledged"] and wake["decision"].startswith("assign: worker")
    judgment = work.get("judgment", "route:p:t1:created:1")
    assert judgment["status"] == "judged" and judgment["questionVersion"] == "route-v1"
    contribution = work.get("contribution", "t1:created:1")
    assert contribution["worker"] == "worker" and contribution["judgmentId"] == "route:p:t1:created:1"
    attempt = work.get("attempt", "t1:created:1:a1")
    assert attempt["status"] == "queued" and attempt["runtime"] == "api"
    assert work.get("task", "t1")["owner"] == "owner", "routing never changes the accountable owner"
    assert len(jev.calls) == 1


def test_j2_low_confidence_falls_back_to_a_reasoning_turn(work, store):
    grant_project(work)
    add_task(work, "t1")
    reasoner = FakeReasoner("worker")
    r, _ = router(work, store, jev=FakeJev(jev_answer(confidence=0.41)), reasoner=reasoner)
    [out] = route_pending(r, work["cos"])
    assert work.get("judgment", "route:p:t1:created:1")["status"] == "needs_review"
    assert reasoner.calls == 1 and out.decision.startswith("assign: worker (reasoning turn")


def test_j3_jev_outage_is_recorded_once_and_routing_still_decides(work, store):
    grant_project(work)
    add_task(work, "t1")
    jev = FakeJev(error="JEV_HTTP_503")
    r, _ = router(work, store, jev=jev, reasoner=FakeReasoner("worker"))
    [out] = route_pending(r, work["cos"])
    j = work.get("judgment", "route:p:t1:created:1")
    assert j["status"] == "unavailable" and j["error"] == "JEV_HTTP_503"
    assert len(jev.calls) == 1, "no automatic retry: usage may already be incurred"
    assert out.decision.startswith("assign: worker")


def test_j5_hosted_disallowed_never_calls_jev(work, store):
    grant_project(work, hosted=False)
    add_task(work, "t1")
    jev = FakeJev()
    r, _ = router(work, store, jev=jev, reasoner=FakeReasoner("worker"))
    [out] = route_pending(r, work["cos"])
    assert jev.calls == [] and out.decision.startswith("assign: worker")


def test_no_verdict_and_no_reasoning_waits_instead_of_guessing(work, store):
    grant_project(work)
    add_task(work, "t1")
    r, _ = router(work, store, jev=FakeJev(error="JEV_HTTP_503"), reasoner=FakeReasoner(None))
    [out] = route_pending(r, work["cos"])
    assert out.decision.startswith("wait: no Jev verdict")
    assert work.get("contribution", "t1:created:1") is None


def test_ae6_crash_between_contribution_and_queue_recovers_without_a_duplicate(work, store):
    grant_project(work)
    add_task(work, "t1")
    r, _ = router(work, store)
    real = r._cmd
    def crash_on_queue(out, op, *a, **k):
        if op == "attempt.queue":
            raise SystemExit("killed")
        return real(out, op, *a, **k)
    r._cmd = crash_on_queue
    with pytest.raises(SystemExit):
        route_pending(r, work["cos"])
    assert work.get("contribution", "t1:created:1") is not None and work.get("attempt", "t1:created:1:a1") is None
    jev = FakeJev(jev_answer(choice="owner"))  # a different answer after restart must not matter
    r2, _ = router(work, store, jev=jev)
    [out] = route_pending(r2, work["cos"])
    snap = work["operator"].snapshot()
    assert [c["id"] for c in snap["contribution"]] == ["t1:created:1"]
    assert [a["id"] for a in snap["attempt"]] == ["t1:created:1:a1"]
    assert jev.calls == [], "the recorded judgment is reused, not paid for twice"
    assert out.decision.startswith("assign: worker (resumed after restart")


def test_none_fit_escalates_once_and_unrelated_work_continues(work, store):
    grant_project(work)
    add_task(work, "t1")
    add_task(work, "t2")
    answers = {"t1": "none_fit", "t2": "worker"}
    class PerTask(FakeJev):
        def __call__(self, key, request):
            self.response = jev_answer(choice=answers[request["state"]["task"]["title"]])
            return super().__call__(key, request)
    r, notes = router(work, store, jev=PerTask())
    outs = route_pending(r, work["cos"])
    assert [o.decision.split(":")[0] for o in outs] == ["escalate", "assign"]
    t1 = work.get("task", "t1")
    assert t1["status"] == "blocked" and t1["reason"].startswith("intent:")
    assert len(notes) == 1 and "Needs you (intent)" in notes[0][0]
    assert work.get("attempt", "t2:created:1:a1")["status"] == "queued"


def test_c4_reserved_action_blocks_with_an_approve_button_before_any_agent_starts(work, store):
    grant_project(work)
    add_task(work, "ship", reservedAction="publish_push_deploy")
    jev = FakeJev()
    r, notes = router(work, store, jev=jev)
    [out] = route_pending(r, work["cos"])
    assert out.decision.startswith("escalate: reserved")
    assert jev.calls == [] and work.get("attempt", "ship:created:1:a1") is None
    buttons = notes[0][1]
    data = buttons[0][0]["callback_data"]
    assert data.startswith("r:") and len(data.encode()) <= 64, "Telegram caps callback data at 64 bytes"
    assert store.get(f"cb:{data[2:]}") == {"taskId": "ship", "scopeRevision": 1}, "the token maps to task and revision, never to an action"


def test_paused_project_holds_its_wakes_unacknowledged(work, store):
    grant_project(work)
    add_task(work, "t1")
    work.cmd("cos", "project.pause", "p", {"reason": "/pause"})
    r, _ = router(work, store)
    [out] = route_pending(r, work["cos"])
    assert out.decision == "hold: project paused" and not work.get("wake", "p:t1:created")["acknowledged"]
