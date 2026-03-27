"""
Pipeline Funnel Dashboard — Phase D1
Computes funnel conversion rates across IdeaForge → Metroplex pipeline stages.
Read-only access to both databases.

The Loop: dashboard exposes funnel conversion rates → degradations visible →
other loops react → dashboard reflects improvement.
"""
import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Max days to prevent unbounded queries
MAX_DAYS = 90


def _readonly_connect(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection with row factory."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists in the database."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0


def _safe_rate(numerator: int | None, denominator: int | None) -> float | None:
    """Compute rate, returning None on division by zero or None inputs."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(numerator / denominator, 4)


def compute_funnel_metrics(
    metroplex_db_path: str,
    ideaforge_db_path: str,
    days: int = 7,
) -> dict:
    """Compute full pipeline funnel metrics over the last N days.

    Opens both DBs read-only. Never writes to either database.

    Args:
        metroplex_db_path: Path to metroplex.db
        ideaforge_db_path: Path to ideaforge.db
        days: Lookback window (capped at 90)

    Returns:
        Dict with keys: funnel, conversion_rates, per_source, per_day,
        anomalies, subsystem_summaries
    """
    days = min(days, MAX_DAYS)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    result = {
        "days": days,
        "cutoff": cutoff,
        "funnel": {},
        "conversion_rates": {},
        "per_source": [],
        "per_day": [],
        "anomalies": [],
        "subsystem_summaries": {},
    }

    # --- IdeaForge metrics ---
    ig_conn = None
    try:
        ig_conn = _readonly_connect(ideaforge_db_path)

        # Signals ingested
        result["funnel"]["signals_ingested"] = ig_conn.execute(
            "SELECT COUNT(*) FROM signals WHERE harvested_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        # Ideas created
        result["funnel"]["ideas_created"] = ig_conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE synthesized_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        # Ideas scored
        result["funnel"]["ideas_scored"] = ig_conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE scored_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        # Ideas classified
        result["funnel"]["ideas_classified"] = ig_conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE classified_at >= ? "
            "AND status IN ('classified','exported','dismissed')",
            (cutoff,),
        ).fetchone()[0]

        # Ideas dismissed
        result["funnel"]["ideas_dismissed"] = ig_conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE status = 'dismissed' AND classified_at >= ?",
            (cutoff,),
        ).fetchone()[0]

        # Per-day breakdown from IdeaForge
        ig_daily = {}
        rows = ig_conn.execute(
            "SELECT DATE(harvested_at) as day, COUNT(*) as cnt "
            "FROM signals WHERE harvested_at >= ? GROUP BY day ORDER BY day",
            (cutoff,),
        ).fetchall()
        for r in rows:
            ig_daily.setdefault(r["day"], {})["signals_ingested"] = r["cnt"]

        rows = ig_conn.execute(
            "SELECT DATE(synthesized_at) as day, COUNT(*) as cnt "
            "FROM ideas WHERE synthesized_at >= ? GROUP BY day ORDER BY day",
            (cutoff,),
        ).fetchall()
        for r in rows:
            ig_daily.setdefault(r["day"], {})["ideas_created"] = r["cnt"]

        # Per-source breakdown from IdeaForge
        source_idea_data = {}
        if _table_exists(ig_conn, "source_metrics"):
            rows = ig_conn.execute(
                "SELECT * FROM source_metrics"
            ).fetchall()
            for r in rows:
                source_idea_data[r["source"] if "source" in r.keys() else "unknown"] = dict(r)

    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        logger.warning("IdeaForge DB read failed (may be locked): %s", e)
        result["subsystem_summaries"]["ideaforge_error"] = str(e)
    finally:
        if ig_conn:
            ig_conn.close()

    # --- Metroplex metrics ---
    mx_conn = None
    try:
        mx_conn = _readonly_connect(metroplex_db_path)

        # Ideas approved (triage decisions)
        if _table_exists(mx_conn, "triage_decisions"):
            result["funnel"]["ideas_approved"] = mx_conn.execute(
                "SELECT COUNT(*) FROM triage_decisions "
                "WHERE decision='approve' AND decided_at >= ?",
                (cutoff,),
            ).fetchone()[0]

        # Builds dispatched (distinct queue_job_ids)
        if _table_exists(mx_conn, "build_jobs"):
            result["funnel"]["builds_dispatched"] = mx_conn.execute(
                "SELECT COUNT(DISTINCT queue_job_id) FROM build_jobs WHERE queued_at >= ?",
                (cutoff,),
            ).fetchone()[0]

            # Builds succeeded: latest row per queue_job_id with status='completed'
            result["funnel"]["builds_succeeded"] = mx_conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT queue_job_id FROM build_jobs "
                "  WHERE queued_at >= ? "
                "  GROUP BY queue_job_id "
                "  HAVING MAX(id) IN ("
                "    SELECT id FROM build_jobs WHERE status='completed'"
                "  )"
                ")",
                (cutoff,),
            ).fetchone()[0]

            # Builds failed: latest row per queue_job_id with status in failed/abandoned
            result["funnel"]["builds_failed"] = mx_conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT queue_job_id FROM build_jobs "
                "  WHERE queued_at >= ? "
                "  GROUP BY queue_job_id "
                "  HAVING MAX(id) IN ("
                "    SELECT id FROM build_jobs WHERE status IN ('failed','abandoned')"
                "  )"
                ")",
                (cutoff,),
            ).fetchone()[0]

            # Unique idea counts (de-duplicated across retries with different queue_job_ids)
            result["funnel"]["builds_succeeded_unique"] = mx_conn.execute(
                "SELECT COUNT(DISTINCT b.idea_id) FROM build_jobs b "
                "WHERE b.id IN ("
                "  SELECT MAX(id) FROM build_jobs WHERE queued_at >= ? GROUP BY queue_job_id"
                ") AND b.status = 'completed'",
                (cutoff,),
            ).fetchone()[0]

            result["funnel"]["builds_failed_unique"] = mx_conn.execute(
                "SELECT COUNT(DISTINCT b.idea_id) FROM build_jobs b "
                "WHERE b.id IN ("
                "  SELECT MAX(id) FROM build_jobs WHERE queued_at >= ? GROUP BY queue_job_id"
                ") AND b.status IN ('failed', 'abandoned')",
                (cutoff,),
            ).fetchone()[0]

            result["funnel"]["builds_dispatched_unique"] = mx_conn.execute(
                "SELECT COUNT(DISTINCT idea_id) FROM build_jobs WHERE queued_at >= ?",
                (cutoff,),
            ).fetchone()[0]

        # Builds published
        if _table_exists(mx_conn, "publish_jobs"):
            result["funnel"]["builds_published"] = mx_conn.execute(
                "SELECT COUNT(*) FROM publish_jobs WHERE status='published' AND published_at >= ?",
                (cutoff,),
            ).fetchone()[0]

        # Per-day from metroplex
        mx_daily = {}
        if _table_exists(mx_conn, "triage_decisions"):
            rows = mx_conn.execute(
                "SELECT DATE(decided_at) as day, COUNT(*) as cnt "
                "FROM triage_decisions WHERE decision='approve' AND decided_at >= ? "
                "GROUP BY day ORDER BY day",
                (cutoff,),
            ).fetchall()
            for r in rows:
                mx_daily.setdefault(r["day"], {})["ideas_approved"] = r["cnt"]

        if _table_exists(mx_conn, "build_jobs"):
            rows = mx_conn.execute(
                "SELECT DATE(queued_at) as day, COUNT(DISTINCT queue_job_id) as cnt "
                "FROM build_jobs WHERE queued_at >= ? GROUP BY day ORDER BY day",
                (cutoff,),
            ).fetchall()
            for r in rows:
                mx_daily.setdefault(r["day"], {})["builds_dispatched"] = r["cnt"]

        # Per-source: builds and publishes per source
        per_source_builds = {}
        if _table_exists(mx_conn, "build_jobs"):
            rows = mx_conn.execute(
                "SELECT queue_job_id FROM build_jobs WHERE queued_at >= ?",
                (cutoff,),
            ).fetchall()
            for r in rows:
                parts = r["queue_job_id"].split("-", 2)
                if len(parts) >= 3 and parts[0] == "metroplex":
                    src = parts[1]
                elif len(parts) == 2 and parts[0] == "metroplex" and parts[1].isdigit():
                    src = "ideaforge"
                else:
                    src = "unknown"
                per_source_builds.setdefault(src, {"builds": 0, "publishes": 0})
                per_source_builds[src]["builds"] += 1

        if _table_exists(mx_conn, "publish_jobs") and _table_exists(mx_conn, "build_jobs"):
            rows = mx_conn.execute(
                "SELECT b.queue_job_id FROM publish_jobs p "
                "JOIN build_jobs b ON b.queue_job_id = p.build_job_id "
                "WHERE p.status='published'",
            ).fetchall()
            for r in rows:
                parts = r["queue_job_id"].split("-", 2)
                if len(parts) >= 3 and parts[0] == "metroplex":
                    src = parts[1]
                elif len(parts) == 2 and parts[0] == "metroplex" and parts[1].isdigit():
                    src = "ideaforge"
                else:
                    src = "unknown"
                per_source_builds.setdefault(src, {"builds": 0, "publishes": 0})
                per_source_builds[src]["publishes"] += 1

        for src, data in sorted(per_source_builds.items()):
            result["per_source"].append({
                "source": src,
                "builds": data["builds"],
                "publishes": data["publishes"],
            })

        # Feasibility rejections
        if _table_exists(mx_conn, "feasibility_predictions"):
            result["subsystem_summaries"]["feasibility_rejections"] = mx_conn.execute(
                "SELECT COUNT(*) FROM feasibility_predictions "
                "WHERE predicted_outcome LIKE '%fail%'",
            ).fetchone()[0]

    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        logger.warning("Metroplex DB read failed (may be locked): %s", e)
        result["subsystem_summaries"]["metroplex_error"] = str(e)
    finally:
        if mx_conn:
            mx_conn.close()

    # --- Conversion rates ---
    f = result["funnel"]
    result["conversion_rates"] = {
        "signals_to_ideas": _safe_rate(f.get("ideas_created"), f.get("signals_ingested")),
        "ideas_to_scored": _safe_rate(f.get("ideas_scored"), f.get("ideas_created")),
        "scored_to_classified": _safe_rate(f.get("ideas_classified"), f.get("ideas_scored")),
        "classified_to_approved": _safe_rate(f.get("ideas_approved"), f.get("ideas_classified")),
        "approved_to_dispatched": _safe_rate(f.get("builds_dispatched"), f.get("ideas_approved")),
        "dispatched_to_succeeded": _safe_rate(f.get("builds_succeeded"), f.get("builds_dispatched")),
        "dispatched_to_succeeded_unique": _safe_rate(f.get("builds_succeeded_unique"), f.get("builds_dispatched_unique")),
        "succeeded_to_published": _safe_rate(f.get("builds_published"), f.get("builds_succeeded")),
    }

    # --- Per-day breakdown (merge ig_daily and mx_daily) ---
    all_days = sorted(set(list(ig_daily.keys() if 'ig_daily' in dir() else []) +
                         list(mx_daily.keys() if 'mx_daily' in dir() else [])))
    # Reconstruct safely
    _ig_daily = ig_daily if 'ig_daily' in dir() else {}
    _mx_daily = mx_daily if 'mx_daily' in dir() else {}
    all_days = sorted(set(list(_ig_daily.keys()) + list(_mx_daily.keys())))

    for day in all_days:
        day_data = {"date": day}
        day_data.update(_ig_daily.get(day, {}))
        day_data.update(_mx_daily.get(day, {}))

        # Compute daily conversion rate for signals → ideas
        # Days with signals but no ideas should have rate 0 (not None)
        signals = day_data.get("signals_ingested")
        ideas = day_data.get("ideas_created", 0) if signals else day_data.get("ideas_created")
        day_data["signals_to_ideas_rate"] = _safe_rate(ideas, signals)
        result["per_day"].append(day_data)

    # --- Anomaly detection ---
    # For each conversion rate, compute mean across daily values and flag drops
    rate_keys = ["signals_to_ideas_rate"]
    for rk in rate_keys:
        daily_rates = [d[rk] for d in result["per_day"] if d.get(rk) is not None]
        if len(daily_rates) >= 2:
            mean_rate = sum(daily_rates) / len(daily_rates)
            if mean_rate > 0:
                for d in result["per_day"]:
                    val = d.get(rk)
                    if val is not None and val < mean_rate * 0.5:
                        result["anomalies"].append(
                            f"{d['date']}: {rk} dropped to {val:.4f} "
                            f"(mean={mean_rate:.4f}, <50% threshold)"
                        )

    # --- Subsystem summaries ---
    # Postmortem top categories (reuse postmortem module)
    try:
        from db import StateDB
        from postmortem import get_postmortem_summary

        state_db = StateDB(metroplex_db_path)
        state_db.init_db()
        result["subsystem_summaries"]["postmortem_categories"] = get_postmortem_summary(state_db)
        state_db.close()
    except Exception as e:
        logger.warning("Postmortem summary failed: %s", e)
        result["subsystem_summaries"]["postmortem_categories"] = []

    # Ratchet status (reuse quality_ratchet module)
    try:
        from db import StateDB
        from quality_ratchet import evaluate_ratchet

        state_db = StateDB(metroplex_db_path)
        state_db.init_db()
        result["subsystem_summaries"]["ratchet_status"] = evaluate_ratchet(state_db)
        state_db.close()
    except Exception as e:
        logger.warning("Ratchet evaluation failed: %s", e)
        result["subsystem_summaries"]["ratchet_status"] = {}

    return result


def format_funnel_output(metrics: dict, as_json: bool = False) -> str:
    """Format funnel metrics for CLI display.

    Args:
        metrics: Output from compute_funnel_metrics()
        as_json: If True, return raw JSON

    Returns:
        Formatted string for terminal output
    """
    if as_json:
        import json
        return json.dumps(metrics, indent=2, default=str)

    lines = []
    lines.append(f"{'='*60}")
    lines.append("PIPELINE FUNNEL DASHBOARD")
    lines.append(f"{'='*60}")
    lines.append(f"Period: last {metrics['days']} days (since {metrics['cutoff'][:10]})")
    lines.append("")

    # Funnel stages
    lines.append("Funnel Stages:")
    f = metrics["funnel"]
    stages = [
        ("Signals ingested", f.get("signals_ingested", 0)),
        ("Ideas created", f.get("ideas_created", 0)),
        ("Ideas scored", f.get("ideas_scored", 0)),
        ("Ideas classified", f.get("ideas_classified", 0)),
        ("Ideas dismissed", f.get("ideas_dismissed", 0)),
        ("Ideas approved", f.get("ideas_approved", 0)),
        ("Builds dispatched", f.get("builds_dispatched", 0)),
        ("Builds succeeded", f.get("builds_succeeded", 0)),
        ("Builds failed", f.get("builds_failed", 0)),
        ("Builds published", f.get("builds_published", 0)),
        ("", None),  # blank separator
        ("Builds dispatched (unique)", f.get("builds_dispatched_unique", 0)),
        ("Builds succeeded (unique)", f.get("builds_succeeded_unique", 0)),
        ("Builds failed (unique)", f.get("builds_failed_unique", 0)),
    ]
    for name, count in stages:
        if count is None:
            lines.append("")
        else:
            lines.append(f"  {name:<28} {count:>6}")

    # Conversion rates
    lines.append("")
    lines.append("Conversion Rates:")
    cr = metrics["conversion_rates"]
    for name, rate in cr.items():
        label = name.replace("_", " ").title()
        if rate is not None:
            pct = f"{rate*100:.1f}%"
        else:
            pct = "  N/A"
        lines.append(f"  {label:<30} {pct:>8}")

    # Per-source breakdown
    if metrics["per_source"]:
        lines.append("")
        lines.append("Per-Source Breakdown:")
        lines.append(f"  {'Source':<15} {'Builds':>8} {'Published':>10}")
        lines.append(f"  {'-'*35}")
        for s in metrics["per_source"]:
            lines.append(f"  {s['source']:<15} {s['builds']:>8} {s['publishes']:>10}")

    # Anomalies
    if metrics["anomalies"]:
        lines.append("")
        lines.append("ANOMALIES DETECTED:")
        for a in metrics["anomalies"]:
            lines.append(f"  ! {a}")

    # Subsystem summaries
    ss = metrics.get("subsystem_summaries", {})

    pm = ss.get("postmortem_categories", [])
    if pm:
        lines.append("")
        lines.append("Postmortem Top Categories:")
        for p in pm[:5]:
            avg = f"{p['avg_score']:.1f}" if p.get("avg_score") is not None else "-"
            lines.append(f"  {p['category']:<20} count={p['count']:>3}  avg_score={avg}")

    feas = ss.get("feasibility_rejections")
    if feas is not None:
        lines.append(f"\nFeasibility rejections: {feas}")

    ratchet = ss.get("ratchet_status", {})
    if ratchet.get("activated"):
        lines.append(f"\nRatchet: {ratchet.get('reason', 'active')}")

    return "\n".join(lines)
