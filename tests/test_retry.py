"""Tests for Phase 13f — Automatic build retry with backoff.

Updated to match hardened retry logic that uses COUNT of failed rows
as the hard cap (instead of retry_count column) and keeps status='failed'
until the orchestrator resets priority_queue for re-dispatch.
"""
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


def _create_failed_builds(db, count, queue_job_id="metroplex-ideaforge-1"):
    """Helper: insert `count` failed build rows for the given queue_job_id."""
    for i in range(count):
        job = BuildJob(
            idea_id=1,
            title="Failed Project",
            spec_path="/tmp/spec.txt",
            queue_job_id=queue_job_id,
            status="failed",
            queued_at=datetime.now(),
        )
        db.record_build_job(job)


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
        """When COUNT of failed rows >= MAX_RETRIES, build is not retryable."""
        _create_failed_builds(in_memory_db, StateDB.MAX_RETRIES)

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


class TestGetExhaustedBuilds:
    """Test identifying builds that have exhausted all retries."""

    def test_exhausted_build_found(self, in_memory_db):
        _create_failed_builds(in_memory_db, StateDB.MAX_RETRIES)
        exhausted = in_memory_db.get_exhausted_builds()
        assert len(exhausted) == 1
        assert exhausted[0]["queue_job_id"] == "metroplex-ideaforge-1"

    def test_retryable_build_not_exhausted(self, db_with_failed_build):
        exhausted = db_with_failed_build.get_exhausted_builds()
        assert len(exhausted) == 0

    def test_completed_build_not_exhausted(self, in_memory_db):
        job = BuildJob(
            idea_id=1,
            title="Done",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        exhausted = in_memory_db.get_exhausted_builds()
        assert len(exhausted) == 0


class TestHasExhaustedRetries:
    """Test the exhaustion check used by run_from_queue."""

    def test_not_exhausted_with_one_failure(self, db_with_failed_build):
        assert not db_with_failed_build.has_exhausted_retries("metroplex-ideaforge-1")

    def test_exhausted_at_max(self, in_memory_db):
        _create_failed_builds(in_memory_db, StateDB.MAX_RETRIES)
        assert in_memory_db.has_exhausted_retries("metroplex-ideaforge-1")

    def test_exhausted_above_max(self, in_memory_db):
        """Even with more rows than MAX_RETRIES (legacy data), it's exhausted."""
        _create_failed_builds(in_memory_db, StateDB.MAX_RETRIES + 5)
        assert in_memory_db.has_exhausted_retries("metroplex-ideaforge-1")

    def test_not_exhausted_for_unknown_id(self, in_memory_db):
        assert not in_memory_db.has_exhausted_retries("metroplex-ideaforge-999")


class TestMarkBuildForRetry:
    """Test the retry flagging mechanism."""

    def test_first_retry(self, db_with_failed_build):
        success = db_with_failed_build.mark_build_for_retry("metroplex-ideaforge-1")
        assert success

        cursor = db_with_failed_build.conn.cursor()
        cursor.execute(
            "SELECT status, retry_count, next_retry_at FROM build_jobs WHERE queue_job_id = ?",
            ("metroplex-ideaforge-1",),
        )
        row = cursor.fetchone()
        # Status stays 'failed' — orchestrator handles re-dispatch
        assert row["status"] == "failed"
        assert row["retry_count"] == 1
        assert row["next_retry_at"] is not None

    def test_retry_count_tracks_failed_count(self, in_memory_db):
        """retry_count is set to total failed row count."""
        # Create 2 failed builds
        _create_failed_builds(in_memory_db, 2)

        success = in_memory_db.mark_build_for_retry("metroplex-ideaforge-1")
        assert success

        cursor = in_memory_db.conn.cursor()
        cursor.execute(
            "SELECT retry_count FROM build_jobs WHERE queue_job_id = ? ORDER BY id DESC LIMIT 1",
            ("metroplex-ideaforge-1",),
        )
        row = cursor.fetchone()
        assert row["retry_count"] == 2

    def test_max_retries_blocks_further(self, in_memory_db):
        """When failed count >= MAX_RETRIES, mark_build_for_retry returns False."""
        _create_failed_builds(in_memory_db, StateDB.MAX_RETRIES)

        success = in_memory_db.mark_build_for_retry("metroplex-ideaforge-1")
        assert not success

    def test_nonexistent_build(self, in_memory_db):
        success = in_memory_db.mark_build_for_retry("metroplex-ideaforge-999")
        assert not success

    def test_backoff_increases(self, in_memory_db):
        """Verify that retry backoff intervals increase with each failed row."""
        times = []
        for i in range(StateDB.MAX_RETRIES):
            # Add a new failed row
            job = BuildJob(
                idea_id=1,
                title="Failed Project",
                spec_path="/tmp/spec.txt",
                queue_job_id="metroplex-ideaforge-1",
                status="failed",
                queued_at=datetime.now(),
            )
            in_memory_db.record_build_job(job)

            success = in_memory_db.mark_build_for_retry("metroplex-ideaforge-1")
            if not success:
                break

            cursor = in_memory_db.conn.cursor()
            cursor.execute(
                "SELECT next_retry_at FROM build_jobs WHERE queue_job_id = ? ORDER BY id DESC LIMIT 1",
                ("metroplex-ideaforge-1",),
            )
            row = cursor.fetchone()
            times.append(row["next_retry_at"])

        # Should have gotten backoff times for all but the last retry
        assert len(times) >= 2
        # All retry times should be distinct (increasing backoff)
        assert len(set(times)) == len(times)

    def test_does_not_reset_priority_queue(self, in_memory_db):
        """mark_build_for_retry must NOT reset priority_queue — orchestrator handles that."""
        from models import PriorityItem

        # Set up priority queue item
        item = PriorityItem(
            source="ideaforge",
            source_id="1",
            title="Test",
            description="Test desc",
            priority_score=50.0,
            status="pending",
            idea_data="{}",
        )
        in_memory_db.enqueue_item(item)
        # Mark as dispatched then failed (simulating build lifecycle)
        in_memory_db.update_item_status(1, "dispatched", "dispatched_at")
        in_memory_db.update_item_status(1, "failed", "completed_at")

        # Create failed build
        job = BuildJob(
            idea_id=1,
            title="Failed Project",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="failed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)

        # Retry
        in_memory_db.mark_build_for_retry("metroplex-ideaforge-1")

        # Priority queue should still be 'failed' — NOT reset to 'pending'
        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT status FROM priority_queue WHERE source_id = '1'")
        row = cursor.fetchone()
        assert row["status"] == "failed"


class TestMarkBuildAbandoned:
    """Test the terminal abandonment mechanism."""

    def test_abandon_marks_priority_queue_failed(self, in_memory_db):
        from models import PriorityItem

        item = PriorityItem(
            source="ideaforge",
            source_id="1",
            title="Test",
            description="Test desc",
            priority_score=50.0,
            status="pending",
            idea_data="{}",
        )
        in_memory_db.enqueue_item(item)
        in_memory_db.update_item_status(1, "dispatched", "dispatched_at")

        result = in_memory_db.mark_build_abandoned("metroplex-ideaforge-1")
        assert result

        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT status, completed_at FROM priority_queue WHERE source_id = '1'")
        row = cursor.fetchone()
        assert row["status"] == "failed"
        assert row["completed_at"] is not None

    def test_abandon_noop_for_completed(self, in_memory_db):
        from models import PriorityItem

        item = PriorityItem(
            source="ideaforge",
            source_id="1",
            title="Test",
            description="Test desc",
            priority_score=50.0,
            status="pending",
            idea_data="{}",
        )
        in_memory_db.enqueue_item(item)
        in_memory_db.update_item_status(1, "completed", "completed_at")

        result = in_memory_db.mark_build_abandoned("metroplex-ideaforge-1")
        assert not result  # Already completed, no update

    def test_abandon_unknown_id(self, in_memory_db):
        result = in_memory_db.mark_build_abandoned("metroplex-ideaforge-999")
        assert not result


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

    def test_unreviewed_builds_blocked_in_strict_mode(self, in_memory_db):
        """L5 strict mode: completed builds with NULL review_status are NOT publishable."""
        job = BuildJob(
            idea_id=1,
            title="Old Build",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)

        unpublished = in_memory_db.get_unpublished_builds(require_review=True)
        assert len(unpublished) == 0

    def test_unreviewed_builds_allowed_in_lenient_mode(self, in_memory_db):
        """Lenient mode: completed builds with NULL review_status are publishable."""
        job = BuildJob(
            idea_id=1,
            title="Old Build",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)

        unpublished = in_memory_db.get_unpublished_builds(require_review=False)
        assert len(unpublished) == 1

    def test_reviewed_builds_in_strict_mode(self, in_memory_db):
        """Strict mode: reviewed builds are publishable."""
        job = BuildJob(
            idea_id=1,
            title="Reviewed Build",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        in_memory_db.update_build_review_status("metroplex-ideaforge-1", "reviewed")

        unpublished = in_memory_db.get_unpublished_builds(require_review=True)
        assert len(unpublished) == 1

    def test_require_review_default_is_strict(self, in_memory_db):
        """Default require_review=True blocks unreviewed builds."""
        job = BuildJob(
            idea_id=1,
            title="Unreviewed",
            spec_path="/tmp/spec.txt",
            queue_job_id="metroplex-ideaforge-1",
            status="completed",
            queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)

        # Default (no arg) = strict
        unpublished = in_memory_db.get_unpublished_builds()
        assert len(unpublished) == 0
