"""
Tests for Pipeline Funnel Dashboard (Phase D1).
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from dashboard import compute_funnel_metrics, format_funnel_output, _safe_rate


@pytest.fixture
def ideaforge_db(tmp_path):
    """Create a temporary IdeaForge DB with schema and synthetic data."""
    db_path = str(tmp_path / "ideaforge.db")
    conn = sqlite3.connect(db_path)

    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT,
            subreddit TEXT,
            post_id TEXT,
            title TEXT,
            selftext TEXT,
            author TEXT,
            url TEXT,
            score INTEGER,
            num_comments INTEGER,
            created_utc TEXT,
            signal_type TEXT,
            matched_keywords TEXT,
            harvested_at TEXT,
            processed INTEGER DEFAULT 0,
            batch_id TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            problem_statement TEXT,
            target_audience TEXT,
            source_signals TEXT,
            source_subreddits TEXT,
            signal_count INTEGER,
            opportunity_score REAL,
            problem_score REAL,
            feasibility_score REAL,
            why_now_score REAL,
            competition_score REAL,
            weighted_score REAL,
            score_rationale TEXT,
            artifact_type TEXT,
            route_rationale TEXT,
            route_confidence REAL,
            struggling_user TEXT,
            classified_at TEXT,
            status TEXT,
            synthesized_at TEXT,
            scored_at TEXT,
            exported_at TEXT,
            ultra_magnus_id TEXT,
            claimed_by TEXT,
            claimed_at TEXT,
            factory_fit_score REAL,
            build_outcome TEXT
        )
    """)

    # Insert synthetic data — last 3 days
    now = datetime.now()
    for i in range(20):
        day = now - timedelta(days=i % 3)
        conn.execute(
            "INSERT INTO signals (signal_id, title, harvested_at) VALUES (?, ?, ?)",
            (f"sig-{i}", f"Signal {i}", day.isoformat()),
        )

    for i in range(10):
        day = now - timedelta(days=i % 3)
        status = "classified" if i < 7 else "dismissed"
        conn.execute(
            "INSERT INTO ideas (title, synthesized_at, scored_at, classified_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"Idea {i}", day.isoformat(), day.isoformat(), day.isoformat(), status),
        )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def metroplex_db(tmp_path):
    """Create a temporary Metroplex DB with schema and synthetic data."""
    db_path = str(tmp_path / "metroplex.db")

    from db import StateDB
    state_db = StateDB(db_path)
    state_db.init_db()

    now = datetime.now()

    # Insert triage decisions
    for i in range(5):
        day = now - timedelta(days=i % 3)
        state_db.conn.execute(
            "INSERT INTO triage_decisions (idea_id, title, weighted_score, scaled_score, decision, reason, decided_at) "
            "VALUES (?, ?, ?, ?, 'approve', 'good idea', ?)",
            (i, f"Idea {i}", 7.5, 75.0, day.isoformat()),
        )

    # Insert build jobs
    for i in range(4):
        day = now - timedelta(days=i % 3)
        status = "completed" if i < 3 else "failed"
        state_db.conn.execute(
            "INSERT INTO build_jobs (idea_id, title, spec_path, queue_job_id, status, queued_at) "
            "VALUES (?, ?, '/tmp/spec', ?, ?, ?)",
            (i, f"Idea {i}", f"metroplex-ideaforge-{i}", status, day.isoformat()),
        )

    # Insert publish jobs
    for i in range(2):
        state_db.conn.execute(
            "INSERT INTO publish_jobs (build_job_id, title, repo_name, repo_url, status, project_dir, created_at, published_at) "
            "VALUES (?, ?, ?, ?, 'published', '/tmp', ?, ?)",
            (f"metroplex-ideaforge-{i}", f"Idea {i}", f"repo-{i}",
             f"https://github.com/org/repo-{i}", now.isoformat(), now.isoformat()),
        )

    state_db.conn.commit()
    state_db.close()
    return db_path


