"""Out-of-scope guards.

Covers spec-claims: C-17, C-18, C-20, C-21, C-22, C-23, C-24, C-25, C-26, C-27.

NOTE: All negative-existence scans EXCLUDE this test file (and the tests/ dir
generally) so the forbidden-pattern strings inside this very file don't make
the test mechanically unpassable.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()
TEST_DIR = THIS_FILE.parent
PIPELINE_DIR = ROOT / ".self-healing-pipeline"


def _source_files() -> list[Path]:
    """Project source files, EXCLUDING tests/, .self-healing-pipeline/, and runtime data."""
    out: list[Path] = []
    for p in ROOT.rglob("*.py"):
        rp = p.resolve()
        if rp == THIS_FILE:
            continue
        if TEST_DIR in rp.parents:
            continue
        if PIPELINE_DIR in rp.parents:
            continue
        if any(part in {".venv", "venv", "__pycache__", ".pytest_cache"} for part in rp.parts):
            continue
        out.append(p)
    return out


def _all_source_text() -> str:
    chunks: list[str] = []
    for p in _source_files():
        try:
            chunks.append(p.read_text(errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def test_no_remote_runtime_deps() -> None:
    """Covers C-17: no cloud-SDK dependencies in requirements.txt."""
    req = ROOT / "requirements.txt"
    if not req.exists():
        # absence of cloud deps is also OK
        return
    text = req.read_text().lower()
    forbidden = ["boto3", "boto", "google-cloud", "google-api-python-client", "azure-"]
    for term in forbidden:
        assert term not in text, f"forbidden remote dep: {term}"


def test_no_hardcoded_api_keys() -> None:
    """Covers C-18: no API-key-shape strings in source."""
    text = _all_source_text()
    # Common API key prefixes; allow as variable names but disallow as string literals
    forbidden_patterns = [
        re.compile(r"['\"]sk-[A-Za-z0-9]{20,}['\"]"),
        re.compile(r"['\"]AIza[A-Za-z0-9_\-]{20,}['\"]"),
        re.compile(r"['\"]ghp_[A-Za-z0-9]{20,}['\"]"),
        re.compile(r"['\"]xoxb-[A-Za-z0-9\-]{20,}['\"]"),
    ]
    for pat in forbidden_patterns:
        m = pat.search(text)
        assert m is None, f"hardcoded API key shape found: {m.group(0) if m else ''}"


def test_single_purpose() -> None:
    """Covers C-20: README focuses on elder-care, no other domains."""
    readme = ROOT / "README.md"
    if not readme.exists():
        # README is required by C-10; another test will fail. Don't double-fail.
        return
    text = readme.read_text().lower()
    # Check the README doesn't drift into unrelated domains
    forbidden = ["cryptocurrency", "stock trading", "dating", "recipes"]
    for term in forbidden:
        assert term not in text, f"out-of-scope domain in README: {term}"


def test_no_web_frontend() -> None:
    """Covers C-21: no web framework imports."""
    text = _all_source_text()
    forbidden = ["from flask", "import flask", "from fastapi", "import fastapi", "from django", "import django"]
    for term in forbidden:
        assert term not in text, f"web framework imported: {term}"


def test_no_multi_user_partitioning() -> None:
    """Covers C-22: no user_id partition column in incident persistence."""
    impl = ROOT / "skills" / "incident_logging" / "implementation.py"
    if impl.exists():
        text = impl.read_text().lower()
        assert "user_id" not in text, "multi-user partitioning detected"


def test_no_voice_synthesis() -> None:
    """Covers C-23: no TTS deps."""
    text = _all_source_text().lower()
    forbidden = ["pyttsx3", "gtts", "elevenlabs", "import azure.cognitiveservices.speech"]
    for term in forbidden:
        assert term not in text, f"voice synthesis dep: {term}"


def test_no_cross_session_memory() -> None:
    """Covers C-24: only incident/silent-drop logs are persisted, no chat history."""
    impl = ROOT / "skills" / "incident_logging" / "implementation.py"
    if impl.exists():
        text = impl.read_text().lower()
        assert "conversation_history" not in text
        assert "session_history" not in text


def test_no_calendar_integration() -> None:
    """Covers C-25: no Google Calendar / iCal deps."""
    text = _all_source_text().lower()
    forbidden = ["from googleapiclient", "import googleapiclient", "import ical", "from ical"]
    for term in forbidden:
        assert term not in text, f"calendar dep: {term}"


def test_no_external_http() -> None:
    """Covers C-26: no outbound HTTP libraries in agent code."""
    text = _all_source_text().lower()
    forbidden = ["import requests", "from requests", "import httpx", "from httpx"]
    for term in forbidden:
        assert term not in text, f"external HTTP lib: {term}"


def test_no_insurance_claim() -> None:
    """Covers C-27: no insurance claim flow."""
    text = (_all_source_text() + "\n" + (ROOT / "README.md").read_text() if (ROOT / "README.md").exists() else "").lower()
    forbidden = ["insurance claim", "policy number", "submit a claim"]
    for term in forbidden:
        assert term not in text, f"insurance claim concept: {term}"
