"""
Pipeline Health Check — Phase D
Unified OK/WARN/CRIT health assessment for the Metroplex pipeline.
Standalone module with no imports from Metroplex internals — uses sqlite3 directly.
"""
import logging
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class HealthStatus(IntEnum):
    """Pipeline health status levels, ordered by severity."""
    OK = 0
    WARN = 1
    CRIT = 2


@dataclass
class CheckResult:
    """Result of a single health check."""
    name: str
    status: HealthStatus
    message: str


@dataclass
class HealthReport:
    """Aggregated health report across all checks."""
    overall_status: HealthStatus
    checks: list[CheckResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _readonly_connect(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection with Row factory."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check whether *table* exists in the database."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_cycle_recency(conn: sqlite3.Connection) -> CheckResult:
    """CRIT if last completed cycle >10 min ago, WARN if >5 min."""
    name = "cycle_recency"
    if not _table_exists(conn, "cycles"):
        return CheckResult(name, HealthStatus.WARN, "cycles table does not exist")

    row = conn.execute(
        "SELECT MAX(completed_at) AS last FROM cycles WHERE completed_at IS NOT NULL"
    ).fetchone()

    if row is None or row["last"] is None:
        return CheckResult(name, HealthStatus.WARN, "No completed cycles found")

    try:
        last_dt = datetime.fromisoformat(row["last"])
    except (ValueError, TypeError):
        return CheckResult(name, HealthStatus.WARN, f"Cannot parse completed_at: {row['last']}")

    age_seconds = (datetime.now() - last_dt).total_seconds()
    age_min = age_seconds / 60.0

    if age_min > 10:
        return CheckResult(name, HealthStatus.CRIT, f"Last cycle completed {age_min:.1f} min ago (>10 min)")
    if age_min > 5:
        return CheckResult(name, HealthStatus.WARN, f"Last cycle completed {age_min:.1f} min ago (>5 min)")
    return CheckResult(name, HealthStatus.OK, f"Last cycle completed {age_min:.1f} min ago")


def _check_gate_health(conn: sqlite3.Connection) -> CheckResult:
    """CRIT if any gate has >=3 consecutive errors (circuit breaker tripped)."""
    name = "gate_health"
    if not _table_exists(conn, "cycles"):
        return CheckResult(name, HealthStatus.WARN, "cycles table does not exist")

    # Recent cycles ordered newest-first
    rows = conn.execute(
        "SELECT errors FROM cycles ORDER BY id DESC LIMIT 20"
    ).fetchall()

    if not rows:
        return CheckResult(name, HealthStatus.OK, "No cycles recorded yet")

    # Count consecutive cycles with errors (from most recent backward)
    import json
    consecutive_errors = 0
    for row in rows:
        try:
            errors = json.loads(row["errors"]) if row["errors"] else []
        except (json.JSONDecodeError, TypeError):
            errors = []
        if errors:
            consecutive_errors += 1
        else:
            break

    if consecutive_errors >= 3:
        return CheckResult(
            name, HealthStatus.CRIT,
            f"{consecutive_errors} consecutive cycles with errors — circuit breaker likely tripped"
        )
    if consecutive_errors >= 1:
        return CheckResult(name, HealthStatus.OK, f"{consecutive_errors} recent cycle(s) with errors")
    return CheckResult(name, HealthStatus.OK, "No recent gate errors")


def _check_stuck_builds(conn: sqlite3.Connection) -> CheckResult:
    """CRIT if any build has been 'started' for >2 hours."""
    name = "stuck_builds"
    if not _table_exists(conn, "build_jobs"):
        return CheckResult(name, HealthStatus.OK, "build_jobs table does not exist")

    rows = conn.execute("""
        SELECT queue_job_id, title, queued_at
        FROM build_jobs
        WHERE status = 'started'
          AND queued_at < datetime('now', '-2 hours')
    """).fetchall()

    if rows:
        ids = ", ".join(r["queue_job_id"] for r in rows)
        return CheckResult(
            name, HealthStatus.CRIT,
            f"{len(rows)} stuck build(s) running >2h: {ids}"
        )
    return CheckResult(name, HealthStatus.OK, "No stuck builds")


def _check_ratchet_health(conn: sqlite3.Connection) -> CheckResult:
    """WARN if quality threshold exceeds average quality of recent builds (pipeline starving)."""
    name = "ratchet_health"

    if not _table_exists(conn, "ratchet_state"):
        return CheckResult(name, HealthStatus.OK, "ratchet_state table does not exist — not yet initialised")

    row = conn.execute(
        "SELECT value FROM ratchet_state WHERE key = 'quality_threshold'"
    ).fetchone()
    if row is None:
        return CheckResult(name, HealthStatus.OK, "No quality threshold set yet")

    threshold = row["value"]

    if not _table_exists(conn, "build_jobs"):
        return CheckResult(name, HealthStatus.OK, "build_jobs table does not exist")

    # Average quality of last 20 scored builds
    score_rows = conn.execute("""
        SELECT quality_score FROM build_jobs
        WHERE quality_score IS NOT NULL
        ORDER BY id DESC LIMIT 20
    """).fetchall()

    if not score_rows:
        return CheckResult(name, HealthStatus.OK, "No scored builds to compare against threshold")

    avg_quality = sum(r["quality_score"] for r in score_rows) / len(score_rows)

    if threshold > avg_quality:
        return CheckResult(
            name, HealthStatus.WARN,
            f"Threshold ({threshold:.1f}) > avg recent quality ({avg_quality:.1f}) — pipeline may be starving"
        )
    return CheckResult(
        name, HealthStatus.OK,
        f"Threshold ({threshold:.1f}) <= avg quality ({avg_quality:.1f})"
    )


def _check_queue_drain(conn: sqlite3.Connection) -> CheckResult:
    """WARN if >20 pending items and 0 dispatched in the last hour."""
    name = "queue_drain"
    if not _table_exists(conn, "priority_queue"):
        return CheckResult(name, HealthStatus.OK, "priority_queue table does not exist")

    pending_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM priority_queue WHERE status = 'pending'"
    ).fetchone()
    pending = pending_row["cnt"] if pending_row else 0

    if pending <= 20:
        return CheckResult(name, HealthStatus.OK, f"{pending} pending queue items")

    # Check if anything was dispatched recently
    if not _table_exists(conn, "build_jobs"):
        dispatched = 0
    else:
        dispatched_row = conn.execute("""
            SELECT COUNT(*) AS cnt FROM build_jobs
            WHERE queued_at >= datetime('now', '-1 hour')
        """).fetchone()
        dispatched = dispatched_row["cnt"] if dispatched_row else 0

    if dispatched == 0:
        return CheckResult(
            name, HealthStatus.WARN,
            f"{pending} pending items but 0 dispatched in the last hour — queue not draining"
        )
    return CheckResult(name, HealthStatus.OK, f"{pending} pending, {dispatched} dispatched last hour")


def _check_budget_health(conn: sqlite3.Connection, daily_limit: float = 50.0) -> CheckResult:
    """WARN at 80% daily budget, CRIT at 95%."""
    name = "budget_health"
    if not _table_exists(conn, "cost_ledger"):
        return CheckResult(name, HealthStatus.OK, "cost_ledger table does not exist")

    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(estimated_cost), 0.0) AS total FROM cost_ledger WHERE timestamp LIKE ?",
        (f"{today}%",),
    ).fetchone()
    spent = row["total"] if row else 0.0

    if daily_limit <= 0:
        return CheckResult(name, HealthStatus.OK, f"${spent:.2f} spent today (no limit configured)")

    pct = spent / daily_limit
    if pct >= 0.95:
        return CheckResult(
            name, HealthStatus.CRIT,
            f"${spent:.2f} / ${daily_limit:.2f} ({pct:.0%}) — at or over daily budget"
        )
    if pct >= 0.80:
        return CheckResult(
            name, HealthStatus.WARN,
            f"${spent:.2f} / ${daily_limit:.2f} ({pct:.0%}) — approaching daily budget"
        )
    return CheckResult(name, HealthStatus.OK, f"${spent:.2f} / ${daily_limit:.2f} ({pct:.0%})")


