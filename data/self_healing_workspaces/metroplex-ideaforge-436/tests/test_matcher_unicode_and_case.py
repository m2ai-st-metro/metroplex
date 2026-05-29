"""Unicode / case normalization tests.

Covers spec-claims: C-28, C-54, C-55.
"""

import pytest

from matcher import IncidentMatcher


@pytest.fixture()
def matcher() -> IncidentMatcher:
    return IncidentMatcher()


def test_uppercase_match(matcher: IncidentMatcher) -> None:
    """Covers C-28, C-54: ALL-CAPS input matches."""
    result = matcher.classify("we had an ARGUMENT")
    assert result.matched is True


def test_titlecase_match(matcher: IncidentMatcher) -> None:
    """Covers C-54: TitleCase input matches."""
    result = matcher.classify("we had an Argument")
    assert result.matched is True


def test_lowercase_match(matcher: IncidentMatcher) -> None:
    """Covers C-54: lowercase input matches."""
    result = matcher.classify("we had an argument")
    assert result.matched is True


def test_smart_apostrophe_positive(matcher: IncidentMatcher) -> None:
    """Covers C-55: smart apostrophe in non-negation context still matches normally."""
    ascii_form = matcher.classify("she didn't apologize about the argument")
    smart_form = matcher.classify("she didn’t apologize about the argument")
    # 'argument' is the trigger; 'didn't apologize' negates apologize, not argument.
    # Both forms must classify identically.
    assert ascii_form.matched == smart_form.matched
    assert ascii_form.matched is True
    assert smart_form.matched is True


def test_smart_apostrophe_negation_blocks(matcher: IncidentMatcher) -> None:
    """Covers C-55: 'I didn't have an argument' with smart apostrophe still negates."""
    result = matcher.classify("I didn’t have an argument")
    assert result.matched is False
