"""Tests for Phase 13e — Budget controls, cost rates, and cost ledger."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from config import Config
from cost_rates import estimate_cost, MODEL_RATES
from db import StateDB


# --- Cost Rates ---


class TestCostRates:
    """Test cost rate estimation."""

    def test_known_model(self):
        cost = estimate_cost("sonnet", 1_000_000, 1_000_000)
        assert cost == 3.0 + 15.0  # $3/M input + $15/M output

    def test_opus_model(self):
        cost = estimate_cost("opus", 1_000_000, 1_000_000)
        assert cost == 15.0 + 75.0

    def test_haiku_model(self):
        cost = estimate_cost("haiku", 100_000, 100_000)
        expected = (100_000 * 0.80 + 100_000 * 4.0) / 1_000_000
        assert abs(cost - expected) < 0.001

    def test_unknown_model_returns_zero(self):
        assert estimate_cost("unknown-model-xyz", 1000, 1000) == 0.0

    def test_zero_tokens(self):
        assert estimate_cost("sonnet", 0, 0) == 0.0

    def test_full_model_name(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 500_000, 100_000)
        expected = (500_000 * 3.0 + 100_000 * 15.0) / 1_000_000
        assert abs(cost - expected) < 0.001

    def test_custom_rates_via_env(self, monkeypatch):
        monkeypatch.setenv("METROPLEX_COST_RATES_JSON", '{"custom-model": {"input": 1.0, "output": 2.0}}')
        cost = estimate_cost("custom-model", 1_000_000, 1_000_000)
        assert cost == 3.0  # 1.0 + 2.0


# --- Cost Ledger (DB) ---


class TestCostLedger:
    """Test cost ledger DB operations."""

    def test_record_cost(self, in_memory_db):
        row_id = in_memory_db.record_cost(
            source="spec_expander",
            model="sonnet",
            input_tokens=1000,
            output_tokens=500,
            estimated_cost=0.012,
        )
        assert row_id > 0

    def test_get_daily_spend_today(self, in_memory_db):
        in_memory_db.record_cost("test", "sonnet", 0, 0, 1.50)
        in_memory_db.record_cost("test", "sonnet", 0, 0, 2.50)
        assert in_memory_db.get_daily_spend() == 4.00

    def test_get_daily_spend_specific_date(self, in_memory_db):
        # Insert with specific timestamp
        in_memory_db.connect()
        in_memory_db.conn.execute(
            "INSERT INTO cost_ledger (timestamp, source, model, input_tokens, output_tokens, estimated_cost) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-03-10T10:00:00", "test", "sonnet", 0, 0, 5.00),
        )
        in_memory_db.conn.commit()

        assert in_memory_db.get_daily_spend("2026-03-10") == 5.00
        assert in_memory_db.get_daily_spend("2026-03-11") == 0.00

    def test_get_monthly_spend(self, in_memory_db):
        in_memory_db.record_cost("test", "sonnet", 0, 0, 10.00)
        in_memory_db.record_cost("test", "sonnet", 0, 0, 20.00)
        month = datetime.now().strftime("%Y-%m")
        assert in_memory_db.get_monthly_spend(month) == 30.00

    def test_get_monthly_spend_empty(self, in_memory_db):
        assert in_memory_db.get_monthly_spend("2020-01") == 0.00

    def test_get_cost_breakdown(self, in_memory_db):
        in_memory_db.record_cost("test", "sonnet", 0, 0, 5.00)
        breakdown = in_memory_db.get_cost_breakdown(days=1)
        assert len(breakdown) >= 1
        assert breakdown[0]["total_cost"] == 5.00
        assert breakdown[0]["entry_count"] == 1

    def test_get_cost_breakdown_empty(self, in_memory_db):
        breakdown = in_memory_db.get_cost_breakdown(days=7)
        assert breakdown == []

    def test_record_cost_with_queue_job_id(self, in_memory_db):
        row_id = in_memory_db.record_cost(
            source="um_bridge",
            model="opus",
            input_tokens=0,
            output_tokens=0,
            estimated_cost=3.00,
            queue_job_id="metroplex-ideaforge-42",
        )
        assert row_id > 0

    def test_update_build_estimated_cost(self, in_memory_db):
        from models import BuildJob
        job = BuildJob(
            idea_id=1, title="Test", spec_path="/tmp/s.txt",
            queue_job_id="metroplex-ideaforge-1", status="completed", queued_at=datetime.now(),
        )
        in_memory_db.record_build_job(job)
        assert in_memory_db.update_build_estimated_cost("metroplex-ideaforge-1", 3.50)

        cursor = in_memory_db.conn.cursor()
        cursor.execute("SELECT estimated_cost FROM build_jobs WHERE queue_job_id = ?", ("metroplex-ideaforge-1",))
        assert cursor.fetchone()["estimated_cost"] == 3.50


# --- Budget Gate (Orchestrator) ---


class TestBudgetCheck:
    """Test orchestrator budget check logic."""

    def _make_orchestrator(self, in_memory_db, daily_limit=50.0, monthly_limit=500.0, alert_threshold=0.8):
        from orchestrator import CycleOrchestrator
        from safety import CircuitBreaker, CycleCaps, ShutdownHandler
        from audit import AuditLogger

        config = Config()
        config.daily_cost_limit = daily_limit
        config.monthly_cost_limit = monthly_limit
        config.cost_alert_threshold = alert_threshold

        notifier = MagicMock()
        notifier.notify = MagicMock(return_value=True)

        triage = MagicMock()
        build = MagicMock()
        cb = CircuitBreaker(threshold=3, state_db=in_memory_db)
        caps = CycleCaps(config)
        shutdown = ShutdownHandler()
        audit = AuditLogger()

        orch = CycleOrchestrator(
            config=config,
            triage_gate=triage,
            build_orchestrator=build,
            circuit_breaker=cb,
            cycle_caps=caps,
            shutdown_handler=shutdown,
            state_db=in_memory_db,
            audit_logger=audit,
            notifier=notifier,
        )
        return orch, notifier

    def test_budget_ok_when_no_spend(self, in_memory_db):
        orch, _ = self._make_orchestrator(in_memory_db)
        ok, msg = orch.check_budget()
        assert ok is True
        assert msg == ""

    def test_budget_blocked_daily_limit(self, in_memory_db):
        in_memory_db.record_cost("test", "opus", 0, 0, 50.00)
        orch, _ = self._make_orchestrator(in_memory_db, daily_limit=50.0)
        ok, msg = orch.check_budget()
        assert ok is False
        assert "Daily cost limit" in msg

    def test_budget_blocked_monthly_limit(self, in_memory_db):
        in_memory_db.record_cost("test", "opus", 0, 0, 500.00)
        orch, _ = self._make_orchestrator(in_memory_db, daily_limit=9999.0, monthly_limit=500.0)
        ok, msg = orch.check_budget()
        assert ok is False
        assert "Monthly cost limit" in msg

    def test_budget_alert_threshold_warns(self, in_memory_db):
        in_memory_db.record_cost("test", "opus", 0, 0, 42.00)  # 84% of $50
        orch, notifier = self._make_orchestrator(in_memory_db, daily_limit=50.0, alert_threshold=0.8)
        ok, msg = orch.check_budget()
        assert ok is True  # Not blocked, just warned
        notifier.notify.assert_called()
        # Check that at least one call was a warning
        warning_calls = [c for c in notifier.notify.call_args_list if c[0][1] == "warning"]
        assert len(warning_calls) >= 1

    def test_budget_under_alert_threshold_no_warning(self, in_memory_db):
        in_memory_db.record_cost("test", "opus", 0, 0, 10.00)  # 20% of $50
        orch, notifier = self._make_orchestrator(in_memory_db, daily_limit=50.0, alert_threshold=0.8)
        ok, msg = orch.check_budget()
        assert ok is True
        notifier.notify.assert_not_called()


# --- Config ---


class TestCostConfig:
    """Test budget-related config fields."""

    def test_default_values(self):
        config = Config()
        assert config.daily_cost_limit == 50.0
        assert config.monthly_cost_limit == 500.0
        assert config.cost_alert_threshold == 0.8
        assert config.build_cost_estimate == 3.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("METROPLEX_DAILY_COST_LIMIT", "100.0")
        monkeypatch.setenv("METROPLEX_MONTHLY_COST_LIMIT", "1000.0")
        monkeypatch.setenv("METROPLEX_COST_ALERT_THRESHOLD", "0.9")
        monkeypatch.setenv("METROPLEX_BUILD_COST_ESTIMATE", "5.0")
        config = Config()
        assert config.daily_cost_limit == 100.0
        assert config.monthly_cost_limit == 1000.0
        assert config.cost_alert_threshold == 0.9
        assert config.build_cost_estimate == 5.0
