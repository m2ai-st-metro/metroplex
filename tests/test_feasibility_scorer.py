"""Tests for Pre-Build Feasibility Scorer (L5 B2)."""

from datetime import datetime, timezone

import pytest

from db import StateDB
from feasibility_scorer import (
    ACTIVATION_THRESHOLD,
    DEFAULT_FEATURE_WEIGHTS,
    REJECT_THRESHOLD,
    _compute_keyword_overlap,
    _score_artifact_type,
    _score_dependency_risk,
    _score_factory_fit,
    _score_scope_clarity,
    adjust_feature_weights,
    get_prediction_accuracy,
    get_reject_threshold,
    record_prediction,
    resolve_prediction,
    score_feasibility,
    tighten_reject_threshold,
)


@pytest.fixture
def state_db():
    """Provide an in-memory StateDB with all tables."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


def _insert_postmortem(state_db, queue_job_id, title, category="build_error"):
    """Helper to insert a postmortem record."""
    state_db.connect()
    now = datetime.now(timezone.utc).isoformat()
    state_db.conn.execute(
        """INSERT INTO build_postmortems
        (queue_job_id, idea_id, title, failure_category, failure_stage,
         error_signature, created_at)
        VALUES (?, ?, ?, ?, 'build', '', ?)""",
        (queue_job_id, 1, title, category, now),
    )
    state_db.conn.commit()


def _insert_prediction(state_db, queue_job_id, score, predicted, actual=None, correct=None):
    """Helper to insert a feasibility prediction."""
    state_db.connect()
    now = datetime.now(timezone.utc).isoformat()
    state_db.conn.execute(
        """INSERT INTO feasibility_predictions
        (queue_job_id, feasibility_score, predicted_outcome, actual_outcome,
         correct, feature_weights, created_at, resolved_at)
        VALUES (?, ?, ?, ?, ?, '{}', ?, ?)""",
        (queue_job_id, score, predicted, actual, correct, now,
         now if actual else None),
    )
    state_db.conn.commit()


# --- Static Component Tests ---

class TestScopeClarity:
    def test_empty_problem_statement(self):
        assert _score_scope_clarity({"problem_statement": ""}) == 20.0

    def test_short_problem_statement(self):
        score = _score_scope_clarity({"problem_statement": "Fix something"})
        assert score == 30.0  # < 50 chars

    def test_medium_problem_statement(self):
        ps = "Developers struggle with managing API rate limits across multiple services"
        score = _score_scope_clarity({"problem_statement": ps})
        assert score >= 50.0

    def test_long_specific_problem_statement(self):
        ps = (
            "Over 500 developers waste 2 hours daily configuring deployment pipelines. "
            "Teams with more than 10 members report weekly deployment failures that cost "
            "the company thousands of dollars in lost productivity and customer trust."
        )
        score = _score_scope_clarity({"problem_statement": ps})
        assert score >= 70.0


class TestDependencyRisk:
    def test_no_dependencies(self):
        idea = {"title": "CLI todo tracker", "description": "Simple terminal tool"}
        assert _score_dependency_risk(idea) == 90.0

    def test_one_dependency(self):
        idea = {"title": "Stripe payment handler", "description": "Processes payments"}
        assert _score_dependency_risk(idea) == 70.0

    def test_many_dependencies(self):
        idea = {
            "title": "Cloud orchestrator",
            "description": "Integrates AWS, Stripe, Twilio, Redis, and Kafka",
        }
        assert _score_dependency_risk(idea) <= 30.0


class TestFactoryFit:
    def test_high_factory_fit(self):
        assert _score_factory_fit({"factory_fit_score": 9.0}) == 90.0

    def test_low_factory_fit(self):
        assert _score_factory_fit({"factory_fit_score": 2.0}) == 20.0

    def test_missing_factory_fit(self):
        assert _score_factory_fit({}) == 50.0


class TestArtifactType:
    def test_tool(self):
        assert _score_artifact_type({"artifact_type": "tool"}) == 85.0

    def test_agent(self):
        assert _score_artifact_type({"artifact_type": "agent"}) == 60.0

    def test_product(self):
        assert _score_artifact_type({"artifact_type": "product"}) == 40.0

    def test_unknown(self):
        assert _score_artifact_type({}) == 55.0


# --- Score Feasibility Tests ---

class TestScoreFeasibility:
    def test_high_feasibility_idea(self, state_db):
        idea = {
            "title": "CLI Task Tracker",
            "description": "Simple terminal-based task tracker for developers",
            "problem_statement": (
                "Over 100 developers report frustration with existing task management "
                "tools that require browser context switches. Teams need a CLI-first approach."
            ),
            "factory_fit_score": 8.5,
            "artifact_type": "tool",
        }
        result = score_feasibility(idea, state_db)
        assert result["score"] > 60
        assert result["predicted_outcome"] == "success"
        assert "breakdown" in result
        assert "feature_weights" in result

    def test_low_feasibility_idea(self, state_db):
        idea = {
            "title": "X",
            "description": "Integrates AWS, Stripe, Twilio, Redis, Kafka, Docker, Kubernetes",
            "problem_statement": "",
            "factory_fit_score": 2.0,
            "artifact_type": "product",
        }
        result = score_feasibility(idea, state_db)
        assert result["score"] < 50
        assert result["predicted_outcome"] == "failure"

    def test_learned_component_disabled_below_threshold(self, state_db):
        """Learned component should not activate with < ACTIVATION_THRESHOLD postmortems."""
        idea = {"title": "Test", "description": "Test", "problem_statement": "Test " * 20}
        result = score_feasibility(idea, state_db)
        assert result["learned_active"] is False
        assert result["penalty_multiplier"] == 1.0

    def test_learned_component_active_above_threshold(self, state_db):
        """Learned component should activate with >= ACTIVATION_THRESHOLD postmortems."""
        for i in range(ACTIVATION_THRESHOLD):
            _insert_postmortem(state_db, f"job-{i}", f"Failed MCP API server build {i}")

        idea = {
            "title": "MCP API server",
            "description": "Build an MCP API server tool",
            "problem_statement": "Need better API server tooling for developers",
            "factory_fit_score": 7.0,
            "artifact_type": "tool",
        }
        result = score_feasibility(idea, state_db)
        assert result["learned_active"] is True
        # Should have some penalty due to keyword overlap with failed builds
        assert result["penalty_multiplier"] < 1.0

    def test_learned_component_no_overlap(self, state_db):
        """No keyword overlap means penalty_multiplier stays at 1.0."""
        for i in range(ACTIVATION_THRESHOLD):
            _insert_postmortem(state_db, f"job-{i}", f"blockchain crypto mining tool {i}")

        idea = {
            "title": "Garden Planner App",
            "description": "Track your vegetable garden schedule",
            "problem_statement": "Need better garden planning",
            "factory_fit_score": 7.0,
            "artifact_type": "tool",
        }
        result = score_feasibility(idea, state_db)
        assert result["learned_active"] is True
        # No overlap = no penalty
        assert result["penalty_multiplier"] >= 0.95


class TestRejectThreshold:
    def test_score_below_threshold_rejected(self, state_db):
        """Ideas scoring below REJECT_THRESHOLD should be flagged for rejection."""
        idea = {
            "title": "X",
            "description": "AWS Stripe Twilio Redis Kafka Docker Kubernetes grpc",
            "problem_statement": "",
            "factory_fit_score": 1.0,
            "artifact_type": "product",
        }
        result = score_feasibility(idea, state_db)
        assert result["score"] < REJECT_THRESHOLD

    def test_ratchet_only_tightens(self, state_db):
        """Reject threshold can only increase (tighten)."""
        assert get_reject_threshold(state_db) == REJECT_THRESHOLD

        # Can tighten
        assert tighten_reject_threshold(state_db, 30) is True
        assert get_reject_threshold(state_db) == 30

        # Cannot loosen
        assert tighten_reject_threshold(state_db, 25) is False
        assert get_reject_threshold(state_db) == 30

        # Can tighten further
        assert tighten_reject_threshold(state_db, 35) is True
        assert get_reject_threshold(state_db) == 35


# --- Prediction Recording & Resolution Tests ---

class TestPredictionTracking:
    def test_record_prediction(self, state_db):
        record_prediction(state_db, "test-job-1", 75.0, "success", DEFAULT_FEATURE_WEIGHTS)
        state_db.connect()
        row = state_db.conn.execute(
            "SELECT * FROM feasibility_predictions WHERE queue_job_id = 'test-job-1'"
        ).fetchone()
        assert row is not None
        assert row["feasibility_score"] == 75.0
        assert row["predicted_outcome"] == "success"
        assert row["actual_outcome"] is None

    def test_resolve_prediction_correct(self, state_db):
        record_prediction(state_db, "test-job-2", 75.0, "success", DEFAULT_FEATURE_WEIGHTS)
        resolve_prediction(state_db, "test-job-2", "completed")
        state_db.connect()
        row = state_db.conn.execute(
            "SELECT * FROM feasibility_predictions WHERE queue_job_id = 'test-job-2'"
        ).fetchone()
        assert row["actual_outcome"] == "completed"
        assert row["correct"] == 1

    def test_resolve_prediction_incorrect(self, state_db):
        record_prediction(state_db, "test-job-3", 75.0, "success", DEFAULT_FEATURE_WEIGHTS)
        resolve_prediction(state_db, "test-job-3", "failed")
        state_db.connect()
        row = state_db.conn.execute(
            "SELECT * FROM feasibility_predictions WHERE queue_job_id = 'test-job-3'"
        ).fetchone()
        assert row["correct"] == 0

    def test_resolve_nonexistent_prediction(self, state_db):
        """Resolving a prediction that doesn't exist should not raise."""
        resolve_prediction(state_db, "no-such-job", "completed")


