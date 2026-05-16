"""Tests for README.md four-paragraph Scene opening.

Covers: C-08
"""

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
README = WORKSPACE_ROOT / "README.md"


def _extract_scene_paragraphs(text: str) -> list[str]:
    """Return the four-paragraph Scene block.

    Strategy: find a '## Scene' (or 'Scene') heading and split everything until the
    next '## ' heading on blank-line boundaries. If no Scene heading exists, treat
    the first four non-heading paragraphs as the Scene opening.
    """
    # Try explicit Scene heading first
    scene_match = re.search(r"^##?\s*Scene\b.*?\n(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    if scene_match:
        body = scene_match.group(1)
    else:
        body = text
    # Split on blank lines
    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    # Drop pure heading lines and code fences
    paragraphs = [p for p in raw_paragraphs if not p.startswith("#") and not p.startswith("```")]
    return paragraphs


def test_readme_has_four_scene_paragraphs():
    """Covers C-08: README must have a four-paragraph Scene opening with the right content order."""
    assert README.exists(), "README.md missing"
    text = README.read_text(encoding="utf-8")
    paragraphs = _extract_scene_paragraphs(text)
    assert len(paragraphs) >= 4, f"Scene opening needs at least 4 paragraphs, found {len(paragraphs)}"
    p1, p2, p3, p4 = paragraphs[0], paragraphs[1], paragraphs[2], paragraphs[3]
    # P1: Sarah, mother, Mother's Day argument
    assert "Sarah" in p1, "paragraph 1 must introduce Sarah"
    assert "mother" in p1.lower(), "paragraph 1 must mention her mother"
    assert "Mother" in p1 and "Day" in p1, "paragraph 1 must reference the Mother's Day argument"
    # P2: agent helping document
    assert "document" in p2.lower(), "paragraph 2 must show the agent helping Sarah document"
    # P3: invocation example
    assert "claude --agent Elder-Care_Safety_Support_Companion" in p3, (
        "paragraph 3 must contain the invocation example"
    )
    # P4: Telegram token configuration
    assert "Telegram" in p4 or "telegram" in p4.lower(), "paragraph 4 must mention Telegram"
    assert "token" in p4.lower(), "paragraph 4 must mention the bot token configuration"
