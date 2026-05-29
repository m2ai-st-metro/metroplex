"""Lay-register matcher tests.

Covers spec-claims: C-29, C-50, C-51, C-52, C-53.
"""

import pytest

from matcher import IncidentMatcher


@pytest.fixture()
def matcher() -> IncidentMatcher:
    return IncidentMatcher()


def test_yelled_and_threw_remote(matcher: IncidentMatcher) -> None:
    """Covers C-50: lay-register input must match."""
    result = matcher.classify("she yelled at me and threw the remote")
    assert result.matched is True
    assert len(result.tiers) >= 1


def test_lost_it_and_shoved(matcher: IncidentMatcher) -> None:
    """Covers C-51: 'lost it and shoved me' must match."""
    result = matcher.classify("mom lost it and shoved me")
    assert result.matched is True


def test_got_physical(matcher: IncidentMatcher) -> None:
    """Covers C-52: 'got physical' must match."""
    result = matcher.classify("she got physical")
    assert result.matched is True


def test_embedded_multi_tier(matcher: IncidentMatcher) -> None:
    """Covers C-53: embedded multi-token must register multiple tiers."""
    result = matcher.classify(
        "after dinner we had a huge fight and she threatened to kick me out"
    )
    assert result.matched is True
    # Both an argument/fight tier AND a threat tier
    assert "threat" in result.tiers
    assert any(t in result.tiers for t in ("argument", "fight", "verbal"))