def _check_orphan_processes(metroplex_pid: int | None = None) -> CheckResult:
    """WARN if 1-5 orphan build child processes, CRIT if >5.

    Detects leftover build processes: http.server, uvicorn, testproj,
    serve_results.py, etc. These accumulate when builds complete or fail without
    cleaning up their spawned servers.

    Args:
        metroplex_pid: PID of the Metroplex service. If None, attempts to detect
            from systemd. Pass 0 to skip (e.g., when Metroplex isn't running).
    """
    name = "orphan_processes"

    if metroplex_pid is None:
        metroplex_pid = _detect_metroplex_pid()

    if metroplex_pid == 0:
        return CheckResult(name, HealthStatus.OK, "Metroplex not running — no orphans to check")

    orphans = find_orphan_processes(metroplex_pid)

    count = len(orphans)
    if count > 5:
        sample = ", ".join(f"PID {p[0]}" for p in orphans[:5])
        return CheckResult(
            name, HealthStatus.CRIT,
            f"{count} orphan processes (>5): {sample}..."
        )
    if count >= 1:
        sample = ", ".join(f"PID {p[0]}" for p in orphans)
        return CheckResult(
            name, HealthStatus.WARN,
            f"{count} orphan process(es): {sample}"
        )
    return CheckResult(name, HealthStatus.OK, "No orphan processes")


