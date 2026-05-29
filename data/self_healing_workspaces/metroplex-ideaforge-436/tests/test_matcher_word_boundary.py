"""Word-boundary matcher tests.

Covers spec-claims: C-14, C-45, C-46, C-47, C-48.
"""

import pytest

from matcher import IncidentMatcher


@pytest.fixture()
def matcher() -> IncidentMatcher:
    return IncidentMatcher()


def test_argumentative_does_not_match(matcher: IncidentMatcher) -> None:
    """Covers C-14, C-45: 'argumentative' must not trigger the 'argument' rule."""
    result = matcher.classify("she was being argumentative")
    assert "argument" not in result.matched_tokens
    # Either no match, or matched via a different (legitimate) token — never via "argument"
    assert result.matched is False or all(
        tok != "argument" for tok in result.matched_tokens
    )


def test_incidental_does_not_match(matcher: IncidentMatcher) -> None:
    """Covers C-46: 'incidental' must not trigger the 'incident' rule."""
    result = matcher.classify("this was incidental")
    assert result.matched is False
    assert "incident" not in result.matched_tokens


def test_threatened_does_match(matcher: IncidentMatcher) -> None:
    """Covers C-47 (positive): 'threatened' is real morphology and must match."""
    result = matcher.classify("she threatened me")
    assert result.matched is True
    assert "threat" in result.tiers


def test_threading_does_not_match(matcher: IncidentMatcher) -> None:
    """Covers C-47 (negative collision): 'threading' must not match."""
    result = matcher.classify("I was threading the needle")
    assert result.matched is False


def test_passive_aggression_needs_context(matcher: IncidentMatcher) -> None:
    """Covers C-48: 'passive-aggression' must not auto-trigger as physical 'aggression'."""
    result = matcher.classify("her passive-aggression is exhausting")
    assert result.matched is False or result.silent_drop_reason == "compound_needs_context"


def test_positive_control_aggression_standalone(matcher: IncidentMatcher) -> None:
    """Positive control for C-14: bare 'aggression' must still match."""
    result = matcher.classify("her aggression scared me")
    assert result.matched is True
