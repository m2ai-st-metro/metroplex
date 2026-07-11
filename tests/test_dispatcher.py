"""
Tests for Metroplex Dispatcher -- EA-Claude worker queue integration.
Uses real SQLite with temp DBs (same pattern as test_db.py).
"""
import pytest
import sqlite3
import tempfile
import json
import time
from pathlib import Path

from dispatcher import (
    EAClaudeDispatcher,
    LogDispatcher,
    create_dispatcher,
    route_to_worker,
    build_dispatch_prompt,
    VALID_WORKER_TYPES,
)


# --- Fixtures ---


@pytest.fixture
def dispatch_db():
    """Create a temp DB with the EA-Claude dispatch_queue schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name

    conn = sqlite3.connect(temp_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE dispatch_queue (
            id           TEXT PRIMARY KEY,
            chat_id      TEXT NOT NULL,
            prompt       TEXT NOT NULL,
            worker_type  TEXT NOT NULL,
            status       TEXT DEFAULT 'queued',
            result       TEXT,
            session_id   TEXT,
            created_at   INTEGER NOT NULL,
            started_at   INTEGER,
            completed_at INTEGER,
            error        TEXT,
            notified     INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX idx_dispatch_status ON dispatch_queue(status)")
    conn.execute("CREATE INDEX idx_dispatch_worker ON dispatch_queue(worker_type, status)")
    conn.commit()
    conn.close()

    yield temp_path

    Path(temp_path).unlink(missing_ok=True)


@pytest.fixture
def dispatcher(dispatch_db):
    """Create a real EAClaudeDispatcher with temp DB."""
    return EAClaudeDispatcher(dispatch_db, default_chat_id="test-chat-123")


# --- EAClaudeDispatcher Tests ---


class TestEAClaudeDispatcher:
    """Tests for the real SQLite-backed dispatcher."""

    def test_init_missing_db(self):
        """Raises FileNotFoundError for nonexistent DB."""
        with pytest.raises(FileNotFoundError, match="EA-Claude database not found"):
            EAClaudeDispatcher("/nonexistent/path.db")

    def test_dispatch_creates_task(self, dispatcher, dispatch_db):
        """dispatch() inserts a task into dispatch_queue."""
        task_id = dispatcher.dispatch(
            prompt="Build the widget",
            worker_type="ravage",
        )

        assert task_id  # non-empty UUID string
        assert len(task_id) == 36  # UUID format

        # Verify in DB
        conn = sqlite3.connect(dispatch_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM dispatch_queue WHERE id = ?", (task_id,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["chat_id"] == "test-chat-123"
        assert row["prompt"] == "Build the widget"
        assert row["worker_type"] == "ravage"
        assert row["status"] == "queued"
        assert row["created_at"] > 0

    def test_dispatch_custom_chat_id(self, dispatcher):
        """Custom chat_id overrides default."""
        task_id = dispatcher.dispatch(
            prompt="Test", worker_type="soundwave", chat_id="custom-456"
        )

        result = dispatcher.check_result(task_id)
        assert result["chat_id"] == "custom-456"

    def test_dispatch_invalid_worker(self, dispatcher):
        """Raises ValueError for invalid worker_type."""
        with pytest.raises(ValueError, match="Invalid worker_type"):
            dispatcher.dispatch(prompt="Test", worker_type="megatron")

    def test_dispatch_no_chat_id(self, dispatch_db):
        """Raises ValueError when no chat_id is available."""
        disp = EAClaudeDispatcher(dispatch_db, default_chat_id="")
        with pytest.raises(ValueError, match="No chat_id"):
            disp.dispatch(prompt="Test", worker_type="ravage")

    def test_dispatch_all_worker_types(self, dispatcher):
        """All valid worker types are accepted."""
        for worker in VALID_WORKER_TYPES:
            task_id = dispatcher.dispatch(
                prompt=f"Test {worker}", worker_type=worker
            )
            assert task_id

    def test_check_result_found(self, dispatcher):
        """check_result returns task dict when found."""
        task_id = dispatcher.dispatch(prompt="Check me", worker_type="ravage")
        result = dispatcher.check_result(task_id)

        assert result is not None
        assert result["id"] == task_id
        assert result["status"] == "queued"
        assert result["prompt"] == "Check me"

    def test_check_result_not_found(self, dispatcher):
        """check_result returns None for unknown task_id."""
        result = dispatcher.check_result("nonexistent-uuid")
        assert result is None

    def test_dispatch_timestamps_are_unix_seconds(self, dispatcher):
        """Timestamps use Unix epoch seconds (not milliseconds)."""
        before = int(time.time())
        task_id = dispatcher.dispatch(prompt="Timestamp test", worker_type="ravage")
        after = int(time.time())

        result = dispatcher.check_result(task_id)
        assert before <= result["created_at"] <= after

    def test_multiple_dispatches(self, dispatcher, dispatch_db):
        """Multiple tasks can be dispatched and all are queryable."""
        ids = []
        for i in range(5):
            tid = dispatcher.dispatch(prompt=f"Task {i}", worker_type="ravage")
            ids.append(tid)

        conn = sqlite3.connect(dispatch_db)
        count = conn.execute("SELECT COUNT(*) FROM dispatch_queue").fetchone()[0]
        conn.close()

        assert count == 5
        assert len(set(ids)) == 5  # All unique UUIDs


# --- LogDispatcher Tests ---


class TestLogDispatcher:
    """Tests for the no-op log dispatcher."""

    def test_dispatch_returns_uuid(self):
        """LogDispatcher returns a valid UUID."""
        disp = LogDispatcher()
        task_id = disp.dispatch(prompt="Test", worker_type="ravage")
        assert len(task_id) == 36

    def test_dispatch_records_call(self):
        """LogDispatcher records dispatched calls."""
        disp = LogDispatcher()
        disp.dispatch(prompt="Task A", worker_type="soundwave", chat_id="c1")
        disp.dispatch(prompt="Task B", worker_type="ravage")

        assert len(disp.dispatched) == 2
        assert disp.dispatched[0]["prompt"] == "Task A"
        assert disp.dispatched[0]["worker_type"] == "soundwave"
        assert disp.dispatched[1]["prompt"] == "Task B"

    def test_check_result_terminal_failed(self):
        """LogDispatcher.check_result reports failed so items resolve instead of
        sticking at 'dispatched' forever (loop-map G1)."""
        disp = LogDispatcher()
        task_id = disp.dispatch(prompt="Test", worker_type="ravage")
        result = disp.check_result(task_id)
        assert result is not None
        assert result["status"] == "failed"


# --- Factory Tests ---


class TestCreateDispatcher:
    """Tests for the create_dispatcher factory."""

    def test_creates_real_dispatcher(self, dispatch_db):
        """Returns EAClaudeDispatcher when DB exists."""
        disp = create_dispatcher(dispatch_db, "chat-123")
        assert isinstance(disp, EAClaudeDispatcher)

    def test_creates_log_dispatcher_missing_db(self):
        """Returns LogDispatcher when DB path doesn't exist."""
        disp = create_dispatcher("/nonexistent/path.db", "chat-123")
        assert isinstance(disp, LogDispatcher)

    def test_creates_log_dispatcher_empty_path(self):
        """Returns LogDispatcher when DB path is empty."""
        disp = create_dispatcher("", "chat-123")
        assert isinstance(disp, LogDispatcher)


