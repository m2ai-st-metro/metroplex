"""
Tests for validate_agent_spec — R-A item 1 (agent shape Builder output).

Companion to validate_spec which keeps gating tech-rubric specs. This
suite locks the agent-spec validator's contract end-to-end:
  - the hand-crafted golden fixture passes (stability anchor)
  - missing required sections (agent.yaml, skills/, test_e2e, README) reject
  - CoT leakage rejects (same threshold as tech validator)
  - agent-prompt parroting rejects (new AGENT_PARROT_MARKERS set)
  - length out-of-bounds rejects
  - duplicate Overview rejects

Fixture path: spec_templates/fixtures/agent_spec_golden.md. The test
asserts the path is NOT a symlink before reading — defends against a
future regression that swaps the fixture for a symlink pointing outside
the project tree.
"""
from pathlib import Path

import pytest

from gates.llm_expander import (
    validate_agent_spec,
    AGENT_PARROT_MARKERS,
    AGENT_SPEC_EXPANSION_PROMPT,
    MIN_AGENT_SPEC_CHARS,
    MAX_AGENT_SPEC_CHARS,
)


FIXTURE_PATH = (
    Path(__file__).parent.parent
    / "spec_templates"
    / "fixtures"
    / "agent_spec_golden.md"
)


@pytest.fixture
def golden() -> str:
    """Read the hand-crafted golden agent spec."""
    assert FIXTURE_PATH.exists(), f"golden fixture missing: {FIXTURE_PATH}"
    assert not FIXTURE_PATH.is_symlink(), (
        f"golden fixture must not be a symlink (got: {FIXTURE_PATH})"
    )
    return FIXTURE_PATH.read_text(encoding="utf-8")


class TestGoldenFixture:
    """C4 — the hand-crafted golden fixture is the validator's stability anchor."""

    def test_fixture_path_exists_and_is_not_symlink(self):
        assert FIXTURE_PATH.exists()
        assert not FIXTURE_PATH.is_symlink()
        assert FIXTURE_PATH.is_file()

    def test_fixture_within_length_bounds(self, golden):
        char_count = len(golden)
        assert MIN_AGENT_SPEC_CHARS <= char_count <= MAX_AGENT_SPEC_CHARS, (
            f"golden fixture out of bounds: {char_count} chars "
            f"(allowed {MIN_AGENT_SPEC_CHARS}-{MAX_AGENT_SPEC_CHARS})"
        )

    def test_golden_passes_validator(self, golden):
        ok, reason = validate_agent_spec(golden)
        assert ok, f"golden fixture rejected: {reason}"

    def test_golden_does_not_parrot_prompt(self, golden):
        """The golden is a hand-crafted ideal spec, not a prompt copy."""
        hits = [m for m in AGENT_PARROT_MARKERS if m in golden]
        assert hits == [], f"golden fixture parrots prompt fragments: {hits}"

    def test_golden_references_all_required_artifacts(self, golden):
        """agent.yaml, skills/, test_e2e, README all appear in the golden."""
        text = golden.lower()
        assert "agent.yaml" in text
        assert ("skills/" in text) or ("skill.md" in text)
        assert "test_e2e" in text
        assert "readme" in text


