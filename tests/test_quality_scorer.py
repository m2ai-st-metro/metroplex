"""
Tests for Structural Quality Scorer (Phase 14b).
"""
import pytest
from pathlib import Path

from gates.quality_scorer import score_project, QualityBreakdown, _is_test_file


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