class TestFunnelStages:
    """Test funnel stage count computation."""

    def test_signals_ingested(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["signals_ingested"] == 20

    def test_ideas_created(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["ideas_created"] == 10

    def test_ideas_scored(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["ideas_scored"] == 10

    def test_ideas_classified(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["ideas_classified"] == 10

    def test_ideas_dismissed(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["ideas_dismissed"] == 3

    def test_ideas_approved(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["ideas_approved"] == 5

    def test_builds_dispatched(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["builds_dispatched"] == 4

    def test_builds_succeeded(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["builds_succeeded"] == 3

    def test_builds_failed(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["builds_failed"] == 1

    def test_builds_published(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        assert metrics["funnel"]["builds_published"] == 2


class TestConversionRates:
    """Test conversion rate computation."""

    def test_rates_computed(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        cr = metrics["conversion_rates"]
        # 10 ideas / 20 signals = 0.5
        assert cr["signals_to_ideas"] == 0.5
        # 10/10 = 1.0
        assert cr["ideas_to_scored"] == 1.0
        # 10/10 = 1.0
        assert cr["scored_to_classified"] == 1.0

    def test_division_by_zero_returns_none(self):
        assert _safe_rate(5, 0) is None
        assert _safe_rate(None, 10) is None
        assert _safe_rate(5, None) is None


class TestPerSource:
    """Test per-source breakdown."""

    def test_source_breakdown(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        # All builds are ideaforge source
        assert len(metrics["per_source"]) >= 1
        ideaforge_src = [s for s in metrics["per_source"] if s["source"] == "ideaforge"]
        assert len(ideaforge_src) == 1
        assert ideaforge_src[0]["builds"] == 4
        assert ideaforge_src[0]["publishes"] == 2


class TestAnomalyDetection:
    """Test anomaly detection on daily conversion rates."""

    def test_anomaly_flagged_on_zero_day(self, tmp_path):
        """Inject a day with 0 signals and positive ideas in surrounding days."""
        ig_path = str(tmp_path / "ig.db")
        mx_path = str(tmp_path / "mx.db")

        # Build IdeaForge DB
        conn = sqlite3.connect(ig_path)
        conn.execute("""
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT, subreddit TEXT, post_id TEXT, title TEXT,
                selftext TEXT, author TEXT, url TEXT, score INTEGER,
                num_comments INTEGER, created_utc TEXT, signal_type TEXT,
                matched_keywords TEXT, harvested_at TEXT, processed INTEGER DEFAULT 0,
                batch_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, description TEXT, problem_statement TEXT,
                target_audience TEXT, source_signals TEXT, source_subreddits TEXT,
                signal_count INTEGER, opportunity_score REAL, problem_score REAL,
                feasibility_score REAL, why_now_score REAL, competition_score REAL,
                weighted_score REAL, score_rationale TEXT, artifact_type TEXT,
                route_rationale TEXT, route_confidence REAL, struggling_user TEXT,
                classified_at TEXT, status TEXT, synthesized_at TEXT, scored_at TEXT,
                exported_at TEXT, ultra_magnus_id TEXT, claimed_by TEXT,
                claimed_at TEXT, factory_fit_score REAL, build_outcome TEXT
            )
        """)

        now = datetime.now()
        # Day 0 (today): 10 signals, 5 ideas
        for i in range(10):
            conn.execute(
                "INSERT INTO signals (signal_id, title, harvested_at) VALUES (?, ?, ?)",
                (f"s-0-{i}", "Sig", now.isoformat()),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO ideas (title, synthesized_at, scored_at, classified_at, status) "
                "VALUES (?, ?, ?, ?, 'classified')",
                ("Idea", now.isoformat(), now.isoformat(), now.isoformat()),
            )

        # Day 2: 10 signals, 5 ideas
        day2 = now - timedelta(days=2)
        for i in range(10):
            conn.execute(
                "INSERT INTO signals (signal_id, title, harvested_at) VALUES (?, ?, ?)",
                (f"s-2-{i}", "Sig", day2.isoformat()),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO ideas (title, synthesized_at, scored_at, classified_at, status) "
                "VALUES (?, ?, ?, ?, 'classified')",
                ("Idea", day2.isoformat(), day2.isoformat(), day2.isoformat()),
            )

        # Day 1: 10 signals, 0 ideas (anomaly: 0% conversion vs 50% mean)
        day1 = now - timedelta(days=1)
        for i in range(10):
            conn.execute(
                "INSERT INTO signals (signal_id, title, harvested_at) VALUES (?, ?, ?)",
                (f"s-1-{i}", "Sig", day1.isoformat()),
            )
        # No ideas for day 1

        conn.commit()
        conn.close()

        # Build minimal Metroplex DB
        from db import StateDB
        state_db = StateDB(mx_path)
        state_db.init_db()
        state_db.close()

        metrics = compute_funnel_metrics(mx_path, ig_path, days=7)

        # Day 1 has 0/10 = 0% conversion, mean is ~33%. 0 < 33%*0.5 = ~16.7%
        # So day 1 should be flagged
        assert len(metrics["anomalies"]) >= 1
        assert any("signals_to_ideas" in a for a in metrics["anomalies"])


class TestMissingTables:
    """Test graceful handling of missing tables."""

    def test_missing_source_metrics(self, ideaforge_db, metroplex_db):
        """source_metrics doesn't exist — should not crash."""
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        # Should complete without error
        assert "funnel" in metrics

    def test_empty_dbs(self, tmp_path):
        """Completely empty DBs — should not crash."""
        ig_path = str(tmp_path / "empty_ig.db")
        mx_path = str(tmp_path / "empty_mx.db")

        conn = sqlite3.connect(ig_path)
        conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, harvested_at TEXT)")
        conn.execute("""CREATE TABLE ideas (id INTEGER PRIMARY KEY,
            synthesized_at TEXT, scored_at TEXT, classified_at TEXT, status TEXT)""")
        conn.commit()
        conn.close()

        from db import StateDB
        state_db = StateDB(mx_path)
        state_db.init_db()
        state_db.close()

        metrics = compute_funnel_metrics(mx_path, ig_path, days=7)
        assert metrics["funnel"].get("signals_ingested", 0) == 0


class TestFormatOutput:
    """Test output formatting."""

    def test_text_output(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        output = format_funnel_output(metrics)
        assert "PIPELINE FUNNEL DASHBOARD" in output
        assert "Funnel Stages:" in output
        assert "Conversion Rates:" in output

    def test_json_output(self, ideaforge_db, metroplex_db):
        import json
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=7)
        output = format_funnel_output(metrics, as_json=True)
        parsed = json.loads(output)
        assert "funnel" in parsed
        assert "conversion_rates" in parsed


class TestDaysCap:
    """Test days parameter capping."""

    def test_days_capped_at_90(self, ideaforge_db, metroplex_db):
        metrics = compute_funnel_metrics(metroplex_db, ideaforge_db, days=365)
        assert metrics["days"] == 90
