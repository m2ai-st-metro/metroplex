"""
End-to-end integration tests for the scoring_rubric flow (R-A item 3).

Exercises the full path: build_jobs row carries scoring_rubric -> orchestrator's
quality-scoring helper reads it -> score_project(scoring_rubric=...) applies
(or bypasses) the life_domain category gate -> quality_score persisted on
the build row.

Daemon-safety constraints (per the user spec):
    - Tests NEVER instantiate StateDB against the default `data/metroplex.db`.
      All StateDB instances use `tmp_path / "test.db"`.
    - All filesystem fixtures live under pytest's `tmp_path` (auto-cleaned).
    - No symlinks across the gate; project_dir paths resolve under tmp_path.
"""
import pytest
from datetime import datetime
from unittest.mock import MagicMock
from pathlib import Path

from db import StateDB
from models import BuildJob
from orchestrator import CycleOrchestrator
from audit import AuditLogger
from safety import CircuitBreaker, CycleCaps, ShutdownHandler
from config import Config


# --- fixtures (tmp_path-only; live DB never touched) ---


@pytest.fixture
def tmp_state_db(tmp_path):
    """Tmp StateDB instance — never points at data/metroplex.db."""
    db_path = tmp_path / "test_state.db"
    # Explicit guard: refuse to proceed if someone changes the default path
    # to something inside the project tree.
    assert str(db_path).startswith(str(tmp_path)), (
        "test_state.db must live under tmp_path; this guard catches "
        "accidental writes to the live data/ tree"
    )
    db = StateDB(str(db_path))
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def tmp_orchestrator(tmp_state_db, tmp_path):
    """Build a real CycleOrchestrator wired against the tmp StateDB.

    All gates are MagicMocks since this test only exercises the
    _score_review_pass_builds helper. Notifier is a Mock.
    """
    config = Config()
    audit_log_path = tmp_path / "audit.jsonl"
    audit_logger = AuditLogger(str(audit_log_path))
    circuit_breaker = CircuitBreaker(threshold=3, state_db=tmp_state_db)
    cycle_caps = CycleCaps(config)
    shutdown_handler = ShutdownHandler()

    orch = CycleOrchestrator(
        config=config,
        triage_gate=MagicMock(),
        build_orchestrator=MagicMock(),
        circuit_breaker=circuit_breaker,
        cycle_caps=cycle_caps,
        shutdown_handler=shutdown_handler,
        state_db=tmp_state_db,
        audit_logger=audit_logger,
        cycle_sleep_seconds=1,
    )
    return orch, audit_log_path


def _seed_completed_build(state_db, *, queue_job_id, project_dir, scoring_rubric):
    """Helper: insert a build_jobs row in 'completed'+'reviewed' state with
    the given scoring_rubric and project_dir.
    """
    job = BuildJob(
        idea_id=int(queue_job_id.split("-")[-1]) if queue_job_id.split("-")[-1].isdigit() else 1,
        title=f"Title-{queue_job_id}",
        spec_path="/tmp/spec.txt",
        queue_job_id=queue_job_id,
        status="queued",
        queued_at=datetime.now(),
        scoring_rubric=scoring_rubric,
    )
    state_db.record_build_job(job)
    state_db.update_build_job_status(queue_job_id, "completed")
    state_db.update_build_job_project_dir(queue_job_id, str(project_dir))
    state_db.update_build_review_status(queue_job_id, "reviewed")


def _stub_review_pass(queue_job_id, title):
    r = MagicMock()
    r.verdict = "pass"
    r.queue_job_id = queue_job_id
    r.title = title
    return r


