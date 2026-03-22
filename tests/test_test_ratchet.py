"""
Tests for Test Coverage Ratchet (Phase D2) — threshold auto-tuning.
"""
from datetime import datetime

import pytest

from db import StateDB
from quality_ratchet import (
    evaluate_test_ratchet,
    get_test_coverage_threshold,
    set_test_coverage_threshold,
    TEST_RATCHET_HARD_CAP,
)


@pytest.fixture
def state_db():
    """Create in-memory state database."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


def _insert_published_build(state_db, idea_id, test_ratio):
    """Helper to insert a published build with a test_ratio."""
    state_db.connect()
    cursor = state_db.conn.cursor()
    job_id = f"metroplex-test-{idea_id}"
    now = datetime.now().isoformat()

    cursor.execute(
        "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, "
        "status, queued_at, test_ratio) "
        "VALUES (?, ?, '', ?, 'completed', ?, ?)",
        (idea_id, f"Build {idea_id}", job_id, now, test_ratio),
    )

    cursor.execute(
        "INSERT INTO publish_jobs (build_job_id, title, repo_name, repo_url, "
        "status, project_dir, created_at, published_at) "
        "VALUES (?, ?, ?, ?, 'published', '/tmp', ?, ?)",
        (job_id, f"Build {idea_id}", f"repo-{idea_id}",
         f"https://github.com/org/repo-{idea_id}", now, now),
    )

    state_db.conn.commit()


class TestGetSetThreshold:
    """Test threshold get/set operations."""

    def test_default_is_zero(self, state_db):
        assert get_test_coverage_threshold(state_db) == 0.0

    def test_set_and_get(self, state_db):
        set_test_coverage_threshold(state_db, 0.15)
        assert get_test_coverage_threshold(state_db) == 0.15

    def test_overwrite(self, state_db):
        set_test_coverage_threshold(state_db, 0.1)
        set_test_coverage_threshold(state_db, 0.2)
        assert get_test_coverage_threshold(state_db) == 0.2


class TestEvaluateTestRatchet:
    """Test ratchet evaluation logic."""

    def test_insufficient_data_below_min(self, state_db):
        """< 10 published builds -> no activation."""
        for i in range(5):
            _insert_published_build(state_db, i, 0.3)

        result = evaluate_test_ratchet(state_db)
        assert result["activated"] is False
        assert "Insufficient data" in result["reason"]
        assert result["stats"]["published_count"] == 5

    def test_tightening_with_10_builds(self, state_db):
        """10 published builds with test_ratios -> verify tightening math."""
        # All have test_ratio = 0.3 → median 0.3 → proposed = 0.15
        for i in range(10):
            _insert_published_build(state_db, i, 0.3)

        result = evaluate_test_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is True
        # proposed = median(0.3) * 0.5 = 0.15
        assert result["current_threshold"] == 0.15
        assert "Initial" in result["reason"]

    def test_monotonicity_cannot_decrease(self, state_db):
        """Ratchet must not decrease threshold."""
        set_test_coverage_threshold(state_db, 0.2)

        # All builds have ratio 0.25 → median 0.25 → proposed = 0.125
        # 0.125 < 0.2 → should NOT tighten
        for i in range(10):
            _insert_published_build(state_db, i, 0.25)

        result = evaluate_test_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is False
        assert "prevents loosening" in result["reason"]
        # Threshold unchanged
        assert get_test_coverage_threshold(state_db) == 0.2

    def test_threshold_already_above_proposed(self, state_db):
        """If threshold is already above proposed, no change."""
        set_test_coverage_threshold(state_db, 0.4)

        for i in range(10):
            _insert_published_build(state_db, i, 0.5)

        result = evaluate_test_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is False
        # proposed = 0.25, current = 0.4, so ratchet prevents loosening

    def test_hard_cap_at_05(self, state_db):
        """Threshold never exceeds 0.5."""
        # All builds have ratio 2.0 (lots of tests) → median 2.0 → proposed = 1.0
        # But hard cap at 0.5
        for i in range(10):
            _insert_published_build(state_db, i, 2.0)

        result = evaluate_test_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is True
        assert result["current_threshold"] == TEST_RATCHET_HARD_CAP
        assert result["current_threshold"] <= 0.5

    def test_no_tighten_without_headroom(self, state_db):
        """If median is not > 2x current, no tightening (insufficient headroom)."""
        set_test_coverage_threshold(state_db, 0.1)

        # median = 0.18 → proposed = 0.09 → 0.09 < 0.1 → prevents loosening
        # (this case is caught by monotonicity, not the headroom check)
        for i in range(10):
            _insert_published_build(state_db, i, 0.18)

        result = evaluate_test_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is False

    def test_varied_ratios_uses_median(self, state_db):
        """Verify median is used, not mean."""
        # 8 builds at 0.1, 2 builds at 1.0
        # Mean = (8*0.1 + 2*1.0) / 10 = 0.28
        # Median = 0.1 (since 8/10 are at 0.1)
        # Proposed = 0.1 * 0.5 = 0.05
        for i in range(8):
            _insert_published_build(state_db, i, 0.1)
        for i in range(8, 10):
            _insert_published_build(state_db, i, 1.0)

        result = evaluate_test_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is True
        assert result["current_threshold"] == 0.05


class TestDBMethods:
    """Test StateDB methods for test coverage tracking."""

    def test_update_build_test_ratio(self, state_db):
        """Verify update_build_test_ratio writes correctly."""
        now = datetime.now().isoformat()
        state_db.conn.execute(
            "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at) "
            "VALUES (1, 'Test', '', 'mx-1', 'completed', ?)",
            (now,),
        )
        state_db.conn.commit()

        assert state_db.update_build_test_ratio("mx-1", 0.42)

        row = state_db.conn.execute(
            "SELECT test_ratio FROM build_jobs WHERE queue_job_id = 'mx-1'"
        ).fetchone()
        assert abs(row["test_ratio"] - 0.42) < 0.001

    def test_get_published_test_ratios(self, state_db):
        """Verify get_published_test_ratios returns correct data."""
        for i in range(5):
            _insert_published_build(state_db, i, 0.2 + i * 0.05)

        ratios = state_db.get_published_test_ratios()
        assert len(ratios) == 5
        assert all(isinstance(r, float) for r in ratios)

    def test_get_published_test_ratios_excludes_null(self, state_db):
        """Builds without test_ratio should be excluded."""
        now = datetime.now().isoformat()
        # Build with no test_ratio
        state_db.conn.execute(
            "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at) "
            "VALUES (99, 'No ratio', '', 'mx-99', 'completed', ?)",
            (now,),
        )
        state_db.conn.execute(
            "INSERT INTO publish_jobs (build_job_id, title, repo_name, status, project_dir, created_at) "
            "VALUES ('mx-99', 'No ratio', 'repo-99', 'published', '/tmp', ?)",
            (now,),
        )
        state_db.conn.commit()

        ratios = state_db.get_published_test_ratios()
        assert len(ratios) == 0
