"""README structure test.

Covers spec-claim: C-10.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_has_four_paragraphs() -> None:
    """Covers C-10: README is a four-paragraph story."""
    readme = ROOT / "README.md"
    assert readme.exists(), "README.md missing"
    text = readme.read_text().strip()
    # Strip the leading heading line(s) (lines starting with #)
    lines = text.splitlines()
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    body = "\n".join(lines).strip()
    # Split on blank-line boundaries
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    # Filter out lines that are just headings or fences
    paragraphs = [p for p in paragraphs if not p.startswith("#")]
    assert len(paragraphs) == 4, (
        f"README must have exactly 4 narrative paragraphs (excluding headings); "
        f"found {len(paragraphs)}"
    )
