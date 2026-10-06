"""Card fidelity (2026-10-05): proposal Q-20261005-045218-sidewalk-app-card became
card-cc97709c with 4 of its 5 done-when bullets dropped, a mid-word title
('...(Devastato') and a build task tagged external_contact because
'mechanisms' contains 'sms'. The fixture is that proposal's body, verbatim."""

from __future__ import annotations

import json
from pathlib import Path

from cos.intake import Intake, _clip, infer_reserved, stated_done_when
from tests.test_intake_daemon import ScriptedReasoner, daemon, text

SIDEWALK = (Path(__file__).parent / "fixtures" / "sidewalk_proposal.md").read_text()


# ---------------------------------------------------------------- done-when block


def test_the_sidewalk_done_when_keeps_all_five_bullets():
    stated = stated_done_when(SIDEWALK)
    bullets = [line for line in stated.splitlines() if line.startswith("- ")]
    assert len(bullets) == 5, stated
    assert bullets[0] == "- The existing offline kit command still passes in a fresh offline checkout."
    assert bullets[-1].startswith("- Independent review covers the actual final UI/backend change")
    assert "  recovery, export collision/interruption, path containment, and rejected cross-origin" in stated, "wrapped lines stay"
    assert stated.endswith("human creative review."), "the block ends at the blank line"
    assert "Devastator method evidence" not in stated


def test_single_line_done_when_is_unchanged():
    assert stated_done_when("Do it.\nDone when: tests pass\nThanks") == "tests pass"
    assert stated_done_when("Done when: npm test passes\n\n- unrelated bullet") == "npm test passes"
    assert stated_done_when("- Done when the README exists\n- Write the parser") == "the README exists", "a sibling bullet is not continuation"


def test_multi_bullet_forms():
    assert stated_done_when("Done when:\n- a passes\n- b passes\nNotes: later") == "- a passes\n- b passes"
    assert stated_done_when("Done when:\n\n* a passes\n* b passes") == "* a passes\n* b passes", "blank line before the list"
    assert stated_done_when("**Done when:**\n1. a passes\n2. b passes") == "1. a passes\n2. b passes"
    assert stated_done_when("Done when all of:\n- a passes\n- b passes") == "all of:\n- a passes\n- b passes"
    assert stated_done_when("Done when: both hold\n  - a passes\n  - b passes") == "both hold\n- a passes\n- b passes"
    assert stated_done_when("- Done when:\n  - a passes\n  - b passes\n- Next item") == "- a passes\n- b passes", "nested under a bullet header"


def test_done_when_still_rejects_non_statements():
    for not_stated in ("Done when:\n?", "Done when:\n- ?", "**Done when:**\n\n?"):
        assert stated_done_when(not_stated) is None, not_stated
    assert stated_done_when("Done when:\nnode --test passes\n  offline") == "node --test passes\noffline", "text on the line after the colon"


class RecordingCard(ScriptedReasoner):
    """Card drafts that paraphrase the done-when, as Qwen did."""

    def complete_json(self, system, user, max_tokens=None, schema=None):
        if system.startswith("Draft a project card"):
            self.card_prompt = user
            draft = super().complete_json(system, user, max_tokens)
            return {**draft, "doneWhen": "- The existing offline kit command still passes in a fresh offline checkout."}
        return super().complete_json(system, user, max_tokens)


def test_the_card_done_when_is_the_stated_block_not_the_models_paraphrase(work, store):
    reasoner = RecordingCard()
    d, transport = daemon(work, store, reasoner)
    d.handle(text(SIDEWALK))
    card = store.get_card(next(c["card_id"] for c in store.cards_in("awaiting_yes")))["card"]
    assert card["doneWhen"] == stated_done_when(SIDEWALK)
    assert json.loads(reasoner.card_prompt)["statedDoneWhen"] == card["doneWhen"]
    assert "- Focused API tests cover valid edits" in transport.sent[-1]["text"], "Matthew sees every bullet he approves"


# ---------------------------------------------------------------- reserved-action keywords


def test_keywords_inside_other_words_are_not_reserved():
    t2 = ("Implement and Verify Eight Workflows with Recovery",
          "Develop the eight app workflows on top of the kit, ensuring persistent revision storage and functional recovery mechanisms.",
          "All eight workflows execute successfully; system demonstrates saving a revision, modifying it, and successfully restoring the previous state.")
    assert infer_reserved(*t2) is None, "card-cc97709c t2: 'mechanisms' is not 'sms'"
    for safe in ("Model the organisms dataset", "Render prisms in the demo", "Add an emergency stop flag", "Overwhelm check for the queue"):
        assert infer_reserved(safe, "") is None, safe


def test_word_start_matching_keeps_inflections_and_true_positives():
    assert infer_reserved("Send an SMS to the team", "") == "external_contact"
    assert infer_reserved("Emailing the beta list", "") == "external_contact"
    assert infer_reserved("Deploys nightly", "") == "publish_push_deploy"
    assert infer_reserved("Merged branch lands on main", "") == "publish_push_deploy"
    assert infer_reserved("Redeploy the API", "") == "publish_push_deploy"
    assert infer_reserved("Republish the docs site", "") == "publish_push_deploy"
    assert infer_reserved("Edit ~/.env.shared", "") == "live_fleet_config"
    assert infer_reserved("Write the parser", "", "released package visible on PyPI") == "publish_push_deploy"


# ---------------------------------------------------------------- title length


def test_long_titles_are_cut_at_a_word_and_marked():
    title = "Where the side-walk returned: live Digital Maker-style production app (Devastator pilot)"
    clipped = _clip(title, 80)
    assert clipped == "Where the side-walk returned: live Digital Maker-style production app…"
    assert len(clipped) <= 80
    assert _clip("short title", 80) == "short title"
    assert _clip("x" * 100, 80) == "x" * 79 + "…", "no word boundary: hard cut, still marked"


def test_normalize_never_cuts_a_title_mid_word():
    long = "Where the side-walk returned: live Digital Maker-style production app (Devastator pilot)"
    card = Intake._normalize({"title": long, "goals": []}, [{"id": "owner"}])
    assert card["title"].endswith("production app…") and "Devastato" not in card["title"]
