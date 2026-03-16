"""
Tests for Quality Ratchet (Phase 14e) — threshold auto-tuning.
"""
from datetime import datetime

import pytest

from db import StateDB
from quality_ratchet import (
    evaluate_ratchet,
    get_quality_threshold,
    set_quality_threshold,
    MIN_RECORDS_TO_ACTIVATE,
)


@pytest.fixture
def state_db():
    """Create in-memory state database."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


def _insert_build(state_db, idea_id, title, status, quality_score,
                   review_status=None, published=False):
    """Helper to insert a build job (and optionally a publish job)."""
    state_db.connect()
    cursor = state_db.conn.cursor()
    job_id = f"metroplex-{idea_id}"
    now = datetime.now().isoformat()

    cursor.execute(
        "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, "
        "status, queued_at, quality_score, review_status) "
        "VALUES (?, ?, '', ?, ?, ?, ?, ?)",
        (idea_id, title, job_id, status, now, quality_score, review_status),
    )

    if published:
        cursor.execute(
            "INSERT INTO publish_jobs (build_job_id, title, repo_name, repo_url, "
            "status, project_dir, created_at, published_at) "
            "VALUES (?, ?, ?, ?, 'published', '/tmp', ?, ?)",
            (job_id, title, f"repo-{idea_id}",
             f"https://github.com/org/repo-{idea_id}", now, now),
        )

    state_db.conn.commit()


class TestGetSetThreshold:
    """Tests for threshold read/write."""

    def test_no_threshold_returns_none(self, state_db):
        assert get_quality_threshold(state_db) is None

    def test_set_and_get(self, state_db):
        set_quality_threshold(state_db, 52.0)
        assert get_quality_threshold(state_db) == 52.0

    def test_overwrite(self, state_db):
        set_quality_threshold(state_db, 40.0)
        set_quality_threshold(state_db, 55.0)
        assert get_quality_threshold(state_db) == 55.0


class TestEvaluateRatchet:
    """Tests for the ratchet evaluation logic."""

    def test_insufficient_data(self, state_db):
        """Ratchet doesn't activate with < 30 records."""
        for i in range(10):
            _insert_build(state_db, i, f"Build {i}", "completed", 50.0)

        result = evaluate_ratchet(state_db)
        assert result["activated"] is False
        assert "Insufficient data" in result["reason"]
        assert result["stats"]["scored_count"] == 10

    def test_activates_with_sufficient_data(self, state_db):
        """Ratchet activates with 30+ records."""
        for i in range(20):
            _insert_build(state_db, i, f"Pub {i}", "completed", 60.0,
                         review_status="reviewed", published=True)
        for i in range(20, 40):
            _insert_build(state_db, i, f"Fail {i}", "completed", 35.0,
                         review_status="review_failed")

        result = evaluate_ratchet(state_db)
        assert result["activated"] is True

    def test_sets_initial_threshold(self, state_db):
        """First run sets the threshold (no prior value)."""
        for i in range(20):
            _insert_build(state_db, i, f"Pub {i}", "completed", 60.0,
                         review_status="reviewed", published=True)
        for i in range(20, 40):
            _insert_build(state_db, i, f"Fail {i}", "completed", 35.0,
                         review_status="review_failed")

        result = evaluate_ratchet(state_db)
        assert result["tightened"] is True
        assert result["current_threshold"] is not None
        assert "Initial threshold" in result["reason"]

    def test_tightens_when_data_supports(self, state_db):
        """Threshold tightens when proposed > current."""
        # Start with a low threshold
        set_quality_threshold(state_db, 30.0)

        for i in range(20):
            _insert_build(state_db, i, f"Pub {i}", "completed", 60.0,
                         review_status="reviewed", published=True)
        for i in range(20, 40):
            _insert_build(state_db, i, f"Fail {i}", "completed", 35.0,
                         review_status="review_failed")

        result = evaluate_ratchet(state_db)
        assert result["tightened"] is True
        new_threshold = result["current_threshold"]
        assert new_threshold > 30.0

    def test_ratchet_prevents_loosening(self, state_db):
        """Threshold never decreases."""
        # Set a high threshold
        set_quality_threshold(state_db, 80.0)

        for i in range(20):
            _insert_build(state_db, i, f"Pub {i}", "completed", 60.0,
                         review_status="reviewed", published=True)
        for i in range(20, 40):
            _insert_build(state_db, i, f"Fail {i}", "completed", 35.0,
                         review_status="review_failed")

        result = evaluate_ratchet(state_db)
        assert result["tightened"] is False
        assert "ratchet prevents loosening" in result["reason"]
        # Threshold unchanged
        assert get_quality_threshold(state_db) == 80.0

    def test_no_published_builds(self, state_db):
        """No threshold change without published builds."""
        for i in range(35):
            _insert_build(state_db, i, f"Fail {i}", "failed", 30.0)

        result = evaluate_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is False
        assert "No published builds" in result["reason"]

    def test_no_failed_builds(self, state_db):
        """No threshold change without failed builds."""
        for i in range(35):
            _insert_build(state_db, i, f"Pub {i}", "completed", 60.0,
                         review_status="reviewed", published=True)

        result = evaluate_ratchet(state_db)
        assert result["activated"] is True
        assert result["tightened"] is False
        assert "No failed builds" in result["reason"]

    def test_headroom_constraint(self, state_db):
        """Proposed threshold maintains minimum headroom below published avg."""
        # Published avg very close to fail avg
        for i in range(20):
            _insert_build(state_db, i, f"Pub {i}", "completed", 50.0,
                         review_status="reviewed", published=True)
        for i in range(20, 40):
            _insert_build(state_db, i, f"Fail {i}", "completed", 48.0,
                         review_status="review_failed")

        result = evaluate_ratchet(state_db)
        if result["tightened"]:
            # Threshold should be at most pub_avg - MIN_HEADROOM (50 - 5 = 45)
            assert result["current_threshold"] <= 45.0

    def test_stats_populated(self, state_db):
        """Result stats contain expected fields."""
        for i in range(20):
            _insert_build(state_db, i, f"Pub {i}", "completed", 60.0,
                         review_status="reviewed", published=True)
        for i in range(20, 40):
            _insert_build(state_db, i, f"Fail {i}", "completed", 35.0,
                         review_status="review_failed")

        result = evaluate_ratchet(state_db)
        stats = result["stats"]
        assert stats["scored_count"] == 40
        assert stats["published_count"] == 20
        assert stats["failed_count"] == 20
        assert stats["published_avg"] == 60.0
        assert stats["failed_avg"] == 35.0
