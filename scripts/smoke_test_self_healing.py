#!/usr/bin/env python3
"""End-to-end smoke test for the SelfHealingAdapter <-> self-healing-daemon seam.

WHAT THIS DOES
--------------
Queues a trivial, hand-crafted calculator spec through
`SelfHealingAdapter.queue()` and then polls `SelfHealingAdapter.poll()` until
the job reaches a terminal state (completed / failed) or a wall-clock timeout
fires. Prints a SUCCESS / FAILED / TIMEOUT / DAEMON-DOWN block and exits
with a distinct code so an operator (or a future cron) can tell at a glance
whether the live seam is healthy.

This is NOT a unit test. It exercises the real daemon skill, the real file
queue under `data/self_healing_queue/`, and the real workspace root under
`data/self_healing_workspaces/`. Unit coverage of the adapter lives in
`tests/test_self_healing_adapter.py`; deterministic seam coverage lives in
`tests/test_self_healing_integration.py`.

PRECONDITIONS
-------------
- The metroplex venv is active.
- The self-healing daemon is already running in a SEPARATE terminal. Start
  it with:
      (cd /home/apexaipc/projects/metroplex && claude)
  and then type `/self-healing-daemon start` inside that session.
- The daemon heartbeat file under
  `data/self_healing_queue/heartbeat-worker-1.txt` is fresh (<120s old).

WHY PRODUCTION PATHS
--------------------
The `self-healing-daemon` skill hardcodes its queue and workspace roots to
the live metroplex paths. This driver uses a job id of the form
`smoke-test-<epoch>` so it cannot collide with a real build. On both success
AND failure the workspace directory is PRESERVED so an operator can inspect
the generated code, `state.json`, and any Planner/Builder/Judge artifacts.

RECOVERY NOTE
-------------
If this driver is Ctrl-C'd or crashes mid-poll, the daemon keeps running and
the job state lives entirely on the filesystem. You can inspect it via
`/self-healing-daemon status` in the daemon session, or by looking at the
`pending/`, `in_flight/worker-1/`, `completed/`, and `failed/` subdirs of
`data/self_healing_queue/`. The driver only removes the pending queue file
on Ctrl-C IF the daemon has not yet picked it up; it never touches the
workspace directory.

EXIT CODES
----------
    0 passed       -- job reached status=completed
    1 failed       -- job reached status=failed (or spec missing, or
                      adapter.queue itself returned status=failed)
    2 timeout      -- wall-clock timeout expired before terminal state
    3 daemon down  -- heartbeat stale at start, or went stale mid-poll
  130 interrupted  -- driver received SIGINT
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Make the metroplex repo root importable whether the script is run directly
# (`python scripts/smoke_test_self_healing.py`) or loaded by importlib from
# the test suite. Both `config` and `adapters` live at the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.self_healing_adapter import SelfHealingAdapter  # noqa: E402
from config import Config  # noqa: E402


DEFAULT_TIMEOUT_SECONDS = 900  # 15 min
POLL_INTERVAL_SECONDS = 10
LIVENESS_CHECK_INTERVAL_SECONDS = 60
DEFAULT_SPEC_PATH = Path(__file__).resolve().parent / "fixtures" / "smoke_test_spec.md"

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_DAEMON_DOWN = 3
EXIT_INTERRUPTED = 130

_SEPARATOR = "====================================================="


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="End-to-end smoke test for the self-healing daemon seam."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Wall-clock timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=DEFAULT_SPEC_PATH,
        help="Path to the spec file to queue (default: bundled calculator fixture).",
    )
    return parser.parse_args()


def _validate_spec_path(path: Path) -> Path:
    """Return the absolute spec path, or exit(EXIT_FAILED) with a clear error."""
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        print(f"ERROR: spec file not found: {resolved}")
        sys.exit(EXIT_FAILED)
    if not resolved.is_file():
        print(f"ERROR: spec path is not a file: {resolved}")
        sys.exit(EXIT_FAILED)
    return resolved


def _build_config() -> Config:
    """Construct Config the same way metroplex.py does: bare instantiation."""
    return Config()


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    minutes = total // 60
    secs = total % 60
    return f"{minutes}m {secs}s"


def _print_start_banner(
    job_id: str,
    spec_path: Path,
    timeout: int,
    adapter: SelfHealingAdapter,
) -> None:
    print(_SEPARATOR)
    print("SELF-HEALING DAEMON SMOKE TEST")
    print(f"  job_id:          {job_id}")
    print(f"  spec_path:       {spec_path}")
    print(f"  timeout:         {timeout}s ({_format_elapsed(timeout)})")
    print(f"  queue_root:      {adapter.queue_root.resolve()}")
    print(f"  workspace_root:  {adapter.workspace_root.resolve()}")
    print(_SEPARATOR)


def _print_success_block(
    job_id: str,
    target_dir: Path,
    job_data: dict[str, Any],
    elapsed: float,
) -> int:
    state_file = (target_dir / ".self-healing-pipeline" / "state.json").resolve()
    print(_SEPARATOR)
    print("SMOKE TEST PASSED")
    print(f"  job_id:          {job_id}")
    print(f"  elapsed:         {_format_elapsed(elapsed)}")
    print(f"  attempts:        {job_data.get('attempt')}")
    print(f"  target_dir:      {target_dir.resolve()}")
    print(f"  state.json:      {state_file}")
    print(f"  judge_verdict:   {job_data.get('judge_verdict')}")
    print()
    print(f"Inspect built code at: {target_dir.resolve()}")
    print(_SEPARATOR)
    return EXIT_PASSED


def _print_failure_block(
    job_id: str,
    target_dir: Path,
    job_data: dict[str, Any],
    elapsed: float,
    adapter: SelfHealingAdapter,
) -> int:
    state_file = (target_dir / ".self-healing-pipeline" / "state.json").resolve()
    pending_file = (adapter.pending_dir / f"{job_id}.json").resolve()
    failed_file = (adapter.queue_root / "failed" / f"{job_id}.json").resolve()
    escalation_reason = job_data.get("escalation_reason") or "(not in poll output)"
    print(_SEPARATOR)
    print("SMOKE TEST FAILED (escalated)")
    print(f"  job_id:              {job_id}")
    print(f"  elapsed:             {_format_elapsed(elapsed)}")
    print(f"  attempts:            {job_data.get('attempt')}")
    print(f"  escalation_reason:   {escalation_reason}")
    print(f"  target_dir:          {target_dir.resolve()}   <-- inspect for debugging")
    print(f"  state.json:          {state_file}")
    print(f"  pending_queue_file:  {pending_file}")
    print(f"  failed_queue_file:   {failed_file}")
    print(_SEPARATOR)
    return EXIT_FAILED


def _print_timeout_block(
    job_id: str,
    target_dir: Path,
    adapter: SelfHealingAdapter,
    elapsed: float,
) -> int:
    state_file = target_dir / ".self-healing-pipeline" / "state.json"
    pending_file = (adapter.pending_dir / f"{job_id}.json").resolve()
    in_flight_file = (
        adapter.queue_root / "in_flight" / "worker-1" / f"{job_id}.json"
    ).resolve()

    # Pull latest status snapshot from poll() so the operator sees where it stalled.
    poll = adapter.poll()
    job_data = next((j for j in poll["jobs"] if j["job_id"] == job_id), None) or {}
    status = job_data.get("status")
    sh_state = job_data.get("self_healing_state")
    attempt = job_data.get("attempt")

    print(_SEPARATOR)
    print(f"SMOKE TEST TIMED OUT ({int(elapsed)} seconds)")
    print(f"  current_status:         {status}")
    print(f"  current_self_healing:   {sh_state}")
    print(f"  current_attempt:        {attempt}")
    print(f"  target_dir:             {target_dir.resolve()}")
    print("  state.json contents:")
    if state_file.exists():
        try:
            parsed = json.loads(state_file.read_text())
            pretty = json.dumps(parsed, indent=2)
        except (json.JSONDecodeError, OSError) as e:
            pretty = f"(could not parse state.json: {e})"
        for line in pretty.splitlines():
            print(f"    {line}")
    else:
        print("    (file does not exist)")
    print(f"  pending_queue_file:     {pending_file}")
    print(f"  in_flight_queue_file:   {in_flight_file}")
    print()
    print("Daemon has NOT been interrupted. Check /self-healing-daemon status")
    print("in the daemon session, or re-run with a larger --timeout.")
    print(_SEPARATOR)
    return EXIT_TIMEOUT


def _print_daemon_died_block(
    job_id: str,
    target_dir: Path,
    elapsed: float,
) -> int:
    print(_SEPARATOR)
    print("DAEMON DIED MID-BUILD (heartbeat went stale)")
    print(f"  job_id:      {job_id}")
    print(f"  elapsed:     {_format_elapsed(elapsed)}")
    print(f"  target_dir:  {target_dir.resolve()}   <-- inspect for postmortem")
    print()
    print("The daemon has crashed or been stopped. Re-start it from the daemon")
    print("session, then inspect the job's last state.json for diagnostics.")
    print(_SEPARATOR)
    return EXIT_DAEMON_DOWN


def _print_daemon_down_precondition(adapter: SelfHealingAdapter) -> None:
    print("ERROR: Daemon heartbeat is stale or missing.")
    print()
    print("Start it in a separate terminal with:")
    print("    (cd /home/apexaipc/projects/metroplex && claude)")
    print("Then type: /self-healing-daemon start")
    print()
    print(f"Queue root: {adapter.queue_root.resolve()}")


def _handle_abort(
    job_id: str,
    pending_file: Path,
    target_dir: Path,
) -> int:
    """Clean up the pending queue entry if possible, print a recovery note,
    and return EXIT_INTERRUPTED. The workspace is intentionally preserved."""
    if pending_file.exists():
        try:
            pending_file.unlink()
            print(f"Removed pending queue file: {pending_file.resolve()}")
        except OSError as e:
            print(f"Could not remove pending queue file {pending_file}: {e}")
    else:
        print("Pending queue file already claimed by daemon; leaving in-flight job alone.")
    print(f"Workspace preserved at: {target_dir.resolve()}")
    print("Daemon has NOT been interrupted.")
    return EXIT_INTERRUPTED


def main() -> int:
    args = _parse_args()
    spec_path = _validate_spec_path(args.spec)
    config = _build_config()
    # KEEP THIS ONE ADAPTER INSTANCE ALIVE FOR THE FULL RUN.
    # SelfHealingAdapter.poll() only returns jobs from the in-memory `_jobs`
    # dict that queue() populates. Reconstructing the adapter after queue()
    # makes poll() return {"jobs": []} forever.
    adapter = SelfHealingAdapter(config)

    if not adapter.is_active():
        _print_daemon_down_precondition(adapter)
        return EXIT_DAEMON_DOWN

    job_id = f"smoke-test-{int(time.time())}"
    target_dir = adapter.workspace_root / job_id
    pending_file = adapter.pending_dir / f"{job_id}.json"

    _print_start_banner(job_id, spec_path, args.timeout, adapter)

    interrupted = {"flag": False}

    def _handle_sigint(signum, frame):  # noqa: ARG001
        interrupted["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)

    result = adapter.queue(spec_path=spec_path, job_id=job_id, model="opus")
    if result.status == "failed":
        print(f"ERROR: adapter.queue returned status=failed: {result.error}")
        return EXIT_FAILED

    print("Queued.")
    print(f"  Job file:        {pending_file.resolve()}")
    print(f"  Target workspace: {target_dir.resolve()}")
    print()

    start = time.monotonic()
    last_tuple: tuple[Any, Any, Any] = (None, None, None)
    last_liveness = start

    while True:
        if interrupted["flag"]:
            return _handle_abort(job_id, pending_file, target_dir)

        elapsed = time.monotonic() - start
        if elapsed > args.timeout:
            return _print_timeout_block(job_id, target_dir, adapter, elapsed)

        now = time.monotonic()
        if now - last_liveness >= LIVENESS_CHECK_INTERVAL_SECONDS:
            if not adapter.is_active():
                return _print_daemon_died_block(job_id, target_dir, elapsed)
            last_liveness = now

        poll = adapter.poll()
        job_data = next(
            (j for j in poll["jobs"] if j["job_id"] == job_id), None
        )
        if job_data is None:
            print(f"WARNING: poll() returned no entry for {job_id}. Retrying.")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        status = job_data["status"]
        sh_state = job_data.get("self_healing_state")
        attempt = job_data.get("attempt")
        current = (status, sh_state, attempt)

        if current != last_tuple:
            ts = time.strftime("%H:%M:%S")
            print(
                f"[{ts}] {job_id}: status={status} "
                f"self_healing={sh_state} attempt={attempt}"
            )
            last_tuple = current

        if status == "completed":
            return _print_success_block(job_id, target_dir, job_data, elapsed)
        if status == "failed":
            return _print_failure_block(
                job_id, target_dir, job_data, elapsed, adapter
            )

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
