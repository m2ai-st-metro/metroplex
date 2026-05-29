"""E2E tests: Sarah documents an incident with the agent.

Covers spec-claims: C-01, C-02, C-13.
"""

import json
from pathlib import Path

import pytest

from agent import handle_message, AgentState


@pytest.fixture()
def state(tmp_path: Path) -> AgentState:
    return AgentState(data_dir=tmp_path)


def test_logs_aggression_incident(state: AgentState) -> None:
    """Covers C-01, C-02: agent logs an aggression incident."""
    resp = handle_message("Mom screamed at me and threw the remote tonight", state)
    assert resp.incident_logged is True
    incidents_file = state.data_dir / "incidents.jsonl"
    assert incidents_file.exists()
    lines = [json.loads(l) for l in incidents_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert "screamed" in lines[0]["raw_input"].lower() or "threw" in lines[0]["raw_input"].lower()


def test_handle_message_returns_calm_acknowledgment(state: AgentState) -> None:
    """Covers C-01: response opens with empathetic acknowledgment, no diagnostic terms."""
    resp = handle_message("Mom screamed at me and threw the remote tonight", state)
    text = resp.text.lower()
    # No diagnostic terms
    for term in ("diagnose", "diagnosis", "disorder", "schizophrenia", "bipolar"):
        assert term not in text, f"diagnostic term leaked: {term}"
    # Some form of acknowledgment
    assert any(
        marker in text
        for marker in ("thank you", "i hear", "that sounds", "i'm sorry", "documented", "logged")
    ), f"no acknowledgment marker in: {resp.text!r}"


def test_negation_not_logged(state: AgentState) -> None:
    """Covers C-13: 'I didn't have an argument' is NOT logged as an incident."""
    resp = handle_message("I didn't have an argument with mom", state)
    assert resp.incident_logged is False
    silent = state.data_dir / "silent_drops.jsonl"
    assert silent.exists()
    rows = [json.loads(l) for l in silent.read_text().splitlines() if l.strip()]
    assert any(r.get("reason") == "negated" for r in rows)
