"""Stated card fields (2026-10-06): a live run of the refiled side-walk proposal
through real Qwen kept the done-when (enforced in code since #18) but in 3 of 6
drafts paraphrased the rest: the repo path dropped out of scopeIn, 'original
attachments' out of scopeOut, and once the app's name out of the objective.
The proposal states those fields; the card now carries them as written, the
same way it carries a stated done-when. The model fills only what is unstated.

Same live run: one proposal produced no card at all (REASONING_NOT_JSON, a
backtick-quoted string copied from the proposal's markdown), and a long card
would have been cut at 4000 characters by Bot.send while the Yes still
approved the whole hash."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from cos.bot import Bot
from cos.intake import (
    CARD_SCHEMA,
    DECOMPOSE_SCHEMA,
    _clip,
    stated_done_when,
    stated_fields,
)
from cos.reasoning import OpenAICompatibleReasoner
from tests.test_intake_daemon import (
    FakeTransport,
    ScriptedReasoner,
    button,
    daemon,
    text,
    yes_button,
)

SIDEWALK = (Path(__file__).parent / "fixtures" / "sidewalk_proposal.md").read_text()
SIDEWALK_TITLE = "Where the side-walk returned: live Digital Maker-style production app (Devastator pilot)"


# ---------------------------------------------------------------- parsing


def test_the_sidewalk_proposal_states_every_card_field():
    f = stated_fields(SIDEWALK)
    assert f["title"] == SIDEWALK_TITLE
    assert f["objective"].startswith("Deliver Where the side-walk returned as a persistent interactive application")
    assert f["objective"].endswith("stays portable across native AI tools.")
    assert "\n" not in f["objective"], "wrapped prose is joined into one paragraph"
    assert len(f["goals"]) == 3
    assert f["goals"][0].startswith("Build on the existing reviewed kit result as the app's base")
    assert "p-devastator-offline-film--0734 with its task owner; this card neither accepts" in f["goals"][0]
    assert f["goals"][2].endswith("handoff/restart/backup instructions.")
    [scope_in] = f["scopeIn"]
    assert "/home/apexaipc/projects/research/devastator;" in scope_in and "Isolated assigned checkout only." in scope_in
    assert "original attachments, or historical frozen evidence" in scope_in
    [scope_out] = f["scopeOut"]
    assert scope_out.startswith("a rendered movie, Flow generation") and scope_out.endswith("a separate creative/media deliverable.")
    assert "Architecture" not in json.dumps(f), "an unlabeled paragraph is not swallowed by the one before it"


def test_list_and_inline_forms():
    f = stated_fields("Objective: ship it\n\nOut of scope:\n- billing\n- auth rewrite\n  across services\n\nIn scope: the CLI only\nGoal 1: parser\nGoal 2: docs\n")
    assert f["objective"] == "ship it"
    assert f["scopeOut"] == ["billing", "auth rewrite across services"]
    assert f["scopeIn"] == ["the CLI only"], "a following label ends the block even without a blank line"
    assert f["goals"] == ["parser", "docs"]
    assert stated_fields("**Scope out:** nothing paid\n")["scopeOut"] == ["nothing paid"]
    assert stated_fields("Writable scope:\n\nsrc/ and tests/\n")["scopeIn"] == ["src/ and tests/"]


def test_unstated_fields_are_absent_and_mentions_are_not_labels():
    assert stated_fields("build the thing, done when tests pass") == {}
    assert stated_fields("My objective is speed. The title of the post: TBD") == {}, "line start only"
    assert stated_fields("Objective:\n?") == {}, "a bare label is not a statement"
    assert stated_fields("Goal 2: second\nGoal 1: first")["goals"] == ["first", "second"], "ordered by number"


# ---------------------------------------------------------------- the card carries them


class ParaphrasingCard(ScriptedReasoner):
    """Card drafts that paraphrase stated fields, as Qwen did on 2026-10-06."""

    def __init__(self, goals=3, **kw):
        super().__init__(**kw)
        self.goal_count, self.schemas = goals, {}

    def complete_json(self, system, user, max_tokens=None, schema=None):
        self.schemas[system[:20]] = schema
        if system.startswith("Draft a project card"):
            return {"title": "Telegram Mini App Contract & Evidence Verification", "objective": "Deliver a persistent app from Surface.",
                    "doneWhen": "It works", "scopeIn": ["Additive app source code"], "scopeOut": ["Rendered movie"],
                    "goals": [{"goalId": f"g{i + 1}", "statement": f"paraphrase {i + 1}", "doneWhen": f"model done {i + 1}"} for i in range(self.goal_count)],
                    "owner": "owner", "kill": "Matthew cancels"}
        return super().complete_json(system, user, max_tokens)


def _card(work, store, reasoner, body=SIDEWALK, title="Proposal title"):
    work["cos"].command("proposal.file", "Q-test", 0, {"title": title, "body": body, "source": {"type": "test", "ref": "t"}})
    d, transport = daemon(work, store, reasoner)
    d.handle(text("card Q-test"))
    [stored] = store.cards_in("awaiting_yes")
    return d, transport, stored["card"]


def test_a_card_carries_the_stated_fields_not_the_models_paraphrase(work, store):
    _, transport, card = _card(work, store, ParaphrasingCard())
    f = stated_fields(SIDEWALK)
    assert card["title"] == _clip(SIDEWALK_TITLE, 80) and "Where the side-walk returned" in card["title"]
    assert card["objective"] == f["objective"]
    assert [g["statement"] for g in card["goals"]] == f["goals"]
    assert [g["doneWhen"] for g in card["goals"]] == ["model done 1", "model done 2", "model done 3"], "per-goal done-when stays the model's"
    assert card["scopeIn"] == f["scopeIn"] and card["scopeOut"] == f["scopeOut"]
    assert card["doneWhen"] == stated_done_when(SIDEWALK)
    shown = "".join(m["text"] for m in transport.sent if m["text"].startswith("CARD:") or "reply_markup" in m)
    assert "/home/apexaipc/projects/research/devastator;" in shown and "neither accepts" in shown


def test_stated_goals_win_even_when_the_model_returns_fewer(work, store):
    _, _, card = _card(work, store, ParaphrasingCard(goals=2))
    assert [g["goalId"] for g in card["goals"]] == ["g1", "g2", "g3"]
    assert card["goals"][2]["statement"] == stated_fields(SIDEWALK)["goals"][2]
    assert card["goals"][2]["doneWhen"], "a goal the model skipped still gets a non-empty done-when"


def test_a_proposal_without_a_title_line_keeps_the_proposal_title(work, store):
    _, _, card = _card(work, store, ParaphrasingCard(), body="Objective: run the parser\nDone when: tests pass", title="Prompt Pocket v2 execution: tap-to-run prompts")
    assert card["title"] == "Prompt Pocket v2 execution: tap-to-run prompts"


def test_an_idea_without_stated_fields_is_still_drafted_by_the_model(work, store):
    reasoner = ParaphrasingCard()
    d, _ = daemon(work, store, reasoner)
    d.handle(text("build the parser, done when tests pass"))
    [stored] = store.cards_in("awaiting_yes")
    assert stored["card"]["objective"] == "Deliver a persistent app from Surface."
    assert stored["card"]["title"] == "Telegram Mini App Contract & Evidence Verification"


def test_card_and_decompose_calls_are_grammar_constrained(work, store):
    reasoner = ParaphrasingCard()
    d, transport, _ = _card(work, store, reasoner)
    d.handle(button(yes_button(transport)))
    assert reasoner.schemas["Draft a project card"] == CARD_SCHEMA
    assert reasoner.schemas["Break an approved pr"] == DECOMPOSE_SCHEMA


# ---------------------------------------------------------------- reasoner and bot


def test_the_reasoner_sends_the_schema_as_llama_json_schema(monkeypatch):
    seen = {}

    class FakeCompletions:
        def create(self, **kw):
            seen.update(kw)
            msg = types.SimpleNamespace(content='{"ok": true}')
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    r = OpenAICompatibleReasoner("http://m5/v1", "local", "qwen")
    assert r.complete_json("s", "u", max_tokens=10, schema={"type": "object"}) == {"ok": True}
    assert seen["extra_body"]["json_schema"] == {"type": "object"}
    assert seen["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    r.complete_json("s", "u", max_tokens=10)
    assert "json_schema" not in seen["extra_body"], "unconstrained calls stay unconstrained"


def test_a_long_message_is_split_never_cut_and_buttons_ride_the_last_part():
    transport = FakeTransport()
    bot = Bot(transport, "1", ("1",))
    long = "\n".join(f"line {i:04d} " + "x" * 80 for i in range(100))
    buttons = [[{"text": "Yes", "callback_data": "yes:c:h"}]]
    bot.send(long, buttons)
    parts = transport.sent
    assert len(parts) >= 3 and all(len(p["text"]) <= 4000 for p in parts)
    assert "\n".join(p["text"] for p in parts) == long, "nothing is dropped"
    assert ["reply_markup" in p for p in parts] == [False] * (len(parts) - 1) + [True]
    bot.send("short")
    assert transport.sent[-1]["text"] == "short"
