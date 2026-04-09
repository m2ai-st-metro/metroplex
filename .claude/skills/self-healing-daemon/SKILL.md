---
name: self-healing-daemon
description: >
  Long-running daemon loop that processes Metroplex build jobs through the
  /self-healing-pipeline Planner/Builder/Judge skill, one at a time, inside a
  single interactive Claude Code session. Avoids headless boot tax by paying
  it once at session startup rather than per build. Use when invoked as
  `/self-healing-daemon start`, `/self-healing-daemon status`, or
  `/self-healing-daemon stop`. Project-scoped to Metroplex — only loads when
  claude is started from /home/apexaipc/projects/metroplex/.
user_invocable: true
---

# Self-Healing Daemon

A queue-watcher loop that dispatches Metroplex build jobs into the
`/self-healing-pipeline` skill from a persistent interactive Claude Code
session. The session pays boot tax once at startup and processes many builds
before it needs to restart, keeping billing on Max base subscription instead
of extra usage credits.

## Trigger

- `/self-healing-daemon start` -- begin processing the queue
- `/self-healing-daemon status` -- report heartbeat age, in-flight job, queue depth
- `/self-healing-daemon stop` -- write the shutdown flag and exit cleanly

## Paths

All paths are absolute. Do not use `cd` (blocked by hook).

```
QUEUE_ROOT     = /home/apexaipc/projects/metroplex/data/self_healing_queue
PENDING_DIR    = $QUEUE_ROOT/pending
IN_FLIGHT_DIR  = $QUEUE_ROOT/in_flight/worker-1
COMPLETED_DIR  = $QUEUE_ROOT/completed
FAILED_DIR     = $QUEUE_ROOT/failed
HEARTBEAT      = $QUEUE_ROOT/heartbeat-worker-1.txt
SHUTDOWN_FLAG  = $QUEUE_ROOT/shutdown.flag
WORKSPACE_ROOT = /home/apexaipc/projects/metroplex/data/self_healing_workspaces
```

## Job file schema

Each file in `PENDING_DIR` is a JSON document:

```json
{
  "job_id": "metroplex-ideaforge-199",
  "target_dir": "/absolute/path/to/workspace",
  "spec_path": "/absolute/path/to/workspace/spec.md",
  "model": "opus",
  "queued_at": "2026-04-08T21:00:00+00:00"
}
```

The adapter writes these atomically (tmp + rename). Trust the file once it
appears in `PENDING_DIR`.

## Startup (on `/self-healing-daemon start`)

1. Ensure directories exist: `PENDING_DIR`, `IN_FLIGHT_DIR`, `COMPLETED_DIR`, `FAILED_DIR`.
2. Remove any stale `SHUTDOWN_FLAG` left over from a previous run.
3. Touch `HEARTBEAT` (write current timestamp).
4. Print: `"Self-healing daemon started as worker-1. Queue: $QUEUE_ROOT"`.
5. Enter the main loop.

## Main loop

Repeat until `SHUTDOWN_FLAG` is present:

