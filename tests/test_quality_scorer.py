"""
Tests for Structural Quality Scorer (Phase 14b).
"""
import pytest
from pathlib import Path

from gates.quality_scorer import score_project, QualityBreakdown, _is_test_file


def _make_life_domain_project(
    tmp_path: Path,
    *,
    agent_yaml: bool = True,
    skill: bool = True,
    e2e_test: bool = True,
    skill_name: str = "foo",
    e2e_filename: str = "test_e2e_foo.py",
) -> Path:
    """Helper: build a minimal agent project shape under tmp_path.

    Toggle each component independently to test missing-shape cases.
    """
    if agent_yaml:
        (tmp_path / "agent.yaml").write_text("name: Test Agent\n")
    if skill:
        skill_dir = tmp_path / "skills" / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# foo skill")
    if e2e_test:
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / e2e_filename).write_text(
            "def test_e2e(): assert True\n"
        )
    return tmp_path


class TestScoreProject:
    """Tests for the score_project function."""

    def test_empty_directory(self, tmp_path):
        """Empty directory scores only no_secrets (no secret files to find)."""
        breakdown = score_project(tmp_path)
        assert breakdown.has_source_code == 0.0
        assert breakdown.source_file_count == 0
        assert breakdown.no_secrets == 8.0  # No files = no secrets

    def test_nonexistent_directory(self, tmp_path):
        """Nonexistent directory scores 0."""
        breakdown = score_project(tmp_path / "nope")
        assert breakdown.total_score == 0.0

    def test_minimal_project(self, tmp_path):
        """Single source file gets has_source_code + file_count points."""
        (tmp_path / "main.py").write_text("print('hello')")
        breakdown = score_project(tmp_path)
        assert breakdown.has_source_code == 10.0
        assert breakdown.source_file_count == 1
        assert breakdown.total_score >= 10.0

    def test_readme_adds_points(self, tmp_path):
        """README.md adds 8 points."""
        (tmp_path / "main.py").write_text("x = 1")
        (tmp_path / "README.md").write_text("# Project")
        breakdown = score_project(tmp_path)
        assert breakdown.has_readme == 8.0

    def test_gitignore_adds_points(self, tmp_path):
        """gitignore adds 4 points."""
        (tmp_path / "main.py").write_text("x = 1")
        (tmp_path / ".gitignore").write_text("__pycache__/")
        breakdown = score_project(tmp_path)
        assert breakdown.has_gitignore == 4.0

    def test_test_files_add_points(self, tmp_path):
        """Test files add has_tests (12) + test_ratio points."""
        (tmp_path / "app.py").write_text("x = 1")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_app.py").write_text("def test_x(): pass")
        breakdown = score_project(tmp_path)
        assert breakdown.has_tests == 12.0
        assert breakdown.test_file_count == 1
        assert breakdown.test_ratio > 0
        assert breakdown.source_file_count == 2  # app.py + test_app.py

    def test_test_ratio_scaling(self, tmp_path):
        """30%+ test ratio = full 8 points."""
        # 3 source files, 1 test = 1/3 = 33% > 30%
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("x = 2")
        (tmp_path / "c.py").write_text("x = 3")
        (tmp_path / "test_a.py").write_text("def test(): pass")
        breakdown = score_project(tmp_path)
        assert breakdown.test_ratio == 8.0

    def test_test_ratio_partial(self, tmp_path):
        """Low test ratio gets partial points."""
        # 10 source files, 1 test = 1/10 = 10% -> (0.1/0.3)*8 = 2.7
        for i in range(10):
            (tmp_path / f"mod{i}.py").write_text(f"x = {i}")
        (tmp_path / "test_mod0.py").write_text("def test(): pass")
        breakdown = score_project(tmp_path)
        assert 2.0 <= breakdown.test_ratio <= 3.0

    def test_no_secrets_adds_points(self, tmp_path):
        """Clean project gets 8 points for no secrets."""
        (tmp_path / "main.py").write_text("x = 1")
        breakdown = score_project(tmp_path)
        assert breakdown.no_secrets == 8.0

    def test_secrets_lose_points(self, tmp_path):
        """Secret files remove the 8 points."""
        (tmp_path / "main.py").write_text("x = 1")
        (tmp_path / ".env").write_text("SECRET=bad")
        breakdown = score_project(tmp_path)
        assert breakdown.no_secrets == 0.0

    def test_dependency_file_adds_points(self, tmp_path):
        """requirements.txt adds 4 points."""
        (tmp_path / "main.py").write_text("x = 1")
        (tmp_path / "requirements.txt").write_text("flask")
        breakdown = score_project(tmp_path)
        assert breakdown.has_dependency_file == 4.0

    def test_package_json_adds_points(self, tmp_path):
        """package.json adds 4 points."""
        (tmp_path / "index.js").write_text("console.log(1)")
        (tmp_path / "package.json").write_text('{"name": "test"}')
        breakdown = score_project(tmp_path)
        assert breakdown.has_dependency_file == 4.0

    def test_file_count_healthy(self, tmp_path):
        """3-500 files gets full 6 points."""
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text(f"x = {i}")
        breakdown = score_project(tmp_path)
        assert breakdown.file_count_healthy == 6.0

    def test_file_count_minimal(self, tmp_path):
        """1-2 files gets 3 points."""
        (tmp_path / "main.py").write_text("x = 1")
        breakdown = score_project(tmp_path)
        assert breakdown.file_count_healthy == 3.0

    def test_ignores_node_modules(self, tmp_path):
        """Files in node_modules are excluded from scoring."""
        (tmp_path / "index.js").write_text("x = 1")
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "lib.js").write_text("module.exports = {}")
        breakdown = score_project(tmp_path)
        assert breakdown.source_file_count == 1  # Only index.js

    def test_well_structured_project(self, tmp_path):
        """A well-structured project scores high on static metrics."""
        (tmp_path / "README.md").write_text("# My Project")
        (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc")
        (tmp_path / "requirements.txt").write_text("fastapi\npydantic")
        (tmp_path / ".git").mkdir()

        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("from fastapi import FastAPI")
        (src / "models.py").write_text("class User: pass")
        (src / "utils.py").write_text("def helper(): pass")

        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_main.py").write_text("def test_app(): pass")
        (tests / "test_models.py").write_text("def test_user(): pass")

        breakdown = score_project(tmp_path)
        # Should get: 10 + 8 + 4 + 12 + 8 + 8 + 6 + 4 = 60 static
        assert breakdown.static_score == 60.0
        assert breakdown.total_score == 60.0  # No Tyrest

    def test_tyrest_adds_40_points(self, tmp_path):
        """Tyrest overall=1.0 adds full 40 points."""
        (tmp_path / "main.py").write_text("x = 1")
        breakdown = score_project(tmp_path, tyrest_overall=1.0)
        assert breakdown.tyrest_overall == 40.0
        assert breakdown.tyrest_available is True

    def test_tyrest_partial_score(self, tmp_path):
        """Tyrest overall=0.7 adds 28 points."""
        (tmp_path / "main.py").write_text("x = 1")
        breakdown = score_project(tmp_path, tyrest_overall=0.7)
        assert breakdown.tyrest_overall == 28.0

    def test_tyrest_none_adds_nothing(self, tmp_path):
        """No Tyrest score = 0 Tyrest points."""
        (tmp_path / "main.py").write_text("x = 1")
        breakdown = score_project(tmp_path, tyrest_overall=None)
        assert breakdown.tyrest_overall == 0.0
        assert breakdown.tyrest_available is False

    def test_perfect_score(self, tmp_path):
        """Perfect project with Tyrest scores 100."""
        (tmp_path / "README.md").write_text("# Perfect")
        (tmp_path / ".gitignore").write_text("*.pyc")
        (tmp_path / "requirements.txt").write_text("flask")
        (tmp_path / ".git").mkdir()

        for i in range(3):
            (tmp_path / f"mod{i}.py").write_text(f"x = {i}")
        (tmp_path / "test_mod0.py").write_text("def test(): pass")

        breakdown = score_project(tmp_path, tyrest_overall=1.0)
        assert breakdown.total_score == 100.0


class TestIsTestFile:
    """Tests for the _is_test_file helper."""

    def test_test_prefix(self):
        assert _is_test_file(Path("test_main.py")) is True

    def test_test_suffix(self):
        assert _is_test_file(Path("main_test.py")) is True

    def test_spec_file(self):
        assert _is_test_file(Path("app.spec.ts")) is True

    def test_dot_test_file(self):
        assert _is_test_file(Path("app.test.js")) is True

    def test_tests_directory(self):
        assert _is_test_file(Path("/project/tests/test_main.py"), Path("/project")) is True

    def test_regular_file(self):
        assert _is_test_file(Path("main.py")) is False

    def test_underscore_tests_dir(self):
        assert _is_test_file(Path("/project/__tests__/app.js"), Path("/project")) is True


class TestQualityBreakdown:
    """Tests for the QualityBreakdown dataclass."""

    def test_static_score(self):
        b = QualityBreakdown(has_source_code=10.0, has_readme=8.0, no_secrets=8.0)
        assert b.static_score == 26.0

    def test_total_score(self):
        b = QualityBreakdown(has_source_code=10.0, tyrest_overall=20.0)
        assert b.total_score == 30.0

    def test_total_rounds(self):
        b = QualityBreakdown(has_source_code=10.0, test_ratio=2.7)
        assert b.total_score == 12.7


class TestLifeDomainCategoryGate:
    """Tests for the scoring_rubric='life_domain' category gate (R-A item 4)."""

    # C1 — Missing-shape triggers category_failed

    def test_life_domain_missing_agent_yaml(self, tmp_path):
        """Missing agent.yaml triggers category_failed with reason."""
        _make_life_domain_project(tmp_path, agent_yaml=False)
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.total_score == 0.0
        assert breakdown.category_failure_reason == 'missing_agent_yaml'

    def test_life_domain_missing_skill_manifest(self, tmp_path):
        """Missing skills/<n>/SKILL.md triggers category_failed."""
        _make_life_domain_project(tmp_path, skill=False)
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.total_score == 0.0
        assert breakdown.category_failure_reason == 'missing_skill_manifest'

    def test_life_domain_missing_e2e_test(self, tmp_path):
        """Missing E2E test triggers category_failed."""
        _make_life_domain_project(tmp_path, e2e_test=False)
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.total_score == 0.0
        assert breakdown.category_failure_reason == 'missing_e2e_test'

    def test_life_domain_empty_skills_dir(self, tmp_path):
        """skills/ dir exists but has no SKILL.md -> missing_skill_manifest."""
        (tmp_path / "agent.yaml").write_text("name: Test\n")
        (tmp_path / "skills").mkdir()  # empty
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_e2e_foo.py").write_text("def test_e2e(): pass\n")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.category_failure_reason == 'missing_skill_manifest'

    def test_life_domain_failure_precedence(self, tmp_path):
        """Completely empty -> agent.yaml is checked first."""
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.category_failure_reason == 'missing_agent_yaml'

    # C2 — Shape-passing artifacts proceed to existing scoring

    def test_life_domain_full_shape_proceeds(self, tmp_path):
        """Full agent shape -> category passes, normal scoring applies."""
        _make_life_domain_project(tmp_path)
        (tmp_path / "README.md").write_text("# Agent\n")
        (tmp_path / "requirements.txt").write_text("anthropic\n")
        (tmp_path / "agent.py").write_text("# implementation\n")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is False
        assert breakdown.category_failure_reason is None
        assert breakdown.total_score > 0
        assert breakdown.has_source_code == 10.0
        assert breakdown.has_readme == 8.0
        assert breakdown.has_dependency_file == 4.0

    def test_life_domain_matches_default_when_passing(self, tmp_path):
        """For a shape-passing project, life_domain == default scoring."""
        _make_life_domain_project(tmp_path)
        (tmp_path / "README.md").write_text("# Agent\n")
        (tmp_path / "requirements.txt").write_text("anthropic\n")
        (tmp_path / "agent.py").write_text("# impl\n")
        b_default = score_project(tmp_path)
        b_life = score_project(tmp_path, scoring_rubric='life_domain')
        # Per-field static equality (the numeric envelope is identical)
        assert b_default.has_source_code == b_life.has_source_code
        assert b_default.has_readme == b_life.has_readme
        assert b_default.has_gitignore == b_life.has_gitignore
        assert b_default.has_tests == b_life.has_tests
        assert b_default.test_ratio == b_life.test_ratio
        assert b_default.no_secrets == b_life.no_secrets
        assert b_default.file_count_healthy == b_life.file_count_healthy
        assert b_default.has_dependency_file == b_life.has_dependency_file
        assert b_default.total_score == b_life.total_score
        assert b_default.source_file_count == b_life.source_file_count
        assert b_default.test_file_count == b_life.test_file_count
        assert b_default.total_file_count == b_life.total_file_count

    # C3 — scoring_rubric='tech' bypasses the gate entirely

    def test_tech_rubric_no_gate_applied(self, tmp_path):
        """tech rubric on agentless project scores normally (no gate)."""
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Tech\n")
        breakdown = score_project(tmp_path, scoring_rubric='tech')
        assert breakdown.category_failed is False
        assert breakdown.category_failure_reason is None
        assert breakdown.has_source_code == 10.0
        assert breakdown.has_readme == 8.0

    def test_tech_rubric_equals_default(self, tmp_path):
        """tech rubric is field-identical to no rubric."""
        (tmp_path / "main.py").write_text("x=1")
        (tmp_path / "README.md").write_text("# Tech\n")
        b_default = score_project(tmp_path)
        b_tech = score_project(tmp_path, scoring_rubric='tech')
        assert b_default.has_source_code == b_tech.has_source_code
        assert b_default.has_readme == b_tech.has_readme
        assert b_default.total_score == b_tech.total_score
        assert b_default.category_failed == b_tech.category_failed

    # C4 — No rubric arg preserves current behavior

    def test_default_rubric_no_category_gate(self, tmp_path):
        """No rubric arg on empty dir -> existing behavior (no gate)."""
        breakdown = score_project(tmp_path)
        assert breakdown.category_failed is False
        assert breakdown.category_failure_reason is None
        # Existing behavior: empty dir scores only no_secrets
        assert breakdown.no_secrets == 8.0
        assert breakdown.has_source_code == 0.0

    def test_default_rubric_on_agent_project_still_no_gate(self, tmp_path):
        """Even with full agent shape, default rubric does NOT set
        category_failed flags — the gate is opt-in."""
        _make_life_domain_project(tmp_path)
        breakdown = score_project(tmp_path)
        assert breakdown.category_failed is False
        assert breakdown.category_failure_reason is None

    # Heuristic-specific tests (E2E detection)

    def test_e2e_heuristic_tests_dir(self, tmp_path):
        """test_e2e*.py under tests/ qualifies."""
        _make_life_domain_project(
            tmp_path, e2e_filename="test_e2e_scene.py",
        )
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is False

    def test_e2e_heuristic_root(self, tmp_path):
        """test_e2e.py at project root (no tests/ dir) qualifies."""
        (tmp_path / "agent.yaml").write_text("name: T\n")
        skill_dir = tmp_path / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# foo")
        (tmp_path / "test_e2e.py").write_text(
            "def test_e2e(): assert True\n"
        )
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is False

    def test_e2e_heuristic_regular_test_not_counted(self, tmp_path):
        """A non-e2e test file does NOT satisfy the gate."""
        (tmp_path / "agent.yaml").write_text("name: T\n")
        skill_dir = tmp_path / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# foo")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_helper.py").write_text("def test_h(): pass\n")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.category_failure_reason == 'missing_e2e_test'

    @pytest.mark.parametrize("rel_path", [
        "tests/test_e2e.py",
        "tests/test_e2e_scene_response.py",
        "tests/e2e/test_anything.py",
        "test_e2e.py",
    ])
    def test_e2e_heuristic_variants(self, tmp_path, rel_path):
        """All four variants qualify as E2E tests."""
        (tmp_path / "agent.yaml").write_text("name: T\n")
        skill_dir = tmp_path / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# foo")
        test_file = tmp_path / rel_path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("def test_e2e(): pass\n")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is False, (
            f"Variant {rel_path} did not qualify as E2E"
        )

    def test_skill_manifest_nested(self, tmp_path):
        """skills/companion/SKILL.md qualifies."""
        _make_life_domain_project(tmp_path, skill_name="companion")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is False

    def test_skill_manifest_wrong_location(self, tmp_path):
        """Bare SKILL.md at project root does NOT count."""
        (tmp_path / "agent.yaml").write_text("name: T\n")
        (tmp_path / "SKILL.md").write_text("# wrong place")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_e2e.py").write_text("def test_e2e(): pass\n")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.category_failure_reason == 'missing_skill_manifest'

    def test_skill_manifest_skills_skill_md_does_not_count(self, tmp_path):
        """skills/SKILL.md (one level deep) does NOT count — needs <n>/SKILL.md."""
        (tmp_path / "agent.yaml").write_text("name: T\n")
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "SKILL.md").write_text("# wrong depth")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_e2e.py").write_text("def test_e2e(): pass\n")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.category_failure_reason == 'missing_skill_manifest'

    def test_unknown_rubric_treated_as_none(self, tmp_path, caplog):
        """Unknown rubric values fail open to default behavior."""
        (tmp_path / "main.py").write_text("x=1")
        breakdown = score_project(tmp_path, scoring_rubric='bogus_rubric')
        # Should NOT crash; should behave like default
        assert breakdown.category_failed is False
        assert breakdown.has_source_code == 10.0

    def test_category_failure_reason_default_none(self):
        """QualityBreakdown defaults category_failure_reason to None."""
        b = QualityBreakdown()
        assert b.category_failed is False
        assert b.category_failure_reason is None

    def test_category_failed_zeros_total_score(self):
        """When category_failed, total_score is 0.0 regardless of components."""
        b = QualityBreakdown(
            has_source_code=10.0,
            has_readme=8.0,
            tyrest_overall=40.0,
            category_failed=True,
            category_failure_reason='missing_agent_yaml',
        )
        assert b.total_score == 0.0

    # JS/TS E2E detection (Codex round 1 medium finding — ClaudeClaw agents
    # are TypeScript-first per user CLAUDE.md, so JS/TS agent projects must
    # be able to pass the gate).

    @pytest.mark.parametrize("rel_path", [
        "tests/agent.e2e.ts",
        "tests/agent.e2e.spec.ts",
        "tests/agent.e2e.test.ts",
        "tests/agent.e2e.js",
        "tests/e2e/agent.spec.ts",
        "tests/e2e/agent.test.js",
        "e2e/login.spec.ts",
        "test/agent.e2e.tsx",
    ])
    def test_e2e_heuristic_js_ts_variants(self, tmp_path, rel_path):
        """JS/TS E2E test variants qualify."""
        (tmp_path / "agent.yaml").write_text("name: T\n")
        skill_dir = tmp_path / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# foo")
        test_file = tmp_path / rel_path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(
            "describe('e2e', () => { it('works', () => {}); });\n"
        )
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is False, (
            f"JS/TS variant {rel_path} did not qualify as E2E"
        )

    def test_e2e_heuristic_regular_ts_test_not_counted(self, tmp_path):
        """A regular .spec.ts (no 'e2e' marker, no e2e/ dir) does NOT
        satisfy the gate."""
        (tmp_path / "agent.yaml").write_text("name: T\n")
        skill_dir = tmp_path / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# foo")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "helper.spec.ts").write_text("// unit test\n")
        breakdown = score_project(tmp_path, scoring_rubric='life_domain')
        assert breakdown.category_failed is True
        assert breakdown.category_failure_reason == 'missing_e2e_test'

    def test_unknown_rubric_logs_warning(self, tmp_path, caplog):
        """Unknown rubric values emit a WARNING-level log entry."""
        import logging
        (tmp_path / "main.py").write_text("x=1")
        with caplog.at_level(logging.WARNING, logger='gates.quality_scorer'):
            score_project(tmp_path, scoring_rubric='typo_rubric')
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            'typo_rubric' in r.getMessage() for r in warnings
        ), (
            f"expected WARNING mentioning typo_rubric; got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_strict_rubric_raises_on_unknown(self, tmp_path):
        """strict_rubric=True turns unknown rubric into ValueError."""
        (tmp_path / "main.py").write_text("x=1")
        with pytest.raises(ValueError, match="unknown scoring_rubric"):
            score_project(
                tmp_path,
                scoring_rubric='typo_rubric',
                strict_rubric=True,
            )

    def test_strict_rubric_accepts_valid_values(self, tmp_path):
        """strict_rubric=True still accepts the valid rubrics."""
        _make_life_domain_project(tmp_path)
        # life_domain should pass without error
        b1 = score_project(tmp_path, scoring_rubric='life_domain', strict_rubric=True)
        assert b1.category_failed is False
        # tech should pass without error
        b2 = score_project(tmp_path, scoring_rubric='tech', strict_rubric=True)
        assert b2.category_failed is False
        # None should pass without error
        b3 = score_project(tmp_path, scoring_rubric=None, strict_rubric=True)
        assert b3.category_failed is False

    def test_strict_rubric_default_is_false(self, tmp_path):
        """Default strict_rubric=False means unknown still falls open."""
        (tmp_path / "main.py").write_text("x=1")
        # Should NOT raise
        b = score_project(tmp_path, scoring_rubric='typo')
        assert b.has_source_code == 10.0
