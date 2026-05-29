"""Negation-aware matcher tests.

Covers spec-claims: C-13, C-40, C-41, C-42, C-43, C-44 plus positive controls.
"""

import pytest

from matcher import IncidentMatcher


@pytest.fixture()
def matcher() -> IncidentMatcher:
    return IncidentMatcher()


def test_basic_negation_didnt(matcher: IncidentMatcher) -> None:
    """Covers C-13: 'I didn't have an argument' is not a match."""
    result = matcher.classify("I didn't have an argument")
    assert result.matched is False
    assert result.silent_drop_reason == "negated"


def test_negation_did_not(matcher: IncidentMatcher) -> None:
    """Covers C-40: 'I did not have an argument' is not a match."""
    result = matcher.classify("I did not have an argument")
    assert result.matched is False


def test_negation_no_subject(matcher: IncidentMatcher) -> None:
    """Covers C-41: 'no argument happened' is not a match."""
    result = matcher.classify("no argument happened")
    assert result.matched is False


def test_negation_wasnt(matcher: IncidentMatcher) -> None:
    """Covers C-42: 'she wasn't aggressive' is not a match."""
    result = matcher.classify("she wasn't aggressive")
    assert result.matched is False


def test_negation_modal_subject(matcher: IncidentMatcher) -> None:
    """Covers C-43: 'there was no incident tonight' is not a match."""
    result = matcher.classify("there was no incident tonight")
    assert result.matched is False


def test_negation_never(matcher: IncidentMatcher) -> None:
    """Covers C-44: 'she never threatened me' is not a match."""
    result = matcher.classify("she never threatened me")
    assert result.matched is False


def test_positive_control_argument(matcher: IncidentMatcher) -> None:
    """Positive control for C-13: bare 'argument' must still match."""
    result = matcher.classify("we had an argument")
    assert result.matched is True
    assert "argument" in result.matched_tokens


def test_positive_control_aggressive(matcher: IncidentMatcher) -> None:
    """Positive control for C-13: bare 'aggressive' must still match."""
    result = matcher.classify("she was aggressive")
    assert result.matched is True
