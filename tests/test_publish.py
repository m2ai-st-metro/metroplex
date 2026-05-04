"""Tests for Gate 4 — publish gate with multi-target mirroring."""
import os
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gates.publish import PublishGate
from audit import AuditLogger


# --- Subprocess routing helpers --------------------------------------------

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_router(rules, calls):
    """
    Build a subprocess.run replacement.

    rules: list of (predicate, FakeProc) tuples; first matching predicate wins.
    calls: list to which every received argv is appended for inspection.
    """
    def _run(argv, capture_output=True, text=True, timeout=None, **_):
        calls.append(argv)
        for pred, result in rules:
            if pred(argv):
                return result
        return FakeProc(returncode=0, stdout="", stderr="")
    return _run


def cmd_starts(argv, prefix):
    return argv[: len(prefix)] == list(prefix)


def cmd_contains(argv, needle):
    return any(needle in str(a) for a in argv)


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture
def project_with_git(tmp_path):
    """A minimal project directory that the publish gate will accept."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("placeholder")
    return tmp_path


@pytest.fixture
def published_build(in_memory_db, project_with_git):
    """Insert a completed+reviewed build_jobs row pointing at project_with_git."""
    in_memory_db.connect()
    in_memory_db.conn.execute(
        """
        INSERT INTO build_jobs
            (idea_id, title, spec_path, queue_job_id, status, queued_at,
             review_status, project_dir, base_job_id, retry_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "Sample Tool",
            "/tmp/spec.txt",
            "metroplex-test-1",
            "completed",
            datetime.now().isoformat(),
            "reviewed",
            str(project_with_git),
            "metroplex-test-1",
            0,
        ),
    )
    in_memory_db.conn.commit()
    return "metroplex-test-1"


@pytest.fixture
def gate(test_config, in_memory_db, temp_audit_log):
    return PublishGate(
        config=test_config,
        state_db=in_memory_db,
        audit_logger=AuditLogger(log_path=str(temp_audit_log)),
    )


# --- Tests ------------------------------------------------------------------

def test_dual_target_both_succeed(gate, published_build, in_memory_db):
    """Default config publishes to GitHub primary + GitLab mirror."""
    gate.config.publish_targets = ["github", "gitlab"]
    calls = []
    rules = [
        # GitHub: repo doesn't exist, then create succeeds
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("gh", "api")) and cmd_contains(a, "orgs/"), FakeProc(returncode=0)),
        # GitLab: project doesn't exist (404), then POST returns 201
        (lambda a: cmd_starts(a, ("curl",)) and cmd_contains(a, "/projects/") and "POST" not in a, FakeProc(returncode=0, stdout="404")),
        (lambda a: cmd_starts(a, ("curl",)) and "POST" in a, FakeProc(returncode=0, stdout="201")),
        # git: no existing remote, branch is main, push succeeds
        (lambda a: cmd_starts(a, ("git",)) and "remote" in a and "get-url" in a, FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("git",)) and "branch" in a, FakeProc(returncode=0, stdout="main\n")),
        (lambda a: cmd_starts(a, ("git",)) and "push" in a, FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch.dict(os.environ, {"GITLAB_TOKEN": "fake-token"}, clear=False):
        with patch("gates.publish.subprocess.run", make_router(rules, calls)):
            results = gate.run(dry_run=False)

    assert len(results) == 1
    job = results[0]
    assert job.status == "published"
    assert job.repo_url and "github.com" in job.repo_url
    assert any("gitlab.com" in u for u in job.mirror_urls)
    assert job.targets_status == {"github": "published", "gitlab": "published"}
    assert job.error is None
    # Both target remotes added
    pushed_remotes = [a for a in calls if cmd_starts(a, ("git",)) and "push" in a]
    assert any("origin" in c for c in pushed_remotes)
    assert any("gitlab" in c for c in pushed_remotes)


def test_primary_failure_fails_job(gate, published_build):
    """Primary (GitHub) creation failure aborts before any mirror attempt."""
    gate.config.publish_targets = ["github", "gitlab"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("gh", "api")), FakeProc(returncode=1, stderr="API error: rate limited")),
    ]
    with patch.dict(os.environ, {"GITLAB_TOKEN": "fake-token"}, clear=False):
        with patch("gates.publish.subprocess.run", make_router(rules, calls)):
            results = gate.run(dry_run=False)

    assert len(results) == 1
    job = results[0]
    assert job.status == "failed"
    assert "github (primary)" in (job.error or "")
    assert "rate limited" in (job.error or "")
    assert job.mirror_urls == []
    # GitLab side never invoked
    assert not any(cmd_starts(a, ("curl",)) for a in calls)


