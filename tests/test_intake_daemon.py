"""Intake and control through the daemon, against a real Teletraan (C1-C3, K4, C4 approve)."""

from __future__ import annotations

import json

from cos.bot import Bot, Inbound
from cos.daemon import Daemon
from cos.intake import Intake, stated_done_when
from cos.reasoning import ReasoningUnavailable
from cos.routing import Router, route_pending
from cos.safety import CircuitBreaker, CycleCaps, ShutdownHandler
from tests.conftest import MATTHEW
from tests.test_routing import FakeJev, FakeReasoner, add_task, config, grant_project


class ScriptedReasoner:
    """Answers by which system prompt it sees: classify, card, decompose."""

    def __init__(self, label="new_objective", question=None, decompose=True):
        self.label, self.question, self.decompose_ok, self.prompts = label, question, decompose, []

    def complete_json(self, system, user, max_tokens=None):
        self.prompts.append(system[:20])
        if system.startswith("You triage"):
            return {"label": self.label, "question": self.question, "title": "Build the thing"}
        if system.startswith("Draft a project card"):
            return {"title": "Build the thing", "objective": "Build it well", "doneWhen": "A reviewer can run it", "scopeIn": ["code"], "scopeOut": ["publishing"],
                    "goals": [{"goalId": "g1", "statement": "Write code", "doneWhen": "Tests pass"}, {"goalId": "g2", "statement": "Write docs", "doneWhen": "README exists"}],
                    "owner": "owner", "kill": "Matthew cancels"}
        if not self.decompose_ok:
            raise ReasoningUnavailable("down")
        return {"tasks": [{"goalId": "g1", "title": "Code", "objective": "Write code", "acceptance": "Tests pass", "dependsOn": []},
                          {"goalId": "g2", "title": "Docs", "objective": "Write docs", "acceptance": "README exists", "dependsOn": [0]},
                          {"goalId": "not-a-goal", "title": "Rogue", "objective": "x", "acceptance": "x", "dependsOn": []}]}


class FakeTransport:
    def __init__(self):
        self.sent = []

    def __call__(self, method, payload):
        if method == "sendMessage":
            self.sent.append(payload)
            return {"message_id": len(self.sent)}
        return True


def daemon(work, store, reasoner):
    transport = FakeTransport()
    bot = Bot(transport, MATTHEW, (MATTHEW,))
    send = lambda text, buttons=None: bot.send(text, buttons)  # noqa: E731
    cfg = config()
    breaker = CircuitBreaker(store)
    router = Router(work["cos"], cfg, store, breaker, CycleCaps(store, 900), notify=send, jev_post=FakeJev(), reasoner_factory=lambda c, h: FakeReasoner("worker"))
    d = Daemon(cfg, work["cos"], store, bot, Intake(work["cos"], store, reasoner, send, cfg), router, breaker, ShutdownHandler())
    return d, transport


def text(t, mid="1", who=MATTHEW):
    return Inbound("text", t, who, MATTHEW, mid)


def button(data, who=MATTHEW):
    return Inbound("callback", data, who, MATTHEW, "99", callback_id="cb")


def yes_button(transport):
    return next(b for m in transport.sent if "reply_markup" in m for row in m["reply_markup"]["inline_keyboard"] for b in row if b["callback_data"].startswith("yes:"))["callback_data"]


def test_c1_idea_question_card_yes_creates_a_granted_decomposed_project(work, store):
    d, transport = daemon(work, store, ScriptedReasoner(question="Which repo?"))
    d.handle(text("build the thing"))
    assert "One question" in transport.sent[-1]["text"]
    assert work["operator"].snapshot()["project"] == [], "nothing exists before the yes"
    d.handle(text("the metroplex repo", mid="2"))
    card_msg = transport.sent[-1]
    assert card_msg["text"].startswith("CARD: Build the thing") and "Nothing runs before you tap it" in card_msg["text"]
    d.handle(button(yes_button(transport)))
    snap = work["operator"].snapshot()
    [project] = snap["project"]
    assert project["approvalId"].startswith("approval-card-") and [g["goalId"] for g in project["goals"]] == ["g1", "g2"]
    tasks = sorted(snap["task"], key=lambda t: t["id"])
    assert [t["title"] for t in tasks] == ["Code", "Docs"], "a task citing a goal not on the card is never created"
    assert all(t["spec"]["cardGoalId"] in ("g1", "g2") and t["spec"]["checkpoints"] for t in tasks)
    assert tasks[1]["dependencies"] == [tasks[0]["id"]]
    assert "Granted" in transport.sent[-1]["text"]


