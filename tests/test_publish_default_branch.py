"""Tests for Gate 4 publish default-branch behavior.

Regression tests for the bug where Metroplex's publish gate left newly-created
GitHub/GitLab repos with the build's *feature* branch as `default_branch`,
because it pushed only the currently-checked-out branch and never PATCHed
the host's default-branch field.

Diagnostic: ~/diagnostics/diagnose-default-branch-2026-05-05.md (HIGH conf).
Fix: gates/publish.py::_push_to_remote — push local `main` first if it
exists, then push the feature branch, then call `gh api ... PATCH
default_branch=main` (and the GitLab analog) idempotently.
"""
import os
from datetime import datetime
from unittest.mock import patch

import pytest

from gates.publish import PublishGate
from audit import AuditLogger


# --- Subprocess routing helpers (mirrors test_publish.py) -------------------

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_router(rules, calls):
    """Build a subprocess.run replacement that records every argv."""
    def _run(argv, capture_output=True, text=True, timeout=None, **_):
        calls.append(list(argv))
        for pred, result in rules:
            if pred(argv):
                return result
        return FakeProc(returncode=0, stdout="", stderr="")
    return _run


def cmd_starts(argv, prefix):
    return list(argv[: len(prefix)]) == list(prefix)


def cmd_contains(argv, needle):
    return any(needle in str(a) for a in argv)


def is_git_push(argv, branch=None):
    if not cmd_starts(argv, ("git",)):
        return False
    if "push" not in argv:
        return False
    if branch is not None and branch not in argv:
        return False
    return True


def is_rev_parse_main(argv):
    return (
        cmd_starts(argv, ("git",))
        and "rev-parse" in argv
        and "refs/heads/main" in argv
    )


def is_gh_patch_default(argv):
    return (
        cmd_starts(argv, ("gh", "api"))
        and "PATCH" in argv
        and any("default_branch=main" in str(a) for a in argv)
    )


def is_glab_put_default(argv):
    return (
        cmd_starts(argv, ("glab", "api"))
        and "PUT" in argv
        and any("default_branch=main" in str(a) for a in argv)
    )


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture
def project_with_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("placeholder")
    return tmp_path


@pytest.fixture
def published_build(in_memory_db, project_with_git):
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
            "Default Branch Test",
            "/tmp/spec.txt",
            "metroplex-defbranch-1",
            "completed",
            datetime.now().isoformat(),
            "reviewed",
            str(project_with_git),
            "metroplex-defbranch-1",
            0,
        ),
    )
    in_memory_db.conn.commit()
    return "metroplex-defbranch-1"


@pytest.fixture
def gate(test_config, in_memory_db, temp_audit_log):
    return PublishGate(
        config=test_config,
        state_db=in_memory_db,
        audit_logger=AuditLogger(log_path=str(temp_audit_log)),
    )


# --- Tests ------------------------------------------------------------------

