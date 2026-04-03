"""
Regression tests for WAL (Write-Ahead Logging) consistency.

Validates that:
1. StateDB enables WAL mode on init
2. StateDB sets busy_timeout on all connections
3. WAL checkpoint on connect() works correctly
4. Reader/writer connections set busy_timeout
5. Concurrent read/write does not produce "database is locked"
"""
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from db import StateDB


@pytest.fixture
def file_db():
    """Provide a file-based StateDB for WAL tests (WAL requires a real file)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = StateDB(db_path)
    db.init_db()
    yield db
    db.close()
    Path(db_path).unlink(missing_ok=True)
    # Clean up WAL/SHM files
    Path(db_path + "-wal").unlink(missing_ok=True)
    Path(db_path + "-shm").unlink(missing_ok=True)


class TestWALModeEnabled:
    """Verify WAL journal mode is set on init."""

    def test_init_db_enables_wal(self, file_db):
        """StateDB.init_db() must set journal_mode=WAL."""
        row = file_db.conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal", f"Expected WAL mode, got '{row[0]}'"

    def test_in_memory_db_skips_wal_gracefully(self):
        """In-memory DBs cannot use WAL; init_db should not crash."""
        db = StateDB(":memory:")
        db.init_db()
        # In-memory DBs use 'memory' journal mode; just verify no crash
        row = db.conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] in ("memory", "wal")
        db.close()


class TestBusyTimeout:
    """Verify busy_timeout is set on all connection paths."""

    def test_connect_sets_busy_timeout(self, file_db):
        """StateDB.connect() must set busy_timeout=5000."""
        row = file_db.conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 5000, f"Expected busy_timeout=5000, got {row[0]}"

    def test_reconnect_sets_busy_timeout(self, file_db):
        """Closing and re-connecting must restore busy_timeout."""
        db_path = file_db.db_path
        file_db.close()
        file_db.connect()
        row = file_db.conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 5000

    def test_init_db_sets_busy_timeout(self):
        """A fresh init_db on file DB must have busy_timeout."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = StateDB(db_path)
            db.init_db()
            row = db.conn.execute("PRAGMA busy_timeout").fetchone()
            assert row[0] == 5000
            db.close()
        finally:
            Path(db_path).unlink(missing_ok=True)
            Path(db_path + "-wal").unlink(missing_ok=True)
            Path(db_path + "-shm").unlink(missing_ok=True)


class TestWALCheckpoint:
    """Verify WAL checkpoint behavior on connect."""

    def test_checkpoint_on_connect_sees_external_writes(self, file_db):
        """A second connection via connect() should see writes from another connection."""
        db_path = file_db.db_path

        # Write via a separate connection (simulating another process)
        ext_conn = sqlite3.connect(db_path)
        ext_conn.execute("PRAGMA journal_mode=WAL")
        ext_conn.execute(
            "INSERT INTO gate_status (gate, consecutive_failures, halted) "
            "VALUES ('triage', 99, 0) "
            "ON CONFLICT(gate) DO UPDATE SET consecutive_failures = 99"
        )
        ext_conn.commit()
        ext_conn.close()

        # Reconnect StateDB (triggers checkpoint)
        file_db.close()
        file_db.connect()

        row = file_db.conn.execute(
            "SELECT consecutive_failures FROM gate_status WHERE gate = 'triage'"
        ).fetchone()
        assert row[0] == 99, "Checkpoint did not make external write visible"


class TestConcurrentAccess:
    """Verify that concurrent readers and writers do not deadlock or error."""

    def test_concurrent_read_write_no_locked_error(self, file_db):
        """Multiple threads reading and writing should not raise 'database is locked'."""
        db_path = file_db.db_path
        errors = []

        def writer():
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                for i in range(20):
                    conn.execute(
                        "INSERT INTO triage_decisions "
                        "(idea_id, title, weighted_score, scaled_score, decision, reason, decided_at) "
                        "VALUES (?, ?, 5.0, 50.0, 'approve', 'test', datetime('now'))",
                        (1000 + i, f"concurrent-test-{i}"),
                    )
                    conn.commit()
                    time.sleep(0.01)
                conn.close()
            except Exception as e:
                errors.append(f"writer: {e}")

        def reader():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.execute("PRAGMA busy_timeout=5000")
                conn.row_factory = sqlite3.Row
                for _ in range(20):
                    conn.execute(
                        "SELECT COUNT(*) FROM triage_decisions"
                    ).fetchone()
                    time.sleep(0.01)
                conn.close()
            except Exception as e:
                errors.append(f"reader: {e}")

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"Concurrent access errors: {errors}"

    def test_concurrent_writers_with_busy_timeout(self, file_db):
        """Two writers with busy_timeout should both succeed (serialized by WAL)."""
        db_path = file_db.db_path
        errors = []

        def writer(offset):
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                for i in range(10):
                    conn.execute(
                        "INSERT INTO triage_decisions "
                        "(idea_id, title, weighted_score, scaled_score, decision, reason, decided_at) "
                        "VALUES (?, ?, 5.0, 50.0, 'approve', 'test', datetime('now'))",
                        (2000 + offset + i, f"writer-{offset}-{i}"),
                    )
                    conn.commit()
                    time.sleep(0.005)
                conn.close()
            except Exception as e:
                errors.append(f"writer-{offset}: {e}")

        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(100,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"Concurrent writer errors: {errors}"

        # Verify all rows were written
        row = file_db.conn.execute(
            "SELECT COUNT(*) FROM triage_decisions WHERE idea_id >= 2000"
        ).fetchone()
        assert row[0] == 20, f"Expected 20 rows, got {row[0]}"
