"""The Metroplex CoS daemon: one process, one thread, three cadences.

- bot long-poll (continuous): Matthew's ideas, card buttons, /pause /resume /stop /status
- wake loop (every wake_poll_s): route pending Teletraan wakes
- sweep (every sweep_s): motion checks that raise fenced wakes

Owner: Matthew. Sink: the Metroplex bot chat plus data/decisions.log. Kill:
/pause (no new work, running attempts continue), /stop (pause, then exit),
any loop breaker at 3 consecutive failures halts that loop and messages once.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from audit import AuditLogger
from cos.bot import Bot, Inbound
from cos.intake import Intake
from cos.motion import sweep
from cos.routing import Router, route_pending
from cos.safety import LOOPS, CircuitBreaker, ShutdownHandler
from cos.store import LocalStore
from cos.teletraan_client import TeletraanClient, WorkError

CONTROL = ("/pause", "/resume", "/stop", "/status")

log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config: Any, client: TeletraanClient, store: LocalStore, bot: Bot, intake: Intake, router: Router,
                 breaker: CircuitBreaker, shutdown: ShutdownHandler, audit: AuditLogger | None = None, clock: Callable[[], float] = time.time):
        self.config, self.client, self.store, self.bot, self.intake, self.router = config, client, store, bot, intake, router
        self.breaker, self.shutdown, self.audit, self.clock = breaker, shutdown, audit, clock
        self._next = {"wake": 0.0, "sweep": 0.0, "expire": 0.0}

    # ------------------------------------------------------------------ control
    def _log(self, action: str, **details: Any) -> None:
        if self.audit:
            self.audit.log_decision("cos", action, details)

    def pause(self, reason: str = "/pause") -> str:
        paused = []
        for p in self.client.snapshot().get("project", []):
            if not p.get("paused"):
                try:
                    self.client.command("project.pause", p["id"], p["revision"], {"reason": reason})
                    paused.append(p["id"])
                except WorkError as e:
                    log.warning("pause %s failed: %s", p["id"], e.code)
        self.store.set("paused_projects", sorted(set(self.store.get("paused_projects", []) + paused)))
        self.store.set_paused(True, reason)
        self._log("pause", projects=paused)
        return f"Paused. No new work starts in {len(paused)} project(s); running attempts finish. /resume to continue."

    def resume(self) -> str:
        resumed = []
        snap = {p["id"]: p for p in self.client.snapshot().get("project", [])}
        for pid in self.store.get("paused_projects", []):
            p = snap.get(pid)
            if p and p.get("paused"):
                self.client.command("project.resume", pid, p["revision"], {})
                resumed.append(pid)
        self.store.set("paused_projects", [])
        self.store.set_paused(False)
        self._log("resume", projects=resumed)
        return f"Resumed {len(resumed)} project(s). Ready work is being routed."

    def status(self) -> str:
        pending = len(self.client.wake_pending().get("wakes", []))
        open_loops = [name for name in LOOPS if self.breaker.is_open(name)]
        cards = len(self.store.cards_in("awaiting_yes"))
        state = "PAUSED" if self.store.paused else "running"
        return f"Metroplex {state}. Pending wakes: {pending}. Cards awaiting yes: {cards}. Open breakers: {', '.join(open_loops) or 'none'}."

    # ------------------------------------------------------------------ inbound
    def handle(self, item: Inbound) -> None:
        command = item.text.strip().split()[0].lower() if item.kind == "text" and item.text.strip() else ""
        if self.breaker.is_open("intake", now=self.clock()) and command not in CONTROL:
            # The breaker is honored, not just announced (review N11), and a
            # tapped button always gets an answer so its spinner stops (R4).
            if item.kind == "callback" and item.callback_id:
                self.bot.answer(item.callback_id, "Metroplex intake is paused")
            self.bot.send("Metroplex intake is paused after repeated failures. `metroplex reset intake` to resume; /status /pause /resume /stop still work.")
            return
        try:
            if item.kind == "callback":
                reply = self._callback(item)
                if item.callback_id:
                    self.bot.answer(item.callback_id, reply[:200])
                self.bot.send(reply)
                return
            text = item.text.strip()
            command = text.split()[0].lower() if text else ""
            if command == "/pause":
                self.bot.send(self.pause())
            elif command == "/resume":
                self.bot.send(self.resume())
            elif command == "/stop":
                self.bot.send(self.pause("/stop") + " Metroplex is stopping.")
                self.shutdown.request()
            elif command == "/status":
                self.bot.send(self.status())
            elif command.startswith("/"):
                self.bot.send("Commands: /status /pause /resume /stop. Anything else is an idea.")
            else:
                self.intake.handle_text(text, item.message_id)
            if command not in CONTROL:
                self.breaker.record_success("intake")  # control commands never close it (R4)
        except Exception as e:  # noqa: BLE001 - one bad message must not stop the bot
            log.exception("inbound failed")
            # Never fail silently: say what broke and that a retry is safe
            # (every step is replay-safe, nothing is duplicated).
            if item.kind == "callback" and item.callback_id:
                self.bot.answer(item.callback_id, "That did not finish")
            if isinstance(e, WorkError) and e.code == "COMMAND_ID_CONFLICT":
                self.bot.send("That conflicts with an earlier action on the same item; nothing new was done. Send /status, or start the item over.")
            else:
                self.bot.send(f"That did not finish ({type(e).__name__}: {e}). Tap or send it again to retry; nothing is duplicated.")
            if command not in CONTROL and self.breaker.record_failure("intake", now=self.clock()):  # (Q2)
                self.bot.send(f"Metroplex intake stopped after 3 failures ({e}). `metroplex reset intake` after fixing.")

    def _callback(self, item: Inbound) -> str:
        parts = item.text.split(":")
        kind = parts[0]
        if kind == "yes" and len(parts) == 3:
            result = self.intake.approve(parts[1], parts[2], item.from_id, item.chat_id, item.message_id)
            self._log("card_yes", card=parts[1], result=result)
            return result
        if kind == "edit" and len(parts) == 2:
            return self.intake.edit(parts[1])
        if kind == "drop" and len(parts) == 2:
            return self.intake.drop(parts[1])
        if kind in ("r", "hold") and len(parts) == 2:
            ref = self.store.get(f"cb:{parts[1]}")
            if not ref:
                return "That button has expired."
            if kind == "hold":
                return "Held. The task stays blocked until you approve it."
            result = self.intake.approve_reserved(ref["taskId"], ref["scopeRevision"], item.from_id, item.chat_id, item.message_id, block_revision=ref["blockRevision"])
            self._log("reserved_yes", task=ref["taskId"], result=result)
            return result
        return "Unknown button."

    # ------------------------------------------------------------------ cadences
    def tick(self) -> None:
        now = self.clock()
        if now >= self._next["wake"]:
            self._next["wake"] = now + self.config.wake_poll_s
            if not self.store.paused and not self.breaker.is_open("turn", now=now):
                try:
                    for out in route_pending(self.router, self.client):
                        self._log("turn", decision=out.decision, commands=out.commands)
                except Exception as e:  # noqa: BLE001
                    log.exception("wake loop failed")
                    if self.breaker.record_failure("turn", now=now):
                        self.bot.send(f"Metroplex routing stopped after 3 failures ({e}). `metroplex reset turn` after fixing.")
        if now >= self._next["sweep"]:
            self._next["sweep"] = now + self.config.sweep_s
            if not self.store.paused and not self.breaker.is_open("motion", now=now):
                try:
                    found = sweep(self.client, self.store, self.config.thresholds, self.config.caps.wakes_per_sweep, lambda text, b=None: self.bot.send(text, b), now=now)
                    if found:
                        self._log("sweep", findings=[f"{f.check} {f.task_id}" for f in found])
                    self.breaker.record_success("motion")
                except Exception as e:  # noqa: BLE001
                    log.exception("sweep failed")
                    if self.breaker.record_failure("motion", now=now):
                        self.bot.send(f"Metroplex motion checks stopped after 3 failures ({e}). `metroplex reset motion` after fixing.")
        if now >= self._next["expire"]:
            self._next["expire"] = now + 3600
            self.intake.expire_cards()
            self.intake.resume_granting()

    def run(self) -> None:
        self.shutdown.install()
        offset = int(self.store.get("bot_offset", 0))
        self.bot.send("Metroplex is up. " + self.status())
        while not self.shutdown.requested:
            try:
                items, offset = self.bot.poll(offset, timeout=min(25, self.config.wake_poll_s))
                self.store.set("bot_offset", offset)
                for item in items:
                    self.handle(item)
            except Exception:  # noqa: BLE001 - network blips must not kill the daemon
                log.exception("bot poll failed")
                self.shutdown.wait(5)
            self.tick()
        log.info("Metroplex stopped cleanly")
