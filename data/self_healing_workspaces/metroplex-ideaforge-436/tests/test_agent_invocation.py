"""E2E tests for agent invocation shape and agent.yaml structure.

Covers: C-01, C-02, C-03, C-09 from .self-healing-pipeline/spec-claims.md
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
AGENT_YAML = WORKSPACE_ROOT / "agent.yaml"
README = WORKSPACE_ROOT / "README.md"


def _load_agent_yaml() -> dict:
    text = AGENT_YAML.read_text(encoding="utf-8")
    return yaml.safe_load(text)


def test_agent_yaml_has_correct_name():
    """Covers C-01: agent name."""
    data = _load_agent_yaml()
    assert data["name"] == "Elder-Care Safety & Support Companion"


def test_agent_yaml_has_required_fields():
    """Covers C-02: agent.yaml must declare name, description, model, telegram_bot_token_env."""
    data = _load_agent_yaml()
    for required in ("name", "description", "model", "telegram_bot_token_env"):
        assert required in data, f"agent.yaml missing required field: {required}"


def test_agent_yaml_telegram_env_placeholder():
    """Covers C-03: env-var placeholder name must be ELDER_CARE_BOT_TOKEN."""
    data = _load_agent_yaml()
    assert data["telegram_bot_token_env"] == "ELDER_CARE_BOT_TOKEN"


def test_agent_invocation_string_matches_spec():
    """Covers C-09: README must show the agent invocation form from the spec."""
    text = README.read_text(encoding="utf-8")
    assert "claude --agent Elder-Care_Safety_Support_Companion" in text, (
        "README must include the spec-mandated invocation string"
    )