def test_mirror_failure_publishes_with_warning(gate, published_build):
    """Mirror push failure: status stays published, error notes the mirror failure."""
    gate.config.publish_targets = ["github", "gitlab"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("gh", "api")), FakeProc(returncode=0)),
        # GitLab: project exists check returns 200 (already there) - skip create
        (lambda a: cmd_starts(a, ("curl",)) and cmd_contains(a, "/projects/") and "POST" not in a, FakeProc(returncode=0, stdout="200")),
        (lambda a: cmd_starts(a, ("git",)) and "remote" in a and "get-url" in a, FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("git",)) and "branch" in a, FakeProc(returncode=0, stdout="main\n")),
        # GitHub push (origin) succeeds; GitLab push (gitlab remote) fails
        (lambda a: cmd_starts(a, ("git",)) and "push" in a and "gitlab" in a, FakeProc(returncode=1, stderr="Permission denied (publickey)")),
        (lambda a: cmd_starts(a, ("git",)) and "push" in a, FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch.dict(os.environ, {"GITLAB_TOKEN": "fake-token"}, clear=False):
        with patch("gates.publish.subprocess.run", make_router(rules, calls)):
            results = gate.run(dry_run=False)

    job = results[0]
    assert job.status == "published"
    assert job.repo_url and "github.com" in job.repo_url
    assert job.mirror_urls == []
    assert job.targets_status["github"] == "published"
    assert job.targets_status["gitlab"].startswith("failed:")
    assert "mirror failures" in (job.error or "")


def test_github_only(gate, published_build):
    """publish_targets=['github'] makes no GitLab calls."""
    gate.config.publish_targets = ["github"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)) and "remote" in a and "get-url" in a, FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("git",)) and "branch" in a, FakeProc(returncode=0, stdout="main\n")),
        (lambda a: cmd_starts(a, ("git",)) and "push" in a, FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch("gates.publish.subprocess.run", make_router(rules, calls)):
        results = gate.run(dry_run=False)

    job = results[0]
    assert job.status == "published"
    assert job.targets_status == {"github": "published"}
    assert job.mirror_urls == []
    assert not any(cmd_starts(a, ("curl",)) for a in calls)


def test_gitlab_only_as_primary(gate, published_build):
    """publish_targets=['gitlab'] makes GitLab the only/primary surface."""
    gate.config.publish_targets = ["gitlab"]
    calls = []
    rules = [
        # Project doesn't exist, create succeeds
        (lambda a: cmd_starts(a, ("curl",)) and cmd_contains(a, "/projects/") and "POST" not in a, FakeProc(returncode=0, stdout="404")),
        (lambda a: cmd_starts(a, ("curl",)) and "POST" in a, FakeProc(returncode=0, stdout="201")),
        (lambda a: cmd_starts(a, ("git",)) and "remote" in a and "get-url" in a, FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("git",)) and "branch" in a, FakeProc(returncode=0, stdout="main\n")),
        (lambda a: cmd_starts(a, ("git",)) and "push" in a, FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch.dict(os.environ, {"GITLAB_TOKEN": "fake-token"}, clear=False):
        with patch("gates.publish.subprocess.run", make_router(rules, calls)):
            results = gate.run(dry_run=False)

    job = results[0]
    assert job.status == "published"
    assert job.repo_url and "gitlab.com" in job.repo_url
    assert job.targets_status == {"gitlab": "published"}
    # No gh CLI invocation
    assert not any(cmd_starts(a, ("gh",)) for a in calls)
    # Primary uses 'origin' remote
    pushes = [a for a in calls if cmd_starts(a, ("git",)) and "push" in a]
    assert any("origin" in c for c in pushes)


def test_gitlab_missing_token(gate, published_build):
    """GitLab target fails clearly when GITLAB_TOKEN unset."""
    gate.config.publish_targets = ["gitlab"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    env_no_token = {k: v for k, v in os.environ.items() if k != "GITLAB_TOKEN"}
    with patch.dict(os.environ, env_no_token, clear=True):
        with patch("gates.publish.subprocess.run", make_router(rules, calls)):
            results = gate.run(dry_run=False)

    job = results[0]
    assert job.status == "failed"
    assert "GITLAB_TOKEN" in (job.error or "")


def test_persisted_targets_status_roundtrip(gate, published_build, in_memory_db):
    """targets_status and mirror_urls survive DB write+read."""
    gate.config.publish_targets = ["github", "gitlab"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("curl",)) and cmd_contains(a, "/projects/") and "POST" not in a, FakeProc(returncode=0, stdout="200")),
        (lambda a: cmd_starts(a, ("git",)) and "remote" in a and "get-url" in a, FakeProc(returncode=1)),
        (lambda a: cmd_starts(a, ("git",)) and "branch" in a, FakeProc(returncode=0, stdout="main\n")),
        (lambda a: cmd_starts(a, ("git",)) and "push" in a, FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch.dict(os.environ, {"GITLAB_TOKEN": "fake-token"}, clear=False):
        with patch("gates.publish.subprocess.run", make_router(rules, calls)):
            gate.run(dry_run=False)

    rows = in_memory_db.get_all_publish_jobs()
    assert len(rows) == 1
    row = rows[0]
    assert row["targets_status"] == {"github": "published", "gitlab": "published"}
    assert any("gitlab.com" in u for u in row["mirror_urls"])
