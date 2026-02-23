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
        assert config.um_db == "/home/apexaipc/projects/ultra-magnus/idea-factory/data/idea-factory.db"
        assert config.stfactory_db == "/home/apexaipc/projects/st-factory/data/persona_metrics.db"

    def test_default_thresholds(self):
        config = Config()
        assert config.approve_threshold == 70
        assert config.reject_threshold == 40

    def test_default_caps(self):
        config = Config()
        assert config.max_approve_per_cycle == 3
        assert config.max_patches_per_cycle == 5

    def test_default_circuit_breaker(self):
        config = Config()
        assert config.circuit_breaker_threshold == 3

    def test_default_cycle_sleep_seconds(self):
        config = Config()
        assert config.cycle_sleep_seconds == 60

    def test_default_build_model(self):
        config = Config()
        assert config.build_model == "opus"

    def test_default_academy_repo(self):
        config = Config()
        assert config.academy_repo == "m2ai-portfolio/agent-persona-academy"


class TestConfigEnvOverrides:
    """Test Config environment variable overrides."""

    def test_override_db_paths(self):
        env = {
            "METROPLEX_IDEAFORGE_DB": "/tmp/test_ideaforge.db",
            "METROPLEX_UM_DB": "/tmp/test_um.db",
            "METROPLEX_STFACTORY_DB": "/tmp/test_stfactory.db",
        }
        with patch.dict(os.environ, env):
            config = Config()
            assert config.ideaforge_db == "/tmp/test_ideaforge.db"
            assert config.um_db == "/tmp/test_um.db"
            assert config.stfactory_db == "/tmp/test_stfactory.db"

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
            "METROPLEX_MAX_PATCHES_PER_CYCLE": "10",
        }
        with patch.dict(os.environ, env):
            config = Config()
            assert config.max_approve_per_cycle == 5
            assert config.max_patches_per_cycle == 10

    def test_override_build_model(self):
        with patch.dict(os.environ, {"METROPLEX_BUILD_MODEL": "sonnet"}):
            config = Config()
            assert config.build_model == "sonnet"

    def test_override_cycle_sleep_seconds(self):
        with patch.dict(os.environ, {"METROPLEX_CYCLE_SLEEP_SECONDS": "120"}):
            config = Config()
            assert config.cycle_sleep_seconds == 120

    def test_invalid_int_env_keeps_default(self):
        with patch.dict(os.environ, {"METROPLEX_APPROVE_THRESHOLD": "not_a_number"}):
            config = Config()
            assert config.approve_threshold == 70  # default preserved


class TestConfigValidation:
    """Test Config.validate() method."""

    def test_validate_missing_db_paths(self):
        config = Config()
        config.ideaforge_db = "/nonexistent/path.db"
        config.um_db = "/nonexistent/path2.db"
        config.stfactory_db = "/nonexistent/path3.db"
        config.yce_dir = "/nonexistent/dir"

        warnings = config.validate()
        assert len(warnings) >= 4
        assert any("IdeaForge DB" in w for w in warnings)
        assert any("Ultra-Magnus DB" in w for w in warnings)
        assert any("ST Factory DB" in w for w in warnings)
        assert any("YCE directory" in w for w in warnings)

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
        config.um_db = str(db_file)
        config.stfactory_db = str(db_file)
        config.yce_dir = str(tmp_path)

        warnings = config.validate()
        # Only threshold-related warnings should remain (none if defaults)
        db_warnings = [w for w in warnings if "not found" in w]
        assert len(db_warnings) == 0
