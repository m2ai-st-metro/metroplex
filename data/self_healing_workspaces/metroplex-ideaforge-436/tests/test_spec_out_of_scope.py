"""Tests for out-of-scope (T1) claims and no-hardcoded-secrets.

Covers: C-25, C-26, C-27, C-28, C-29, C-30, C-31, C-33
"""

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
OWN_PATH = Path(__file__).resolve()


def _source_files() -> list[Path]:
    """All non-test Python files under workspace root (excluding venv, state, this scanner)."""
    result = []
    for p in WORKSPACE_ROOT.rglob("*.py"):
        rp = p.resolve()
        if rp == OWN_PATH:
            continue
        parts = p.parts
        if "venv" in parts or ".venv" in parts:
            continue
        if ".self-healing-pipeline" in parts:
            continue
        if "tests" in parts:
            # exclude tests dir entirely from source scan
            continue
        result.append(p)
    return result


def _all_text_files() -> list[Path]:
    """All non-binary source files in repo (used for secret pattern scans)."""
    extensions = {".py", ".yaml", ".yml", ".md", ".txt", ".json", ".toml", ".ini", ".cfg"}
    result = []
    for p in WORKSPACE_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in extensions:
            continue
        rp = p.resolve()
        if rp == OWN_PATH:
            continue
        parts = p.parts
        if "venv" in parts or ".venv" in parts:
            continue
        if ".self-healing-pipeline" in parts:
            continue
        if "tests" in parts:
            continue
        result.append(p)
    return result


def test_no_web_frontend():
    """Covers C-28: no web frontend in T1."""
    forbidden = ("from fastapi", "import fastapi", "from flask", "import flask",
                 "from django", "import django", "from starlette", "import starlette")
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in forbidden:
            assert bad not in text, f"{p} imports forbidden web framework: {bad}"


def test_no_calendar_integration():
    """Covers C-29: no calendar integration."""
    forbidden = ("googleapiclient.discovery", "google.calendar", "caldav", "icalendar")
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in forbidden:
            assert bad not in text, f"{p} imports forbidden calendar lib: {bad}"


def test_no_external_http_services():
    """Covers C-30: no external HTTP services."""
    forbidden = ("import requests", "from requests", "import httpx", "from httpx",
                 "urllib.request.urlopen", "import aiohttp", "from aiohttp")
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in forbidden:
            assert bad not in text, f"{p} imports forbidden HTTP lib: {bad}"


def test_no_insurance_claim_submission():
    """Covers C-31: no insurance claim submission."""
    forbidden_pattern = re.compile(r"\b(submit|file)_?insurance_?claim\b", re.IGNORECASE)
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        m = forbidden_pattern.search(text)
        assert not m, f"{p} contains forbidden insurance-claim function: {m.group(0) if m else ''}"


def test_no_multi_user_partitioning():
    """Covers C-25: no multi-user partitioning (no user_id / tenant_id partitioning columns)."""
    # Look for SQL or ORM patterns that imply multi-tenant partitioning, NOT just a `user` mention.
    forbidden_pattern = re.compile(r"\bpartition\s+by\s+(user_id|tenant_id)\b", re.IGNORECASE)
    forbidden_columns = (
        "tenant_id = Column",
        "tenant_id: str = Field",
    )
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        m = forbidden_pattern.search(text)
        assert not m, f"{p} declares multi-tenant partitioning"
        for bad in forbidden_columns:
            assert bad not in text, f"{p} declares forbidden tenant column: {bad}"


def test_no_voice_synthesis():
    """Covers C-26: no voice synthesis."""
    forbidden = ("import pyttsx3", "from pyttsx3", "from gtts", "import gtts",
                 "from elevenlabs", "import elevenlabs", "openai.audio.speech")
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in forbidden:
            assert bad not in text, f"{p} imports forbidden voice-synthesis lib: {bad}"


def test_no_cross_session_memory():
    """Covers C-27: no cross-session persistent storage."""
    forbidden = (
        "sqlite3.connect",
        "psycopg2.connect",
        "psycopg.connect",
        "sqlalchemy.create_engine",
        "from sqlalchemy import create_engine",
    )
    for p in _source_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in forbidden:
            assert bad not in text, f"{p} sets up persistent storage: {bad}"


def test_no_hardcoded_secrets():
    """Covers C-33: no hardcoded API keys / secrets in source."""
    # Common API key signatures. The test file itself is excluded by _all_text_files()
    # filtering "tests" out of the scan scope.
    signatures = (
        re.compile(r"sk-[A-Za-z0-9]{20,}"),           # OpenAI / Anthropic
        re.compile(r"AKIA[0-9A-Z]{16}"),               # AWS access key
        re.compile(r"AIza[0-9A-Za-z\-_]{35}"),         # Google API
        re.compile(r"\d{9,10}:[A-Za-z0-9_-]{30,40}"),  # Telegram bot token
        re.compile(r"ghp_[A-Za-z0-9]{30,}"),           # GitHub PAT
        re.compile(r"xoxb-[A-Za-z0-9-]{20,}"),         # Slack bot token
    )
    for p in _all_text_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in signatures:
            m = pat.search(text)
            assert not m, f"{p} contains a hardcoded secret matching {pat.pattern}"
