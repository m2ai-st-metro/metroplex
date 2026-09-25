"""Metroplex-local state: cards, breakers, counters, pause flag, bot offset.

None of this is work state. Teletraan owns tasks, projects, grants and wakes;
losing this file loses pending cards and loop counters, never work.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  card_id TEXT PRIMARY KEY, card TEXT NOT NULL, card_hash TEXT NOT NULL,
  status TEXT NOT NULL, source TEXT, question TEXT, project_id TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS breakers(loop TEXT PRIMARY KEY, failures INTEGER NOT NULL, opened_at REAL);
CREATE TABLE IF NOT EXISTS counters(key TEXT NOT NULL, window INTEGER NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(key, window));
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

CARD_STATES = ("drafting", "awaiting_yes", "granted", "dropped", "expired")


class LocalStore:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # cards ---------------------------------------------------------------
    def put_card(self, card_id: str, card: dict[str, Any], card_hash: str, status: str, source: str | None = None, question: str | None = None) -> None:
        if status not in CARD_STATES:
            raise ValueError(status)
        now = time.time()
        self.db.execute(
            "INSERT INTO cards VALUES(?,?,?,?,?,?,NULL,?,?) ON CONFLICT(card_id) DO UPDATE SET card=excluded.card, card_hash=excluded.card_hash, status=excluded.status, question=excluded.question, updated_at=excluded.updated_at",
            (card_id, json.dumps(card), card_hash, status, source, question, now, now),
        )

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT card_id, card, card_hash, status, source, question, project_id, created_at FROM cards WHERE card_id=?", (card_id,)).fetchone()
        if not row:
            return None
        return {"card_id": row[0], "card": json.loads(row[1]), "card_hash": row[2], "status": row[3], "source": row[4], "question": row[5], "project_id": row[6], "created_at": row[7]}

    def set_card_status(self, card_id: str, status: str, project_id: str | None = None) -> None:
        if status not in CARD_STATES:
            raise ValueError(status)
        self.db.execute("UPDATE cards SET status=?, project_id=COALESCE(?, project_id), updated_at=? WHERE card_id=?", (status, project_id, time.time(), card_id))

    def cards_in(self, status: str) -> list[dict[str, Any]]:
        ids = [r[0] for r in self.db.execute("SELECT card_id FROM cards WHERE status=? ORDER BY created_at", (status,))]
        return [c for c in (self.get_card(i) for i in ids) if c]

    # breakers / counters -------------------------------------------------
    def get_breaker(self, loop: str) -> tuple[int, float | None]:
        row = self.db.execute("SELECT failures, opened_at FROM breakers WHERE loop=?", (loop,)).fetchone()
        return (row[0], row[1]) if row else (0, None)

    def set_breaker(self, loop: str, failures: int, opened_at: float | None) -> None:
        self.db.execute("INSERT INTO breakers VALUES(?,?,?) ON CONFLICT(loop) DO UPDATE SET failures=excluded.failures, opened_at=excluded.opened_at", (loop, failures, opened_at))

    def get_counter(self, key: str, window: int) -> int:
        row = self.db.execute("SELECT used FROM counters WHERE key=? AND window=?", (key, window)).fetchone()
        return row[0] if row else 0

    def set_counter(self, key: str, window: int, used: int) -> None:
        self.db.execute("INSERT INTO counters VALUES(?,?,?) ON CONFLICT(key, window) DO UPDATE SET used=excluded.used", (key, window, used))
        self.db.execute("DELETE FROM counters WHERE key=? AND window<?", (key, window - 1))

    # kv ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    @property
    def paused(self) -> bool:
        return bool(self.get("paused", False))

    def set_paused(self, value: bool, reason: str | None = None) -> None:
        self.set("paused", value)
        self.set("pause_reason", reason)