def _make_full_agent_shape(project_dir: Path) -> None:
    """Create a directory that satisfies all three life_domain shape checks
    plus enough scoring fodder to produce a positive total_score.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "agent.yaml").write_text("name: test-agent\n")
    skill_dir = project_dir / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Foo Skill\n")
    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_e2e_foo.py").write_text(
        "def test_e2e_foo():\n    assert True\n"
    )
    (project_dir / "README.md").write_text("# Agent\n")
    (project_dir / "requirements.txt").write_text("requests==2.31.0\n")
    (project_dir / "agent.py").write_text("def run():\n    return 0\n")
    (project_dir / ".gitignore").write_text("__pycache__/\n")


# --- C5a: life_domain rubric gates a no-shape project ---


def test_life_domain_rubric_gates_no_shape_project_e2e(tmp_orchestrator, tmp_path):
    """Full flow: build with rubric='life_domain' + project_dir lacking
    agent.yaml -> quality_score persisted as 0.0 (gate fired)."""
    orch, _audit_path = tmp_orchestrator
    state_db = orch.state_db

    project_dir = tmp_path / "no_shape_proj"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("only a readme\n")
    # No agent.yaml, no skills/, no E2E test -> gate must fire.

    _seed_completed_build(
        state_db,
        queue_job_id="metroplex-ideaforge-501",
        project_dir=project_dir,
        scoring_rubric="life_domain",
    )

    review_results = [_stub_review_pass("metroplex-ideaforge-501", "T-501")]
    scored = orch._score_review_pass_builds(review_results, dry_run=False)

    assert scored == 1
    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-501")
    assert row is not None
    assert row["quality_score"] == 0.0, (
        f"life_domain gate should have set quality_score=0.0; got {row['quality_score']}"
    )
    assert row["scoring_rubric"] == "life_domain"


def test_life_domain_audit_emits_category_failure_reason(tmp_orchestrator, tmp_path):
    """When the gate fires, the audit log entry carries category_failure_reason
    in details — operator-visible signal that the score=0 is intentional, not a
    scoring bug."""
    import json

    orch, audit_path = tmp_orchestrator
    state_db = orch.state_db

    project_dir = tmp_path / "no_agent_yaml_proj"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("x")
    # Skills + E2E test exist but agent.yaml is missing -> reason locked.
    skill_dir = project_dir / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Foo\n")
    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_e2e_foo.py").write_text("def test_e2e_foo(): pass\n")

    _seed_completed_build(
        state_db,
        queue_job_id="metroplex-ideaforge-502",
        project_dir=project_dir,
        scoring_rubric="life_domain",
    )

    review_results = [_stub_review_pass("metroplex-ideaforge-502", "T-502")]
    orch._score_review_pass_builds(review_results, dry_run=False)

    # AuditLogger appends a JSON line per decision. Read and grep.
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    quality_entries = [
        json.loads(line) for line in audit_lines
        if line.strip() and json.loads(line).get("gate") == "quality"
    ]
    assert len(quality_entries) >= 1
    last = quality_entries[-1]
    details = last.get("details", {})
    assert details.get("scoring_rubric") == "life_domain"
    assert details.get("category_failed") is True
    assert details.get("category_failure_reason") == "missing_agent_yaml"


# --- C5b: life_domain rubric on a full-shape project produces a positive score ---


def test_life_domain_rubric_full_shape_produces_positive_score(
    tmp_orchestrator, tmp_path,
):
    """Build with rubric='life_domain' + project_dir containing agent.yaml +
    skills/foo/SKILL.md + tests/test_e2e_foo.py + README + source ->
    quality_score > 0 (gate passes, static scoring runs)."""
    orch, _ = tmp_orchestrator
    state_db = orch.state_db

    project_dir = tmp_path / "full_shape_proj"
    _make_full_agent_shape(project_dir)

    _seed_completed_build(
        state_db,
        queue_job_id="metroplex-ideaforge-503",
        project_dir=project_dir,
        scoring_rubric="life_domain",
    )

    review_results = [_stub_review_pass("metroplex-ideaforge-503", "T-503")]
    scored = orch._score_review_pass_builds(review_results, dry_run=False)

    assert scored == 1
    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-503")
    assert row is not None
    assert row["quality_score"] > 0, (
        f"full-shape life_domain build should score > 0; got {row['quality_score']}"
    )


# --- C5c: tech rubric on no-shape project still positive (gate bypassed) ---


def test_tech_rubric_does_not_gate_no_shape_project(tmp_orchestrator, tmp_path):
    """Tech rubric is the publish-path-safe bypass: gate must NOT apply even
    when agent shape is absent."""
    orch, _ = tmp_orchestrator
    state_db = orch.state_db

    project_dir = tmp_path / "tech_proj"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# Tech\n")
    (project_dir / "main.py").write_text("print('hi')\n")
    (project_dir / "requirements.txt").write_text("flask\n")

    _seed_completed_build(
        state_db,
        queue_job_id="metroplex-ideaforge-504",
        project_dir=project_dir,
        scoring_rubric="tech",
    )

    review_results = [_stub_review_pass("metroplex-ideaforge-504", "T-504")]
    scored = orch._score_review_pass_builds(review_results, dry_run=False)

    assert scored == 1
    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-504")
    assert row is not None
    assert row["quality_score"] > 0, (
        "tech rubric must bypass the gate; got "
        f"quality_score={row['quality_score']}"
    )
    assert row["scoring_rubric"] == "tech"


# --- C5d: NULL rubric (legacy row) still positive (backward-compat) ---


def test_null_rubric_legacy_row_backward_compat(tmp_orchestrator, tmp_path):
    """Pre-rubric legacy build rows have scoring_rubric=NULL. The scorer
    must treat NULL identically to today (no gate) — backward-compat is
    load-bearing for builds queued before this migration."""
    orch, _ = tmp_orchestrator
    state_db = orch.state_db

    project_dir = tmp_path / "legacy_null_proj"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# Legacy\n")
    (project_dir / "main.py").write_text("print('legacy')\n")

    _seed_completed_build(
        state_db,
        queue_job_id="metroplex-ideaforge-505",
        project_dir=project_dir,
        scoring_rubric=None,
    )

    review_results = [_stub_review_pass("metroplex-ideaforge-505", "T-505")]
    scored = orch._score_review_pass_builds(review_results, dry_run=False)

    assert scored == 1
    row = state_db.get_build_by_queue_job_id("metroplex-ideaforge-505")
    assert row is not None
    assert row["scoring_rubric"] is None
    assert row["quality_score"] > 0, (
        "NULL rubric must preserve pre-R-A-item-4 behavior (no gate); got "
        f"quality_score={row['quality_score']}"
    )


# --- C8 guard: this test file never instantiates StateDB against the live DB ---


def test_test_file_only_constructs_statedb_with_tmp_paths():
    """Static defense (C8): every StateDB(...) construction in this test file
    must use a tmp_path-rooted argument. The check is narrowly scoped to
    `StateDB(` call sites so docstrings/comments that mention the live DB
    path for human readers do not false-positive.
    """
    import re

    source = Path(__file__).read_text(encoding="utf-8")

    # Find every StateDB(...) call. The argument must be either:
    #   - a variable name (e.g. `str(db_path)`) that we trust by convention
    #   - a tmp_path expression (e.g. `str(tmp_path / "..."`)
    # but MUST NOT be a string literal containing "data/metroplex.db".
    call_sites = re.findall(r"StateDB\(([^)]*)\)", source)
    assert call_sites, "expected at least one StateDB(...) construction"

    for arg in call_sites:
        assert '"data/' not in arg and "'data/" not in arg, (
            f"StateDB call site uses a forbidden live-DB path literal: "
            f"StateDB({arg}). Use tmp_path."
        )
