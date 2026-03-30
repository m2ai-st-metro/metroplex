"""
Pipeline Watchdog — Phase D + Phase I (Galvatron Integration)
External stall detector, runs via systemd timer every 15 minutes.
Alerts via Telegram when the pipeline is stuck or unhealthy.
On WARN/CRIT: sends alert to Galvatron's Telegram chat and dispatches
a diagnostic mission via ClaudeClaw Mission Control.
"""
import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from config import Config
from health import (
    HealthStatus,
    run_health_checks,
    format_report,
    kill_orphan_processes,
    _detect_metroplex_pid,
)
from notifier import create_notifier, FilteredNotifier

logger = logging.getLogger(__name__)

# Default DB path relative to project root
DEFAULT_DB_PATH = str(Path(__file__).parent / "data" / "metroplex.db")

# ClaudeClaw project root for Mission Control CLI
CLAUDECLAW_ROOT = Path.home() / "projects" / "claudeclaw"
MISSION_CLI = CLAUDECLAW_ROOT / "dist" / "mission-cli.js"


def _build_alert_message(report) -> str:
    """Build a concise Telegram-friendly alert from a HealthReport."""
    status_label = {
        HealthStatus.OK: "OK",
        HealthStatus.WARN: "WARNING",
        HealthStatus.CRIT: "CRITICAL",
    }

    lines: list[str] = []
    lines.append(f"<b>Metroplex Watchdog: {status_label[report.overall_status]}</b>")
    lines.append("")

    # Only include non-OK checks to keep alerts focused
    failing = [c for c in report.checks if c.status != HealthStatus.OK]
    if not failing:
        lines.append("All checks passed.")
    else:
        for check in failing:
            prefix = "!!" if check.status == HealthStatus.CRIT else "!"
            lines.append(f"{prefix} <b>{check.name}</b>: {check.message}")

    lines.append("")
    lines.append(f"<i>{report.timestamp}</i>")
    return "\n".join(lines)


def _build_galvatron_prompt(report) -> str:
    """Build a diagnostic prompt for Galvatron based on failing health checks."""
    failing = [c for c in report.checks if c.status != HealthStatus.OK]
    status_label = "CRITICAL" if report.overall_status == HealthStatus.CRIT else "WARNING"

    lines = [
        f"AUTOMATED HEALTH ALERT: {status_label}",
        "",
        "The Metroplex watchdog detected the following issues:",
        "",
    ]
    for check in failing:
        severity = "CRIT" if check.status == HealthStatus.CRIT else "WARN"
        lines.append(f"- [{severity}] {check.name}: {check.message}")

    lines.extend([
        "",
        "Run a full pipeline health diagnostic. For each failing check:",
        "1. Determine root cause using DB queries and log analysis",
        "2. Assess severity (P0/P1/P2)",
        "3. For P0: execute fix protocol immediately",
        "4. For P1: diagnose and report findings",
        "5. For P2: log to hive mind for next session",
        "",
        "Report your findings and any actions taken.",
    ])
    return "\n".join(lines)


