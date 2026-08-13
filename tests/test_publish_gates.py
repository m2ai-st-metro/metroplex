"""Tests for the publish-time defect gates added 2026-08 (audit of 10 published products).

Gate A: internal-artifact denylist, enforced in two independent places —
  gates/review.py (Gate 4.5, pre-push, blocking) and gates/publish.py (Gate 4,
  immediately before the actual git push, second layer).
Gate B: build-the-artifact verification (gates/review.py _check_builds_clean).
Gate C: claims-vs-code report (gates/readme.py _run_claims_check), report-only.
"""
import json
import subprocess
from pathlib import Path

import pytest

from gates.review import ReviewGate
from gates.publish import PublishGate
from gates.readme import ReadmeGate


# --- Gate A: review.py pre-push denylist ------------------------------------

class TestGateAReviewDenylist:
    @pytest.fixture
    def review_gate(self, test_config, in_memory_db, temp_audit_log):
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        return ReviewGate(config=test_config, state_db=in_memory_db, audit_logger=audit)

    def _base_project(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").write_text("# Project")
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("def test_x(): pass")
        return tmp_path

    @pytest.mark.parametrize("artifact_name", [
        ".codebase_learnings.json",
        ".linear_project.json",
        "app_spec.txt",
        ".claude_settings.json",
        ".heartbeat-callback",
        "spec.md",
    ])
    def test_denylisted_file_fails(self, review_gate, tmp_path, artifact_name):
        project = self._base_project(tmp_path)
        (project / artifact_name).write_text("internal pipeline state")
        passed, failed = review_gate._run_checks(project)
        hit = [f for f in failed if f.startswith("no_build_artifacts")]
        assert len(hit) == 1
        assert artifact_name in hit[0]

    @pytest.mark.parametrize("dir_name", ["venv", ".venv", "screenshots"])
    def test_denylisted_dir_fails(self, review_gate, tmp_path, dir_name):
        project = self._base_project(tmp_path)
        (project / dir_name).mkdir()
        (project / dir_name / "x.txt").write_text("x")
        passed, failed = review_gate._run_checks(project)
        hit = [f for f in failed if f.startswith("no_build_artifacts")]
        assert len(hit) == 1
        assert dir_name in hit[0]

    def test_leaked_home_path_in_content_fails(self, review_gate, tmp_path):
        project = self._base_project(tmp_path)
        (project / "notes.txt").write_text(
            "see /home/apexaipc/projects/metroplex/data/self_healing_queue/heartbeat-worker-1.txt"
        )
        passed, failed = review_gate._run_checks(project)
        hit = [f for f in failed if f.startswith("no_leaked_content")]
        assert len(hit) == 1
        assert "notes.txt" in hit[0]

    @pytest.mark.parametrize("leaked", ["M2A-4821", "metroplex-ideaforge-238"])
    def test_leaked_ticket_or_job_id_in_content_fails(self, review_gate, tmp_path, leaked):
        project = self._base_project(tmp_path)
        (project / "notes.txt").write_text(f"ref {leaked} for context")
        passed, failed = review_gate._run_checks(project)
        hit = [f for f in failed if f.startswith("no_leaked_content")]
        assert len(hit) == 1

    def test_leaked_id_in_commit_message_fails(self, review_gate, tmp_path):
        project = self._base_project(tmp_path)
        subprocess.run(["git", "init"], cwd=project, capture_output=True)
        subprocess.run(["git", "-C", str(project), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(project), "config", "user.name", "t"], capture_output=True)
        subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True)
        subprocess.run(
            ["git", "-C", str(project), "commit", "-m",
             "Initial build via self-healing daemon (job metroplex-ideaforge-238)"],
            capture_output=True,
        )
        passed, failed = review_gate._run_checks(project)
        hit = [f for f in failed if f.startswith("no_leaked_commit_messages")]
        assert len(hit) == 1

    def test_clean_project_passes_gate_a_checks(self, review_gate, tmp_path):
        project = self._base_project(tmp_path)
        # Disable Gate B (build verify) for this check — this project has no
        # pyproject.toml/setup.py, so it self-skips anyway, but be explicit.
        passed, failed = review_gate._run_checks(project)
        assert "no_build_artifacts" in passed
        assert "no_leaked_content" in passed
        assert "no_leaked_commit_messages" in passed


# --- Gate A: publish.py second independent layer ----------------------------

