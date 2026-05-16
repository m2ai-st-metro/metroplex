"""Safety-constraint tests: negation, word boundary, Unicode, register, embedded, case.

Covers: C-13, C-14, C-15, C-16, C-17, C-19, C-20, C-21, C-22, C-23,
        C-35, C-36, C-38, C-39, C-40, C-41, C-42, C-43, C-44, C-45,
        C-46, C-47, C-48, C-49, C-50, C-51, C-54
"""

from __future__ import annotations

import logging

import agent


def _skill_names(result) -> list[str]:
    return [r.get("skill") for r in result]


# --- Negation cases (must NOT trigger) ---

def test_negation_didnt_document():
    """Covers C-13, C-40: 'I didn't document the incident' must NOT trigger Incident Documentation."""
    result = agent.dispatch("I didn't document the incident")
    assert "Incident Documentation" not in _skill_names(result)


def test_negation_almost_documented():
    """Covers C-14, C-41: 'I almost documented it' must NOT trigger."""
    result = agent.dispatch("I almost documented it")
    assert "Incident Documentation" not in _skill_names(result)


def test_negation_never_document():
    """Covers C-42: 'I never document anything' must NOT trigger."""
    result = agent.dispatch("I never document anything")
    assert "Incident Documentation" not in _skill_names(result)


def test_negation_didnt_need_help():
    """Covers C-19, C-43: 'I didn't need help' must NOT trigger Safety Protocol."""
    result = agent.dispatch("I didn't need help")
    assert "Safety Protocol" not in _skill_names(result)


def test_negation_almost_called_for_help():
    """Covers C-20, C-44: 'I almost called for help' must NOT trigger."""
    result = agent.dispatch("I almost called for help")
    assert "Safety Protocol" not in _skill_names(result)


def test_negation_no_longer_need_help():
    """Covers C-45: 'I no longer need help' must NOT trigger Safety Protocol."""
    result = agent.dispatch("I no longer need help")
    assert "Safety Protocol" not in _skill_names(result)


# --- Word-boundary cases (must NOT trigger) ---

def test_word_boundary_documentary():
    """Covers C-15, C-46: 'documentary' must NOT trigger Incident Documentation."""
    result = agent.dispatch("I watched a documentary last night")
    assert "Incident Documentation" not in _skill_names(result)


def test_word_boundary_documented_fragment():
    """Covers C-16, C-47: 'a well-documented disease' must NOT trigger."""
    result = agent.dispatch("This is a well-documented disease")
    assert "Incident Documentation" not in _skill_names(result)


def test_word_boundary_helpless():
    """Covers C-21, C-48: 'helpless' must NOT trigger Safety Protocol."""
    result = agent.dispatch("I feel helpless")
    assert "Safety Protocol" not in _skill_names(result)


def test_word_boundary_helper():
    """Covers C-22, C-49: 'helper' must NOT trigger Safety Protocol."""
    result = agent.dispatch("my helper is on her way")
    assert "Safety Protocol" not in _skill_names(result)


# --- Unicode mutations ---

def test_unicode_smart_apostrophe_in_negation():
    """Covers C-38: smart apostrophe (U+2019) in negation must still suppress the match."""
    # U+2019 RIGHT SINGLE QUOTATION MARK
    result = agent.dispatch("I didn’t document the incident")
    assert "Incident Documentation" not in _skill_names(result), (
        "smart-apostrophe negation must still be detected as negation"
    )


def test_unicode_smart_double_quote():
    """Covers C-39: smart double quotes around an utterance must not bypass matching."""
    # U+201C / U+201D
    result = agent.dispatch("“I need help”, she said")
    assert "Safety Protocol" in _skill_names(result)


# --- Warning-log cases (dropped inputs) ---

def test_warning_log_on_dropped_incident(caplog):
    """Covers C-17, C-36: WARNING-level log when an unrelated input is dropped."""
    caplog.set_level(logging.WARNING)
    result = agent.dispatch("the sky is blue today")
    assert result == [] or all(r.get("skill") not in ("Incident Documentation", "Safety Protocol") for r in result)
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "expected a WARNING-level log when dropping unrelated input"


def test_warning_log_on_dropped_safety_request(caplog):
    """Covers C-23, C-36: WARNING log when an unrelated input is dropped (safety side)."""
    caplog.set_level(logging.WARNING)
    agent.dispatch("the weather is nice")
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "expected a WARNING-level log when dropping non-safety input"


# --- Lay-register synonyms (positive matches) ---

def test_lay_register_documentation_synonyms():
    """Covers C-50: lay-register documentation synonyms must trigger Incident Documentation."""
    cases = [
        "please write this down",
        "log this for me",
        "I need to make a note about tonight",
    ]
    for utterance in cases:
        result = agent.dispatch(utterance)
        assert "Incident Documentation" in _skill_names(result), (
            f"lay-register utterance failed to trigger Incident Documentation: {utterance!r}"
        )


def test_lay_register_safety_synonyms():
    """Covers C-51: lay-register safety synonyms must trigger Safety Protocol."""
    cases = [
        "I'm scared",
        "this isn't safe",
        "I need someone here",
    ]
    for utterance in cases:
        result = agent.dispatch(utterance)
        assert "Safety Protocol" in _skill_names(result), (
            f"lay-register utterance failed to trigger Safety Protocol: {utterance!r}"
        )


# --- Case-insensitivity ---

def test_case_insensitive_document_incident():
    """Covers C-54: case variants of 'document incident' must all trigger."""
    for variant in ("DOCUMENT INCIDENT", "Document Incident", "document INCIDENT"):
        result = agent.dispatch(variant)
        assert "Incident Documentation" in _skill_names(result), (
            f"case variant did not trigger: {variant!r}"
        )


# --- Fail-safe on unrecognized input ---

def test_unrecognized_input_does_not_silent_succeed(caplog):
    """Covers C-35: unrecognized input must not silently route anywhere; must emit WARNING."""
    caplog.set_level(logging.WARNING)
    result = agent.dispatch("zxcvbnm random gibberish 12345")
    assert result == [], f"unrecognized input must produce empty dispatch result, got {result}"
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "expected WARNING when no skill matched"
