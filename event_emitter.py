"""
Sky-Lynx Event Emitter - Phase F
Writes pipeline events as JSON files for Sky-Lynx reactive triggers.
Uses atomic file writes (temp + rename) for crash safety.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_EVENTS_DIR = Path.home() / ".local" / "share" / "skylynx-events"


class EventEmitter:
    """Emits pipeline events as JSON files for Sky-Lynx consumption."""

    def __init__(self, events_dir: Path | None = None):
        self.events_dir = events_dir or Path(
            os.environ.get("SKYLYNX_EVENTS_DIR", str(DEFAULT_EVENTS_DIR))
        )
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, details: dict) -> Path | None:
        """Write an event file atomically. Returns the path, or None on failure."""
        event = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "metroplex",
            "details": details,
        }
        filename = f"{time.time_ns()}.json"
        target = self.events_dir / filename
        tmp = self.events_dir / f".{filename}.tmp"
        try:
            tmp.write_text(json.dumps(event))
            tmp.rename(target)
            logger.info("Emitted event %s -> %s", event_type, filename)
            return target
        except Exception as e:
            logger.warning("Failed to emit event %s: %s", event_type, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None


def create_event_emitter() -> EventEmitter:
    """Factory function matching the pattern used by create_outcome_emitter."""
    return EventEmitter()