class TestGateAPublishSecondLayer:
    @pytest.fixture
    def publish_gate(self, test_config, in_memory_db, temp_audit_log):
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        return PublishGate(config=test_config, state_db=in_memory_db, audit_logger=audit)

    def test_denylisted_file_blocks_push_before_any_subprocess(self, publish_gate, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        (tmp_path / "app_spec.txt").write_text("internal spec")

        calls = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            raise AssertionError("subprocess.run must not be called once the denylist scan hits")

        monkeypatch.setattr("gates.publish.subprocess.run", fake_run)

        status, url, error = publish_gate._publish_to_target(
            "github", tmp_path, "some-repo", "Some Repo", remote_name="origin"
        )
        assert status == "failed"
        assert "denylist" in error
        assert "app_spec.txt" in error
        assert calls == []

    def test_clean_project_reaches_target_dispatch(self, publish_gate, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        (tmp_path / "README.md").write_text("# ok")

        dispatched = {}

        def fake_publish_github(self, project_dir, repo_name, title, remote_name):
            dispatched["called"] = True
            return ("published", "https://github.com/org/repo", None)

        monkeypatch.setattr(PublishGate, "_publish_github", fake_publish_github)

        status, url, error = publish_gate._publish_to_target(
            "github", tmp_path, "some-repo", "Some Repo", remote_name="origin"
        )
        assert dispatched.get("called") is True
        assert status == "published"

    def test_scan_reuses_review_gate_constants_not_a_copy(self):
        """The two layers must share one source of truth for the denylist,
        never two independently-maintained lists that can drift apart."""
        import gates.publish as publish_mod
        import gates.review as review_mod
        assert publish_mod.BUILD_ARTIFACT_FILES is review_mod.BUILD_ARTIFACT_FILES
        assert publish_mod.BUILD_ARTIFACT_DIRS is review_mod.BUILD_ARTIFACT_DIRS
        assert publish_mod.LEAK_CONTENT_PATTERNS is review_mod.LEAK_CONTENT_PATTERNS


# --- Gate B: build-the-artifact verification ---------------------------------

class TestGateBBuildVerify:
    @pytest.fixture
    def review_gate(self, test_config, in_memory_db, temp_audit_log):
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        return ReviewGate(config=test_config, state_db=in_memory_db, audit_logger=audit)

    def test_skips_non_python_project(self, review_gate, tmp_path):
        (tmp_path / "index.js").write_text("console.log('hi')")
        ok, detail = review_gate._check_builds_clean(tmp_path)
        assert ok is True
        assert "skipped" in detail

    def test_no_importable_package_fails(self, review_gate, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\nversion='0.1.0'\n"
            "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        )
        # No __init__.py anywhere -> _infer_package_name returns None
        ok, detail = review_gate._check_builds_clean(tmp_path)
        assert ok is False
        assert "no importable package name" in detail

    def test_real_installable_package_passes(self, review_gate, tmp_path):
        """91-passing-tests-but-broken-package is exactly the hole this closes:
        build a genuinely pip-installable, importable package and confirm the
        gate installs it into a throwaway venv and imports it successfully."""
        pkg_dir = tmp_path / "widget_tool"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("VERSION = '0.1.0'\n")
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'widget-tool'\nversion = '0.1.0'\n"
            "[build-system]\nrequires = ['setuptools>=61']\nbuild-backend = 'setuptools.build_meta'\n"
        )
        ok, detail = review_gate._check_builds_clean(tmp_path)
        assert ok is True, detail
        assert "widget_tool" in detail

    def test_broken_import_fails(self, review_gate, tmp_path):
        """Package installs fine but raises on import — the exact failure
        mode the audit found ('91 passing tests... still fail to import')."""
        pkg_dir = tmp_path / "broken_tool"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("raise ImportError('missing real dependency')\n")
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'broken-tool'\nversion = '0.1.0'\n"
            "[build-system]\nrequires = ['setuptools>=61']\nbuild-backend = 'setuptools.build_meta'\n"
        )
        ok, detail = review_gate._check_builds_clean(tmp_path)
        assert ok is False
        assert "import broken_tool failed" in detail


# --- Gate C: claims-vs-code report (readme.py) -------------------------------

class TestGateCClaimsCheck:
    @pytest.fixture
    def readme_gate(self, test_config, in_memory_db, temp_audit_log):
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        return ReadmeGate(config=test_config, state_db=in_memory_db, audit_logger=audit)

    def test_extracts_bullets_from_does_section(self, readme_gate):
        content = (
            "# Title\n\n"
            "### 5. What Widget Tool Does\n"
            "- Retrains automatically on performance degradation\n"
            "- Exports reports to CSV\n\n"
            "### 6. Quick Start\n"
            "- not a claim bullet\n"
        )
        bullets = readme_gate._extract_feature_bullets(content)
        assert bullets == [
            "Retrains automatically on performance degradation",
            "Exports reports to CSV",
        ]

    def test_unbacked_claim_flagged_and_report_written(self, readme_gate, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text("def export_csv():\n    return 'ok'\n")

        readme_content = (
            "### 5. What Widget Does\n"
            "- Automatic retraining on performance degradation\n"
            "- Exports reports to CSV\n"
        )
        readme_gate._run_claims_check("job-1", "Widget", readme_content, project)

        report_path = tmp_path / "data" / "claims_reports" / "job-1.json"
        assert report_path.is_file()
        report = json.loads(report_path.read_text())
        assert report["claims_total"] == 2
        # "Exports reports to CSV" has keyword backing (export_csv, reports, csv-ish);
        # "Automatic retraining..." has no backing anywhere in main.py.
        assert any("retraining" in c.lower() for c in report["claims_unbacked"])

    def test_all_claims_backed_writes_empty_unbacked_list(self, readme_gate, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text("def export_reports_to_csv():\n    return 'ok'\n")

        readme_content = (
            "### 5. What Widget Does\n"
            "- Exports reports to CSV\n"
        )
        readme_gate._run_claims_check("job-2", "Widget", readme_content, project)
        report = json.loads((tmp_path / "data" / "claims_reports" / "job-2.json").read_text())
        assert report["claims_unbacked"] == []

    def test_no_does_section_is_a_noop(self, readme_gate, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        readme_gate._run_claims_check("job-3", "Widget", "# Just a title\n", project)
        assert not (tmp_path / "data" / "claims_reports" / "job-3.json").exists()

    def test_claims_check_never_raises_and_never_blocks_readme_job(self, readme_gate, tmp_path, monkeypatch):
        """_process_one wraps this in try/except — verify a hard failure inside
        the checker (bad project_path) is swallowed, not propagated."""
        monkeypatch.chdir(tmp_path)
        # project_path does not exist -> rglob would raise if not handled
        bogus = tmp_path / "does-not-exist"
        try:
            readme_gate._run_claims_check("job-4", "Widget", "### 5. What X Does\n- claim\n", bogus)
        except Exception as e:
            pytest.fail(f"_run_claims_check must not raise: {e}")
