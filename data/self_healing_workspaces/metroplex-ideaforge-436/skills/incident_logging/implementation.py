"""Incident-logging persistence.

Every classification decision is durable. Confirmed incidents are
append-only JSON lines in ``incidents.jsonl``; silent drops (negated,
no_match, compound_needs_context) go in ``silent_drops.jsonl``. Both
files live under the agent's runtime ``data/`` directory.

Spec source: see ``spec-claims.md`` C-15 (silent-drop audit trail) and
C-36 (write-then-read verification before any "logged" confirmation).
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _serialize(record: dict[str, Any]) -> str:
    def _default(o: Any) -> Any:
        if is_dataclass(o):
            return asdict(o)
        if isinstance(o, set):
            return sorted(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return str(o)

    return json.dumps(record, default=_default)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_incident(record: dict[str, Any], data_dir: Path) -> Path:
    """Append ``record`` to ``incidents.jsonl`` with write-then-read verification.

    Returns the path of the written file. Raises ``IOError`` if the
    record cannot be read back identically — caller is expected to treat
    that as a failed log and NOT report "logged" to the user.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "incidents.jsonl"
    record = {**record, "logged_at": _utcnow_iso()}
    line = _serialize(record)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
    # Write-then-read verification: re-read the last line and confirm it
    # round-trips to the same dict.
    last = target.read_text(encoding="utf-8").splitlines()[-1]
    parsed = json.loads(last)
    if parsed.get("raw_input") != record.get("raw_input"):
        raise IOError("incident write-then-read verification failed")
    return target


def list_incidents(data_dir: Path) -> list[dict[str, Any]]:
    target = data_dir / "incidents.jsonl"
    if not target.exists():
        return []
    return [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]


def log_silent_drop(reason: str, raw_input: str, data_dir: Path) -> Path:
    """Append a silent-drop audit row to ``silent_drops.jsonl``.

    The caller passes the reason emitted by IncidentMatcher
    (``negated`` | ``no_match`` | ``compound_needs_context`` | ``empty``).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "silent_drops.jsonl"
    record = {
        "reason": reason,
        "raw_input": raw_input,
        "logged_at": _utcnow_iso(),
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(_serialize(record) + "\n")
        fh.flush()
    return target
