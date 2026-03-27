"""
Pipeline Watchdog — Phase D
External stall detector, runs via systemd timer every 15 minutes.
Alerts via Telegram when the pipeline is stuck or unhealthy.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

from config import Config
from health import HealthStatus, run_health_checks, format_report
from notifier import create_notifier, FilteredNotifier

logger = logging.getLogger(__name__)

# Default DB path relative to project root
DEFAULT_DB_PATH = str(Path(__file__).parent / "data" / "metroplex.db")


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


def run_watchdog(dry_run: bool = False) -> int:
    """Run the watchdog cycle.

    1. Execute health checks against metroplex.db.
    2. If CRIT or WARN, send a Telegram alert.
    3. If OK, stay silent (no notification).

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

    if dry_run:
        print(formatted)
        return int(report.overall_status)

    # Only alert on WARN or CRIT
    if report.overall_status == HealthStatus.OK:
        logger.info("All checks OK — no alert sent")
        return 0

    # Build and send alert
    alert_message = _build_alert_message(report)
    level = "error" if report.overall_status == HealthStatus.CRIT else "warning"

    inner = create_notifier(config.telegram_bot_token, config.telegram_chat_id)
    notifier = FilteredNotifier(inner, mode="all")

    delivered = notifier.notify(alert_message, level=level)
    if delivered:
        logger.info("Alert sent via Telegram (level=%s)", level)
    else:
        logger.warning("Failed to deliver Telegram alert")

    return int(report.overall_status)


def main():
    """CLI entry point for the watchdog."""
    parser = argparse.ArgumentParser(
        description="Metroplex Pipeline Watchdog — external stall detector"
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
