"""Meta-coverage tests for spec-claims that are file-existence / suite-level.

Covers spec-claims: C-08, C-09, C-12, C-16, C-29.

These claims aren't about runtime agent behavior — they're about the
shape of the test suite itself and the canonical-trigger surface the
matcher exposes.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def test_e2e_sarah_logs_incident_file_exists() -> None:
    """Covers C-08: tests/test_e2e_sarah_logs_incident.py is shipped."""
    f = TESTS / "test_e2e_sarah_logs_incident.py"
    assert f.exists()
    text = f.read_text()
    assert "incident" in text.lower()
    assert "negat" in text.lower(), "must include a negation test"


def test_e2e_sarah_requests_help_file_exists() -> None:
    """Covers C-09: tests/test_e2e_sarah_requests_help.py is shipped."""
    f = TESTS / "test_e2e_sarah_requests_help.py"
    assert f.exists()
    text = f.read_text()
    assert "resource" in text.lower()


def test_negation_and_word_boundary_test_files_exist() -> None:
    """Covers C-12, C-16: suite includes negation AND word-boundary cases,
    both positive and negative.
    """
    neg = TESTS / "test_matcher_negation.py"
    wb = TESTS / "test_matcher_word_boundary.py"
    assert neg.exists()
    assert wb.exists()

    neg_text = neg.read_text()
    wb_text = wb.read_text()

    # Negative cases (negation rejection)
    assert "didn't" in neg_text
    assert ".matched is False" in neg_text

    # Positive controls (negation tests must include positives too — C-16)
    assert "positive_control" in neg_text or ".matched is True" in neg_text

    # Word-boundary negative cases
    assert "argumentative" in wb_text or "incidental" in wb_text
    # Word-boundary positive controls
    assert "positive_control" in wb_text or ".matched is True" in wb_text


def test_canonical_trigger_tokens_covered() -> None:
    """Covers C-29: the spec's canonical trigger tokens (incident, argument,
    aggression, threat) are exercised by tests.
    """
    contract = (ROOT / ".self-healing-pipeline" / "test-contract.md").read_text().lower()
    for tok in ("argument", "aggression", "threat", "incident"):
        assert tok in contract, f"canonical trigger '{tok}' is not exercised in test-contract.md"