class WorkerOwnerReasoner(ScriptedReasoner):
    """Reproduces 2026-09-26: the model names a worker as card owner."""

    def complete_json(self, system, user, max_tokens=None):
        out = super().complete_json(system, user, max_tokens)
        if system.startswith("Draft a project card"):
            self.card_prompt = user
            out["owner"] = "kup"
        return out


def test_a_worker_never_owns_a_card_even_when_the_model_names_it(work, store):
    work.cmd("operator", "agent.create", "kup", {"name": "Kup", "reportsTo": "owner", "capabilities": ["code"]})
    reasoner = WorkerOwnerReasoner()
    d, transport = daemon(work, store, reasoner)
    d.handle(text("build the thing"))
    assert '"kup"' not in reasoner.card_prompt, "workers are not offered to the model as owners"
    assert "Owner: kup" not in transport.sent[-1]["text"]
    d.handle(button(yes_button(transport)))
    [project] = work["operator"].snapshot()["project"]
    assert project["owner"] != "kup"


def test_decompose_outage_falls_back_to_one_task_per_goal(work, store):
    d, transport = daemon(work, store, ScriptedReasoner(decompose=False))
    d.handle(text("build the thing"))
    d.handle(button(yes_button(transport)))
    assert sorted(t["title"] for t in work["operator"].snapshot()["task"]) == ["Write code", "Write docs"]


def test_c2_drop_creates_nothing_and_a_later_yes_is_refused(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    card_id = yes_button(transport).split(":")[1]
    d.handle(button(f"drop:{card_id}"))
    d.handle(button(yes_button(transport)))
    assert work["operator"].snapshot()["project"] == []
    assert "no longer waiting" in transport.sent[-1]["text"]


def test_c3_stale_card_button_is_refused(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    _, card_id, _ = yes_button(transport).split(":")
    d.handle(button(f"yes:{card_id}:0000000000000000"))
    assert work["operator"].snapshot()["project"] == []
    assert "older version" in transport.sent[-1]["text"]


def test_c3_bot_drops_updates_from_anyone_but_matthew_in_his_chat():
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "text": "hi", "from": {"id": 666}, "chat": {"id": MATTHEW}}},
        {"update_id": 2, "message": {"message_id": 2, "text": "hi", "from": {"id": MATTHEW}, "chat": {"id": 555}}},
        {"update_id": 3, "callback_query": {"id": "c", "data": "yes:x:y", "from": {"id": 666}, "message": {"message_id": 3, "chat": {"id": MATTHEW}}}},
        {"update_id": 4, "message": {"message_id": 4, "text": "real", "from": {"id": MATTHEW}, "chat": {"id": MATTHEW}}},
    ]
    bot = Bot(lambda m, p: updates if m == "getUpdates" else True, MATTHEW, (MATTHEW,))
    items, offset = bot.poll(0)
    assert [i.text for i in items] == ["real"] and offset == 5


def test_note_is_filed_as_an_inert_proposal(work, store):
    d, transport = daemon(work, store, ScriptedReasoner(label="note"))
    d.handle(text("maybe someday: a thing"))
    snap = work["operator"].snapshot()
    assert len(snap["proposal"]) == 1 and snap["proposal"][0]["status"] == "inert"
    assert snap["task"] == [] and snap["project"] == []