# --- Accuracy Tests ---

class TestPredictionAccuracy:
    def test_accuracy_returns_none_below_minimum(self, state_db):
        """Need at least 10 resolved predictions."""
        for i in range(5):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "completed", 1)
        assert get_prediction_accuracy(state_db) is None

    def test_accuracy_computes_correctly(self, state_db):
        # 8 correct, 2 incorrect = 80%
        for i in range(8):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "completed", 1)
        for i in range(8, 10):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "failed", 0)

        accuracy = get_prediction_accuracy(state_db)
        assert accuracy == pytest.approx(0.8, abs=0.01)

    def test_accuracy_windowed(self, state_db):
        """Accuracy should use only the last N predictions."""
        # First 10: all wrong
        for i in range(10):
            _insert_prediction(state_db, f"old-{i}", 70.0, "success", "failed", 0)
        # Next 10: all correct
        for i in range(10):
            _insert_prediction(state_db, f"new-{i}", 70.0, "success", "completed", 1)

        # Window of 10 should see 100% accuracy (most recent)
        accuracy = get_prediction_accuracy(state_db, window=10)
        assert accuracy == pytest.approx(1.0)


# --- Weight Adjustment Tests ---

class TestAdjustFeatureWeights:
    def test_returns_none_when_insufficient_data(self, state_db):
        """Should return None when fewer than 10 resolved predictions."""
        result = adjust_feature_weights(state_db)
        assert result is None

    def test_disables_learned_when_accuracy_low(self, state_db):
        """Accuracy < 60% should disable learned component."""
        # 5 correct, 15 incorrect = 25%
        for i in range(5):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "completed", 1)
        for i in range(5, 20):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "failed", 0)

        result = adjust_feature_weights(state_db)
        assert result is not None
        assert result["learned_disabled"] is True

    def test_enables_learned_when_accuracy_high(self, state_db):
        """Accuracy > 75% should enable wider learned component."""
        # 16 correct, 4 incorrect = 80%
        for i in range(16):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "completed", 1)
        for i in range(16, 20):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "failed", 0)

        result = adjust_feature_weights(state_db)
        assert result is not None
        assert result["learned_disabled"] is False

    def test_returns_none_when_accuracy_moderate(self, state_db):
        """Accuracy between 60-75% should return None (no adjustment)."""
        # 13 correct, 7 incorrect = 65%
        for i in range(13):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "completed", 1)
        for i in range(13, 20):
            _insert_prediction(state_db, f"job-{i}", 70.0, "success", "failed", 0)

        result = adjust_feature_weights(state_db)
        assert result is None


