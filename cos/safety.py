"""Runaway guards without dollars: circuit breakers, per-cycle caps, pause, shutdown.

Ported from the gate-era safety.py (CircuitBreaker, CycleCaps, ShutdownHandler)
and generalized from triage/build/publish to the CoS loops. BudgetEnforcer is
gone by decision (no cost tracking).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Literal

from cos.store import LocalStore

log = logging.getLogger(__name__)

Loop = Literal["intake", "turn", "motion", "briefing", "jev", "reasoning"]
LOOPS: tuple[str, ...] = ("intake", "turn", "motion", "briefing", "jev", "reasoning")


class CircuitBreaker:
    """Opens a loop after `threshold` consecutive failures. The jev breaker also
    closes itself after `cooldown_s` so routing retries Jev later; the other
    loops stay open until reset by Matthew (`metroplex reset <loop>`)."""

    def __init__(self, store: LocalStore, threshold: int = 3, jev_cooldown_s: int = 15 * 60):
        self.store = store
        self.threshold = threshold
        self.jev_cooldown_s = jev_cooldown_s

    def record_success(self, loop: Loop) -> None:
        self.store.set_breaker(loop, failures=0, opened_at=None)

    def record_failure(self, loop: Loop, now: float | None = None) -> bool:
        """Returns True when this failure opened the breaker (message once)."""
        failures, opened_at = self.store.get_breaker(loop)
        failures += 1
        opened_now = failures >= self.threshold and opened_at is None
        self.store.set_breaker(loop, failures=failures, opened_at=(time.time() if now is None else now) if failures >= self.threshold else opened_at)
        if opened_now:
            log.warning("breaker %s opened after %d consecutive failures", loop, failures)
        return opened_now

    def is_open(self, loop: Loop, now: float | None = None) -> bool:
        failures, opened_at = self.store.get_breaker(loop)
        if opened_at is None:
            return False
        if loop == "jev" and (time.time() if now is None else now) - opened_at >= self.jev_cooldown_s:
            self.store.set_breaker(loop, failures=0, opened_at=None)
            return False
        return True

    def reset(self, loop: str) -> None:
        for name in LOOPS if loop == "all" else (loop,):
            self.store.set_breaker(name, failures=0, opened_at=None)


class CycleCaps:
    """Sliding per-cycle counters, persisted so a restart cannot reset a thrash."""

    def __init__(self, store: LocalStore, cycle_s: int):
        self.store = store
        self.cycle_s = cycle_s

    def _window(self, now: float) -> int:
        return int(now // self.cycle_s)

    def take(self, key: str, limit: int, now: float | None = None) -> bool:
        """Consume one unit of `key` in the current cycle; False when at cap."""
        window = self._window(time.time() if now is None else now)
        used = self.store.get_counter(key, window)
        if used >= limit:
            return False
        self.store.set_counter(key, window, used + 1)
        return True


class ShutdownHandler:
    """SIGTERM/SIGINT finish the current step, then the daemon exits cleanly."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._event.set())

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> None:
        self._event.set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; returns True early when shutdown is requested."""
        return self._event.wait(seconds)
