"""Agent metadata tests.

Covers spec-claims: C-06, C-07, C-19.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_agent_yaml_fields() -> None:
    """Covers C-06: agent.yaml has the spec-mandated fields."""
    agent_yaml = ROOT / "agent.yaml"
    assert agent_yaml.exists(), "agent.yaml missing"
    data = yaml.safe_load(agent_yaml.read_text())
    assert data["name"] == "Elder-Care Safety & Support Companion"
    assert data["model"] == "claude-sonnet-4-6"
    assert data["telegram_bot_token_env"] == "ELDERCARE_BOT_TOKEN"


def test_skill_md_frontmatter() -> None:
    """Covers C-07: incident_logging SKILL.md has spec-required frontmatter."""
    skill_md = ROOT / "skills" / "incident_logging" / "SKILL.md"
    assert skill_md.exists(), "incident_logging SKILL.md missing"
    text = skill_md.read_text()
    assert text.startswith("---"), "skill.md must start with frontmatter delimiter"
    # Extract the frontmatter block
    parts = text.split("---", 2)
    assert len(parts) >= 3, "missing closing frontmatter delimiter"
    front = yaml.safe_load(parts[1])
    assert front["name"] == "Incident Logging"
    assert "description" in front and front["description"]
    # spec line 22: trigger: "incident|argument|aggression|threat"
    assert "incident" in front["trigger"]
    assert "argument" in front["trigger"]
    assert "aggression" in front["trigger"]
    assert "threat" in front["trigger"]


def test_skills_bundled_in_agent_dir() -> None:
    """Covers C-19: skills are bundled in the agent directory."""
    skills_dir = ROOT / "skills"
    assert skills_dir.is_dir()
    assert (skills_dir / "incident_logging" / "SKILL.md").exists()
