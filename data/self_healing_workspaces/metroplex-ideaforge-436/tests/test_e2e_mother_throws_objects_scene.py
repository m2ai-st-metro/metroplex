"""E2E test for the Mother's Day "throwing objects" scene.

Covers: C-37, C-53
"""

from __future__ import annotations

import agent


def _skill_names(result) -> list[str]:
    return [r.get("skill") for r in result]


def test_physical_danger_triggers_safety_protocol():
    """Covers C-37: physical-danger incident must surface Safety Protocol, not just documentation."""
    result = agent.dispatch("my mom just threw a vase at me, i need help")
    names = _skill_names(result)
    assert "Safety Protocol" in names, (
        f"physical-danger incident must trigger Safety Protocol; got {names}"
    )


def test_multi_intent_triggers_both_skills():
    """Covers C-53: a multi-intent utterance must trigger both skills."""
    result = agent.dispatch(
        "I need help and want to document the incident — she threw a glass at me"
    )
    names = _skill_names(result)
    assert "Safety Protocol" in names, f"Safety Protocol missing from multi-intent dispatch; got {names}"
    assert "Incident Documentation" in names, (
        f"Incident Documentation missing from multi-intent dispatch; got {names}"
    )
