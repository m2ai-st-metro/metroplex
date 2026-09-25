"""Foundation: card hash parity with Teletraan, spec lint, client errors, safety."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests.conftest import TELETRAAN_ROOT
from cos.safety import CircuitBreaker, CycleCaps
from cos.spec import SpecError, card_digest, validate_card, validate_task_spec
from cos.teletraan_client import WorkError


def make_card(**overrides):
    card = {
        "title": "Pilot",
        "objective": "Deliver the pilot",
        "doneWhen": "A reviewer can run it",
        "owner": "owner",
        "goals": [{"goalId": "g1", "statement": "Build", "doneWhen": "Tests pass"}],
        "reservedActions": ["publish_push_deploy"],
        "hostedAllowed": True,
        "humanOnly": False,
        "scopeOut": ["no publish"],
        "note": "naïve – unicode stays raw",
    }
    card.update(overrides)
    return card


def test_card_digest_matches_teletraan_byte_for_byte():
    node = shutil.which("node")
    if not node or not (TELETRAAN_ROOT / "src/domain/work.mjs").exists():
        pytest.skip("node or Teletraan checkout unavailable")
    card = make_card()
    script = f"import('{TELETRAAN_ROOT}/src/domain/work.mjs').then(m => console.log(m.cardDigest(JSON.parse(process.argv[1]))))"
    js = subprocess.run([node, "-e", script, json.dumps(card)], capture_output=True, text=True, check=True).stdout.strip()
    assert card_digest(card) == js
    # Key order must not matter; content must.
    assert card_digest(dict(reversed(list(card.items())))) == js
    assert card_digest(make_card(title="Other")) != js


def test_card_validation_names_every_problem():
    assert validate_card(make_card()) == []
    problems = validate_card(make_card(doneWhen="", goals=[], reservedActions=["launch"]))
    assert "doneWhen is missing" in problems
    assert any("goals" in p for p in problems)
    assert any("reservedActions" in p for p in problems)


def test_task_spec_lint_matches_teletraan_codes():
    good = {"doneWhen": "x", "checkpoints": [{"name": "a", "evidenceExpected": "b"}], "qualityChecks": [{"name": "t", "howVerified": "test"}], "cardGoalId": "g1"}
    assert validate_task_spec(good) is good
    for bad in ({**good, "checkpoints": []}, {**good, "qualityChecks": [{"name": "t", "howVerified": "vibes"}]}, {**good, "reservedAction": "nope"}, {**good, "doneWhen": " "}):
        with pytest.raises(SpecError, match="INVALID_TASK_SPEC"):
            validate_task_spec(bad)


def test_client_surfaces_domain_rejections(work):
    with pytest.raises(WorkError) as err:
        work["cos"].command("agent.create", "rogue", 0, {"name": "rogue"})
    assert err.value.code == "COS_OPERATION_FORBIDDEN"
    assert err.value.command_rejected is True


def test_breaker_opens_once_and_jev_breaker_cools_down(store):
    b = CircuitBreaker(store, threshold=3, jev_cooldown_s=900)
    assert [b.record_failure("turn", now=1) for _ in range(4)] == [False, False, True, False]
    assert b.is_open("turn", now=10_000)
    b.record_failure("jev", now=0)
    b.record_failure("jev", now=0)
    b.record_failure("jev", now=0)
    assert b.is_open("jev", now=100)
    assert not b.is_open("jev", now=1000), "jev retries after the cooldown"
    b.reset("all")
    assert not b.is_open("turn")


def test_cycle_caps_persist_per_window(store):
    caps = CycleCaps(store, cycle_s=900)
    assert [caps.take("turns:p", 3, now=10) for _ in range(4)] == [True, True, True, False]
    assert caps.take("turns:p", 3, now=10 + 900), "next cycle resets"
    assert caps.take("turns:q", 3, now=10), "keys are independent"