# --- Worker Routing Tests ---


class TestRouteToWorker:
    """Tests for route_to_worker()."""

    def test_skylynx_claude_md(self):
        assert route_to_worker("skylynx", "claude_md_update") == "ravage"

    def test_skylynx_pipeline(self):
        assert route_to_worker("skylynx", "pipeline_change") == "ravage"

    def test_skylynx_case_study(self):
        assert route_to_worker("skylynx", "case_study_addition") == "soundwave"

    def test_ideaforge_default(self):
        assert route_to_worker("ideaforge", "") == "ravage"

    def test_unknown_source(self):
        assert route_to_worker("unknown_source", "") == "default"

    def test_unknown_type(self):
        assert route_to_worker("skylynx", "unknown_type") == "ravage"  # falls back to (skylynx, "")


# --- Prompt Building Tests ---


class TestBuildDispatchPrompt:
    """Tests for build_dispatch_prompt()."""

    def test_basic_prompt(self):
        """Builds a prompt with title and source."""
        item = {
            "source": "ideaforge",
            "source_id": "42",
            "title": "Build Scanner Tool",
            "description": "Security scanner for MCP",
            "priority_score": 74.0,
            "idea_data": json.dumps({
                "description": "Security scanner for MCP servers",
                "problem_statement": "MCP servers lack auditing",
            }),
        }

        prompt = build_dispatch_prompt(item)
        assert "[metroplex:ideaforge]" in prompt
        assert "Build Scanner Tool" in prompt
        assert "Security scanner" in prompt
        assert "74.0" in prompt

    def test_skylynx_prompt_includes_type(self):
        """Sky-Lynx items include recommendation type."""
        item = {
            "source": "skylynx",
            "source_id": "sl-001",
            "title": "Fix Session Tracking",
            "description": "Session tracking broken",
            "priority_score": 127.5,
            "idea_data": json.dumps({
                "description": "Session tracking broken",
                "_recommendation_type": "pipeline_change",
                "_scope": "all_personas",
            }),
        }

        prompt = build_dispatch_prompt(item)
        assert "pipeline_change" in prompt
        assert "all_personas" in prompt

    def test_empty_idea_data(self):
        """Handles empty or missing idea_data gracefully."""
        item = {
            "source": "ideaforge",
            "source_id": "1",
            "title": "Minimal Item",
            "description": "",
            "priority_score": 50.0,
            "idea_data": "",
        }

        prompt = build_dispatch_prompt(item)
        assert "Minimal Item" in prompt
        assert "50.0" in prompt
