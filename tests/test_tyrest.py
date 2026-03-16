"""Tests for Tyrest QA gate (Gate 2.5 / Gate 4.25)."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from openai import APIError

from gates.tyrest import (
    TyrestGate,
    TyrestSpecResult,
    TyrestBuildResult,
    apply_confidence_gating,
    scan_project,
)


# --- Confidence Gating Tests ---


class TestConfidenceGating:
    def test_high_confidence_approve_passes(self):
        verdict, overridden = apply_confidence_gating("APPROVE", 0.9)
        assert verdict == "APPROVE"
        assert not overridden

    def test_low_confidence_approve_escalates(self):
        verdict, overridden = apply_confidence_gating("APPROVE", 0.6)
        assert verdict == "REQUEST_REVIEW"
        assert overridden

    def test_high_confidence_reject_passes(self):
        verdict, overridden = apply_confidence_gating("REJECT", 0.9)
        assert verdict == "REJECT"
        assert not overridden

    def test_low_confidence_reject_escalates(self):
        verdict, overridden = apply_confidence_gating("REJECT", 0.6)
        assert verdict == "REQUEST_REVIEW"
        assert overridden

    def test_very_low_confidence_always_escalates(self):
        verdict, overridden = apply_confidence_gating("APPROVE", 0.3)
        assert verdict == "REQUEST_REVIEW"
        assert overridden

    def test_request_review_passes_through(self):
        verdict, overridden = apply_confidence_gating("REQUEST_REVIEW", 0.8)
        assert verdict == "REQUEST_REVIEW"
        assert not overridden

    def test_custom_thresholds(self):
        verdict, overridden = apply_confidence_gating(
            "APPROVE", 0.85, approve_min=0.9
        )
        assert verdict == "REQUEST_REVIEW"
        assert overridden


# --- Project Scanner Tests ---


class TestScanProject:
    def test_scan_basic_project(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Project")
        (tmp_path / ".gitignore").write_text("__pycache__/")

        result = scan_project(tmp_path)
        assert result["file_count"] == 3
        assert result["source_file_count"] == 1
        assert result["has_readme"] is True
        assert result["has_gitignore"] is True

    def test_scan_with_tests(self, tmp_path):
        (tmp_path / "app.py").write_text("pass")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_app.py").write_text("pass")

        result = scan_project(tmp_path)
        assert result["source_file_count"] == 2
        assert result["test_file_count"] == 1

    def test_scan_ignores_node_modules(self, tmp_path):
        (tmp_path / "index.js").write_text("module.exports = {}")
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("junk")

        result = scan_project(tmp_path)
        assert result["source_file_count"] == 1
        assert result["file_count"] == 1

    def test_scan_empty_project(self, tmp_path):
        result = scan_project(tmp_path)
        assert result["file_count"] == 0
        assert result["source_file_count"] == 0
        assert result["has_readme"] is False


# --- TyrestGate Tests ---


def _mock_openai_response(content: dict):
    """Create a mock OpenAI chat completion response."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps(content)
    return mock_response


class TestTyrestGateDisabled:
    def test_spec_review_auto_approves_when_disabled(self):
        gate = TyrestGate(enabled=False)
        result = gate.review_spec("some spec")
        assert result.approved
        assert result.model_used == "disabled"

    def test_build_review_auto_approves_when_disabled(self, tmp_path):
        gate = TyrestGate(enabled=False)
        result = gate.review_build(tmp_path, "some spec")
        assert result.approved
        assert result.model_used == "disabled"


