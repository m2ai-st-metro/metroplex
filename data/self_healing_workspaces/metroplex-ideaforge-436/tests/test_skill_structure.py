"""Tests for skill directory structure and frontmatter.

Covers: C-04, C-05, C-06, C-07, C-11, C-12, C-32
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = WORKSPACE_ROOT / "skills"
TESTS_DIR = WORKSPACE_ROOT / "tests"


def _list_skill_dirs() -> list[Path]:
    if not SKILLS_DIR.exists():
        return []
    return [p for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")]


def _parse_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    # Match either ```yaml ... ``` fenced frontmatter or --- ... --- frontmatter
    fence_match = re.search(r"```yaml\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return yaml.safe_load(fence_match.group(1)) or {}
    dash_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if dash_match:
        return yaml.safe_load(dash_match.group(1)) or {}
    # Allow plain YAML at top of file
    try:
        # Try parsing the first contiguous block of lines that look like YAML
        lines = text.splitlines()
        block = []
        for line in lines:
            if line.startswith("#"):
                break
            block.append(line)
        return yaml.safe_load("\n".join(block)) or {}
    except Exception:
        return {}


def test_at_least_two_skills_exist():
    """Covers C-04: at least two skills under skills/ with SKILL.md + implementation.py."""
    skills = _list_skill_dirs()
    assert len(skills) >= 2, f"expected ≥2 skills, found {len(skills)}: {[s.name for s in skills]}"
    for s in skills:
        assert (s / "SKILL.md").exists(), f"{s.name} missing SKILL.md"
        assert (s / "implementation.py").exists(), f"{s.name} missing implementation.py"


def test_skill_md_has_frontmatter():
    """Covers C-05: each SKILL.md has YAML frontmatter with name, description, trigger."""
    skills = _list_skill_dirs()
    assert skills, "no skills found"
    for s in skills:
        fm = _parse_frontmatter(s / "SKILL.md")
        for required in ("name", "description", "trigger"):
            assert required in fm, f"{s.name}/SKILL.md frontmatter missing {required}"


def test_e2e_test_files_exist():
    """Covers C-06: test_e2e_*.py files exist."""
    e2e = list(TESTS_DIR.glob("test_e2e_*.py"))
    assert len(e2e) >= 3, f"expected ≥3 test_e2e_*.py files, found {len(e2e)}"


def test_three_specific_e2e_files_exist():
    """Covers C-07: three named e2e files exist."""
    for name in (
        "test_e2e_mother_throws_objects_scene.py",
        "test_e2e_sarah_seeks_help_scene.py",
        "test_e2e_incident_documentation_scene.py",
    ):
        assert (TESTS_DIR / name).exists(), f"missing required e2e test file: {name}"


def test_incident_documentation_trigger_phrase():
    """Covers C-11: Incident Documentation trigger is 'document incident'."""
    fm = _parse_frontmatter(SKILLS_DIR / "incident_documentation" / "SKILL.md")
    assert fm.get("trigger") == "document incident"


def test_safety_protocol_skill_exists():
    """Covers C-12: Safety Protocol skill is present."""
    sp_dir = SKILLS_DIR / "safety_protocol"
    assert (sp_dir / "SKILL.md").exists(), "skills/safety_protocol/SKILL.md missing"
    assert (sp_dir / "implementation.py").exists(), "skills/safety_protocol/implementation.py missing"


def test_skills_are_bundled_locally():
    """Covers C-32: skills bundled in agent dir, not loaded from global registry."""
    # Scan source files (excluding tests and this file's own scan content) for global-registry imports.
    own_path = Path(__file__).resolve()
    forbidden_imports = (
        "from ccos_skills import",
        "from claude_skills_global import",
        "from global_skill_registry import",
    )
    py_files = [
        p
        for p in WORKSPACE_ROOT.rglob("*.py")
        if p.resolve() != own_path
        and "venv" not in p.parts
        and ".self-healing-pipeline" not in p.parts
    ]
    for p in py_files:
        if "tests" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in forbidden_imports:
            assert bad not in text, f"{p} imports from a forbidden global skill registry: {bad}"
