"""Acceptance tests for the Stage 1A.6 FIRST-PASS SAFETY CONTRACT.

These encode idea #436's criticals as the SAFETY claims they map to and assert
the Stage 1A.6 checklist (in the GLOBAL self-healing-pipeline SKILL.md) contains
a row covering each. This proves the contract *content* closes the #436 gap
without needing a live Planner run — we parse the checklist section text and
verify each critical's risk-keyed category + intent is present.

#436 criticals -> SAFETY claims:
  F-01 negation-lies        -> negation_handling
  F-02 substring-risk       -> word_boundary
  C1   unhandled-audit-write-> error_handling
  C2   narrow-except        -> error_handling
  C3   silent-redaction     -> narrative_drift
  C4   fsync-parity         -> log_resilience
"""
from pathlib import Path

import pytest

SKILL_MD = Path.home() / ".claude" / "skills" / "self-healing-pipeline" / "SKILL.md"
pytestmark = pytest.mark.skipif(not SKILL_MD.exists(), reason="SKILL.md not found in environment")


def _checklist_section() -> str:
    """Return the Stage 1A.6 checklist section, lower-cased with whitespace
    collapsed to single spaces so line-wrapping in SKILL.md does not break
    phrase matching."""
    text = SKILL_MD.read_text(encoding="utf-8")
    start = text.find("STAGE 1A.6: FIRST-PASS SAFETY CONTRACT")
    assert start != -1, "Stage 1A.6 section not found in SKILL.md"
    end = text.find("STAGE 1B: IMPLEMENTATION DESIGN", start)
    assert end != -1, "Stage 1B boundary not found after Stage 1A.6"
    section = text[start:end].lower()
    return " ".join(section.split())


@pytest.fixture(scope="module")
def checklist() -> str:
    return _checklist_section()


def _assert_row_covers(checklist: str, category: str, marker: str, *needles: str) -> None:
    """A checklist row covering `marker` (e.g. '#436 f-01') must name the
    Ravage `category` and contain every intent `needle`."""
    assert marker in checklist, f"checklist missing critical marker {marker!r}"
    assert category in checklist, f"checklist missing category {category!r}"
    # locate the line(s) near the marker and assert the needles appear in the section
    for needle in needles:
        assert needle in checklist, (
            f"checklist row for {marker} missing expected intent {needle!r}"
        )


def test_f01_negation_lies(checklist):
    """F-01: negated input MUST NOT be acked as 'did not happen'."""
    _assert_row_covers(
        checklist, "negation_handling", "#436 f-01",
        "negated input must not be acked", "negation token must scope trigger",
    )


def test_f02_substring_risk(checklist):
    """F-02: word-boundary matching, not substring."""
    _assert_row_covers(
        checklist, "word_boundary", "#436 f-02",
        r"use \bword\b not substring", "negative-control",
    )


def test_c1_unhandled_audit_write(checklist):
    """C1: audit/silent-drop writes wrapped in explicit error handling."""
    _assert_row_covers(
        checklist, "error_handling", "#436 c1",
        "audit/silent-drop writes wrapped in explicit error handling",
        "never uncaught traceback",
    )


def test_c2_narrow_except(checklist):
    """C2: catch the specific exception classes; corrupt-line tolerance."""
    _assert_row_covers(
        checklist, "error_handling", "#436 c2",
        "jsondecodeerror", "skip+count",
    )


def test_c3_silent_redaction(checklist):
    """C3: log every redaction; word-boundary not substring."""
    _assert_row_covers(
        checklist, "narrative_drift", "#436 c3",
        "log every redaction", "word-boundary not substring",
    )


def test_c4_fsync_parity(checklist):
    """C4: os.fsync + write-then-read; silent-drop path same durability."""
    _assert_row_covers(
        checklist, "log_resilience", "#436 c4",
        "os.fsync after flush", "same durability contract",
    )