def _dispatch_to_galvatron(report) -> bool:
    """Dispatch a diagnostic mission to Galvatron via ClaudeClaw Mission Control.

    Returns True if the mission was dispatched successfully.
    """
    if not MISSION_CLI.exists():
        logger.warning("Mission CLI not found at %s -- skipping Galvatron dispatch", MISSION_CLI)
        return False

    prompt = _build_galvatron_prompt(report)
    status_label = "CRITICAL" if report.overall_status == HealthStatus.CRIT else "WARNING"
    priority = "10" if report.overall_status == HealthStatus.CRIT else "7"

    try:
        env = os.environ.copy()
        # Source shared env for DB_ENCRYPTION_KEY
        shared_env = Path.home() / ".env.shared"
        if shared_env.exists():
            for line in shared_env.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env.setdefault(key.strip(), value.strip())

        result = subprocess.run(
            [
                "node", str(MISSION_CLI),
                "create",
                "--agent", "galvatron",
                "--title", f"Watchdog {status_label}: auto-diagnostic",
                "--priority", priority,
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(CLAUDECLAW_ROOT),
            env=env,
        )

        if result.returncode == 0:
            logger.info("Galvatron mission dispatched: %s", result.stdout.strip())
            return True
        else:
            logger.warning("Mission dispatch failed (rc=%d): %s", result.returncode, result.stderr.strip())
            return False

    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Failed to dispatch Galvatron mission: %s", e)
        return False


def _notify_galvatron(alert_message: str, level: str) -> bool:
    """Send alert to Galvatron's Telegram chat in addition to Metroplex's."""
    bot_token = os.environ.get("GALVATRON_BOT_TOKEN", "")
    chat_id = os.environ.get("GALVATRON_CHAT_ID", "")

    if not bot_token or not chat_id:
        logger.info("Galvatron Telegram not configured -- skipping Galvatron alert")
        return False

    galvatron_notifier = create_notifier(bot_token, chat_id)
    return galvatron_notifier.notify(alert_message, level=level)


def run_watchdog(dry_run: bool = False) -> int:
    """Run the watchdog cycle.

    1. Execute health checks against metroplex.db.
    2. If orphan processes at CRIT level, kill them (self-healing).
    3. If CRIT or WARN, send Telegram alert to both Metroplex and Galvatron.
    4. If CRIT or WARN, dispatch a diagnostic mission to Galvatron via Mission Control.
    5. If OK, stay silent (no notification).

    Args:
        dry_run: If True, print the report to stdout instead of sending Telegram.

    Returns:
        Exit code: 0=OK, 1=WARN, 2=CRIT.
    """
    config = Config()
    db_path = os.environ.get("METROPLEX_DB_PATH", DEFAULT_DB_PATH)

    report = run_health_checks(
        db_path=db_path,
        daily_cost_limit=config.daily_cost_limit,
    )

    # Always log the report locally
    formatted = format_report(report)
    logger.info("Watchdog health check:\n%s", formatted)

    # Self-healing: kill orphan processes if CRIT
    orphan_check = next((c for c in report.checks if c.name == "orphan_processes"), None)
    if orphan_check and orphan_check.status == HealthStatus.CRIT and not dry_run:
        mpid = _detect_metroplex_pid()
        if mpid > 0:
            killed = kill_orphan_processes(mpid)
            logger.info("Self-healed: killed %d orphan processes", len(killed))

    if dry_run:
        print(formatted)
        return int(report.overall_status)

    # Only alert on WARN or CRIT
    if report.overall_status == HealthStatus.OK:
        logger.info("All checks OK -- no alert sent")
        return 0

    # Build alert
    alert_message = _build_alert_message(report)
    level = "error" if report.overall_status == HealthStatus.CRIT else "warning"

    # Alert 1: Metroplex Telegram (existing behavior)
    inner = create_notifier(config.telegram_bot_token, config.telegram_chat_id)
    notifier = FilteredNotifier(inner, mode="all")
    delivered = notifier.notify(alert_message, level=level)
    if delivered:
        logger.info("Alert sent via Metroplex Telegram (level=%s)", level)

    # Alert 2: Galvatron Telegram
    galvatron_delivered = _notify_galvatron(alert_message, level)
    if galvatron_delivered:
        logger.info("Alert sent via Galvatron Telegram (level=%s)", level)

    # Alert 3: Dispatch diagnostic mission to Galvatron via Mission Control
    mission_dispatched = _dispatch_to_galvatron(report)
    if mission_dispatched:
        logger.info("Galvatron diagnostic mission dispatched via Mission Control")

    return int(report.overall_status)


def main():
    """CLI entry point for the watchdog."""
    parser = argparse.ArgumentParser(
        description="Metroplex Pipeline Watchdog -- external stall detector"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print health report to stdout without sending Telegram alerts",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    sys.exit(run_watchdog(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