class TestValidateAgentSpec:
    """C2 — validator rejects the right things."""

    def test_missing_agent_yaml_section_rejected(self, golden):
        # Replace every reference to agent.yaml with a benign placeholder
        tainted = golden.replace("agent.yaml", "config_placeholder")
        ok, reason = validate_agent_spec(tainted)
        assert not ok
        assert "agent.yaml" in reason

    def test_missing_skills_section_rejected(self, golden):
        tainted = golden.replace("skills/", "modules/")
        # Also replace bare SKILL.md mentions
        tainted = tainted.replace("SKILL.md", "MODULE.md")
        tainted = tainted.replace("Skills", "Modules")
        ok, reason = validate_agent_spec(tainted)
        assert not ok
        assert "skills" in reason.lower() or "SKILL.md" in reason

    def test_missing_e2e_test_reference_rejected(self, golden):
        tainted = golden.replace("test_e2e", "test_unit")
        ok, reason = validate_agent_spec(tainted)
        assert not ok
        assert "test_e2e" in reason

    def test_missing_readme_section_rejected(self, golden):
        # Replace every form of README the validator catches (case-insensitive)
        # The validator uses `"readme" not in spec_lower`, so removing all
        # casings of README defeats it.
        tainted = golden.replace("README", "DOCUMENT")
        tainted = tainted.replace("Readme", "Document")
        tainted = tainted.replace("readme", "document")
        ok, reason = validate_agent_spec(tainted)
        assert not ok
        assert "README" in reason or "readme" in reason.lower()

    def test_cot_leakage_rejected(self, golden):
        """3+ CoT markers in spec text → reject. Reuses COT_MARKERS."""
        tainted = golden + (
            "\nlet's consider this. hmm, alternatively we could.\n"
            "let me think about it. wait, on second thought.\n"
        )
        ok, reason = validate_agent_spec(tainted)
        assert not ok
        assert "CoT" in reason

    def test_agent_prompt_parroting_rejected(self, golden):
        """LLM that copies AGENT_PARROT_MARKERS instruction fragments → reject."""
        for marker in AGENT_PARROT_MARKERS:
            tainted = golden + f"\n\n{marker}\n"
            ok, reason = validate_agent_spec(tainted)
            assert not ok, f"validator did not reject parrot marker: {marker!r}"
            assert "parroting" in reason.lower(), (
                f"reason did not mention parroting for {marker!r}: {reason}"
            )

    def test_under_length_rejected(self):
        """Short specs (< MIN_AGENT_SPEC_CHARS) → 'Degenerate spec' (mirrors
        the tech validator's wording so log greps stay consistent)."""
        short_spec = "# Tiny\n## A\n## B\n## C\n## D\n"
        ok, reason = validate_agent_spec(short_spec)
        assert not ok
        assert "Degenerate spec" in reason

    def test_over_length_rejected(self):
        """Long specs (> MAX_AGENT_SPEC_CHARS) → 'Over-scoped spec'."""
        # Construct a spec well beyond MAX_AGENT_SPEC_CHARS. Each filler
        # line is ~14 chars; we need MAX + buffer.
        header = "# Big - Agent Specification\n## Overview\nx\n## Agent shape\nx\n## Constraints\nx\n## Success criteria\nx\nagent.yaml skills/ SKILL.md test_e2e README\n"
        filler_line = "filler line\n"
        padding_count = (MAX_AGENT_SPEC_CHARS // len(filler_line)) + 100
        tainted = header + (filler_line * padding_count)
        assert len(tainted) > MAX_AGENT_SPEC_CHARS, "fixture must exceed the ceiling"
        ok, reason = validate_agent_spec(tainted)
        assert not ok
        assert "Over-scoped spec" in reason

    def test_duplicate_overview_rejected(self, golden):
        tainted = golden + "\n## Overview\nA second overview should not appear.\n"
        ok, reason = validate_agent_spec(tainted)
        assert not ok
        assert "Duplicate content" in reason

    def test_insufficient_headers_rejected(self):
        """Spec with < MIN_AGENT_SECTION_HEADERS (= 4) ## headers → reject.

        Build a spec with enough chars to pass length check but only 3
        ## headings. Include required topical markers so we hit the
        header-count branch, not the missing-section branch.
        """
        body = (
            "# Title - Agent Specification\n\n"
            "## Overview\n"
            "agent.yaml skills/ test_e2e README content here.\n\n"
            "## Agent shape\n"
            "Some text here.\n\n"
            "## Constraints\n"
            "Some text here.\n\n"
            # Only 3 ## headers — short of the 4-header minimum.
        )
        filler = "text line filler content with more words to add density\n"
        # Pad past the char floor so the header-count branch fires.
        while len(body) < MIN_AGENT_SPEC_CHARS + 200:
            body += filler
        ok, reason = validate_agent_spec(body)
        assert not ok
        assert "Insufficient structure" in reason

    def test_empty_string_rejected(self):
        ok, reason = validate_agent_spec("")
        assert not ok
        # Could fail on either length or section count; either is fine.
        assert ("Degenerate spec" in reason) or ("Insufficient structure" in reason)


class TestTokenLeakGuard:
    """R-A item 1 / Codex Round 2 MEDIUM: validate_agent_spec rejects specs
    where `telegram_bot_token_env:` holds an actual token value instead of
    an env-var NAME.

    Defense in depth against (a) prompt injection that smuggles a hardcoded
    token via the idea fields, and (b) a Builder LLM that misreads the
    prompt and bakes a real token into agent.yaml.
    """

    def test_real_token_value_rejected(self, golden):
        # Telegram bot token shape: <digits>:<base64-ish>
        bad = golden.replace(
            "telegram_bot_token_env: NEWBORN_TRIAGE_BOT_TOKEN",
            "telegram_bot_token_env: 1234567890:AAFxxxxxxxxxxxxxxxxxxxxx",
        )
        ok, reason = validate_agent_spec(bad)
        assert not ok
        assert "env-var NAME" in reason

    def test_lowercase_value_rejected(self, golden):
        bad = golden.replace(
            "telegram_bot_token_env: NEWBORN_TRIAGE_BOT_TOKEN",
            "telegram_bot_token_env: secret_token_value",
        )
        ok, reason = validate_agent_spec(bad)
        assert not ok
        assert "env-var NAME" in reason

    def test_mixed_case_value_rejected(self, golden):
        bad = golden.replace(
            "telegram_bot_token_env: NEWBORN_TRIAGE_BOT_TOKEN",
            "telegram_bot_token_env: MyTokenValue",
        )
        ok, reason = validate_agent_spec(bad)
        assert not ok
        assert "env-var NAME" in reason

    def test_quoted_token_value_rejected(self, golden):
        """Quotes don't bypass the check — the regex strips them."""
        bad = golden.replace(
            "telegram_bot_token_env: NEWBORN_TRIAGE_BOT_TOKEN",
            "telegram_bot_token_env: '1234567890:AAFsecretsecret'",
        )
        ok, reason = validate_agent_spec(bad)
        assert not ok
        assert "env-var NAME" in reason

    def test_valid_env_name_accepted(self, golden):
        for name in ("MYAGENT_BOT_TOKEN", "TRIAGE_TOKEN", "FOO_BAR_BAZ_TOKEN", "X_TOKEN"):
            valid = golden.replace(
                "telegram_bot_token_env: NEWBORN_TRIAGE_BOT_TOKEN",
                f"telegram_bot_token_env: {name}",
            )
            ok, reason = validate_agent_spec(valid)
            assert ok, f"valid env name {name!r} rejected: {reason}"

    def test_no_token_env_line_passes(self, golden):
        """If the spec doesn't mention telegram_bot_token_env at all,
        the validator doesn't trip on it (the field is optional in the
        spec text; the gate downstream checks the rendered agent.yaml)."""
        # Remove the line entirely
        stripped = "\n".join(
            line for line in golden.splitlines()
            if "telegram_bot_token_env" not in line
        )
        ok, reason = validate_agent_spec(stripped)
        # May fail other checks but NOT for token-env reasons.
        if not ok:
            assert "env-var NAME" not in reason

    def test_multiple_token_env_lines_all_checked(self, golden):
        """If a spec includes two YAML examples, both are validated."""
        bad = golden + (
            "\n\n## Secondary example\n\n```yaml\n"
            "telegram_bot_token_env: 9999:realtoken\n"
            "```\n"
        )
        ok, reason = validate_agent_spec(bad)
        assert not ok
        assert "env-var NAME" in reason