class TestTyrestSpecReview:
    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_approve_spec(self):
        gate = TyrestGate(enabled=True)
        mock_resp = _mock_openai_response({
            "verdict": "APPROVE",
            "confidence": 0.9,
            "reasoning": "Clear spec, buildable scope.",
            "scores": {
                "buildability": 0.9,
                "scope_realism": 0.85,
                "spec_clarity": 0.9,
                "overall": 0.88,
            },
            "risk_flags": [],
            "suggestions": [],
        })
        gate._client = MagicMock()
        gate._client.chat.completions.create.return_value = mock_resp

        result = gate.review_spec("Build a CLI tool that converts MD to PDF")
        assert result.approved
        assert result.overall == 0.88
        assert result.risk_flags == []

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_reject_spec(self):
        gate = TyrestGate(enabled=True)
        mock_resp = _mock_openai_response({
            "verdict": "REJECT",
            "confidence": 0.95,
            "reasoning": "Requires paid API keys and real user data.",
            "scores": {
                "buildability": 0.2,
                "scope_realism": 0.1,
                "spec_clarity": 0.5,
                "overall": 0.2,
            },
            "risk_flags": ["requires_paid_api", "scope_too_large"],
            "suggestions": ["Remove external API dependencies"],
        })
        gate._client = MagicMock()
        gate._client.chat.completions.create.return_value = mock_resp

        result = gate.review_spec("Build a full payment processing platform")
        assert result.rejected
        assert "requires_paid_api" in result.risk_flags

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_api_error_returns_request_review(self):
        gate = TyrestGate(enabled=True)
        gate._client = MagicMock()
        gate._client.chat.completions.create.side_effect = APIError(
            message="rate limited", request=MagicMock(), body=None,
        )

        result = gate.review_spec("some spec")
        assert result.verdict == "REQUEST_REVIEW"
        assert "api_error" in result.risk_flags

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_low_confidence_approve_gets_overridden(self):
        gate = TyrestGate(enabled=True, approve_confidence=0.8)
        mock_resp = _mock_openai_response({
            "verdict": "APPROVE",
            "confidence": 0.6,
            "reasoning": "Probably buildable.",
            "scores": {
                "buildability": 0.7,
                "scope_realism": 0.6,
                "spec_clarity": 0.7,
                "overall": 0.65,
            },
            "risk_flags": [],
            "suggestions": [],
        })
        gate._client = MagicMock()
        gate._client.chat.completions.create.return_value = mock_resp

        result = gate.review_spec("some spec")
        assert result.verdict == "REQUEST_REVIEW"


class TestTyrestBuildReview:
    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_approve_build(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "README.md").write_text("# Project")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("pass")

        gate = TyrestGate(enabled=True)
        mock_resp = _mock_openai_response({
            "verdict": "APPROVE",
            "confidence": 0.85,
            "reasoning": "Build matches spec with tests and docs.",
            "scores": {
                "spec_alignment": 0.9,
                "completeness": 0.85,
                "quality_signals": 0.8,
                "overall": 0.85,
            },
            "flags": [],
        })
        gate._client = MagicMock()
        gate._client.chat.completions.create.return_value = mock_resp

        result = gate.review_build(tmp_path, "Build a hello world CLI")
        assert result.approved
        assert result.overall == 0.85

    def test_missing_project_dir_rejects(self):
        gate = TyrestGate(enabled=True)
        result = gate.review_build(Path("/nonexistent"), "some spec")
        assert result.rejected
        assert result.model_used == "hard_gate"
        assert "project_dir_missing" in result.flags

    def test_no_source_code_rejects(self, tmp_path):
        (tmp_path / "README.md").write_text("# Empty project")

        gate = TyrestGate(enabled=True)
        result = gate.review_build(tmp_path, "some spec")
        assert result.rejected
        assert result.model_used == "hard_gate"
        assert "no_source_code" in result.flags


class TestTyrestConfig:
    def test_config_loads_tyrest_fields(self):
        with patch.dict("os.environ", {
            "TYREST_ENABLED": "true",
            "TYREST_MODEL": "gpt-4o-mini",
            "TYREST_APPROVE_MIN_CONFIDENCE": "0.8",
            "TYREST_REJECT_MIN_CONFIDENCE": "0.7",
        }):
            from config import Config
            c = Config()
            assert c.tyrest_enabled is True
            assert c.tyrest_model == "gpt-4o-mini"
            assert c.tyrest_approve_confidence == 0.8
            assert c.tyrest_reject_confidence == 0.7

    def test_config_tyrest_disabled(self):
        with patch.dict("os.environ", {"TYREST_ENABLED": "false"}):
            from config import Config
            c = Config()
            assert c.tyrest_enabled is False
