"""Tests for Phase 13f — Automatic build retry with backoff."""
from datetime import datetime, timedelta

import pytest

from db import StateDB
from models import BuildJob


@pytest.fixture
def db_with_failed_build(in_memory_db):
    """DB with a single failed build."""
    job = BuildJob(
        idea_id=1,
        title="Failed Project",
        spec_path="/tmp/spec.txt",
        queue_job_id="metroplex-ideaforge-1",
        status="failed",
        queued_at=datetime.now(),
    )
    in_memory_db.record_build_job(job)
    return in_memory_db


class TestGetRetryableBuilds:
    """Test identifying builds eligible for retry."""

    def test_failed_build_is_retryable(self, db_with_failed_build):
        retryable = db_with_failed_build.get_retryable_builds()
        assert len(retryable) == 1
        assert retryable[0]["queue_job_id"] == "metroplex-ideaforge-1"

    def test_queued_build_not_retryable(self, in_memory_db):
        job = BuildJob(
            idea_id=1,
            title="Queued Project",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="queued",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        retryable = in_memory_db.get_retryable_builds()
        assert len(retryable) == 0

    def test_completed_build_not_retryable(self, in_memory_db):
        job = BuildJob(
            idea_id=1,
            title="Done",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        retryable = in_memory_db.get_retryable_builds()
        assert len(retryable) == 0

    def test_max_retries_exhausted_not_retryable(self, in_memory_db):
        job = BuildJob(
            idea_id=1,
            title="Exhausted",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="failed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        # Manually set retry_count to max
        cursor = in_memory_db.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET retry_count = ? WHERE queue_job_id = ?",
            (StateDB.MAX_RETRIES, "metroplex-ideaforge-1"),
        )
        in_memory_db.conn.commit()

        retryable = in_memory_db.get_retryable_builds()
        assert len(retryable) == 0

    def test_future_retry_not_retryable_yet(self, in_memory_db):
        job = BuildJob(
            idea_id=1,
            title="Future Retry",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="failed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        # Set next_retry_at to the future
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        cursor = in_memory_db.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET retry_count = 1, next_retry_at = ? WHERE queue_job_id = ?",
            (future, "metroplex-ideaforge-1"),
        )
        in_memory_db.conn.commit()

        retryable = in_memory_db.get_retryable_builds()
        assert len(retryable) == 0


class TestMarkBuildForRetry:
    """Test the retry reset mechanism."""

    def test_first_retry(self, db_with_failed_build):
        success = db_with_failed_build.mark_build_for_retry("metroplex-ideaforge-1")
        assert success

        cursor = db_with_failed_build.conn.cursor()
        cursor.execute(
            "SELECT status, retry_count, next_retry_at FROM build_jobs WHERE queue_job_id = ?",
            ("metroplex-ideaforge-1",),
        )
        row = cursor.fetchone()
        assert row["status"] == "queued"
        assert row["retry_count"] == 1
        assert row["next_retry_at"] is not None

    def test_retry_count_increments(self, db_with_failed_build):
        # First retry
        db_with_failed_build.mark_build_for_retry("metroplex-ideaforge-1")

        # Simulate failure again
        cursor = db_with_failed_build.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET status = 'failed' WHERE queue_job_id = ?",
            ("metroplex-ideaforge-1",),
        )
        db_with_failed_build.conn.commit()

        # Second retry
        success = db_with_failed_build.mark_build_for_retry("metroplex-ideaforge-1")
        assert success

        cursor.execute(
            "SELECT retry_count FROM build_jobs WHERE queue_job_id = ?",
            ("metroplex-ideaforge-1",),
        )
        row = cursor.fetchone()
        assert row["retry_count"] == 2

    def test_max_retries_blocks_further(self, db_with_failed_build):
        # Set retry_count to max
        cursor = db_with_failed_build.conn.cursor()
        cursor.execute(
            "UPDATE build_jobs SET retry_count = ? WHERE queue_job_id = ?",
            (StateDB.MAX_RETRIES, "metroplex-ideaforge-1"),
        )
        db_with_failed_build.conn.commit()

        success = db_with_failed_build.mark_build_for_retry("metroplex-ideaforge-1")
        assert not success

    def test_nonexistent_build(self, in_memory_db):
        success = in_memory_db.mark_build_for_retry("metroplex-ideaforge-999")
        assert not success

    def test_backoff_increases(self, db_with_failed_build):
        """Verify that retry backoff intervals increase."""
        times = []
        for i in range(StateDB.MAX_RETRIES):
            db_with_failed_build.mark_build_for_retry("metroplex-ideaforge-1")
            cursor = db_with_failed_build.conn.cursor()
            cursor.execute(
                "SELECT next_retry_at FROM build_jobs WHERE queue_job_id = ?",
                ("metroplex-ideaforge-1",),
            )
            row = cursor.fetchone()
            times.append(row["next_retry_at"])

            # Re-fail for next iteration (if not last)
            if i < StateDB.MAX_RETRIES - 1:
                cursor.execute(
                    "UPDATE build_jobs SET status = 'failed' WHERE queue_job_id = ?",
                    ("metroplex-ideaforge-1",),
                )
                db_with_failed_build.conn.commit()

        # All retry times should be set and distinct
        assert len(set(times)) == StateDB.MAX_RETRIES


class TestReviewableBuildsIntegration:
    """Test that review gate and retry work together with publish gate."""

    def test_reviewed_builds_are_publishable(self, in_memory_db):
        """Builds with review_status='reviewed' should appear in get_unpublished_builds."""
        job = BuildJob(
            idea_id=1,
            title="Reviewed",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        in_memory_db.update_build_review_status("metroplex-ideaforge-1", "reviewed")

        unpublished = in_memory_db.get_unpublished_builds()
        assert len(unpublished) == 1
        assert unpublished[0]["queue_job_id"] == "metroplex-ideaforge-1"

    def test_review_failed_builds_not_publishable(self, in_memory_db):
        """Builds with review_status='review_failed' should NOT appear in get_unpublished_builds."""
        job = BuildJob(
            idea_id=1,
            title="Failed Review",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        in_memory_db.update_build_review_status("metroplex-ideaforge-1", "review_failed")

        unpublished = in_memory_db.get_unpublished_builds()
        assert len(unpublished) == 0

    def test_unreviewed_builds_still_publishable(self, in_memory_db):
        """Backward compat: completed builds with NULL review_status are still publishable."""
        job = BuildJob(
            idea_id=1,
            title="Old Build",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        # Don't set review_status — should still be publishable

        unpublished = in_memory_db.get_unpublished_builds()
        assert len(unpublished) == 1
