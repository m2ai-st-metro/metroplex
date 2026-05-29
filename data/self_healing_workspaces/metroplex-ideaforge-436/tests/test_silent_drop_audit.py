"""Silent-drop audit tests.

Covers spec-claims: C-15, C-30, C-31, C-36.
"""

import json
from pathlib import Path

import pytest

from agent import handle_message, AgentState


@pytest.fixture()
def state(tmp_path: Path) -> AgentState:
    return AgentState(data_dir=tmp_path)


def test_silent_drops_are_persisted(state: AgentState) -> None:
    """Covers C-15: every silent drop is recorded with a reason."""
    handle_message("I didn't have an argument", state)
    drops_file = state.data_dir / "silent_drops.jsonl"
    assert drops_file.exists()
    rows = [json.loads(l) for l in drops_file.read_text().splitlines() if l.strip()]
    assert len(rows) >= 1
    assert "reason" in rows[0]
    assert "raw_input" in rows[0]


def test_unknown_input_does_not_say_no_incident(state: AgentState) -> None:
    """Covers C-30, C-31: unknown input must not assert 'no incident detected'.

    Fail-safe direction is toward asking the user OR a neutral acknowledgment,
    NOT toward the assertive 'nothing happened' branch.
    """
    resp = handle_message("the weather was nice today", state)
    text = resp.text.lower()
    # The system must not assert there was no incident.
    forbidden_assertions = [
        "no incident detected",
        "no incident occurred",
        "nothing matches an incident",
        "no aggression detected",
    ]
    for phrase in forbidden_assertions:
        assert phrase not in text, f"forbidden assertive negative: {phrase!r} in {resp.text!r}"


def test_logged_incident_round_trips_through_disk(state: AgentState) -> None:
    """Covers C-36: confirmation requires successful write-then-read verification."""
    resp = handle_message("Mom yelled at me and threw the remote", state)
    assert resp.incident_logged is True
    incidents = state.data_dir / "incidents.jsonl"
    assert incidents.exists()
    rows = [json.loads(l) for l in incidents.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    # The agent's response must reflect what is actually on disk
    assert rows[0]["raw_input"].lower().startswith("mom yelled")
