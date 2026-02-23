"""
Tests for external database readers (IdeaForge, ST Factory, Ultra-Magnus).
"""
import pytest
import sqlite3
import json
import tempfile
from pathlib import Path

from readers import IdeaForgeReader, STFactoryReader, UMReader


# --- IdeaForge Reader Tests ---


@pytest.fixture
def ideaforge_test_db():
    """Create an in-memory IdeaForge database with test data."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # Create ideas table with IdeaForge schema
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
            status TEXT
        )
    """)

    # Insert test ideas with different scores
    test_ideas = [
        (1, "AI Task Scheduler", "AI-powered task scheduling", "Users struggle with scheduling",
         "Busy professionals", 8.5, 9.0, 8.0, 8.5, 9.0, 7.5, "tool", 15, "scored"),
        (2, "Code Review Bot", "Automated code review assistant", "Manual reviews are slow",
         "Software teams", 7.2, 7.5, 7.0, 7.2, 7.0, 7.0, "agent", 10, "scored"),
        (3, "Meeting Summarizer", "AI meeting summary generator", "Notes are incomplete",
         "Remote workers", 9.1, 9.5, 9.0, 8.5, 9.0, 8.0, "product", 20, "scored"),
        (4, "Unscored Idea", "This idea has no score", "Some problem",
         "Some audience", None, None, None, None, None, None, "tool", 5, "draft"),
        (5, "Draft Idea", "This idea is not scored yet", "Another problem",
         "Another audience", 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, "agent", 3, "draft"),
    ]

    for idea in test_ideas:
        cursor.execute("""
            INSERT INTO ideas (
                id, title, description, problem_statement, target_audience,
                weighted_score, opportunity_score, problem_score, feasibility_score,
                why_now_score, competition_score, artifact_type, signal_count, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, idea)

    conn.commit()

    # Save to a temporary file for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name

    # Copy in-memory DB to file
    file_conn = sqlite3.connect(temp_path)
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    yield temp_path

    # Cleanup
    Path(temp_path).unlink(missing_ok=True)


def test_ideaforge_reader_initialization(ideaforge_test_db):
    """Test IdeaForge reader initialization."""
    reader = IdeaForgeReader(ideaforge_test_db)
    assert reader.db_path == ideaforge_test_db
    assert reader.conn is not None
    reader.close()


def test_ideaforge_reader_missing_db():
    """Test IdeaForge reader raises FileNotFoundError for missing DB."""
    with pytest.raises(FileNotFoundError, match="IdeaForge database not found"):
        IdeaForgeReader("/nonexistent/path/to/db.db")


def test_ideaforge_get_unprocessed_ideas(ideaforge_test_db):
    """Test getting unprocessed ideas sorted by weighted_score DESC."""
    reader = IdeaForgeReader(ideaforge_test_db)

    ideas = reader.get_unprocessed_ideas()

    # Should return only scored ideas with non-null weighted_score
    assert len(ideas) == 3

    # Should be sorted by weighted_score DESC
    assert ideas[0]["weighted_score"] == 9.1  # Meeting Summarizer
    assert ideas[1]["weighted_score"] == 8.5  # AI Task Scheduler
    assert ideas[2]["weighted_score"] == 7.2  # Code Review Bot

    # Verify all required fields are present
    first_idea = ideas[0]
    required_fields = [
        "id", "title", "description", "problem_statement", "target_audience",
        "weighted_score", "opportunity_score", "problem_score", "feasibility_score",
        "why_now_score", "competition_score", "artifact_type", "signal_count", "status"
    ]
    for field in required_fields:
        assert field in first_idea

    # Verify specific values
    assert first_idea["title"] == "Meeting Summarizer"
    assert first_idea["artifact_type"] == "product"
    assert first_idea["signal_count"] == 20
    assert first_idea["status"] == "scored"

    reader.close()


def test_ideaforge_get_idea_by_id(ideaforge_test_db):
    """Test getting a specific idea by ID."""
    reader = IdeaForgeReader(ideaforge_test_db)

    # Get existing idea
    idea = reader.get_idea_by_id(2)
    assert idea is not None
    assert idea["title"] == "Code Review Bot"
    assert idea["weighted_score"] == 7.2

    # Get non-existent idea
    idea = reader.get_idea_by_id(999)
    assert idea is None

    reader.close()


# --- ST Factory Reader Tests ---


@pytest.fixture
def stfactory_test_db():
    """Create an in-memory ST Factory database with test data."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # Create persona_patches table
    cursor.execute("""
        CREATE TABLE persona_patches (
            id INTEGER PRIMARY KEY,
            patch_id TEXT NOT NULL,
            persona_id TEXT NOT NULL,
            rationale TEXT,
            from_version TEXT,
            to_version TEXT,
            raw_json TEXT,
            status TEXT
        )
    """)

    # Create improvement_recommendations table
    cursor.execute("""
        CREATE TABLE improvement_recommendations (
            id INTEGER PRIMARY KEY,
            recommendation TEXT,
            status TEXT
        )
    """)

    # Create outcome_records table
    cursor.execute("""
        CREATE TABLE outcome_records (
            id INTEGER PRIMARY KEY,
            event_type TEXT,
            emitted_at TEXT
        )
    """)

    # Insert test patches
    test_patches = [
        (1, "patch-001", "persona-alpha", "Improve error handling",
         "1.0", "1.1", json.dumps({"operation": "add", "field": "error_handler"}), "proposed"),
        (2, "patch-002", "persona-beta", "Add new feature",
         "2.0", "2.1", json.dumps({"operation": "update", "field": "feature_x"}), "proposed"),
        (3, "patch-003", "persona-gamma", "Already applied",
         "3.0", "3.1", json.dumps({"operation": "remove", "field": "deprecated"}), "applied"),
    ]

    for patch in test_patches:
        cursor.execute("""
            INSERT INTO persona_patches (
                id, patch_id, persona_id, rationale, from_version, to_version, raw_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, patch)

    # Insert test recommendations
    cursor.execute("""
        INSERT INTO improvement_recommendations (id, recommendation, status)
        VALUES (1, 'Optimize performance', 'pending'),
               (2, 'Refactor module X', 'pending'),
               (3, 'Already done', 'completed')
    """)

    # Insert test outcome records
    cursor.execute("""
        INSERT INTO outcome_records (id, event_type, emitted_at)
        VALUES (1, 'build_success', '2024-01-01T10:00:00'),
               (2, 'test_pass', '2024-01-01T11:00:00'),
               (3, 'deploy_complete', '2024-01-01T12:00:00')
    """)

    conn.commit()

    # Save to a temporary file for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name

    # Copy in-memory DB to file
    file_conn = sqlite3.connect(temp_path)
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    yield temp_path

    # Cleanup
    Path(temp_path).unlink(missing_ok=True)


def test_stfactory_reader_initialization(stfactory_test_db):
    """Test ST Factory reader initialization."""
    reader = STFactoryReader(stfactory_test_db)
    assert reader.db_path == stfactory_test_db
    assert reader.conn is not None
    reader.close()


def test_stfactory_reader_missing_db():
    """Test ST Factory reader raises FileNotFoundError for missing DB."""
    with pytest.raises(FileNotFoundError, match="ST Factory database not found"):
        STFactoryReader("/nonexistent/path/to/db.db")


def test_stfactory_get_proposed_patches(stfactory_test_db):
    """Test getting proposed patches."""
    reader = STFactoryReader(stfactory_test_db)

    patches = reader.get_proposed_patches()

    # Should return only proposed patches
    assert len(patches) == 2

    # Verify first patch
    patch = patches[0]
    assert patch["patch_id"] == "patch-001"
    assert patch["persona_id"] == "persona-alpha"
    assert patch["rationale"] == "Improve error handling"
    assert patch["from_version"] == "1.0"
    assert patch["to_version"] == "1.1"

    # Verify raw_json is parsed from JSON string to dict
    assert isinstance(patch["raw_json"], dict)
    assert patch["raw_json"]["operation"] == "add"
    assert patch["raw_json"]["field"] == "error_handler"

    reader.close()


def test_stfactory_get_pending_recommendations(stfactory_test_db):
    """Test getting pending recommendations."""
    reader = STFactoryReader(stfactory_test_db)

    recommendations = reader.get_pending_recommendations()

    # Should return only pending recommendations
    assert len(recommendations) == 2

    assert recommendations[0]["recommendation"] == "Optimize performance"
    assert recommendations[1]["recommendation"] == "Refactor module X"

    reader.close()


def test_stfactory_update_patch_status(stfactory_test_db):
    """Test updating patch status (write operation)."""
    reader = STFactoryReader(stfactory_test_db)

    # Update patch status
    reader.update_patch_status("patch-001", "applied")

    # Verify the status was updated
    # Need to open a new read connection to check
    verify_conn = sqlite3.connect(stfactory_test_db)
    verify_conn.row_factory = sqlite3.Row
    cursor = verify_conn.cursor()

    cursor.execute("SELECT status FROM persona_patches WHERE patch_id = ?", ("patch-001",))
    row = cursor.fetchone()
    assert row["status"] == "applied"

    verify_conn.close()
    reader.close()


def test_stfactory_get_outcome_records(stfactory_test_db):
    """Test getting outcome records."""
    reader = STFactoryReader(stfactory_test_db)

    records = reader.get_outcome_records(limit=2)

    # Should return most recent 2 records in DESC order
    assert len(records) == 2
    assert records[0]["event_type"] == "deploy_complete"  # Most recent
    assert records[1]["event_type"] == "test_pass"

    # Test with default limit
    all_records = reader.get_outcome_records()
    assert len(all_records) == 3

    reader.close()


# --- Ultra-Magnus Reader Tests ---


@pytest.fixture
def um_test_db():
    """Create an in-memory Ultra-Magnus database with test data."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # Create ideas table
    cursor.execute("""
        CREATE TABLE ideas (
            id TEXT PRIMARY KEY,
            title TEXT,
            current_stage TEXT,
            current_status TEXT
        )
    """)

    # Create build_results table
    cursor.execute("""
        CREATE TABLE build_results (
            id INTEGER PRIMARY KEY,
            idea_id TEXT,
            outcome TEXT,
            github_repo TEXT
        )
    """)

    # Create evaluation_results table
    cursor.execute("""
        CREATE TABLE evaluation_results (
            id INTEGER PRIMARY KEY,
            idea_id TEXT,
            overall_score REAL,
            recommendation TEXT
        )
    """)

    # Insert test ideas
    cursor.execute("""
        INSERT INTO ideas (id, title, current_stage, current_status)
        VALUES ('idea-001', 'AI Assistant', 'build', 'in_progress'),
               ('idea-002', 'Data Tool', 'evaluation', 'completed')
    """)

    # Insert test build results
    cursor.execute("""
        INSERT INTO build_results (id, idea_id, outcome, github_repo)
        VALUES (1, 'idea-001', 'success', 'org/ai-assistant'),
               (2, 'idea-002', 'success', 'org/data-tool')
    """)

    # Insert test evaluation results
    cursor.execute("""
        INSERT INTO evaluation_results (id, idea_id, overall_score, recommendation)
        VALUES (1, 'idea-002', 8.5, 'Approve for production')
    """)

    conn.commit()

    # Save to a temporary file for testing
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name

    # Copy in-memory DB to file
    file_conn = sqlite3.connect(temp_path)
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    yield temp_path

    # Cleanup
    Path(temp_path).unlink(missing_ok=True)


def test_um_reader_initialization(um_test_db):
    """Test Ultra-Magnus reader initialization."""
    reader = UMReader(um_test_db)
    assert reader.db_path == um_test_db
    assert reader.conn is not None
    reader.close()


def test_um_reader_missing_db():
    """Test Ultra-Magnus reader raises FileNotFoundError for missing DB."""
    with pytest.raises(FileNotFoundError, match="Ultra-Magnus database not found"):
        UMReader("/nonexistent/path/to/db.db")


def test_um_get_idea_pipeline_status(um_test_db):
    """Test getting idea pipeline status."""
    reader = UMReader(um_test_db)

    # Get existing idea
    idea = reader.get_idea_pipeline_status("idea-001")
    assert idea is not None
    assert idea["id"] == "idea-001"
    assert idea["title"] == "AI Assistant"
    assert idea["current_stage"] == "build"
    assert idea["current_status"] == "in_progress"

    # Get non-existent idea
    idea = reader.get_idea_pipeline_status("idea-999")
    assert idea is None

    reader.close()


def test_um_get_recent_builds(um_test_db):
    """Test getting recent builds."""
    reader = UMReader(um_test_db)

    builds = reader.get_recent_builds(limit=1)

    # Should return most recent build (DESC order by id)
    assert len(builds) == 1
    assert builds[0]["idea_id"] == "idea-002"
    assert builds[0]["outcome"] == "success"
    assert builds[0]["github_repo"] == "org/data-tool"
    assert builds[0]["title"] == "Data Tool"

    # Test with default limit
    all_builds = reader.get_recent_builds()
    assert len(all_builds) == 2

    reader.close()


def test_um_get_evaluation_result(um_test_db):
    """Test getting evaluation results."""
    reader = UMReader(um_test_db)

    # Get existing evaluation
    result = reader.get_evaluation_result("idea-002")
    assert result is not None
    assert result["idea_id"] == "idea-002"
    assert result["overall_score"] == 8.5
    assert result["recommendation"] == "Approve for production"

    # Get non-existent evaluation
    result = reader.get_evaluation_result("idea-999")
    assert result is None

    reader.close()


# --- Integration Tests ---


def test_all_readers_raise_filenotfound():
    """Test that all readers raise FileNotFoundError for missing databases."""
    nonexistent_path = "/this/path/definitely/does/not/exist/database.db"

    with pytest.raises(FileNotFoundError):
        IdeaForgeReader(nonexistent_path)

    with pytest.raises(FileNotFoundError):
        STFactoryReader(nonexistent_path)

    with pytest.raises(FileNotFoundError):
        UMReader(nonexistent_path)


def test_all_readers_close_properly(ideaforge_test_db, stfactory_test_db, um_test_db):
    """Test that all readers close connections properly."""
    # IdeaForge
    reader1 = IdeaForgeReader(ideaforge_test_db)
    assert reader1.conn is not None
    reader1.close()
    assert reader1.conn is None

    # ST Factory
    reader2 = STFactoryReader(stfactory_test_db)
    assert reader2.conn is not None
    reader2.close()
    assert reader2.conn is None

    # Ultra-Magnus
    reader3 = UMReader(um_test_db)
    assert reader3.conn is not None
    reader3.close()
    assert reader3.conn is None