def test_pushes_main_before_feature_branch(gate, published_build):
    """When local main + feature branch both exist, main is pushed FIRST."""
    gate.config.publish_targets = ["github"]
    calls = []
    rules = [
        # GitHub repo doesn't exist -> created OK
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=1)),
        (
            lambda a: cmd_starts(a, ("gh", "api"))
            and cmd_contains(a, "orgs/")
            and "PATCH" not in a,
            FakeProc(returncode=0),
        ),
        # PATCH default_branch succeeds
        (is_gh_patch_default, FakeProc(returncode=0)),
        # No existing remote
        (
            lambda a: cmd_starts(a, ("git",))
            and "remote" in a
            and "get-url" in a,
            FakeProc(returncode=1),
        ),
        # Current branch is the feature branch
        (
            lambda a: cmd_starts(a, ("git",)) and "branch" in a and "--show-current" in a,
            FakeProc(returncode=0, stdout="feature/self-heal-test\n"),
        ),
        # Local main DOES exist
        (is_rev_parse_main, FakeProc(returncode=0)),
        # Both pushes succeed
        (lambda a: is_git_push(a), FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch("gates.publish.subprocess.run", make_router(rules, calls)):
        results = gate.run(dry_run=False)

    job = results[0]
    assert job.status == "published", f"unexpected error: {job.error}"

    # Find the push calls in order
    pushes = [c for c in calls if is_git_push(c)]
    assert len(pushes) >= 2, f"expected at least 2 pushes (main + feature), got {pushes}"
    # main must be pushed before the feature branch
    main_idx = next(i for i, c in enumerate(pushes) if "main" in c)
    feature_idx = next(
        i for i, c in enumerate(pushes) if "feature/self-heal-test" in c
    )
    assert main_idx < feature_idx, (
        f"main should be pushed before feature branch; "
        f"got main_idx={main_idx}, feature_idx={feature_idx}, pushes={pushes}"
    )


def test_patches_default_branch_to_main_after_push(gate, published_build):
    """After pushing, gate calls `gh api ... PATCH default_branch=main`."""
    gate.config.publish_targets = ["github"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=1)),
        (
            lambda a: cmd_starts(a, ("gh", "api"))
            and cmd_contains(a, "orgs/")
            and "PATCH" not in a,
            FakeProc(returncode=0),
        ),
        (is_gh_patch_default, FakeProc(returncode=0)),
        (
            lambda a: cmd_starts(a, ("git",))
            and "remote" in a
            and "get-url" in a,
            FakeProc(returncode=1),
        ),
        (
            lambda a: cmd_starts(a, ("git",)) and "branch" in a and "--show-current" in a,
            FakeProc(returncode=0, stdout="feature/x\n"),
        ),
        (is_rev_parse_main, FakeProc(returncode=0)),
        (lambda a: is_git_push(a), FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch("gates.publish.subprocess.run", make_router(rules, calls)):
        gate.run(dry_run=False)

    patch_calls = [c for c in calls if is_gh_patch_default(c)]
    assert len(patch_calls) == 1, (
        f"expected exactly one gh api PATCH default_branch=main call, got {patch_calls}"
    )
    patch_call = patch_calls[0]
    # repos/{org}/{repo} path must reference the right repo
    assert any("repos/" in str(a) and "default-branch-test" in str(a) for a in patch_call), (
        f"PATCH call should target the new repo, got: {patch_call}"
    )


def test_patches_default_branch_via_glab_for_gitlab(gate, published_build):
    """GitLab analog: `glab api projects/... --method PUT -f default_branch=main`."""
    gate.config.publish_targets = ["gitlab"]
    calls = []
    rules = [
        # Project exists check returns 200 -> skip create
        (
            lambda a: cmd_starts(a, ("curl",))
            and cmd_contains(a, "/projects/")
            and "POST" not in a,
            FakeProc(returncode=0, stdout="200"),
        ),
        # glab default-branch PUT succeeds
        (is_glab_put_default, FakeProc(returncode=0)),
        (
            lambda a: cmd_starts(a, ("git",))
            and "remote" in a
            and "get-url" in a,
            FakeProc(returncode=1),
        ),
        (
            lambda a: cmd_starts(a, ("git",)) and "branch" in a and "--show-current" in a,
            FakeProc(returncode=0, stdout="feature/x\n"),
        ),
        (is_rev_parse_main, FakeProc(returncode=0)),
        (lambda a: is_git_push(a), FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch.dict(os.environ, {"GITLAB_TOKEN": "fake-token"}, clear=False):
        with patch("gates.publish.subprocess.run", make_router(rules, calls)):
            gate.run(dry_run=False)

    glab_calls = [c for c in calls if is_glab_put_default(c)]
    assert len(glab_calls) == 1, (
        f"expected exactly one glab api PUT default_branch=main call, got {glab_calls}"
    )


def test_no_local_main_falls_back_to_feature_only(gate, published_build):
    """If local main does not exist, push only the current (feature) branch.

    The PATCH may fail server-side because main isn't there; that's logged
    as a warning, not a publish failure. The publish still reports success.
    """
    gate.config.publish_targets = ["github"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=1)),
        (
            lambda a: cmd_starts(a, ("gh", "api"))
            and cmd_contains(a, "orgs/")
            and "PATCH" not in a,
            FakeProc(returncode=0),
        ),
        # PATCH fails (main isn't on the remote) — must NOT fail the publish
        (
            is_gh_patch_default,
            FakeProc(returncode=1, stderr='HTTP 422: branch "main" does not exist'),
        ),
        (
            lambda a: cmd_starts(a, ("git",))
            and "remote" in a
            and "get-url" in a,
            FakeProc(returncode=1),
        ),
        (
            lambda a: cmd_starts(a, ("git",)) and "branch" in a and "--show-current" in a,
            FakeProc(returncode=0, stdout="feature/no-main\n"),
        ),
        # Local main does NOT exist
        (is_rev_parse_main, FakeProc(returncode=1)),
        (lambda a: is_git_push(a), FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch("gates.publish.subprocess.run", make_router(rules, calls)):
        results = gate.run(dry_run=False)

    job = results[0]
    # Publish must NOT fail because of the PATCH failure
    assert job.status == "published", f"unexpected: {job.status} / {job.error}"
    # Only the feature branch was pushed (no main push)
    pushes = [c for c in calls if is_git_push(c)]
    assert len(pushes) == 1, f"expected exactly 1 push (feature only), got {pushes}"
    assert "feature/no-main" in pushes[0]
    assert not any("main" in c and "feature" not in str(c) for c in pushes), (
        f"should NOT have pushed main when local main doesn't exist; pushes={pushes}"
    )


def test_patch_failure_does_not_fail_publish(gate, published_build):
    """gh api PATCH default_branch failing logs warning but publish succeeds."""
    gate.config.publish_targets = ["github"]
    calls = []
    rules = [
        (lambda a: cmd_starts(a, ("gh", "repo", "view")), FakeProc(returncode=1)),
        (
            lambda a: cmd_starts(a, ("gh", "api"))
            and cmd_contains(a, "orgs/")
            and "PATCH" not in a,
            FakeProc(returncode=0),
        ),
        # PATCH default_branch fails
        (is_gh_patch_default, FakeProc(returncode=1, stderr="HTTP 403: Forbidden")),
        (
            lambda a: cmd_starts(a, ("git",))
            and "remote" in a
            and "get-url" in a,
            FakeProc(returncode=1),
        ),
        (
            lambda a: cmd_starts(a, ("git",)) and "branch" in a and "--show-current" in a,
            FakeProc(returncode=0, stdout="feature/x\n"),
        ),
        (is_rev_parse_main, FakeProc(returncode=0)),
        (lambda a: is_git_push(a), FakeProc(returncode=0)),
        (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
    ]
    with patch("gates.publish.subprocess.run", make_router(rules, calls)):
        results = gate.run(dry_run=False)

    job = results[0]
    assert job.status == "published", (
        f"PATCH failure should NOT fail publish; got {job.status} / {job.error}"
    )


def test_idempotent_double_publish(gate, published_build, in_memory_db):
    """Running the publish flow twice in a row does not error.

    Second run's _push_to_remote sees the remote already configured, the
    repo already on GitHub, and the default_branch already set. Everything
    must be a no-op without raising.
    """
    gate.config.publish_targets = ["github"]
    calls = []

    def rules_for_run(repo_exists: bool):
        return [
            # gh repo view: exists on second run, missing on first
            (
                lambda a: cmd_starts(a, ("gh", "repo", "view")),
                FakeProc(returncode=0 if repo_exists else 1),
            ),
            (
                lambda a: cmd_starts(a, ("gh", "api"))
                and cmd_contains(a, "orgs/")
                and "PATCH" not in a,
                FakeProc(returncode=0),
            ),
            # PATCH default_branch always succeeds (even if no-op server-side)
            (is_gh_patch_default, FakeProc(returncode=0)),
            # Remote exists on second run
            (
                lambda a: cmd_starts(a, ("git",))
                and "remote" in a
                and "get-url" in a,
                FakeProc(returncode=0 if repo_exists else 1),
            ),
            (
                lambda a: cmd_starts(a, ("git",))
                and "branch" in a
                and "--show-current" in a,
                FakeProc(returncode=0, stdout="feature/x\n"),
            ),
            (is_rev_parse_main, FakeProc(returncode=0)),
            (lambda a: is_git_push(a), FakeProc(returncode=0)),
            (lambda a: cmd_starts(a, ("git",)), FakeProc(returncode=0)),
        ]

    # First run: fresh repo
    with patch("gates.publish.subprocess.run", make_router(rules_for_run(False), calls)):
        first = gate.run(dry_run=False)
    assert first[0].status == "published"

    # Re-mark the build as unpublished so the gate picks it up again.
    # get_unpublished_builds() filters out queue_job_ids that already have
    # a publish_jobs row with status='published'; deleting that row is enough
    # to make the build re-pickable.
    in_memory_db.conn.execute(
        "DELETE FROM publish_jobs WHERE build_job_id = ?",
        (published_build,),
    )
    in_memory_db.conn.commit()

    # Second run: everything already in place
    calls.clear()
    with patch("gates.publish.subprocess.run", make_router(rules_for_run(True), calls)):
        second = gate.run(dry_run=False)
    assert second[0].status == "published", (
        f"second run should also publish cleanly; got {second[0].status} / {second[0].error}"
    )
    # Second run still includes a PATCH (idempotent — no-op server-side)
    assert any(is_gh_patch_default(c) for c in calls), (
        "second run should still call PATCH default_branch=main (idempotent)"
    )
