"""Tests for scripts/agent_promote.py (Pass 6 part B)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import agent_promote


def _write_minimal_workspace(
    root: Path,
    *,
    agent_yaml: str = "name: Sample Agent\ndescription: x\nmodel: claude-sonnet-4-6\ntelegram_bot_token_env: SAMPLE_BOT_TOKEN\n",
    include_skill: bool = True,
    include_e2e: bool = True,
    include_state: dict | None = None,
    extras: tuple[str, ...] = ("README.md", "requirements.txt", "episode_log.py"),
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.yaml").write_text(agent_yaml, encoding="utf-8")
    if include_skill:
        (root / "skills" / "sample").mkdir(parents=True, exist_ok=True)
        (root / "skills" / "sample" / "SKILL.md").write_text("---\nname: sample\n---\n", encoding="utf-8")
    if include_e2e:
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "tests" / "test_e2e_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    for name in extras:
        (root / name).write_text("placeholder\n", encoding="utf-8")
    # Always create the pipeline state dir so we can write state.json into it
    state_dir = root / ".self-healing-pipeline"
    state_dir.mkdir(parents=True, exist_ok=True)
    if include_state is not None:
        (state_dir / "state.json").write_text(json.dumps(include_state), encoding="utf-8")
    # Pipeline-internal files that copy plan must exclude
    (state_dir / "plan.md").write_text("internal\n", encoding="utf-8")
    (root / ".heartbeat-callback").write_text("/tmp/x\n", encoding="utf-8")
    return root


class TestSlugify:
    def test_basic(self):
        assert agent_promote._slugify("Nighttime Newborn Triage Copilot") == "nighttime-newborn-triage-copilot"

    def test_punctuation_and_runs(self):
        assert agent_promote._slugify("ABC --- xyz!!!  q") == "abc-xyz-q"

    def test_empty_fallback(self):
        assert agent_promote._slugify("   ") == "unnamed-agent"
        assert agent_promote._slugify("!!!") == "unnamed-agent"


class TestAgentYamlReaders:
    def test_reads_name_unquoted(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        assert agent_promote._read_agent_name(ws) == "Sample Agent"

    def test_reads_name_with_quotes(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "agent.yaml").write_text('name: "Quoted Name"\n', encoding="utf-8")
        assert agent_promote._read_agent_name(ws) == "Quoted Name"

    def test_missing_returns_none(self, tmp_path):
        assert agent_promote._read_agent_name(tmp_path) is None

    def test_reads_telegram_env(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        assert agent_promote._read_telegram_env_var(ws) == "SAMPLE_BOT_TOKEN"


class TestBuildStatus:
    def test_no_state(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        assert agent_promote._read_build_status(ws) is None

    def test_review_rejected_takes_precedence(self, tmp_path):
        ws = _write_minimal_workspace(
            tmp_path / "ws",
            include_state={"status": "passed", "review_verdict": "rejected"},
        )
        assert agent_promote._read_build_status(ws) == "review_rejected"

    def test_passes_through_status_when_not_rejected(self, tmp_path):
        ws = _write_minimal_workspace(
            tmp_path / "ws",
            include_state={"status": "passed", "review_verdict": "approved"},
        )
        assert agent_promote._read_build_status(ws) == "passed"

    def test_malformed_state_returns_none(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        (ws / ".self-healing-pipeline" / "state.json").write_text("{not json", encoding="utf-8")
        assert agent_promote._read_build_status(ws) is None


class TestValidateShape:
    def test_full_shape_ok(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        report = agent_promote.validate_shape(ws)
        assert report.shape_ok
        assert report.has_agent_yaml
        assert report.has_skill_manifest
        assert report.has_e2e_test
        assert "README.md" in report.extras_present
        assert report.agent_name == "Sample Agent"

    def test_missing_agent_yaml_fails(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        (ws / "agent.yaml").unlink()
        report = agent_promote.validate_shape(ws)
        assert not report.shape_ok
        assert not report.has_agent_yaml

    def test_missing_skill_manifest_fails(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws", include_skill=False)
        report = agent_promote.validate_shape(ws)
        assert not report.shape_ok
        assert not report.has_skill_manifest

    def test_missing_e2e_fails(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws", include_e2e=False)
        report = agent_promote.validate_shape(ws)
        assert not report.shape_ok
        assert not report.has_e2e_test

    def test_extras_missing_reported(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws", extras=("README.md",))
        report = agent_promote.validate_shape(ws)
        assert report.shape_ok
        assert "README.md" in report.extras_present
        assert "requirements.txt" in report.extras_missing
        assert "episode_log.py" in report.extras_missing


class TestCopyPlan:
    def test_excludes_pipeline_internal(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        rels = {str(rel) for _, rel in agent_promote._iter_copy_plan(ws)}
        # Pipeline-internal must be excluded
        assert all(not r.startswith(".self-healing-pipeline") for r in rels)
        assert ".heartbeat-callback" not in rels
        # Canonical files must be present
        assert "agent.yaml" in rels
        assert "skills/sample/SKILL.md" in rels
        assert "tests/test_e2e_smoke.py" in rels
        assert "README.md" in rels

    def test_excludes_caches(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        (ws / "__pycache__").mkdir()
        (ws / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
        (ws / ".pytest_cache").mkdir()
        (ws / ".pytest_cache" / "v").write_text("", encoding="utf-8")
        rels = {str(rel) for _, rel in agent_promote._iter_copy_plan(ws)}
        assert all("__pycache__" not in r for r in rels)
        assert all(".pytest_cache" not in r for r in rels)


class TestPromote:
    def test_dry_run_does_not_write(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        target = tmp_path / "out"
        ok, msg = agent_promote.promote(ws, target, "sample", dry_run=True, force=False)
        assert ok
        assert "DRY RUN" in msg
        assert not (target / "sample").exists()

    def test_actual_copy_writes_canonical_shape(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        target = tmp_path / "out"
        ok, msg = agent_promote.promote(ws, target, "sample", dry_run=False, force=False)
        assert ok
        dest = target / "sample"
        assert (dest / "agent.yaml").is_file()
        assert (dest / "skills" / "sample" / "SKILL.md").is_file()
        assert (dest / "tests" / "test_e2e_smoke.py").is_file()
        assert (dest / "README.md").is_file()
        # Pipeline-internal must NOT be copied
        assert not (dest / ".self-healing-pipeline").exists()
        assert not (dest / ".heartbeat-callback").exists()

    def test_existing_dest_refused_without_force(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        target = tmp_path / "out"
        (target / "sample").mkdir(parents=True)
        ok, msg = agent_promote.promote(ws, target, "sample", dry_run=False, force=False)
        assert not ok
        assert "already exists" in msg

    def test_force_overwrites_existing(self, tmp_path):
        ws = _write_minimal_workspace(tmp_path / "ws")
        target = tmp_path / "out"
        dest = target / "sample"
        dest.mkdir(parents=True)
        (dest / "stale.txt").write_text("old", encoding="utf-8")
        ok, _ = agent_promote.promote(ws, target, "sample", dry_run=False, force=True)
        assert ok
        # Stale file remains because copy doesn't delete, but new files land
        assert (dest / "agent.yaml").is_file()
        # The promote() doesn't delete existing files — that's an intentional
        # limitation; --force only bypasses the existence check. Document if
        # this needs to change.


class TestMainIntegration:
    """End-to-end shape-only and refusal paths via main()."""

    def _run(self, monkeypatch, capsys, *argv):
        monkeypatch.setattr(sys, "argv", ["agent_promote.py", *argv])
        code = agent_promote.main()
        out = capsys.readouterr()
        return code, out.out, out.err

    def test_shape_only_pass(self, tmp_path, monkeypatch, capsys):
        ws = _write_minimal_workspace(tmp_path / "ws")
        code, out, _ = self._run(
            monkeypatch, capsys, "--workspace", str(ws), "--shape-only"
        )
        assert code == 0
        assert "SHAPE OK" in out

    def test_shape_only_fail(self, tmp_path, monkeypatch, capsys):
        ws = _write_minimal_workspace(tmp_path / "ws", include_skill=False)
        code, out, err = self._run(
            monkeypatch, capsys, "--workspace", str(ws), "--shape-only"
        )
        assert code == 1
        assert "SHAPE INVALID" in out + err

    def test_rejected_refused_without_force(self, tmp_path, monkeypatch, capsys):
        ws = _write_minimal_workspace(
            tmp_path / "ws",
            include_state={"status": "passed", "review_verdict": "rejected"},
        )
        target = tmp_path / "out"
        code, _, err = self._run(
            monkeypatch,
            capsys,
            "--workspace",
            str(ws),
            "--target",
            str(target),
            "--dry-run",
        )
        assert code == 1
        assert "REFUSED" in err
        assert not (target).exists() or not list(target.iterdir())

    def test_rejected_force_proceeds_to_dry_run(self, tmp_path, monkeypatch, capsys):
        ws = _write_minimal_workspace(
            tmp_path / "ws",
            include_state={"status": "passed", "review_verdict": "rejected"},
        )
        target = tmp_path / "out"
        code, out, _ = self._run(
            monkeypatch,
            capsys,
            "--workspace",
            str(ws),
            "--target",
            str(target),
            "--dry-run",
            "--force",
        )
        assert code == 0
        assert "DRY RUN" in out
        # No files written even with --force, because --dry-run
        assert not target.exists() or not list(target.iterdir())