def _check_self_healing_daemon(queue_root: Path) -> CheckResult:
    """WARN at 15 min stale heartbeat, CRIT at 30 min or if file absent."""
    name = "self_healing_daemon"
    heartbeat = queue_root / "heartbeat-worker-1.txt"

    if not heartbeat.exists():
        return CheckResult(name, HealthStatus.CRIT, "Heartbeat file missing -- daemon is not running")

    age_seconds = time.time() - heartbeat.stat().st_mtime
    age_min = age_seconds / 60.0

    if age_seconds > 1800:
        return CheckResult(name, HealthStatus.CRIT, f"Heartbeat stale {age_min:.1f} min (>30 min) -- daemon likely dead")
    if age_seconds > 900:
        return CheckResult(name, HealthStatus.WARN, f"Heartbeat stale {age_min:.1f} min (>15 min) -- daemon may be stuck")
    return CheckResult(name, HealthStatus.OK, f"Heartbeat fresh {age_min:.1f} min ago")


def _detect_metroplex_pid() -> int:
    """Get the main Metroplex service PID from systemd. Returns 0 if not running."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "metroplex", "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        pid = int(result.stdout.strip())
        return pid if pid > 0 else 0
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return 0


def find_orphan_processes(metroplex_pid: int) -> list[tuple[int, str]]:
    """Find orphan processes in the Metroplex systemd cgroup.

    Build processes (http.server, uvicorn, test servers) get reparented to
    PID 1 when their parent build exits, but they stay in the Metroplex
    cgroup. We use the cgroup process list to find them reliably.

    Returns list of (pid, cmdline) tuples for orphan processes.
    """
    # Patterns that indicate a build-spawned server (not Metroplex itself)
    orphan_patterns = (
        "http.server", "uvicorn", "testproj", "qatest", "verifytest",
        "jwttest", "jwtfix", "quicktest", "serve_results", "tooltest",
        "start_server.py", "src.main",
        "--port 8", "--port 9", "--port 19",
    )

    # Patterns that are legitimate Metroplex processes (never kill)
    safe_patterns = (
        "metroplex.py",
    )

    cgroup_procs = Path(
        "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
        "/app.slice/metroplex.service/cgroup.procs"
    )

    orphans: list[tuple[int, str]] = []
    try:
        if not cgroup_procs.exists():
            logger.debug("Metroplex cgroup not found -- falling back to ppid scan")
            return orphans

        pids = [int(line.strip()) for line in cgroup_procs.read_text().splitlines() if line.strip()]

        for pid in pids:
            if pid == metroplex_pid:
                continue
            try:
                result = subprocess.run(
                    ["ps", "-o", "args=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=5,
                )
                cmdline = result.stdout.strip()
                if not cmdline:
                    continue
            except (subprocess.TimeoutExpired, OSError):
                continue

            if any(s in cmdline for s in safe_patterns):
                continue
            if any(p in cmdline for p in orphan_patterns):
                orphans.append((pid, cmdline))

    except (OSError, ValueError) as e:
        logger.warning("Failed to scan for orphan processes: %s", e)

    return orphans


def kill_orphan_processes(metroplex_pid: int) -> list[tuple[int, str]]:
    """Kill orphan build processes under the Metroplex service tree.

    Returns list of (pid, cmdline) for processes that were killed.
    """
    orphans = find_orphan_processes(metroplex_pid)
    killed: list[tuple[int, str]] = []

    for pid, cmdline in orphans:
        try:
            os.kill(pid, 15)  # SIGTERM
            killed.append((pid, cmdline))
            logger.info("Killed orphan process PID %d: %s", pid, cmdline[:80])
        except OSError as e:
            logger.warning("Failed to kill PID %d: %s", pid, e)

    return killed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_health_checks(
    db_path: str,
    daily_cost_limit: float = 50.0,
    metroplex_pid: int | None = None,
    queue_root: Path | None = None,
) -> HealthReport:
    """Run all pipeline health checks and return an aggregated report.

    Args:
        db_path: Path to metroplex.db (opened read-only).
        daily_cost_limit: Daily spending cap for budget check.
        metroplex_pid: PID of Metroplex service. None = auto-detect, 0 = skip orphan check.
        queue_root: Path to self_healing_queue dir. None = default relative to db_path parent.

    Returns:
        HealthReport with per-check results and an overall status equal to
        the worst (highest severity) individual check.
    """
    try:
        conn = _readonly_connect(db_path)
    except sqlite3.OperationalError as e:
        return HealthReport(
            overall_status=HealthStatus.CRIT,
            checks=[CheckResult("db_connect", HealthStatus.CRIT, f"Cannot open database: {e}")],
        )

    try:
        checks = [
            _check_cycle_recency(conn),
            _check_gate_health(conn),
            _check_stuck_builds(conn),
            _check_ratchet_health(conn),
            _check_queue_drain(conn),
            _check_budget_health(conn, daily_cost_limit),
        ]
    finally:
        conn.close()

    # Orphan process check (no DB needed)
    checks.append(_check_orphan_processes(metroplex_pid))

    # Self-healing daemon heartbeat check
    if queue_root is None:
        queue_root = Path(db_path).parent / "self_healing_queue"
    checks.append(_check_self_healing_daemon(queue_root))

    overall = HealthStatus(max(c.status for c in checks))
    return HealthReport(overall_status=overall, checks=checks)


def format_report(report: HealthReport) -> str:
    """Format a HealthReport as a clean terminal-friendly string."""
    status_labels = {
        HealthStatus.OK: "OK",
        HealthStatus.WARN: "WARN",
        HealthStatus.CRIT: "CRIT",
    }
    status_icons = {
        HealthStatus.OK: "  ",
        HealthStatus.WARN: "! ",
        HealthStatus.CRIT: "!!",
    }

    lines: list[str] = []
    lines.append(f"Pipeline Health: {status_labels[report.overall_status]}")
    lines.append(f"Timestamp:       {report.timestamp}")
    lines.append("-" * 60)

    for check in report.checks:
        icon = status_icons[check.status]
        label = status_labels[check.status].ljust(4)
        lines.append(f"  {icon} [{label}] {check.name}: {check.message}")

    lines.append("-" * 60)
    return "\n".join(lines)
