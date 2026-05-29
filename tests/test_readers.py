"""
Tests for external database readers (IdeaForge, ST Records, Ultra-Magnus).
"""
import pytest
import sqlite3
import json
import tempfile
from pathlib import Path

from readers import IdeaForgeReader, SkyLynxReader


# --- IdeaForge Reader Tests ---


@pytest.fixture
def ideaforge_test_db():
    """Create an in-memory IdeaForge database with test data.

    The fixture seeds the legacy three life_domain ideas (so the historical
    test_ideaforge_get_unprocessed_ideas assertion of 3 rows still holds)
    plus rubric-variation rows used by the R-A item 3 filter tests.
    """
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # Create ideas table with IdeaForge schema. R-A item 3 (2026-05-12)
    # adds the scoring_rubric column to match the live IdeaForge schema.
    cursor.execute("""
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            problem_statement TEXT,
            target_audience TEXT,
            struggling_user TEXT,
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
            strategic_theme TEXT,
            scoring_rubric TEXT DEFAULT 'tech'
        )
    """)

    # Legacy classified rows -- backfilled to scoring_rubric='life_domain'
    # so the existing get_unprocessed_ideas test (which expects 3 rows)
    # continues to pass. Rubric-variation rows (10-12) drive C4 tests.
    test_ideas = [
        (1, "AI Task Scheduler", "AI-powered task scheduling", "Users struggle with scheduling",
         "Busy professionals", 8.5, 9.0, 8.0, 8.5, 9.0, 7.5, "tool", 15, "classified", "life_domain"),
        (2, "Code Review Bot", "Automated code review assistant", "Manual reviews are slow",
         "Software teams", 7.2, 7.5, 7.0, 7.2, 7.0, 7.0, "agent", 10, "classified", "life_domain"),
        (3, "Meeting Summarizer", "AI meeting summary generator", "Notes are incomplete",
         "Remote workers", 9.1, 9.5, 9.0, 8.5, 9.0, 8.0, "product", 20, "classified", "life_domain"),
        (4, "Unscored Idea", "This idea has no score", "Some problem",
         "Some audience", None, None, None, None, None, None, "tool", 5, "draft", "life_domain"),
        (5, "Draft Idea", "This idea is not scored yet", "Another problem",
         "Another audience", 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, "agent", 3, "draft", "life_domain"),
        # R-A item 3 filter probes
        (10, "Tech Path Idea", "A tech-rubric idea", "Devs need a thing",
         "Engineers", 8.0, 8.0, 8.0, 8.0, 8.0, 8.0, "tool", 8, "classified", "tech"),
        (11, "Null Rubric Legacy", "Pre-rubric legacy row", "Old problem",
         "Old audience", 7.5, 7.5, 7.5, 7.5, 7.5, 7.5, "tool", 7, "classified", None),
        (12, "Life Dismissed", "A life_domain idea already dismissed", "Some problem",
         "Some audience", 8.2, 8.2, 8.2, 8.2, 8.2, 8.2, "agent", 12, "dismissed", "life_domain"),
    ]

    for idea in test_ideas:
        cursor.execute("""
            INSERT INTO ideas (
                id, title, description, problem_statement, target_audience,
                weighted_score, opportunity_score, problem_score, feasibility_score,
                why_now_score, competition_score, artifact_type, signal_count, status,
                scoring_rubric
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    # Should return only scored/classified ideas with non-null weighted_score
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
    assert first_idea["status"] == "classified"

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


# --- R-A item 3: scoring_rubric filter (life_domain allow-list) ---


def test_ideaforge_filters_to_life_domain_rubric(ideaforge_test_db):
    """get_unprocessed_ideas admits only scoring_rubric='life_domain' rows.

    Fixture seeds rows 10 (tech), 11 (NULL rubric), and 12 (life_domain but
    status='dismissed'). All must be excluded.
    """
    reader = IdeaForgeReader(ideaforge_test_db)
    ideas = reader.get_unprocessed_ideas()
    returned_ids = {row["id"] for row in ideas}

    # Legacy classified life_domain ids (1, 2, 3) admitted.
    assert 1 in returned_ids
    assert 2 in returned_ids
    assert 3 in returned_ids

    # Tech rubric row (10) MUST NOT slip through.
    assert 10 not in returned_ids, (
        "tech-rubric ideas must be filtered out (D1 archive 2026-05-11)"
    )

    # NULL-rubric legacy row (11) MUST NOT slip through.
    assert 11 not in returned_ids, (
        "NULL-rubric legacy rows pre-date the rubric concept and must not "
        "enter the build queue"
    )

    # Dismissed life_domain row (12) excluded by the status filter.
    assert 12 not in returned_ids

    reader.close()


def test_ideaforge_get_unprocessed_ideas_includes_scoring_rubric(ideaforge_test_db):
    """The reader's return contract must include scoring_rubric so the build
    gate can wire it into BuildJob.scoring_rubric.
    """
    reader = IdeaForgeReader(ideaforge_test_db)
    ideas = reader.get_unprocessed_ideas()

    assert len(ideas) >= 1
    for idea in ideas:
        assert "scoring_rubric" in idea, (
            "scoring_rubric must be present on every returned dict"
        )
        assert idea["scoring_rubric"] == "life_domain", (
            f"only life_domain rows may be returned; got rubric="
            f"{idea['scoring_rubric']} for id={idea['id']}"
        )

    reader.close()


def test_ideaforge_get_idea_by_id_includes_scoring_rubric(ideaforge_test_db):
    """get_idea_by_id must also return scoring_rubric.

    The build gate's stale-snapshot-refresh path (build.py: run_from_queue)
    pulls fresh fields via get_idea_by_id and merges them into the idea
    dict; without scoring_rubric in this return shape, the refresh path
    cannot restore the rubric for priority_queue snapshots that pre-dated
    the rubric column.
    """
    reader = IdeaForgeReader(ideaforge_test_db)

    # life_domain row
    idea = reader.get_idea_by_id(1)
    assert idea is not None
    assert idea["scoring_rubric"] == "life_domain"

    # tech row (get_idea_by_id has no status/rubric filter; returns whatever exists)
    idea = reader.get_idea_by_id(10)
    assert idea is not None
    assert idea["scoring_rubric"] == "tech"

    # NULL-rubric legacy row
    idea = reader.get_idea_by_id(11)
    assert idea is not None
    assert idea["scoring_rubric"] is None

    reader.close()


@pytest.fixture
def ideaforge_legacy_schema_db(tmp_path):
    """IdeaForge schema WITHOUT scoring_rubric column.

    Mirrors the upstream-DB state that exists before the IdeaForge schema
    migration lands. Used to assert the reader's PRAGMA fallback works.
    """
    db_path = tmp_path / "ideaforge_legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            problem_statement TEXT,
            target_audience TEXT,
            struggling_user TEXT,
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
    conn.execute("""
        INSERT INTO ideas (
            id, title, description, problem_statement, target_audience,
            weighted_score, opportunity_score, problem_score, feasibility_score,
            why_now_score, competition_score, artifact_type, signal_count, status
        ) VALUES (
            1, 'Legacy Idea', 'pre-rubric DB', 'p', 'aud',
            8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 'tool', 3, 'classified'
        )
    """)
    conn.commit()
    conn.close()
    return str(db_path)


def test_ideaforge_reader_falls_back_on_legacy_schema(ideaforge_legacy_schema_db):
    """R-A item 3 / Codex Round 1 HIGH: when the upstream DB lacks the
    scoring_rubric column, the reader must NOT raise OperationalError.
    It falls back to a legacy query shape and returns the row(s) without
    a scoring_rubric field.
    """
    reader = IdeaForgeReader(ideaforge_legacy_schema_db)

    # MUST NOT raise.
    ideas = reader.get_unprocessed_ideas()
    assert len(ideas) == 1
    assert ideas[0]["title"] == "Legacy Idea"
    # Legacy fallback: no scoring_rubric key in the returned dict.
    assert "scoring_rubric" not in ideas[0]

    # get_idea_by_id also falls back.
    idea = reader.get_idea_by_id(1)
    assert idea is not None
    assert "scoring_rubric" not in idea

    reader.close()


# --- Sky-Lynx Reader Tests ---


@pytest.fixture
def skylynx_test_db():
    """Create a test DB with the full improvement_recommendations schema."""
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    # Full schema matching production persona_metrics.db
    cursor.execute("""
        CREATE TABLE improvement_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT NOT NULL UNIQUE,
            session_id TEXT,
            recommendation_type TEXT NOT NULL,
            target_system TEXT DEFAULT 'persona',
            title TEXT NOT NULL,
            priority TEXT DEFAULT 'medium',
            scope TEXT,
            target_department TEXT,
            status TEXT DEFAULT 'pending',
            emitted_at TEXT NOT NULL,
            raw_json TEXT NOT NULL
        )
    """)

    # Also need persona_patches and outcome_records for STRecordsReader compat
    cursor.execute("""
        CREATE TABLE persona_patches (
            id INTEGER PRIMARY KEY, patch_id TEXT, persona_id TEXT,
            rationale TEXT, from_version TEXT, to_version TEXT, raw_json TEXT, status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE outcome_records (
            id INTEGER PRIMARY KEY, event_type TEXT, emitted_at TEXT
        )
    """)

    # Insert test recommendations with different priorities and target_systems
    recs = [
        ("sl-001", "sky-lynx-2026-02-08", "pipeline_change", "pipeline",
         "Session Tracking Fix", "high", "all_personas",
         json.dumps({
             "recommendation_id": "sl-001",
             "recommendation_type": "pipeline_change",
             "target_system": "pipeline",
             "title": "Session Tracking Fix",
             "description": "0 sessions with 3 completed ideas indicates measurement issue",
             "suggested_change": "Add session tracking to pipeline",
             "priority": "high",
             "status": "pending",
             "emitted_at": "2026-02-08T18:00:00",
         })),
        ("sl-002", "sky-lynx-2026-02-08", "claude_md_update", "claude_md",
         "Offline Workflow Documentation", "medium", "all_personas",
         json.dumps({
             "recommendation_id": "sl-002",
             "recommendation_type": "claude_md_update",
             "target_system": "claude_md",
             "title": "Offline Workflow Documentation",
             "description": "Ideas completing without recorded sessions",
             "suggested_change": "",
             "priority": "medium",
             "status": "pending",
             "emitted_at": "2026-02-08T18:01:00",
         })),
        ("sl-003", "sky-lynx-2026-02-08", "persona_update", "persona",
         "Persona Tune-up", "low", "department_a",
         json.dumps({
             "recommendation_id": "sl-003",
             "recommendation_type": "persona_update",
             "target_system": "persona",
             "title": "Persona Tune-up",
             "description": "Persona accuracy below threshold",
             "suggested_change": "",
             "priority": "low",
             "status": "pending",
             "emitted_at": "2026-02-08T18:02:00",
         })),
        ("sl-004", "sky-lynx-2026-02-01", "pipeline_change", "pipeline",
         "Already Dispatched", "high", "all_personas",
         json.dumps({
             "recommendation_id": "sl-004",
             "title": "Already Dispatched",
             "description": "This was already sent",
             "status": "dispatched",
             "emitted_at": "2026-02-01T10:00:00",
         })),
    ]

    for r in recs:
        cursor.execute("""
            INSERT INTO improvement_recommendations
            (recommendation_id, session_id, recommendation_type, target_system,
             title, priority, scope, status, emitted_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (r[0], r[1], r[2], r[3], r[4], r[5], r[6],
              "dispatched" if r[0] == "sl-004" else "pending",
              "2026-02-08T18:00:00", r[7]))

    conn.commit()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name

    file_conn = sqlite3.connect(temp_path)
    conn.backup(file_conn)
    file_conn.close()
    conn.close()

    yield temp_path

    Path(temp_path).unlink(missing_ok=True)


def test_skylynx_reader_initialization(skylynx_test_db):
    """Test Sky-Lynx reader initialization."""
    reader = SkyLynxReader(skylynx_test_db)
    assert reader.db_path == skylynx_test_db
    assert reader.conn is not None
    reader.close()


def test_skylynx_reader_missing_db():
    """Test Sky-Lynx reader raises FileNotFoundError for missing DB."""
    with pytest.raises(FileNotFoundError, match="ST Records database not found"):
        SkyLynxReader("/nonexistent/path/to/db.db")


def test_skylynx_get_pending_recommendations(skylynx_test_db):
    """Test getting pending recommendations filtered by target_system."""
    reader = SkyLynxReader(skylynx_test_db)

    recs = reader.get_pending_recommendations()

    # Should only return pending recs with target_system in ('pipeline', 'claude_md')
    # sl-001 (pipeline, pending) -- yes
    # sl-002 (claude_md, pending) -- yes
    # sl-003 (persona, pending) -- NO (filtered out by target_system)
    # sl-004 (pipeline, dispatched) -- NO (not pending)
    assert len(recs) == 2

    # Should be ordered by priority: high before medium
    assert recs[0]["recommendation_id"] == "sl-001"
    assert recs[0]["priority"] == "high"
    assert recs[1]["recommendation_id"] == "sl-002"
    assert recs[1]["priority"] == "medium"

    # raw_json should be parsed into a dict
    assert isinstance(recs[0]["raw_json"], dict)
    assert recs[0]["raw_json"]["description"] == "0 sessions with 3 completed ideas indicates measurement issue"

    reader.close()


def test_skylynx_priority_to_score():
    """Test priority label to numeric score mapping."""
    # Use a dummy path trick: we test the static method via a temp DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = f.name
    # Create a minimal valid DB
    conn = sqlite3.connect(temp_path)
    conn.execute("CREATE TABLE improvement_recommendations (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE persona_patches (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE outcome_records (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    try:
        reader = SkyLynxReader(temp_path)
        assert reader.priority_to_score("critical") == 95.0
        assert reader.priority_to_score("high") == 85.0
        assert reader.priority_to_score("medium") == 70.0
        assert reader.priority_to_score("low") == 50.0
        assert reader.priority_to_score("unknown") == 60.0  # default fallback
        reader.close()
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_skylynx_recommendation_to_idea(skylynx_test_db):
    """Test converting a recommendation to an idea dict for spec generation."""
    reader = SkyLynxReader(skylynx_test_db)
    recs = reader.get_pending_recommendations()
    rec = recs[0]  # sl-001, pipeline_change, high

    idea = reader.recommendation_to_idea(rec)

    # Required fields for SpecGenerator
    assert idea["id"] == "sl-001"
    assert idea["title"] == "Session Tracking Fix"
    assert "measurement issue" in idea["description"]
    assert "Add session tracking" in idea["description"]  # suggested_change appended
    assert idea["problem_statement"] == "0 sessions with 3 completed ideas indicates measurement issue"
    assert "ST Metro ecosystem" in idea["target_audience"]
    # R-A 1.7 (2026-05-12): every Sky-Lynx idea is stamped as a CCOS agent
    # under the life_domain rubric so the post-pivot dequeue guard admits it.
    assert idea["artifact_type"] == "ccos_agent"
    assert idea["scoring_rubric"] == "life_domain"
    assert idea["_source"] == "skylynx"
    assert idea["_recommendation_type"] == "pipeline_change"

    # claude_md_update -> same CCOS agent stamp (R-A 1.7 unifies the
    # recommendation_type → artifact_type mapping under the pivot)
    rec2 = recs[1]  # sl-002, claude_md_update
    idea2 = reader.recommendation_to_idea(rec2)
    assert idea2["artifact_type"] == "ccos_agent"
    assert idea2["scoring_rubric"] == "life_domain"
    assert idea2["_recommendation_type"] == "claude_md_update"

    reader.close()


def test_skylynx_mark_dispatched(skylynx_test_db):
    """Test marking a recommendation as dispatched."""
    reader = SkyLynxReader(skylynx_test_db)

    # Verify sl-001 is pending
    recs = reader.get_pending_recommendations()
    assert any(r["recommendation_id"] == "sl-001" for r in recs)

    # Mark dispatched
    reader.mark_dispatched("sl-001")

    # Re-read: sl-001 should no longer appear as pending
    # Need to close and reopen to get fresh read-only view
    reader.close()
    reader = SkyLynxReader(skylynx_test_db)
    recs = reader.get_pending_recommendations()
    assert not any(r["recommendation_id"] == "sl-001" for r in recs)

    # Verify status in DB
    verify_conn = sqlite3.connect(skylynx_test_db)
    verify_conn.row_factory = sqlite3.Row
    cursor = verify_conn.cursor()
    cursor.execute(
        "SELECT status FROM improvement_recommendations WHERE recommendation_id = ?",
        ("sl-001",)
    )
    row = cursor.fetchone()
    assert row["status"] == "dispatched"
    verify_conn.close()

    reader.close()


# --- Integration Tests ---


def test_all_readers_raise_filenotfound():
    """Test that all readers raise FileNotFoundError for missing databases."""
    nonexistent_path = "/this/path/definitely/does/not/exist/database.db"

    with pytest.raises(FileNotFoundError):
        IdeaForgeReader(nonexistent_path)

    with pytest.raises(FileNotFoundError):
        SkyLynxReader(nonexistent_path)

def test_all_readers_close_properly(ideaforge_test_db, skylynx_test_db):
    """Test that all readers close connections properly."""
    # IdeaForge
    reader1 = IdeaForgeReader(ideaforge_test_db)
    assert reader1.conn is not None
    reader1.close()
    assert reader1.conn is None

    # Sky-Lynx
    reader_sl = SkyLynxReader(skylynx_test_db)
    assert reader_sl.conn is not None
    reader_sl.close()
    assert reader_sl.conn is None
