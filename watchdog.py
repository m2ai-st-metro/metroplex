"""
Pipeline Watchdog — Phase D + Phase I

External stall detector, runs via systemd timer every 5 minutes.
Alerts via Telegram when the pipeline is stuck or unhealthy.

On WARN/CRIT:
  - Sends alert to Metroplex Telegram and (if configured) Kup Telegram.
  - Dispatches a diagnostic mission via ccos Mission Control IFF
    WATCHDOG_MISSION_AGENT is set to a real ccos agent id. Default off:
    skip dispatch and rely on Telegram. Introduced 2026-05-07 after the
    prior hard-coded "galvatron" target — a retired bot — silently
    accumulated 968 zombie tasks in the ccos queue.
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


def _build_diagnostic_prompt(report) -> str:
    """Build a diagnostic prompt for the auto-dispatch agent based on failing health checks."""
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


def _dispatch_diagnostic_mission(report) -> bool:
    """Dispatch a diagnostic mission to a configured ccos agent via Mission Control.

    Gated on `WATCHDOG_MISSION_AGENT`. Default OFF: when the env var is unset
    or empty, no mission is dispatched (Telegram alerts still fire). Set to a
    real ccos agent id (e.g. "ops") to opt in. This default-off behaviour was
    introduced 2026-05-07 after the prior hard-coded "galvatron" target —
    a retired bot — quietly accumulated 968 zombie tasks in ccos.

    Returns True if the mission was dispatched successfully.
    """
    agent = os.environ.get("WATCHDOG_MISSION_AGENT", "").strip()
    if not agent:
        logger.info("WATCHDOG_MISSION_AGENT unset -- skipping mission dispatch (Telegram only)")
        return False

    if not MISSION_CLI.exists():
        logger.warning("Mission CLI not found at %s -- skipping mission dispatch", MISSION_CLI)
        return False

    prompt = _build_diagnostic_prompt(report)
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
                "--agent", agent,
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
            logger.info("Diagnostic mission dispatched to %s: %s", agent, result.stdout.strip())
            return True
        else:
            logger.warning(
                "Mission dispatch to %s failed (rc=%d): %s",
                agent, result.returncode, result.stderr.strip(),
            )
            return False

    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Failed to dispatch diagnostic mission to %s: %s", agent, e)
        return False


def _notify_kup(alert_message: str, level: str) -> bool:
    """Send alert to Kup's Telegram chat in addition to Metroplex's."""
    bot_token = os.environ.get("KUP_BOT_TOKEN", "")
    chat_id = os.environ.get("KUP_CHAT_ID", "")

    if not bot_token or not chat_id:
        logger.info("Kup Telegram not configured -- skipping Kup alert")
        return False

    kup_notifier = create_notifier(bot_token, chat_id)
    return kup_notifier.notify(alert_message, level=level)


def _restart_self_healing_daemon() -> bool:
    """Run the restart script for the self-healing daemon.

    Only called when METROPLEX_AUTO_RESTART_SELF_HEALING=true and the
    self_healing_daemon check is CRIT. Returns True if the script exited 0.
    """
    restart_script = Path(__file__).parent / "deploy" / "restart-self-healing-daemon.sh"
    if not restart_script.exists():
        logger.warning("Restart script not found at %s -- skipping auto-restart", restart_script)
        return False
    try:
        result = subprocess.run(
            [str(restart_script)],
            timeout=120,
            capture_output=False,
        )
        if result.returncode == 0:
            logger.info("Auto-restart of self-healing daemon succeeded")
            return True
        logger.warning("Auto-restart script exited %d", result.returncode)
        return False
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Auto-restart script failed: %s", e)
        return False


def run_watchdog(dry_run: bool = False) -> int:
    """Run the watchdog cycle.

    1. Execute health checks against metroplex.db.
    2. If orphan processes at CRIT level, kill them (self-healing).
    3. If self_healing_daemon CRIT and METROPLEX_AUTO_RESTART_SELF_HEALING=true,
       run the restart script before alerting.
    4. If CRIT or WARN, send Telegram alert to both Metroplex and Kup.
    5. If CRIT or WARN AND WATCHDOG_MISSION_AGENT is set, dispatch a
       diagnostic mission to that agent via ccos Mission Control.
    6. If OK, stay silent (no notification).

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

    # Auto-restart self-healing daemon on CRIT (feature-flagged, default OFF)
    daemon_check = next((c for c in report.checks if c.name == "self_healing_daemon"), None)
    auto_restart = os.environ.get("METROPLEX_AUTO_RESTART_SELF_HEALING", "").lower() == "true"
    if daemon_check and daemon_check.status == HealthStatus.CRIT and auto_restart and not dry_run:
        logger.warning("self_healing_daemon CRIT -- attempting auto-restart (METROPLEX_AUTO_RESTART_SELF_HEALING=true)")
        _restart_self_healing_daemon()

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

    # Alert 2: Kup Telegram
    kup_delivered = _notify_kup(alert_message, level)
    if kup_delivered:
        logger.info("Alert sent via Kup Telegram (level=%s)", level)

    # Alert 3: Dispatch diagnostic mission via ccos Mission Control (gated on WATCHDOG_MISSION_AGENT)
    mission_dispatched = _dispatch_diagnostic_mission(report)
    if mission_dispatched:
        logger.info("Diagnostic mission dispatched via ccos Mission Control")

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
