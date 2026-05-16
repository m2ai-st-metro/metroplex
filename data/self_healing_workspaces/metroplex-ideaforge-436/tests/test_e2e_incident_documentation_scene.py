"""E2E test for the incident documentation scene.

Covers: C-10, C-52, C-50
"""

from __future__ import annotations

import agent


def _by_skill(result, name):
    return [r for r in result if r.get("skill") == name]


def test_incident_is_recorded_and_categorized():
    """Covers C-10: Incident Documentation records and categorizes incidents."""
    result = agent.dispatch("please document this incident")
    docs = _by_skill(result, "Incident Documentation")
    assert docs, "Incident Documentation must trigger on canonical phrasing"
    d = docs[0]
    assert d.get("logged") is True, "incident must be marked as logged"
    assert "category" in d, "incident must be categorized"


def test_canonical_trigger_in_long_sentence():
    """Covers C-52: canonical trigger embedded in a longer sentence must still match."""
    result = agent.dispatch(
        "can you help me document the incident that happened tonight, she threw a vase at me"
    )
    assert any(r.get("skill") == "Incident Documentation" for r in result)


def test_lay_register_record_what_happened_triggers():
    """Covers C-50: lay-register 'record what just happened' must trigger Incident Documentation."""
    result = agent.dispatch("can you record what just happened")
    assert any(r.get("skill") == "Incident Documentation" for r in result)