# --- Keyword Overlap Tests ---

class TestKeywordOverlap:
    def test_no_postmortems(self):
        idea = {"title": "Test tool", "description": "A testing tool"}
        assert _compute_keyword_overlap(idea, []) == 1.0

    def test_high_overlap(self):
        idea = {"title": "MCP API server builder", "description": "Build MCP servers"}
        postmortems = [
            {"title": "MCP API server builder failed", "failure_category": "dependency_error"},
            {"title": "MCP server build crashed", "failure_category": "build_error"},
        ]
        result = _compute_keyword_overlap(idea, postmortems)
        assert result < 1.0  # Should have penalty

    def test_no_overlap(self):
        idea = {"title": "garden planner app", "description": "vegetable scheduling"}
        postmortems = [
            {"title": "blockchain mining tool failed", "failure_category": "timeout"},
        ]
        result = _compute_keyword_overlap(idea, postmortems)
        assert result >= 0.95  # Minimal penalty

    def test_dependency_error_penalized_most(self):
        idea = {"title": "Test server builder", "description": "Build servers"}
        pm_dep = [{"title": "Test server builder", "failure_category": "dependency_error"}]
        pm_spec = [{"title": "Test server builder", "failure_category": "spec_unclear"}]

        penalty_dep = _compute_keyword_overlap(idea, pm_dep)
        penalty_spec = _compute_keyword_overlap(idea, pm_spec)
        # dependency_error has higher weight, so penalty should be stronger (lower multiplier)
        assert penalty_dep <= penalty_spec
