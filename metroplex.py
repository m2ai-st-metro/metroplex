#!/usr/bin/env python3
"""Metroplex CoS command line.

  metroplex.py run              run the daemon (systemd: deploy/metroplex.service)
  metroplex.py status           one-line status (read-only)
  metroplex.py sweep --dry-run  show what the motion sweep would raise (read-only)
  metroplex.py pause | resume   stop or restart new work across Metroplex projects
  metroplex.py reset <loop|all> close a circuit breaker after fixing the cause
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit import AuditLogger  # noqa: E402
from cos.bot import Bot, http_transport  # noqa: E402
from cos.config import Config  # noqa: E402
from cos.intake import Intake  # noqa: E402
from cos.motion import find  # noqa: E402
from cos.reasoning import reasoner_for  # noqa: E402
from cos.routing import Router  # noqa: E402
from cos.safety import LOOPS, CircuitBreaker, CycleCaps, ShutdownHandler  # noqa: E402
from cos.store import LocalStore  # noqa: E402
from cos.teletraan_client import TeletraanClient  # noqa: E402


def build(config: Config):
    store = LocalStore(config.data_dir / "cos.db")
    client = TeletraanClient.from_token_file(config.work_socket, config.cos_token_file)
    breaker = CircuitBreaker(store, config.caps.breaker_threshold)
    caps = CycleCaps(store, config.caps.cycle_s)
    if not (config.bot_token and config.chat_id and config.approver_ids):
        raise SystemExit("METROPLEX_TELEGRAM_BOT_TOKEN, METROPLEX_TELEGRAM_CHAT_ID and METROPLEX_APPROVER_IDS are required")
    bot = Bot(http_transport(config.bot_token), config.chat_id, config.approver_ids)
    send = lambda text, buttons=None: bot.send(text, buttons)  # noqa: E731
    router = Router(client, config, breaker, caps, notify=send)
    intake = Intake(client, store, reasoner_for(config, True), send, config)
    return store, client, bot, intake, router, breaker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="metroplex")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sweep_p = sub.add_parser("sweep")
    sweep_p.add_argument("--dry-run", action="store_true", required=True)
    reset_p = sub.add_parser("reset")
    reset_p.add_argument("loop", choices=[*LOOPS, "all"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()

    if args.cmd == "reset":
        store = LocalStore(config.data_dir / "cos.db")
        CircuitBreaker(store).reset(args.loop)
        print(f"reset {args.loop}")
        return 0
    if args.cmd == "sweep":
        client = TeletraanClient.from_token_file(config.work_socket, config.cos_token_file)
        for f in find(client.snapshot(), config.thresholds, time.time()):
            print(f"{f.check:14} {f.task_id:32} -> {f.reason or 'notify'}: {f.detail}")
        return 0

    from cos.daemon import Daemon

    store, client, bot, intake, router, breaker = build(config)
    daemon = Daemon(config, client, store, bot, intake, router, breaker, ShutdownHandler(), AuditLogger(str(config.data_dir / "decisions.log")))
    if args.cmd == "status":
        print(daemon.status())
    elif args.cmd == "pause":
        print(daemon.pause("cli pause"))
    elif args.cmd == "resume":
        print(daemon.resume())
    else:
        daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
