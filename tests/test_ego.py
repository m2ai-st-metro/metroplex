"""Tests for the EGO learning module (Phase F)."""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from learning.config import IMPROVEMENT_THRESHOLD
from learning.evaluator import Comparison, evaluate
from learning.ledger import (
    get_experiment_summary,
    get_latest_applied,
    init_ego_tables,
    log_experiment,
    mark_applied,
    mark_rolled_back,
)
from learning.mutator import generate_variant, get_current_constraint_mapping
from learning.applier import apply_variant, get_active_variant, rollback_variant, ACTIVE_VARIANT_PATH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ego_db():
    """In-memory SQLite DB with EGO tables initialized."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_ego_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def sample_mapping():
    return get_current_constraint_mapping()


@pytest.fixture
def sample_breakdown():
    return [
        {"category": "dependency_error", "count": 15},
        {"category": "spec_unclear", "count": 10},
        {"category": "timeout", "count": 5},
        {"category": "test_failure", "count": 3},
    ]


@pytest.fixture
def sample_error_samples():
    return [
        "ModuleNotFoundError: No module named 'foobar'",
        "pytest FAILED: 2 passed, 3 failed",
        "TimeoutError: Build exceeded 90 minutes",
    ]


@pytest.fixture
def variant_path(tmp_path):
    """Override ACTIVE_VARIANT_PATH to a temp directory for test isolation."""
    path = tmp_path / "ego_active_variant.json"
    with patch("learning.applier.ACTIVE_VARIANT_PATH", path):
        yield path


# ---------------------------------------------------------------------------
# Ledger Tests
# ---------------------------------------------------------------------------


class TestLedger:
    def test_init_tables_idempotent(self, ego_db):
        """Calling init twice should not error."""
        init_ego_tables(ego_db)
        tables = ego_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ego_experiments'"
        ).fetchone()
        assert tables is not None

    def test_log_experiment(self, ego_db):
        exp_id = log_experiment(
            ego_db,
            target="failure_feedback",
            parameter="constraint_mapping",
            baseline_value='{"a": "b"}',
            variant_value='{"a": "c"}',
            baseline_score=60.0,
            variant_score=75.0,
            improvement_pct=0.25,
            is_winner=True,
            reason="Variant scored higher",
        )
        assert exp_id == 1

        row = ego_db.execute(
            "SELECT * FROM ego_experiments WHERE id = ?", (exp_id,)
        ).fetchone()
        assert row["target"] == "failure_feedback"
        assert row["is_winner"] == 1
        assert row["status"] == "completed"

    def test_mark_applied(self, ego_db):
        exp_id = log_experiment(
            ego_db, "t", "p", "b", "v", 50, 70, 0.4, True, "test"
        )
        mark_applied(ego_db, exp_id, builds_before=100, success_rate_before=0.45)
        row = ego_db.execute(
            "SELECT * FROM ego_experiments WHERE id = ?", (exp_id,)
        ).fetchone()
        assert row["status"] == "applied"
        assert row["builds_before_apply"] == 100
        assert row["success_rate_before"] == 0.45

    def test_mark_rolled_back(self, ego_db):
        exp_id = log_experiment(
            ego_db, "t", "p", "b", "v", 50, 70, 0.4, True, "test"
        )
        mark_applied(ego_db, exp_id, 100, 0.45)
        mark_rolled_back(ego_db, exp_id, 0.30, "rate dropped")
        row = ego_db.execute(
            "SELECT * FROM ego_experiments WHERE id = ?", (exp_id,)
        ).fetchone()
        assert row["status"] == "rolled_back"
        assert row["success_rate_after"] == 0.30

    def test_get_latest_applied(self, ego_db):
        assert get_latest_applied(ego_db, "t") is None

        exp_id = log_experiment(
            ego_db, "t", "p", "b", "v", 50, 70, 0.4, True, "test"
        )
        mark_applied(ego_db, exp_id, 100, 0.45)
        latest = get_latest_applied(ego_db, "t")
        assert latest is not None
        assert latest["id"] == exp_id

    def test_experiment_summary(self, ego_db):
        log_experiment(ego_db, "t", "p", "b", "v", 50, 70, 0.4, True, "w1")
        log_experiment(ego_db, "t", "p", "b", "v", 50, 45, -0.1, False, "l1")
        summary = get_experiment_summary(ego_db)
        assert summary["total"] == 2
        assert summary["winners"] == 1
        assert summary["applied"] == 0


# ---------------------------------------------------------------------------
# Mutator Tests
# ---------------------------------------------------------------------------


class TestMutator:
    def test_get_current_constraint_mapping(self, sample_mapping):
        assert "spec_unclear" in sample_mapping
        assert "dependency_error" in sample_mapping
        assert len(sample_mapping) == 5
        for v in sample_mapping.values():
            assert isinstance(v, str) and len(v) > 10

    @patch("learning.mutator.OpenAI")
    def test_generate_variant_success(
        self, mock_openai_cls, sample_mapping, sample_breakdown, sample_error_samples
    ):
        variant_data = {
            "spec_unclear": "New constraint for spec_unclear",
            "dependency_error": "New constraint for deps",
            "timeout": "New timeout constraint",
            "test_failure": "New test failure constraint",
            "build_error": "New build error constraint",
        }
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(variant_data)
        mock_openai_cls.return_value.chat.completions.create.return_value = mock_response

        result = generate_variant(
            current_mapping=sample_mapping,
            build_stats={"total": 50, "successful": 25, "failed": 25, "success_rate": 0.5},
            failure_breakdown=sample_breakdown,
            error_samples=sample_error_samples,
            api_key="test-key",
        )
        assert result == variant_data

    @patch("learning.mutator.OpenAI")
    def test_generate_variant_fills_missing_categories(
        self, mock_openai_cls, sample_mapping, sample_breakdown, sample_error_samples
    ):
        """If LLM response is missing a category, baseline value is used."""
        partial_variant = {"spec_unclear": "New constraint"}
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(partial_variant)
        mock_openai_cls.return_value.chat.completions.create.return_value = mock_response

        result = generate_variant(
            current_mapping=sample_mapping,
            build_stats={"total": 50, "successful": 25, "failed": 25, "success_rate": 0.5},
            failure_breakdown=sample_breakdown,
            error_samples=sample_error_samples,
            api_key="test-key",
        )
        assert result["spec_unclear"] == "New constraint"
        assert result["dependency_error"] == sample_mapping["dependency_error"]

    def test_generate_variant_no_api_key(self, sample_mapping):
        with patch.dict("os.environ", {}, clear=True):
            result = generate_variant(
                current_mapping=sample_mapping,
                build_stats={"total": 50, "successful": 25, "failed": 25, "success_rate": 0.5},
                failure_breakdown=[],
                error_samples=[],
            )
        assert result == sample_mapping


# ---------------------------------------------------------------------------
# Evaluator Tests
# ---------------------------------------------------------------------------


class TestEvaluator:
    @patch("learning.evaluator.OpenAI")
    def test_evaluate_winner(
        self, mock_openai_cls, sample_mapping, sample_breakdown, sample_error_samples
    ):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(
            {"score_a": 50, "score_b": 80, "reasoning": "Variant is better"}
        )
        mock_openai_cls.return_value.chat.completions.create.return_value = mock_response

        variant = {k: f"Better: {v}" for k, v in sample_mapping.items()}
        result = evaluate(
            baseline_mapping=sample_mapping,
            variant_mapping=variant,
            failure_breakdown=sample_breakdown,
            error_samples=sample_error_samples,
            api_key="test-key",
        )
        assert isinstance(result, Comparison)
        assert result.is_valid
        assert result.baseline_score == 50
        assert result.variant_score == 80
        assert result.improvement_pct == 0.6  # (80-50)/50
        assert result.is_winner  # 60% > 15% threshold

    @patch("learning.evaluator.OpenAI")
    def test_evaluate_loser(
        self, mock_openai_cls, sample_mapping, sample_breakdown, sample_error_samples
    ):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(
            {"score_a": 70, "score_b": 72, "reasoning": "Marginal improvement"}
        )
        mock_openai_cls.return_value.chat.completions.create.return_value = mock_response

        variant = {k: f"Slightly: {v}" for k, v in sample_mapping.items()}
        result = evaluate(
            baseline_mapping=sample_mapping,
            variant_mapping=variant,
            failure_breakdown=sample_breakdown,
            error_samples=sample_error_samples,
            api_key="test-key",
        )
        assert result.is_valid
        assert not result.is_winner  # ~2.8% < 15% threshold

    def test_evaluate_no_api_key(self, sample_mapping, sample_breakdown, sample_error_samples):
        with patch.dict("os.environ", {}, clear=True):
            result = evaluate(
                baseline_mapping=sample_mapping,
                variant_mapping=sample_mapping,
                failure_breakdown=sample_breakdown,
                error_samples=sample_error_samples,
            )
        assert not result.is_valid

    @patch("learning.evaluator.OpenAI")
    def test_evaluate_records_cost(
        self, mock_openai_cls, in_memory_db, sample_mapping, sample_breakdown, sample_error_samples
    ):
        """When state_db is provided, evaluate() records a cost ledger row."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(
            {"score_a": 60, "score_b": 70, "reasoning": "ok"}
        )
        mock_response.usage.prompt_tokens = 1234
        mock_response.usage.completion_tokens = 567
        mock_openai_cls.return_value.chat.completions.create.return_value = mock_response

        result = evaluate(
            baseline_mapping=sample_mapping,
            variant_mapping=sample_mapping,
            failure_breakdown=sample_breakdown,
            error_samples=sample_error_samples,
            api_key="test-key",
            state_db=in_memory_db,
        )
        assert result.is_valid

        in_memory_db.connect()
        rows = in_memory_db.conn.execute(
            "SELECT source, input_tokens, output_tokens, queue_job_id FROM cost_ledger"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == "ego_evaluator"
        assert rows[0]["input_tokens"] == 1234
        assert rows[0]["output_tokens"] == 567
        assert rows[0]["queue_job_id"] is None

    @patch("learning.evaluator.OpenAI")
    def test_evaluate_without_state_db_does_not_record(
        self, mock_openai_cls, in_memory_db, sample_mapping, sample_breakdown, sample_error_samples
    ):
        """No state_db -> no ledger row."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(
            {"score_a": 60, "score_b": 70, "reasoning": "ok"}
        )
        mock_response.usage.prompt_tokens = 1234
        mock_response.usage.completion_tokens = 567
        mock_openai_cls.return_value.chat.completions.create.return_value = mock_response

        evaluate(
            baseline_mapping=sample_mapping,
            variant_mapping=sample_mapping,
            failure_breakdown=sample_breakdown,
            error_samples=sample_error_samples,
            api_key="test-key",
        )

        in_memory_db.connect()
        rows = in_memory_db.conn.execute("SELECT COUNT(*) AS c FROM cost_ledger").fetchone()
        assert rows["c"] == 0


# ---------------------------------------------------------------------------
# Applier Tests
# ---------------------------------------------------------------------------


class TestApplier:
    def test_apply_and_read_variant(self, variant_path, sample_mapping):
        assert get_active_variant() is None
        applied = apply_variant(sample_mapping, experiment_id=42)
        assert applied
        assert variant_path.exists()

        active = get_active_variant()
        assert active == sample_mapping

    def test_rollback_variant(self, variant_path, sample_mapping):
        apply_variant(sample_mapping, experiment_id=42)
        assert variant_path.exists()

        rolled_back = rollback_variant()
        assert rolled_back
        assert not variant_path.exists()
        assert get_active_variant() is None

    def test_rollback_no_variant(self, variant_path):
        assert rollback_variant()


# ---------------------------------------------------------------------------
# Integration: format_failure_feedback reads EGO variant
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_format_failure_feedback_uses_default(self):
        """Without an active variant, uses hardcoded constraints."""
        from gates.llm_expander import format_failure_feedback

        with patch("learning.applier.ACTIVE_VARIANT_PATH", Path("/nonexistent/path.json")):
            result = format_failure_feedback([
                {"category": "spec_unclear", "count": 5, "stage": "build"},
            ])
        assert "Every feature MUST have concrete CLI commands" in result

    def test_format_failure_feedback_uses_ego_variant(self, variant_path):
        """With an active variant, format_failure_feedback uses EGO's constraints."""
        from gates.llm_expander import format_failure_feedback

        custom = {"spec_unclear": "CUSTOM EGO CONSTRAINT FOR TESTING"}
        apply_variant(custom, experiment_id=99)

        result = format_failure_feedback([
            {"category": "spec_unclear", "count": 5, "stage": "build"},
        ])
        assert "CUSTOM EGO CONSTRAINT FOR TESTING" in result
