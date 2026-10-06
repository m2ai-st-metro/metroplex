"""Card source binding (A) and writable scope (C), against a real Teletraan.

Design: docs/2026-10-05-card-source-binding-design.md. Plan agreed with Codex
(/checkmate, 4 rounds, 2026-10-06). Matthew's decisions: every Telegram objective
is filed as a proposal first; a task without its own scope gets the card's.
"""

from __future__ import annotations

import hashlib
import json

from cos.intake import AMENDMENT_HEADER, AMENDMENT_PRECEDENCE, Intake, _short
from cos.routing import route_pending
from cos.spec import card_digest, validate_card
from tests.test_intake_daemon import ScriptedReasoner, button, daemon, text, yes_button


def sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ScopedReasoner(ScriptedReasoner):
    """Drafts a scoped card; its done-when follows the LAST port the idea names,
    the way a model reads an amendment that wins. Decompose gives g1 a narrower
    scope, g2 a path outside the card, which must fall back to the card's."""

    def __init__(self, scope=("app/", "tests"), **kw):
        super().__init__(**kw)
        self.scope, self.users = list(scope), []

    def complete_json(self, system, user, max_tokens=None, schema=None):
        self.users.append(user)
        out = super().complete_json(system, user, max_tokens, schema)
        if system.startswith("Draft a project card"):
            idea = json.loads(user)["idea"]
            port = "8081" if "8081" in idea else "8080"
            out.update({"writableScope": self.scope, "doneWhen": f"It serves on port {port}", "objective": f"Serve the app on port {port}"})
        elif system.startswith("Break an approved project"):
            out["tasks"][0]["writableScope"] = ["app/api"]
            out["tasks"][1]["writableScope"] = ["docs"]
        return out


def last_yes(transport):
    return next(b for m in reversed(transport.sent) if "reply_markup" in m for row in m["reply_markup"]["inline_keyboard"] for b in row if b["callback_data"].startswith("yes:"))["callback_data"]


def proposals(work):
    return {p["id"]: p for p in work["operator"].snapshot()["proposal"]}


def test_a_telegram_objective_is_filed_first_and_the_card_binds_its_hash(work, store):
    d, transport = daemon(work, store, ScopedReasoner())
    d.handle(text("build the thing.\nObjective: Serve the app on port 8080\nDone when: it serves on port 8080"))
    [stored] = store.cards_in("awaiting_yes")
    card = stored["card"]
    proposal = proposals(work)[card["source"]["id"]]
    assert proposal["status"] == "inert" and card["source"] == {"kind": "proposal", "id": proposal["id"], "sha256": sha(proposal["body"])}
    assert card["writableScope"] == ["app", "tests"] and validate_card(card) == []
    shown = next(m["text"] for m in transport.sent if "reply_markup" in m)
    assert f"Source: {proposal['id']} (sha256 {card['source']['sha256'][:12]})" in shown and "Writable paths: app, tests" in shown


def test_c_grant_scopes_every_task_inside_the_card_and_routing_carries_it(work, store):
    d, transport = daemon(work, store, ScopedReasoner())
    d.handle(text("build the thing.\nObjective: Serve the app on port 8080\nDone when: it serves on port 8080"))
    d.handle(button(yes_button(transport)))
    snap = work["operator"].snapshot()
    [project] = snap["project"]
    assert project["card"]["writableScope"] == ["app", "tests"] and project["card"]["source"]["id"].startswith("Q-metroplex-")
    scopes = {t["title"]: t["spec"]["writableScope"] for t in snap["task"]}
    assert scopes == {"Code": ["app/api"], "Docs": ["app", "tests"]}, "an out-of-card path falls back to the card scope"
    decompose_user = json.loads(d.intake.reasoner.users[-1])
    assert decompose_user["source"] == proposals(work)[project["card"]["source"]["id"]]["body"], "decompose sees the approved source"
    work.cmd("owner", "project.member", project["id"], {"agentId": "worker"})
    sent, real = [], work["cos"].command
    work["cos"].command = lambda op, *a, **k: (sent.append((op, a)), real(op, *a, **k))[1]
    route_pending(d.router, work["cos"])
    created = [a[2] for op, a in sent if op == "contribution.create"]
    assert created and all(p["writableScope"] == next(t["spec"]["writableScope"] for t in snap["task"] if t["id"] == p["taskId"]) for p in created), "Metroplex sends the task scope itself"
    contributions = {c["taskId"]: c["writableScope"] for c in work["operator"].snapshot()["contribution"]}
    by_id = {t["id"]: t["spec"]["writableScope"] for t in snap["task"]}
    assert contributions and all(contributions[tid] == by_id[tid] for tid in contributions)


