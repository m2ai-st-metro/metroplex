"""Shared fixtures. Integration tests run against a real Teletraan work service
(pilot branch) on a temp socket; they skip when node or the checkout is absent."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos.store import LocalStore  # noqa: E402
from cos.teletraan_client import TeletraanClient  # noqa: E402

TELETRAAN_ROOT = Path(os.environ.get("METROPLEX_TELETRAAN_ROOT", str(Path.home() / "projects/worktrees/teletraan-continuity-pair")))
MATTHEW = "7001"


@pytest.fixture
def store():
    s = LocalStore(":memory:")
    yield s
    s.close()


class Work:
    """Handle to a live test Teletraan with one client per principal."""

    def __init__(self, socket: Path, tokens: dict[str, str]):
        self.socket = socket
        self.clients = {name: TeletraanClient(socket, token) for name, token in tokens.items()}

    def __getitem__(self, principal: str) -> TeletraanClient:
        return self.clients[principal]

    def rev(self, kind: str, object_id: str) -> int:
        row = next((o for o in self["operator"].snapshot().get(kind, []) if o["id"] == object_id), None)
        return row["revision"] if row else 0

    def cmd(self, principal: str, op: str, object_id: str, payload: dict | None = None, kind: str | None = None):
        return self[principal].command(op, object_id, self.rev(kind or op.split(".")[0], object_id), payload or {})

    def get(self, kind: str, object_id: str):
        return next((o for o in self["operator"].snapshot().get(kind, []) if o["id"] == object_id), None)


@pytest.fixture
def work(tmp_path):
    node = shutil.which("node")
    if not node or not (TELETRAAN_ROOT / "src/domain/work.mjs").exists():
        pytest.skip("Teletraan pilot checkout or node not available")
    proc = subprocess.Popen([node, str(ROOT / "tests/fixtures/work_service.mjs"), str(TELETRAAN_ROOT), str(tmp_path), MATTHEW], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    if not line:
        proc.kill()
        pytest.fail("work service did not start: " + proc.stderr.read())
    ready = json.loads(line)
    w = Work(Path(ready["socket"]), ready["tokens"])
    for agent in ("owner", "worker"):
        w.cmd("operator", "agent.create", agent, {"name": agent, "capabilities": ["code"] if agent == "worker" else ["lead"]})
    yield w
    proc.terminate()
    proc.wait(timeout=10)
