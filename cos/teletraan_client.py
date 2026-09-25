"""Client for the Teletraan work service (JSON over a Unix socket, half-closed).

Mirrors teletraan src/api/work-client.mjs: one request per connection, the
token travels in the body, the response is {ok, result} or {ok: false, code}.
The principal is bound server-side from the token; this client never sends one.
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any

MAX_RESPONSE = 10_000_000


class WorkError(Exception):
    """A Teletraan rejection. `code` is the domain error (e.g. STALE_WAKE)."""

    def __init__(self, code: str, current: Any = None, command_rejected: bool = False):
        super().__init__(code)
        self.code = code
        self.current = current
        # True only for a rolled-back domain rejection: proof of no effect.
        self.command_rejected = command_rejected


class TeletraanClient:
    def __init__(self, socket_path: Path, token: str, timeout_s: float = 15.0):
        if not token:
            raise ValueError("COS_TOKEN_REQUIRED")
        self.socket_path = Path(socket_path)
        self._token = token
        self.timeout_s = timeout_s

    @classmethod
    def from_token_file(cls, socket_path: Path, token_file: Path) -> "TeletraanClient":
        return cls(socket_path, Path(token_file).read_text().strip())

    def call(self, request: dict[str, Any]) -> Any:
        payload = json.dumps({**request, "token": self._token}).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout_s)
            s.connect(str(self.socket_path))
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            chunks, size = [], 0
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise WorkError("RESPONSE_TOO_LARGE")
                chunks.append(chunk)
        response = json.loads(b"".join(chunks) or b"{}")
        if not response.get("ok"):
            raise WorkError(response.get("code") or "WORK_SERVICE_ERROR", response.get("current"), bool(response.get("commandRejected")))
        return response.get("result")

    # Convenience wrappers -------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return self.call({"action": "snapshot"})

    def wake_pending(self) -> dict[str, Any]:
        return self.call({"action": "wake_pending"})

    def history(self, task_id: str) -> list[dict[str, Any]]:
        return self.call({"action": "history", "taskId": task_id})

    def command(self, operation: str, object_id: str, expected_revision: int, payload: dict[str, Any] | None = None, command_id: str | None = None) -> dict[str, Any]:
        """Issue one command. Pass a deterministic command_id when a retry after a
        crash must replay the same receipt instead of creating a second effect."""
        return self.call({"action": "command", "command": {
            "commandId": command_id or str(uuid.uuid4()),
            "operation": operation,
            "id": object_id,
            "expectedRevision": expected_revision,
            "payload": payload or {},
        }})
