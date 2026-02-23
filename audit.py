"""
Metroplex Audit Logger
Structured JSON logging for all decisions, errors, and cycle events.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


class AuditLogger:
    """Structured audit logger for Metroplex decisions and events."""

    def __init__(self, log_path: str = "data/decisions.log"):
        """Initialize audit logger."""
        self.log_path = Path(log_path)

        # Create data directory if needed
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_log(self, gate: str, action: str, details: Dict[str, Any]):
        """Write a log entry as JSON line."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "gate": gate,
            "action": action,
            "details": details
        }

        with open(self.log_path, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

    def log_decision(self, gate: str, action: str, details: Dict[str, Any]):
        """Log a decision (approve, reject, defer, etc.)."""
        self._write_log(gate, action, details)

    def log_error(self, gate: str, error: str, details: Dict[str, Any] | None = None):
        """Log an error."""
        error_details = {"error": error}
        if details:
            error_details.update(details)
        self._write_log(gate, "error", error_details)

    def log_cycle_start(self, cycle_id: str):
        """Log cycle start."""
        self._write_log("cycle", "start", {"cycle_id": cycle_id})

    def log_cycle_end(self, cycle_id: str, triage_count: int, build_count: int, patch_count: int, errors: list[str]):
        """Log cycle end."""
        self._write_log("cycle", "end", {
            "cycle_id": cycle_id,
            "triage_count": triage_count,
            "build_count": build_count,
            "patch_count": patch_count,
            "errors": errors
        })