1. **Heartbeat**: touch `HEARTBEAT` (updates mtime so the adapter's `is_active()` check returns True).
2. **Shutdown check**: if `SHUTDOWN_FLAG` exists, remove it, print `"Shutting down cleanly"`, exit the loop.
3. **Scan queue**: list `PENDING_DIR/*.json` sorted by mtime ascending.
4. **Idle path**: if the list is empty, run `Bash: sleep 60` and continue the loop. Do not add commentary during idle iterations -- keep the conversation lean.
5. **Claim oldest job**:
   a. Read the oldest pending job file.
   b. Move it atomically from `PENDING_DIR/<job_id>.json` to `IN_FLIGHT_DIR/<job_id>.json` via `mv` (Bash tool, absolute paths).
   c. Print `"Processing job {job_id}: spec={spec_path}, target={target_dir}"`.
6. **Dispatch**: invoke `/self-healing-pipeline` with this task description:
   > Build the spec at `{spec_path}` into `{target_dir}`. Use the Planner/Builder/Judge loop with 3 attempts max. Write all state to `{target_dir}/.self-healing-pipeline/state.json`.
7. **Auto-approve plan gate**: when `/self-healing-pipeline` prompts `"Plan looks good? Say 'go' to start building"`, respond `go` automatically. The daemon does not pause for human review -- Metroplex owns the human review gate separately. If the planner output looks catastrophically wrong, print a warning and respond `go` anyway. The Judge will catch real failures downstream.
8. **Wait for terminal state**: control returns from `/self-healing-pipeline` only when the loop reaches `passed` or `escalated`. No polling needed.
9. **Read final state**: load `{target_dir}/.self-healing-pipeline/state.json`, read the `status` field.
10. **Route to terminal dir**:
    - If `status == "passed"`: move the in-flight job file to `COMPLETED_DIR/<job_id>.json`.
    - If `status == "escalated"` or any other terminal failure: move to `FAILED_DIR/<job_id>.json`.
11. **Log result**: print `"Job {job_id} finished: status={passed|escalated}, attempts={N}"`.
12. **Touch heartbeat** again, loop.

## Status command (`/self-healing-daemon status`)

Report:
- Heartbeat age (seconds since `HEARTBEAT` was last touched -- use `stat` via Bash).
- Pending queue depth (`ls $PENDING_DIR | wc -l`).
- In-flight job (if any, read from `IN_FLIGHT_DIR`).
- Completed count today (`ls $COMPLETED_DIR | wc -l`).
- Failed count today (`ls $FAILED_DIR | wc -l`).

## Stop command (`/self-healing-daemon stop`)

1. Create `SHUTDOWN_FLAG` by touching it.
2. Print: `"Shutdown requested. Daemon will exit after the current job finishes (or immediately if idle)."`
3. Do NOT kill in-flight builds. Let them complete so the state.json is consistent.
4. From a SEPARATE terminal, you can also trigger shutdown by running:
   ```
   touch /home/apexaipc/projects/metroplex/data/self_healing_queue/shutdown.flag
   ```

## Operational discipline

- **Restart daily**. This session pays boot tax once at startup and accumulates
  parent-session context at roughly 5-10k tokens per build. A fresh session
  each morning keeps latency predictable and scrollback clean. Past ~20 builds
  in a 200k session or ~100 in a 1M session, Claude Code auto-compaction
  fires. Compaction is safe but adds latency to the build mid-flight.

- **Do NOT chat with this session while the daemon is running**. The Claude
  mobile app can watch this session via remote control (green dot in the Code
  section), but prompts typed into it will interrupt the loop and eat context.
  For interactive work, open a different session.

- **One daemon per session**. Worker IDs exist for future multi-daemon support
  but only `worker-1` is wired today. Do not start a second daemon in the
  same Claude session.

- **Project-scoped**. This skill only loads when `claude` is started from
  `/home/apexaipc/projects/metroplex/`. Starting from a different directory
  will silently not load it.

- **Spec validation**. If a job file is malformed (missing `target_dir` or
  `spec_path`, or those paths do not exist on disk), move the job file to
  `FAILED_DIR/<job_id>.json`, write an `escalated` state.json with an
  `escalation_reason` of `"daemon: malformed job file"`, and continue the loop.
  Do NOT crash the daemon on a single bad job.

- **Filesystem is the source of truth**. Do not rely on parent-session memory
  of "what did I do earlier." Every loop iteration reads fresh from
  `PENDING_DIR`. Auto-compaction cannot cause the daemon to skip or repeat a
  job, because state lives in the filesystem.

## What this skill does NOT do

- It does NOT poll Metroplex's SQLite DB directly. Metroplex writes job files;
  the daemon consumes job files. The adapter mediates.
- It does NOT spawn parallel builds. One build at a time per daemon.
- It does NOT handle catastrophic Claude session crashes. If the session dies,
  Metroplex will detect a stale heartbeat and pause build dispatch. Restart
  the daemon manually.
- It does NOT escalate to humans via Telegram or other channels. Escalations
  are written to `FAILED_DIR` and picked up by Metroplex's existing
  postmortem/notification paths.
