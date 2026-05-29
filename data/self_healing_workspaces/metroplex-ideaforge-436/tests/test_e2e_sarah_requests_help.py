"""E2E tests: Sarah requests help / safety resources.

Covers spec-claims: C-03, C-04, C-05, C-32, C-33.
"""

from pathlib import Path

import pytest

from agent import handle_message, AgentState


@pytest.fixture()
def state(tmp_path: Path) -> AgentState:
    return AgentState(data_dir=tmp_path)


def test_provides_safety_resources(state: AgentState) -> None:
    """Covers C-03: agent provides safety resources when user expresses fear."""
    resp = handle_message("I'm scared to come home", state)
    assert resp.resources, "expected non-empty resources list"
    # at least one hotline number (matches a phone-number-ish string)
    joined = " ".join(r.contact for r in resp.resources)
    assert any(ch.isdigit() for ch in joined), "expected at least one numeric hotline contact"


def test_risk_assessment_when_user_scared(state: AgentState) -> None:
    """Covers C-05: agent assesses risk level when user fears for safety."""
    resp = handle_message("I'm afraid she'll hurt me", state)
    assert resp.risk_level in {"elevated", "high"}, f"unexpected risk_level: {resp.risk_level!r}"


def test_guilt_phrase_validated_without_suppressing_resources(state: AgentState) -> None:
    """Covers C-33: 'I feel guilty about seeking help' validated AND resources still surfaced."""
    resp = handle_message("I feel guilty about seeking help", state)
    text = resp.text.lower()
    assert any(
        marker in text
        for marker in ("valid", "understandable", "common", "you're not alone", "natural")
    ), f"no validation marker in: {resp.text!r}"
    assert resp.resources, "resources must NOT be suppressed by ambivalence phrasing"