def test_a_an_edit_that_changes_a_stated_requirement_binds_one_effective_source(work, store):
    d, transport = daemon(work, store, ScopedReasoner())
    d.handle(text("build the thing.\nObjective: Serve the app on port 8080\nDone when: it serves on port 8080"))
    edit = next(b for m in transport.sent if "reply_markup" in m for row in m["reply_markup"]["inline_keyboard"] for b in row if b["callback_data"].startswith("edit:"))
    d.handle(button(edit["callback_data"]))
    d.handle(text("use port 8081 instead", mid="7"))
    [stored] = store.cards_in("awaiting_yes")
    card = stored["card"]
    effective = proposals(work)[card["source"]["id"]]
    body = effective["body"]
    assert "port 8080" in body and AMENDMENT_HEADER in body and "use port 8081 instead" in body and body.rstrip().endswith(AMENDMENT_PRECEDENCE)
    assert card["source"]["sha256"] == sha(body)
    assert card["doneWhen"] == "It serves on port 8081", "the amended requirement is not overwritten by the stated 8080"
    assert card["objective"] == "Serve the app on port 8081", "no stated field is carried verbatim from an amended source"
    d.handle(button(last_yes(transport)))
    assert work["operator"].snapshot()["project"][0]["card"]["source"] == card["source"]


def test_a_recovery_through_card_command_keeps_the_amendment(work, store):
    d, transport = daemon(work, store, ScopedReasoner())
    d.handle(text("build the thing.\nObjective: Serve the app on port 8080\nDone when: it serves on port 8080"))
    [first] = store.cards_in("awaiting_yes")
    d.intake.edit(first["card_id"])
    # The effective proposal was filed, then drafting was interrupted; it is
    # redrafted later by id, with no answer argument on that path.
    effective_id, body = d.intake._amend(first["card_id"], "use port 8081 instead", "8")
    d.handle(text(f"card {effective_id}", mid="9"))
    card = store.get_card(f"card-{_short(effective_id)}")["card"]
    assert card["doneWhen"] == "It serves on port 8081", "no verbatim carry of the superseded 8080"
    assert card["objective"] == "Serve the app on port 8081"
    assert card["source"] == {"kind": "proposal", "id": effective_id, "sha256": sha(body)}
    d.handle(button(last_yes(transport)))
    tasks = work["operator"].snapshot()["task"]
    assert tasks and all("8080" not in t["acceptance"] for t in tasks)


def test_a_a_retried_amendment_binds_the_first_stored_body(work, store):
    d, _ = daemon(work, store, ScopedReasoner())
    d.handle(text("build the thing.\nObjective: Serve the app on port 8080\nDone when: it serves on port 8080"))
    [stored] = store.cards_in("awaiting_yes")
    clock = iter([1_000_000.0, 2_000_000.0])
    d.intake.clock = lambda: next(clock)
    base = store.get(f"card_source:{stored['card_id']}")
    first_id, first_body = d.intake._amend(stored["card_id"], "use port 8081 instead", "5")
    store.set(f"card_source:{stored['card_id']}", base)  # interrupted before the card moved on
    again_id, again_body = d.intake._amend(stored["card_id"], "use port 8081 instead", "5")
    assert (again_id, again_body) == (first_id, first_body), "same id, the first filing's body"
    assert "1970-01-12" in again_body, "the retry's new timestamp did not replace the stored body"


def test_a_a_legacy_drafting_card_is_filed_before_it_is_redrafted(work, store):
    d, _ = daemon(work, store, ScopedReasoner())
    store.put_card("card-legacy", {"title": "Old idea"}, "", "drafting", source="an old idea from before binding")
    d.intake.card_command("card-legacy")
    card = store.get_card("card-legacy")["card"]
    proposal = proposals(work)[card["source"]["id"]]
    assert proposal["body"] == "an old idea from before binding" and card["source"]["sha256"] == sha(proposal["body"])


def test_c_scope_is_normalized_and_never_left_empty():
    agents = [{"id": "owner"}]
    base = {"title": "t", "objective": "o", "doneWhen": "d", "goals": [{"goalId": "g1", "statement": "s", "doneWhen": "d"}], "owner": "owner"}
    assert Intake._normalize({**base, "writableScope": ["app/", "/etc", "../up", "app", "  ", 3]}, agents)["writableScope"] == ["app"]
    assert Intake._normalize({**base, "writableScope": []}, agents)["writableScope"] == ["."]
    assert Intake._normalize({**base, "writableScope": ["app", "."]}, agents)["writableScope"] == ["."]
    card = {"writableScope": ["app", "tests"]}
    assert Intake._task_scope(["app/api/"], card) == ["app/api"]
    assert Intake._task_scope(["app/api", "docs"], card) == ["app", "tests"], "one path outside falls back whole"
    assert Intake._task_scope([], card) == ["app", "tests"]
    assert Intake._task_scope(["app"], {}) is None, "legacy cards keep tasks unscoped"


def test_c_render_shows_every_new_hashed_field_and_digest_covers_them():
    card = {"title": "t", "objective": "o", "doneWhen": "d", "owner": "owner", "goals": [{"goalId": "g1", "statement": "s", "doneWhen": "d"}],
            "reservedActions": [], "kill": "k", "writableScope": ["."], "source": {"kind": "proposal", "id": "Q-1", "sha256": "a" * 64}}
    shown = Intake.render(card)
    assert "Writable paths: . (whole repository)" in shown and "Source: Q-1 (sha256 aaaaaaaaaaaa)" in shown
    assert card_digest(card) != card_digest({**card, "writableScope": ["app"]}) != card_digest({**card, "source": {**card["source"], "id": "Q-2"}})
