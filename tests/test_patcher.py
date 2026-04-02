"""
Tests for Patch Gate (Gate 3) - Agent Patches.
"""
import pytest
import sqlite3
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock, call

from config import Config
from db import StateDB
from audit import AuditLogger
from readers.st_records_reader import STRecordsReader
from gates.patcher import PatchGate
from models import PatchApplication


# --- Agent Patch Tests ---


class TestMarkdownSectionPatch:
    """Tests for _apply_markdown_section_patch."""

    def _make_gate(self):
        config = Config()
        state_db = StateDB(":memory:")
        state_db.init_db()
        mock_reader = Mock(spec=STRecordsReader)
        audit_logger = AuditLogger(":memory:")
        return PatchGate(config, state_db, mock_reader, audit_logger)

    def test_add_new_section(self):
        gate = self._make_gate()
        content = "# Agent\n\n## Existing\n\nSome content.\n"
        result = gate._apply_markdown_section_patch(content, "New Section", "add", "New stuff here.")
        assert "## New Section" in result
        assert "New stuff here." in result

    def test_add_to_existing_section(self):
        gate = self._make_gate()
        content = "# Agent\n\n## Rules\n\nRule 1.\n\n## Other\n\nOther stuff.\n"
        result = gate._apply_markdown_section_patch(content, "Rules", "add", "Rule 2.")
        assert "Rule 1." in result
        assert "Rule 2." in result
        assert "## Other" in result

    def test_replace_section(self):
        gate = self._make_gate()
        content = "# Agent\n\n## Rules\n\nOld rules.\n\n## Other\n\nKeep this.\n"
        result = gate._apply_markdown_section_patch(content, "Rules", "replace", "New rules only.")
        assert "New rules only." in result
        assert "Old rules." not in result
        assert "Keep this." in result

    def test_replace_nonexistent_section(self):
        gate = self._make_gate()
        content = "# Agent\n\n## Existing\n\nContent.\n"
        result = gate._apply_markdown_section_patch(content, "Missing", "replace", "Value")
        assert result == content  # No change

    def test_remove_section(self):
        gate = self._make_gate()
        content = "# Agent\n\n## Remove Me\n\nGone.\n\n## Keep\n\nStay.\n"
        result = gate._apply_markdown_section_patch(content, "Remove Me", "remove", None)
        assert "Remove Me" not in result
        assert "Gone." not in result
        assert "Stay." in result

    def test_remove_nonexistent_section(self):
        gate = self._make_gate()
        content = "# Agent\n\n## Existing\n\nContent.\n"
        result = gate._apply_markdown_section_patch(content, "Nope", "remove", None)
        assert "Existing" in result


class TestAgentPatchDryRun:
    """Tests for agent patch dry-run behavior."""

    def test_dry_run_prints_agent_patches(self, capsys):
        config = Config()
        state_db = StateDB(":memory:")
        state_db.init_db()
        mock_reader = Mock(spec=STRecordsReader)
        mock_reader.get_proposed_patches.return_value = []
        mock_reader.get_approved_agent_patches.return_value = [
            {
                "patch_id": "ap-1",
                "agent_id": "galvatron",
                "target": "claude_md",
                "section": "Rules",
                "operation": "add",
                "value": "New rule content",
                "rationale": "test",
                "source_recommendation_ids": [],
                "status": "approved",
            }
        ]
        audit_logger = AuditLogger(":memory:")
        gate = PatchGate(config, state_db, mock_reader, audit_logger)

        results = gate.run(dry_run=True)

        captured = capsys.readouterr()
        assert "[DRY RUN] Would apply agent patch ap-1" in captured.out
        assert "galvatron" in captured.out
        # Should return a PatchApplication with persona_id="agent:galvatron"
        agent_results = [r for r in results if r.persona_id.startswith("agent:")]
        assert len(agent_results) == 1
        assert agent_results[0].patch_id == "ap-1"

    def test_invalid_agent_id_rejected(self):
        config = Config()
        state_db = StateDB(":memory:")
        state_db.init_db()
        mock_reader = Mock(spec=STRecordsReader)
        mock_reader.get_proposed_patches.return_value = []
        mock_reader.get_approved_agent_patches.return_value = [
            {
                "patch_id": "ap-bad",
                "agent_id": "../escape",
                "target": "claude_md",
                "section": "X",
                "operation": "add",
                "value": "hax",
                "rationale": "",
                "source_recommendation_ids": [],
                "status": "approved",
            }
        ]
        audit_logger = AuditLogger(":memory:")
        gate = PatchGate(config, state_db, mock_reader, audit_logger)

        results = gate.run(dry_run=True)

        agent_results = [r for r in results if r.persona_id.startswith("agent:")]
        assert len(agent_results) == 1
        assert agent_results[0].status == "failed"
        assert "path traversal" in agent_results[0].reason


class TestApplyAgentPatch:
    """Integration-style tests for _apply_agent_patch with mocked git."""

    def test_apply_claude_md_patch(self, tmp_path):
        config = Config()
        config.academy_dir = str(tmp_path / "registry")
        state_db = StateDB(":memory:")
        state_db.init_db()
        mock_reader = Mock(spec=STRecordsReader)
        audit_logger = AuditLogger(":memory:")
        gate = PatchGate(config, state_db, mock_reader, audit_logger)

        # Set up fake registry
        agent_dir = tmp_path / "registry" / "agents" / "galvatron"
        agent_dir.mkdir(parents=True)
        (tmp_path / "registry" / ".git").mkdir()
        (agent_dir / "CLAUDE.md").write_text("# Galvatron\n\n## Behavior\n\nBe decisive.\n")
        (agent_dir / "registry.yaml").write_text(
            "agent_id: galvatron\nsync_hash: abc\nlast_synced_at: '2026-03-30'\n"
            "learning:\n  total_patches_applied: 0\n  last_patch_at: null\n"
        )

        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            status, reason = gate._apply_agent_patch(
                "ap-test", "galvatron", "claude_md", "New Section", "add",
                "Important new content.", "test rationale"
            )

        assert status == "applied"
        assert "successfully" in reason

        # Verify file was modified
        modified = (agent_dir / "CLAUDE.md").read_text()
        assert "## New Section" in modified
        assert "Important new content." in modified

        # Verify registry.yaml updated
        import yaml
        reg = yaml.safe_load((agent_dir / "registry.yaml").read_text())
        assert reg["learning"]["total_patches_applied"] == 1

    def test_missing_agent_dir_fails(self, tmp_path):
        config = Config()
        config.academy_dir = str(tmp_path / "registry")
        state_db = StateDB(":memory:")
        state_db.init_db()
        mock_reader = Mock(spec=STRecordsReader)
        audit_logger = AuditLogger(":memory:")
        gate = PatchGate(config, state_db, mock_reader, audit_logger)

        # Set up registry with .git but no agent dir
        (tmp_path / "registry" / ".git").mkdir(parents=True)

        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0, stdout="", stderr="")

            status, reason = gate._apply_agent_patch(
                "ap-test", "nonexistent", "claude_md", "X", "add", "Y", "test"
            )

        assert status == "failed"
        assert "not found" in reason
