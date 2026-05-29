"""Safety-resource tests.

Covers spec-claims: C-03, C-04, C-32.
"""

from pathlib import Path

import pytest

from agent import handle_message, AgentState
from safety.resources import SAFETY_RESOURCES


@pytest.fixture()
def state(tmp_path: Path) -> AgentState:
    return AgentState(data_dir=tmp_path)


def test_resources_surface_on_fear(state: AgentState) -> None:
    """Covers C-03, C-32: fear phrases surface safety resources."""
    resp = handle_message("I'm scared", state)
    assert resp.resources


def test_resources_include_aps() -> None:
    """Covers C-04: SAFETY_RESOURCES contains an Adult Protective Services entry."""
    names = " | ".join(r.name for r in SAFETY_RESOURCES).lower()
    assert "protective" in names or "aps" in names


def test_resources_surfaced_without_match(state: AgentState) -> None:
    """Covers C-32: fear phrases surface resources even when no incident token matched."""
    resp = handle_message("I'm just scared, nothing happened yet", state)
    assert resp.resources, "resources must surface from fear phrasing alone"
