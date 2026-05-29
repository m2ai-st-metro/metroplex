"""
Tests for Metroplex Config module.
"""
import os
import pytest
from unittest.mock import patch
from pathlib import Path

from config import Config


class TestConfigDefaults:
    """Test Config default values."""

    def test_default_db_paths(self):
        config = Config()
        assert config.ideaforge_db == "/home/apexaipc/projects/ideaforge/data/ideaforge.db"
        assert config.um_db == ""  # Deprecated
        assert config.st_records_db == "/home/apexaipc/projects/st-records/data/persona_metrics.db"

    def test_default_thresholds(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METROPLEX_APPROVE_THRESHOLD", None)
            os.environ.pop("METROPLEX_REJECT_THRESHOLD", None)
            config = Config()
            assert config.approve_threshold == 55
            assert config.reject_threshold == 40

    def test_default_caps(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("METROPLEX_MAX_APPROVE_PER_CYCLE", None)
            config = Config()
            assert config.max_approve_per_cycle == 3

    def test_default_circuit_breaker(self):
        config = Config()
        assert config.circuit_breaker_threshold == 3

    def test_default_cycle_sleep_seconds(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("METROPLEX_CYCLE_SLEEP_SECONDS", None)
            config = Config()
            assert config.cycle_sleep_seconds == 60

    def test_default_build_model(self):
        config = Config()
        assert config.build_model == "opus"

    def test_default_build_parallel(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_PARALLEL": ""}):
            config = Config()
            assert config.build_parallel is False

    def test_default_build_max_workers(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_MAX_WORKERS": ""}):
            config = Config()
            assert config.build_max_workers == 2

class TestConfigEnvOverrides:
    """Test Config environment variable overrides."""

    def test_override_db_paths(self):
        env = {
            "METROPLEX_IDEAFORGE_DB": "/tmp/test_ideaforge.db",
            "METROPLEX_UM_DB": "/tmp/test_um.db",
            "METROPLEX_ST_RECORDS_DB": "/tmp/test_st_records.db",
        }
        with patch.dict(os.environ, env):
            config = Config()
            assert config.ideaforge_db == "/tmp/test_ideaforge.db"
            assert config.st_records_db == "/tmp/test_st_records.db"

    def test_override_thresholds(self):
        env = {
            "METROPLEX_APPROVE_THRESHOLD": "80",
            "METROPLEX_REJECT_THRESHOLD": "30",
        }
        with patch.dict(os.environ, env):
            config = Config()
            assert config.approve_threshold == 80
            assert config.reject_threshold == 30

    def test_override_caps(self):
        env = {
            "METROPLEX_MAX_APPROVE_PER_CYCLE": "5",
        }
        with patch.dict(os.environ, env):
            config = Config()
            assert config.max_approve_per_cycle == 5

    def test_override_build_model(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_MODEL": "sonnet"}):
            config = Config()
            assert config.build_model == "sonnet"

    def test_override_cycle_sleep_seconds(self):
        with patch.dict(os.environ, {"METROPLEX_CYCLE_SLEEP_SECONDS": "120"}):
            config = Config()
            assert config.cycle_sleep_seconds == 120

    def test_override_build_parallel_true(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_PARALLEL": "true"}):
            config = Config()
            assert config.build_parallel is True

    def test_override_build_parallel_one(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_PARALLEL": "1"}):
            config = Config()
            assert config.build_parallel is True

    def test_override_build_parallel_false(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_PARALLEL": "false"}):
            config = Config()
            assert config.build_parallel is False

    def test_override_build_max_workers(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_MAX_WORKERS": "3"}):
            config = Config()
            assert config.build_max_workers == 3

    def test_invalid_max_workers_keeps_default(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_MAX_WORKERS": "abc"}):
            config = Config()
            assert config.build_max_workers == 2  # default preserved

    def test_invalid_int_env_keeps_default(self):
        with patch.dict(os.environ, {"METROPLEX_APPROVE_THRESHOLD": "not_a_number"}):
            config = Config()
            assert config.approve_threshold == 55  # default preserved

    def test_override_max_concurrent_builds(self):
        with patch.dict(os.environ, {"METROPLEX_MAX_CONCURRENT_BUILDS": "4"}):
            config = Config()
            assert config.max_concurrent_builds == 4

    def test_invalid_max_concurrent_builds_keeps_default(self):
        with patch.dict(os.environ, {"METROPLEX_MAX_CONCURRENT_BUILDS": "nope"}):
            config = Config()
            assert config.max_concurrent_builds == 1  # default preserved


class TestConfigConcurrencyDefaults:
    """Test Level 2 concurrency config defaults (env-isolated)."""

    def test_default_max_concurrent_builds(self):
        with patch.dict(os.environ, {"METROPLEX_MAX_CONCURRENT_BUILDS": ""}):
            config = Config()
            assert config.max_concurrent_builds == 1


class TestConfigValidation:
    """Test Config.validate() method."""

    def test_validate_missing_db_paths(self):
        config = Config()
        config.ideaforge_db = "/nonexistent/path.db"
        config.st_records_db = "/nonexistent/path3.db"

        warnings = config.validate()
        assert len(warnings) >= 2
        assert any("IdeaForge DB" in w for w in warnings)
        assert any("ST Records DB" in w for w in warnings)

    def test_validate_approve_below_reject(self):
        config = Config()
        config.approve_threshold = 30
        config.reject_threshold = 70

        warnings = config.validate()
        assert any("approve_threshold" in w and "reject_threshold" in w for w in warnings)

    def test_validate_threshold_out_of_range(self):
        config = Config()
        config.approve_threshold = 150

        warnings = config.validate()
        assert any("between 0 and 100" in w for w in warnings)

    def test_validate_cycle_sleep_below_10(self):
        config = Config()
        config.cycle_sleep_seconds = 5
        warnings = config.validate()
        assert any("cycle_sleep_seconds" in w for w in warnings)

    def test_validate_existing_paths_no_warnings(self, tmp_path):
        db_file = tmp_path / "test.db"
        db_file.touch()

        config = Config()
        config.ideaforge_db = str(db_file)
        config.st_records_db = str(db_file)

        warnings = config.validate()
        # Only threshold-related warnings should remain (none if defaults)
        db_warnings = [w for w in warnings if "not found" in w]
        assert len(db_warnings) == 0
