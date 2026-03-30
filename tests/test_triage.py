"""
Tests for Triage Gate (Gate 1) - Score & Decision Logic.
"""
import pytest
import sqlite3
import tempfile
import json
from pathlib import Path
from datetime import datetime

from config import Config
from db import StateDB
from audit import AuditLogger
from readers.ideaforge_reader import IdeaForgeReader
from gates.triage import TriageGate
from models import TriageDecision


# --- Fixtures ---


@pytest.fixture
def triage_config():
    """Provide test configuration with pinned thresholds."""
    config = Config()
    config.approve_threshold = 70
    config.reject_threshold = 40
    config.max_approve_per_cycle = 3
    return config


@pytest.fixture
def in_memory_state_db():
    """Provide in-memory state database."""
    db = StateDB(":memory:")
    db.init_db()
    yield db
    db.close()


@pytest.fixture
def temp_audit_log():
    """Provide temporary audit log file."""
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="w") as f:
        log_path = f.name

    yield log_path

    # Cleanup
    Path(log_path).unlink(missing_ok=True)


@pytest.fixture
def create_ideaforge_db():
    """Factory fixture to create IdeaForge test databases with custom ideas."""
    def _create_db(ideas_data):
        """
        Create IdeaForge test database with specified ideas.

        Args:
            ideas_data: List of tuples (id, title, weighted_score, status)
        """
        conn = sqlite3.connect(":memory:")
        cursor = conn.cursor()

        # Create ideas table
        cursor.execute("""
            CREATE TABLE ideas (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                problem_statement TEXT,
                target_audience TEXT,
                weighted_score REAL,
                opportunity_score REAL,
                problem_score REAL,
                feasibility_score REAL,
                why_now_score REAL,
                competition_score REAL,
                artifact_type TEXT,
                signal_count INTEGER,
                status TEXT,
                claimed_by TEXT,
                claimed_at TEXT,
                strategic_theme TEXT
            )
        """)

        # Insert ideas
        for idea_data in ideas_data:
            idea_id, title, weighted_score, status = idea_data
            cursor.execute("""
                INSERT INTO ideas (
                    id, title, description, problem_statement, target_audience,
                    weighted_score, opportunity_score, problem_score, feasibility_score,
                    why_now_score, competition_score, artifact_type, signal_count, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                idea_id, title, "Test description", "Test problem", "Test audience",
                weighted_score, weighted_score, weighted_score, weighted_score,
                weighted_score, weighted_score, "tool", 10, status
            ))

        conn.commit()

        # Save to temporary file
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_path = f.name

        file_conn = sqlite3.connect(temp_path)
        conn.backup(file_conn)
        file_conn.close()
        conn.close()

        return temp_path

    yield _create_db

    # Cleanup handled by individual tests


# --- Test Cases ---


def test_triage_gate_basic_decision_logic(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test 1: Feed 5 ideas with scores [9.0, 7.5, 5.0, 3.5, 2.0]
    Expected: [approve, approve, defer, reject, reject] with default thresholds (70/40)
    """
    # Create IdeaForge DB with test ideas
    ideas_data = [
        (1, "High Score Idea", 9.0, "classified"),     # 90 -> approve
        (2, "Good Score Idea", 7.5, "classified"),     # 75 -> approve
        (3, "Medium Score Idea", 5.0, "classified"),   # 50 -> defer
        (4, "Low Score Idea", 3.5, "classified"),      # 35 -> reject
        (5, "Very Low Score Idea", 2.0, "classified"), # 20 -> reject
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Run triage
        decisions = triage_gate.run(dry_run=False)

        # Verify results
        assert len(decisions) == 5

        # Check decisions in order (ideas sorted by weighted_score DESC)
        assert decisions[0].idea_id == 1
        assert decisions[0].scaled_score == 90.0
        assert decisions[0].decision == "approve"
        assert decisions[0].reason == "meets approval threshold"

        assert decisions[1].idea_id == 2
        assert decisions[1].scaled_score == 75.0
        assert decisions[1].decision == "approve"
        assert decisions[1].reason == "meets approval threshold"

        assert decisions[2].idea_id == 3
        assert decisions[2].scaled_score == 50.0
        assert decisions[2].decision == "defer"
        assert decisions[2].reason == "in deferral range"

        assert decisions[3].idea_id == 4
        assert decisions[3].scaled_score == 35.0
        assert decisions[3].decision == "reject"
        assert decisions[3].reason == "below rejection threshold"

        assert decisions[4].idea_id == 5
        assert decisions[4].scaled_score == 20.0
        assert decisions[4].decision == "reject"
        assert decisions[4].reason == "below rejection threshold"

        # Verify all decisions have proper fields
        for decision in decisions:
            assert isinstance(decision, TriageDecision)
            assert decision.title is not None
            assert decision.weighted_score > 0
            assert decision.decided_at is not None

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_per_cycle_cap(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test 2: Feed 5 ideas all scoring 8.0
    Expected: first 3 approved, remaining 2 deferred (per-cycle cap of 3)
    """
    # Create IdeaForge DB with 5 ideas all scoring 8.0 (scaled = 80)
    ideas_data = [
        (1, "Idea 1", 8.0, "classified"),
        (2, "Idea 2", 8.0, "classified"),
        (3, "Idea 3", 8.0, "classified"),
        (4, "Idea 4", 8.0, "classified"),
        (5, "Idea 5", 8.0, "classified"),
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Run triage
        decisions = triage_gate.run(dry_run=False)

        # Verify results
        assert len(decisions) == 5

        # First 3 should be approved
        for i in range(3):
            assert decisions[i].decision == "approve"
            assert decisions[i].reason == "meets approval threshold"
            assert decisions[i].scaled_score == 80.0

        # Last 2 should be deferred due to cap
        for i in range(3, 5):
            assert decisions[i].decision == "defer"
            assert decisions[i].reason == "per-cycle cap reached"
            assert decisions[i].scaled_score == 80.0

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_empty_ideas(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test 3: Feed 0 ideas
    Expected: empty list, no errors
    """
    # Create IdeaForge DB with no scored ideas
    ideas_data = []
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Run triage
        decisions = triage_gate.run(dry_run=False)

        # Verify results
        assert len(decisions) == 0
        assert isinstance(decisions, list)

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_dry_run_no_db_write(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db, capsys
):
    """
    Test 4: Verify dry_run=True does NOT write to state_db
    """
    # Create IdeaForge DB with test ideas
    ideas_data = [
        (1, "Test Idea 1", 8.0, "classified"),
        (2, "Test Idea 2", 3.0, "classified"),
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Run triage in dry_run mode
        decisions = triage_gate.run(dry_run=True)

        # Verify decisions were returned
        assert len(decisions) == 2

        # Verify stdout output was printed
        captured = capsys.readouterr()
        assert "[APPROVE]" in captured.out
        assert "[REJECT]" in captured.out
        assert "Test Idea 1" in captured.out
        assert "Test Idea 2" in captured.out

        # Verify state_db is empty (no records written)
        in_memory_state_db.connect()
        cursor = in_memory_state_db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM triage_decisions")
        count = cursor.fetchone()[0]
        assert count == 0, "dry_run should NOT write to state_db"

        # Verify audit log is empty (no logs written)
        with open(temp_audit_log, "r") as f:
            log_content = f.read()
        assert log_content == "", "dry_run should NOT write to audit log"

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_non_dry_run_writes_to_db(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test 5: Verify dry_run=False DOES write to state_db and audit log
    """
    # Create IdeaForge DB with test ideas
    ideas_data = [
        (1, "Test Idea 1", 8.0, "classified"),
        (2, "Test Idea 2", 3.0, "classified"),
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Run triage (not dry_run)
        decisions = triage_gate.run(dry_run=False)

        # Verify decisions were returned
        assert len(decisions) == 2

        # Verify state_db has records
        in_memory_state_db.connect()
        cursor = in_memory_state_db.conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM triage_decisions")
        count = cursor.fetchone()[0]
        assert count == 2, "non-dry_run should write to state_db"

        # Verify specific records
        cursor.execute("""
            SELECT idea_id, title, weighted_score, scaled_score, decision, reason
            FROM triage_decisions
            ORDER BY idea_id
        """)
        rows = cursor.fetchall()

        # Check first record
        assert rows[0][0] == 1  # idea_id
        assert rows[0][1] == "Test Idea 1"  # title
        assert rows[0][2] == 8.0  # weighted_score
        assert rows[0][3] == 80.0  # scaled_score
        assert rows[0][4] == "approve"  # decision
        assert rows[0][5] == "meets approval threshold"  # reason

        # Check second record
        assert rows[1][0] == 2  # idea_id
        assert rows[1][1] == "Test Idea 2"  # title
        assert rows[1][2] == 3.0  # weighted_score
        assert rows[1][3] == 30.0  # scaled_score
        assert rows[1][4] == "reject"  # decision
        assert rows[1][5] == "below rejection threshold"  # reason

        # Verify audit log has records
        with open(temp_audit_log, "r") as f:
            log_lines = f.readlines()

        assert len(log_lines) == 2, "should have 2 audit log entries"

        # Parse and verify first log entry
        log1 = json.loads(log_lines[0])
        assert log1["gate"] == "triage"
        assert log1["action"] == "approve"
        assert log1["details"]["idea_id"] == 1
        assert log1["details"]["title"] == "Test Idea 1"
        assert log1["details"]["weighted_score"] == 8.0
        assert log1["details"]["scaled_score"] == 80.0

        # Parse and verify second log entry
        log2 = json.loads(log_lines[1])
        assert log2["gate"] == "triage"
        assert log2["action"] == "reject"
        assert log2["details"]["idea_id"] == 2
        assert log2["details"]["title"] == "Test Idea 2"

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_score_scaling(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test that scores are correctly scaled from 0-10 to 0-100.
    """
    # Create IdeaForge DB with various scores
    ideas_data = [
        (1, "Score 10.0", 10.0, "classified"),  # -> 100.0
        (2, "Score 5.0", 5.0, "classified"),     # -> 50.0
        (3, "Score 0.0", 0.0, "classified"),     # -> 0.0
        (4, "Score 7.25", 7.25, "classified"),   # -> 72.5
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Run triage
        decisions = triage_gate.run(dry_run=False)

        # Verify score scaling
        assert decisions[0].weighted_score == 10.0
        assert decisions[0].scaled_score == 100.0

        assert decisions[1].weighted_score == 7.25
        assert decisions[1].scaled_score == 72.5

        assert decisions[2].weighted_score == 5.0
        assert decisions[2].scaled_score == 50.0

        assert decisions[3].weighted_score == 0.0
        assert decisions[3].scaled_score == 0.0

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_format_decision(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test _format_decision method output format.
    """
    # Create minimal IdeaForge DB
    ideas_data = [(1, "Test Idea", 8.0, "classified")]
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Test format_decision
        idea = {"id": 42, "title": "My Great Idea"}
        formatted = triage_gate._format_decision(idea, 85.5, "approve")

        # Verify format
        assert "[APPROVE]" in formatted
        assert "ID=42" in formatted
        assert "Score=85.5" in formatted
        assert "My Great Idea" in formatted

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_boundary_conditions(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test boundary conditions at exact threshold values.
    """
    # Create IdeaForge DB with boundary scores
    # approve_threshold=70, reject_threshold=40
    ideas_data = [
        (1, "Exactly 70", 7.0, "classified"),   # 70.0 -> approve (>=)
        (2, "Just below 70", 6.9, "classified"), # 69.0 -> defer
        (3, "Exactly 40", 4.0, "classified"),   # 40.0 -> defer (not <)
        (4, "Just below 40", 3.9, "classified"), # 39.0 -> reject (<)
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        # Create components
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        # Create TriageGate
        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        # Run triage
        decisions = triage_gate.run(dry_run=False)

        # Verify boundary decisions
        # Sorted by score DESC: 7.0, 6.9, 4.0, 3.9
        assert decisions[0].scaled_score == 70.0
        assert decisions[0].decision == "approve"

        assert decisions[1].scaled_score == 69.0
        assert decisions[1].decision == "defer"

        assert decisions[2].scaled_score == 40.0
        assert decisions[2].decision == "defer"

        assert decisions[3].scaled_score == 39.0
        assert decisions[3].decision == "reject"

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_enqueues_approved_into_priority_queue(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test: Approved ideas are inserted into the priority queue.
    Rejected/deferred ideas are NOT inserted.
    """
    ideas_data = [
        (1, "High Score Idea", 9.0, "classified"),     # 90 -> approve
        (2, "Medium Score Idea", 5.0, "classified"),   # 50 -> defer
        (3, "Low Score Idea", 2.0, "classified"),      # 20 -> reject
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        decisions = triage_gate.run(dry_run=False)

        # Verify 3 decisions made
        assert len(decisions) == 3
        assert decisions[0].decision == "approve"
        assert decisions[1].decision == "defer"
        assert decisions[2].decision == "reject"

        # Verify ONLY the approved idea was enqueued
        in_memory_state_db.connect()
        cursor = in_memory_state_db.conn.cursor()
        cursor.execute("SELECT * FROM priority_queue")
        rows = cursor.fetchall()

        assert len(rows) == 1, f"Expected 1 queued item but found {len(rows)}"
        assert rows[0]["source"] == "ideaforge"
        assert rows[0]["source_id"] == "1"  # The approved idea
        assert rows[0]["title"] == "High Score Idea"
        assert rows[0]["status"] == "pending"
        assert rows[0]["priority_score"] == 90.0 * triage_config.ideaforge_weight

        # Verify idea_data is valid JSON with the full idea
        idea_data = json.loads(rows[0]["idea_data"])
        assert idea_data["id"] == 1
        assert idea_data["title"] == "High Score Idea"

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_priority_score_uses_weight(
    in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test: Priority score = scaled_score * ideaforge_weight.
    """
    config = Config()
    config.ideaforge_weight = 1.5

    ideas_data = [
        (1, "Test Idea", 8.0, "classified"),  # scaled=80, priority=80*1.5=120
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        triage_gate = TriageGate(
            config=config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        triage_gate.run(dry_run=False)

        in_memory_state_db.connect()
        cursor = in_memory_state_db.conn.cursor()
        cursor.execute("SELECT priority_score FROM priority_queue")
        row = cursor.fetchone()

        assert row is not None
        assert row["priority_score"] == pytest.approx(120.0)

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)


def test_triage_gate_dry_run_does_not_enqueue(
    triage_config, in_memory_state_db, temp_audit_log, create_ideaforge_db
):
    """
    Test: dry_run=True does NOT insert into priority queue.
    """
    ideas_data = [
        (1, "Test Idea", 9.0, "classified"),  # Would normally be approved
    ]
    db_path = create_ideaforge_db(ideas_data)

    try:
        ideaforge_reader = IdeaForgeReader(db_path)
        audit_logger = AuditLogger(temp_audit_log)

        triage_gate = TriageGate(
            config=triage_config,
            state_db=in_memory_state_db,
            ideaforge_reader=ideaforge_reader,
            audit_logger=audit_logger
        )

        decisions = triage_gate.run(dry_run=True)
        assert len(decisions) == 1
        assert decisions[0].decision == "approve"

        # Verify priority queue is empty
        in_memory_state_db.connect()
        cursor = in_memory_state_db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM priority_queue")
        assert cursor.fetchone()[0] == 0, "dry_run should NOT enqueue"

        ideaforge_reader.close()

    finally:
        Path(db_path).unlink(missing_ok=True)