def test_k4_pause_stops_new_work_and_resume_drains_once(work, store):
    grant_project(work)
    add_task(work, "t1")
    add_task(work, "t2")
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("/pause"))
    assert "Paused" in transport.sent[-1]["text"]
    assert route_pending(d.router, work["cos"])[0].decision == "hold: project paused"
    assert work["operator"].snapshot()["attempt"] == []
    d.handle(text("/resume"))
    unassigned = [w for w in work["operator"].snapshot()["wake"] if w["reason"] == "unassigned"]
    assert sorted(w["taskId"] for w in unassigned) == ["t1", "t2"]
    outs = route_pending(d.router, work["cos"])
    attempts = work["operator"].snapshot()["attempt"]
    # One worker, one live attempt per agent: t1 is assigned exactly once (its
    # held created wake and its resume wake do not double-assign) and t2 waits.
    assert [a["taskId"] for a in attempts] == ["t1"]
    assert any(o.decision.startswith("wait") for o in outs)
    assert all(w["acknowledged"] for w in work["operator"].snapshot()["wake"] if w["taskId"] == "t2")


def test_c4_reserved_action_approval_resumes_and_routes(work, store):
    grant_project(work)
    add_task(work, "ship", reservedAction="publish_push_deploy")
    d, transport = daemon(work, store, ScriptedReasoner())
    route_pending(d.router, work["cos"])
    assert work.get("task", "ship")["status"] == "blocked"
    data = transport.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    d.handle(button(data))
    assert work.get("task", "ship")["status"] == "ready"
    route_pending(d.router, work["cos"])
    assert work.get("attempt", "ship:ready:1:a1")["status"] == "queued"


def test_stop_pauses_then_requests_shutdown(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("/stop"))
    assert d.shutdown.requested and store.paused


def test_first_start_skips_the_telegram_backlog():
    calls = []
    def transport(method, payload):
        calls.append((method, payload))
        return [{"update_id": 41, "message": {"message_id": 1, "text": "old idea", "from": {"id": MATTHEW}, "chat": {"id": MATTHEW}}}] if method == "getUpdates" else True
    bot = Bot(transport, MATTHEW, (MATTHEW,))
    assert bot.skip_backlog()[0] == 42
    assert ("getUpdates", {"offset": -1, "timeout": 0}) in calls


# ---------------------------------------------------------------- Fix A (2026-10-02)
# Matthew wrote an explicit done-when and the classifier still asked about a
# fixture. A stated done-when is now a code-side backstop: no question.


OBSERVED = ("Add a read-only Teletraan view to the dashboard.\n"
            "Done when: node --test web/src/lib/teletraan-view.test.ts passes offline, npm run typecheck still passes, and the page renders the fixture.")


def test_stated_done_when_is_recognized_only_at_a_line_start_with_real_text():
    assert stated_done_when(OBSERVED).startswith("node --test web/src/lib/teletraan-view.test.ts passes offline")
    for stated in ("done-when: tests pass", "DoneWhen: x passes", "- Done when the README exists", "**Done when:** it renders", "Done when:\n- tests pass"):
        assert stated_done_when(stated), stated
    for not_stated in ("I will be done when I get home", "Done whenever you like", "Done when?", "Done when: ?", "done when ",
                       "review the transcripts from this video and create a verification loop"):
        assert stated_done_when(not_stated) is None, not_stated


class RecordingReasoner(ScriptedReasoner):
    def complete_json(self, system, user, max_tokens=None):
        if system.startswith("Draft a project card"):
            self.card_prompt = user
        return super().complete_json(system, user, max_tokens)


def test_a_stated_done_when_drafts_the_card_even_if_the_model_asks(work, store):
    reasoner = RecordingReasoner(question="Should the fixture snapshot be generated from a specific historical Teletraan state or a synthetic example?")
    d, transport = daemon(work, store, reasoner)
    d.handle(text(OBSERVED))
    assert not any("One question" in m["text"] for m in transport.sent), "a stated done-when is never second-guessed"
    assert transport.sent[-1]["text"].startswith("CARD: ") and store.get("open_question") is None
    assert "teletraan-view.test.ts passes offline" in json.loads(reasoner.card_prompt)["statedDoneWhen"]


def test_a_vague_idea_without_a_done_when_still_gets_one_question(work, store):
    d, transport = daemon(work, store, ScriptedReasoner(question="Which video do you mean? None was attached."))
    d.handle(text("review the transcripts from this video and create a verification loop"))
    assert transport.sent[-1]["text"].startswith("One question before I draft the card:")
    assert work["operator"].snapshot()["project"] == [] and store.get("open_question")
