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

## Startup command (CRITICAL)

**The Claude Code session that hosts this daemon MUST be started with
`--dangerously-skip-permissions` (alias `dsp`).** Without it, the daemon's
Planner/Builder/Judge sub-agents will hit permission prompts on tool calls
they haven't been pre-approved for, and the loop will block indefinitely
waiting for a human to click through. There is no human in the loop at
runtime -- that is the entire point of the daemon.

```bash
# Correct: daemon runs unattended
(cd /home/apexaipc/projects/metroplex && claude --dangerously-skip-permissions)

# WRONG: will stall on the first permission prompt mid-build
(cd /home/apexaipc/projects/metroplex && claude)
```

**Safety net**: the project-scoped
`/home/apexaipc/projects/metroplex/.claude/settings.local.json` contains a
`permissions.deny` list that blocks destructive patterns (`rm -rf /`,
`mkfs`, `dd of=/dev/...`, `sudo`, `shutdown`, etc.) even under
`--dangerously-skip-permissions`. Deny rules always win over skip. If you
add new dangerous patterns, update that file first.

If the daemon starts issuing tool calls outside its expected profile (see
"Expected tool-use profile" below), stop the daemon and investigate before
relaunching -- `dsp` means you are trusting the daemon's own judgment, so
the scrollback is your audit log.

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
4. **Idle path**: if the list is empty, enter a chunked idle wait instead of a single long `sleep 60`. Loop six times:
   - Touch `HEARTBEAT`
   - Run `Bash: sleep 10` (foreground, NOT `run_in_background: true` -- see "Hook interactions" below for why)
   - Check `SHUTDOWN_FLAG`: if present, remove it, print `"Shutting down cleanly"`, exit the entire main loop immediately
   - Check `PENDING_DIR` again: if a new job appeared, break out of the idle wait and continue the main loop from Step 5
   After six iterations (~60s elapsed) fall through and continue the main loop. Do not add commentary during idle iterations -- keep the conversation lean.
5. **Claim oldest job**:
   a. Read the oldest pending job file.
   b. Move it atomically from `PENDING_DIR/<job_id>.json` to `IN_FLIGHT_DIR/<job_id>.json` via `mv` (Bash tool, absolute paths). Then **`touch` the in_flight file** to refresh its mtime to claim-time -- `mv` preserves the original queued_at mtime, which causes the daemon health monitor's `oldest_in_flight_age` (which reads file mtime, `scripts/monitor_daemon.sh:33-44`) to fire spurious STUCK warnings whenever a queue backlog exists (confirmed 2026-05-04 build 365: queued at 15:28:26, monitor reported 2195s "stuck" at 16:05:02 -- which is exactly 2196s since queue write, despite the daemon having only just claimed it). The touch makes mtime mean "time since claim", which is the signal the monitor actually wants.
   c. **Write heartbeat callback**: write `{target_dir}/.heartbeat-callback` containing the single line `HEARTBEAT` (absolute path, no trailing newline-only content). The `/self-healing-pipeline` skill reads this file and touches the path between Planner/Builder/Judge phase transitions so the adapter's `is_active()` stays green during long Builder runs. Without this file, heartbeat ages while the Builder is working and the adapter will mark the daemon stale (see "Heartbeat semantics during active builds" below).
   d. Print `"Processing job {job_id}: spec={spec_path}, target={target_dir}"`.
6. **Dispatch**: invoke `/self-healing-pipeline` with this task description:
   > Build the spec at `{spec_path}` into `{target_dir}`. Use the Planner/Builder/Judge loop with 3 attempts max. Write all state to `{target_dir}/.self-healing-pipeline/state.json`. A heartbeat callback file exists at `{target_dir}/.heartbeat-callback` -- touch the path it contains via Bash at the start of each phase (Planner, each Builder attempt, each Judge attempt) so the daemon liveness check stays green.
7. **Auto-approve plan gate**: when `/self-healing-pipeline` prompts `"Plan looks good? Say 'go' to start building"`, respond `go` automatically. The daemon does not pause for human review -- Metroplex owns the human review gate separately. If the planner output looks catastrophically wrong, print a warning and respond `go` anyway. The Judge will catch real failures downstream.
8. **Wait for terminal state**: control returns from `/self-healing-pipeline` only when the loop reaches `passed` or `escalated`. No polling needed.
9. **Read final state**: load `{target_dir}/.self-healing-pipeline/state.json`, read the `status` field.
10. **Workspace prep for downstream gates** (only on `status == "passed"`):
    Metroplex's Gate 4.5 (`gates/review.py`) checks the workspace for artifacts that YCE builds produced natively but the self-healing pipeline does not -- specifically a `README.md` at the workspace root and an initialized git repo. Before routing to `COMPLETED_DIR`, prep the workspace:
    a. Touch `HEARTBEAT`.
    b. If `{target_dir}/README.md` does not exist, write one derived from `{target_dir}/spec.md`. Minimum content: the spec's first heading as the README title and a "Generated by self-healing daemon" footer. If the spec has no heading, use the `job_id` as the title.
    c. If `{target_dir}/.git` does not exist, run (as absolute paths, no `cd`):
       ```
       git -C {target_dir} init -q
       git -C {target_dir} add -A
       git -C {target_dir} -c user.email=daemon@metroplex.local -c user.name="Self-Healing Daemon" commit -q -m "Initial build via self-healing daemon (job {job_id})"
       ```
    d. Touch `HEARTBEAT`.
    Skipping this step is how idea 194 hit `review_failed` on 2026-04-09 despite a clean `judge_verdict=pass` -- missing README and missing `.git` are hard fails in the review gate. On `escalated` status, skip workspace prep; the failure path does not need downstream artifacts.
10.5. **Ravage review** (only on `status == "passed"`):
    After workspace prep, run two code-quality review agents against the build output. These catch bugs, silent failures, and security issues that the Judge's test-based pass/fail cannot see. The daemon session has Agent tool access, so it can spawn sub-agents directly.
    a. Touch `HEARTBEAT`.
    a-bis. **Start heartbeat keep-alive for the parallel Agent block.** The two Agent tool calls in 10.5b run for 2-5 minutes during which the daemon's session is blocked waiting for results and cannot touch `HEARTBEAT` itself. Without keep-alive, the monitor sees stale heartbeat and flaps DEAD/RECOVERED (confirmed 2026-05-04: three flaps inside 30 minutes -- builds 322, 352, 365 -- because Step 10.5a's touch only buys ~120s before Ravage agents push past the threshold). Spawn a detached bash loop that touches `HEARTBEAT` every 60s, save its PID for the kill in 10.5h. Run via Bash tool with absolute paths:
       ```bash
       nohup bash -c 'while sleep 60; do touch /home/apexaipc/projects/metroplex/data/self_healing_queue/heartbeat-worker-1.txt; done' > /dev/null 2>&1 &
       echo $! > /home/apexaipc/projects/metroplex/data/self_healing_queue/.ravage_keepalive.pid
       ```
       This is option (b) from the diagnostic at `~/diagnostics/daemon-flapping-2026-05-04.md` -- option (a) (touches between Agent dispatch and return) is not possible because Claude Code dispatches tools in parallel within a single turn; the daemon's session is blocked during waits. The trade-off is that a hung daemon session would still appear alive via the keep-alive; this is acceptable because the realistic failure mode here is "session healthily waiting on Agent" not "session crashed."
    b. Spawn **both** review agents in parallel using the Agent tool (two Agent calls in a single response). For each agent, the prompt must specify the absolute `{target_dir}` path and instruct the agent to review all source files there (not a git diff -- this is a freshly generated project, so all files are "new"). Exclude `.git/`, `.self-healing-pipeline/`, `node_modules/`, `__pycache__/`, `venv/`, and `*.lock` files from review scope.
       - **Agent 1 -- code-reviewer** (`subagent_type: "pr-review-toolkit:code-reviewer"`): review for bugs, security vulnerabilities, and code quality issues. Ask it to report issues with confidence >= 80 only, grouped by Critical (90-100) and Important (80-89). Tell it the project has no CLAUDE.md guidelines -- judge purely on correctness and security.
       - **Agent 2 -- silent-failure-hunter** (`subagent_type: "pr-review-toolkit:silent-failure-hunter"`): review for empty catch blocks, swallowed errors, silent fallbacks, and missing error propagation. Ask it to report issues with severity CRITICAL, HIGH, or MEDIUM.
    c. When both agents return, write their combined output to `{target_dir}/.self-healing-pipeline/review-report.md` with sections `## Code Review` and `## Silent Failure Analysis`.
    d. Parse the results for **CRITICAL** issues: code-reviewer issues with confidence >= 90, OR silent-failure-hunter issues with severity CRITICAL. Count them.
    e. Update `{target_dir}/.self-healing-pipeline/state.json` with three new fields:
       - `review_verdict`: `"approved"` (0 critical issues) or `"rejected"` (1+ critical issues)
       - `review_critical_count`: integer count of critical issues found
       - `review_report_path`: absolute path to `review-report.md`
    f. If `review_verdict == "rejected"`: print `"Ravage review REJECTED: {N} critical issues found. See {review_report_path}"`. Override the routing status for Step 11 -- this build goes to `FAILED_DIR` even though the Judge passed it. Update `state.json` `status` to `"review_rejected"`.
    g. If `review_verdict == "approved"`: print `"Ravage review approved ({N} non-critical issues noted)"` where N is the total issue count minus critical.
    h. **Stop heartbeat keep-alive and refresh:**
       ```bash
       pid=$(cat /home/apexaipc/projects/metroplex/data/self_healing_queue/.ravage_keepalive.pid 2>/dev/null) && [ -n "$pid" ] && kill "$pid" 2>/dev/null
       rm -f /home/apexaipc/projects/metroplex/data/self_healing_queue/.ravage_keepalive.pid
       touch /home/apexaipc/projects/metroplex/data/self_healing_queue/heartbeat-worker-1.txt
       ```
    On `escalated` status, skip the review entirely (same as workspace prep).
11. **Route to terminal dir**:
    - If `status == "passed"`: move the in-flight job file to `COMPLETED_DIR/<job_id>.json`.
    - If `status == "review_rejected"`: move to `FAILED_DIR/<job_id>.json`. Metroplex will see this as a build failure and apply its normal postmortem/notification flow. The review report at `{target_dir}/.self-healing-pipeline/review-report.md` provides the failure context.
    - If `status == "escalated"` or any other terminal failure: move to `FAILED_DIR/<job_id>.json`.
12. **Log result**: print `"Job {job_id} finished: status={status}, attempts={N}"`. Include `review_verdict` in the log line when present.
13. **Touch heartbeat** again, loop.

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

**Stop gotcha (2026-04-09):** `/self-healing-daemon stop` typed into the
daemon session itself does not interrupt an in-progress `Bash: sleep N`
call. If the daemon is in the idle path of the main loop, the Claude
session will not accept keyboard input until the current tool call
finishes. With the pre-2026-04-09 `sleep 60` idle, that meant up to 60
seconds of waiting; the Step 4 rewrite (six x `sleep 10`) caps that to
~10 seconds. The reliable path is still:

```bash
# From any OTHER terminal (ProBook, Surface, mobile app -- not the daemon session)
touch /home/apexaipc/projects/metroplex/data/self_healing_queue/shutdown.flag
```

The daemon's main loop (Step 2) and idle path (Step 4) both check for
`SHUTDOWN_FLAG` on every iteration, so the daemon exits within one
`sleep 10` cycle of the flag appearing.

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

## Heartbeat semantics during active builds

`SelfHealingAdapter.is_active()` returns True if any
`heartbeat-*.txt` file in `QUEUE_ROOT` was touched within the last
`HEARTBEAT_STALE_SECONDS` (120s). Before 2026-04-09 this was only updated
at the top of the daemon's main loop, so during a long Builder run the
heartbeat would age past 120s even though the daemon was healthily
working -- idea 194's build saw 141s of heartbeat age mid-Builder on
2026-04-09 and looked like a hang.

The fix is the `.heartbeat-callback` file written in Step 5c. It contains
the absolute path to `HEARTBEAT` and is read by `/self-healing-pipeline`
between each phase (Planner, each Builder attempt, each Judge attempt).
As long as the pipeline honors the callback, heartbeat freshness tracks
phase transitions instead of the daemon's main-loop cycle.

If `is_active()` still flags stale during a real build, check:
1. Does `{target_dir}/.heartbeat-callback` exist? If not, Step 5c was
   skipped -- fix the daemon loop.
2. Is `{target_dir}/.self-healing-pipeline/state.json` fresh (mtime <
   2 min)? If yes, the pipeline is working but the callback is not firing
   -- check `/self-healing-pipeline` SKILL.md Step 0a for the callback
   implementation.
3. Is `IN_FLIGHT_DIR/worker-1/<job_id>.json` present with a fresh mtime?
   If yes, the daemon claimed a job and is inside the pipeline -- the
   correct diagnosis is "slow phase transition," not "dead daemon."

Do NOT kill the daemon on a stale heartbeat alone. Always cross-check
against `IN_FLIGHT_DIR` contents and `state.json` mtime before concluding
the daemon crashed.

## Hook interactions

Claude Code nudges the assistant to wrap long-running blocking commands
in `run_in_background: true`. For daemon idle sleeps, **do not comply**:
a background sleep does not gate the next loop iteration the same way a
foreground sleep does, and the daemon ends up spawning multiple
concurrent background sleeps per idle cycle. The Step 4 rewrite uses
six x `sleep 10` (foreground) instead of one `sleep 60` to keep each
individual call short enough that the nudge does not fire, while still
giving the idle path a ~60-second cadence.

User-level PreToolUse Bash hooks (`~/.claude/settings.json`,
`~/.claude/settings.local.json`) fire on every Bash tool call inside the
daemon session, including all sub-agent tool use. Known hooks:
- `block-cd.py` -- blocks bare `cd` commands; daemon uses absolute paths
  so this is a no-op.
- `env.shared` access guard -- blocks `cat|head|tail|less|more|bat|sed|
  awk|grep.*=.*\.env.shared` patterns. The daemon has no reason to touch
  `~/.env.shared`, so this is also a no-op.
- `verify-api-models` (on Edit/Write) -- blocks edits containing hardcoded
  model names or API endpoint patterns. Builders writing code that
  references providers may trip this. If a Builder sub-agent gets blocked
  here, the daemon's Judge will see the failure and route to a retry or
  escalate; no daemon-level workaround needed.
- `Stop` hook (`check-uncommitted.sh`) -- fires when the assistant's turn
  ends. In the daemon this means it runs every time the daemon yields
  control back to the loop host. Safe but adds latency. Do not suppress.

None of these hooks block the daemon's normal happy path. The one thing
that DOES block the daemon is permission prompts on tool calls the
session has not pre-approved -- which is exactly what `dsp` solves
(see "Startup command" above).

## Expected tool-use profile

A human scrolling the daemon session's scrollback should see only these
tool categories in normal operation. Anything else is an anomaly worth
investigating before letting the daemon continue:

| Tool | Expected use | Anomaly signal |
|------|--------------|----------------|
| `Bash` (foreground) | `mkdir`, `mv`, `touch`, `stat`, `ls`, `sleep 10`, `git init/add/commit`, `cat` of job files and state.json | Anything touching `~/.ssh`, `~/.env.shared`, `/etc`, `/home/apexaipc/.claude`, `sudo`, network tools (`curl`, `wget`, `ssh`, `scp`), package managers (`apt`, `pip install` outside the project venv), process control (`kill`, `pkill`) |
| `Read` | `spec.md`, `state.json`, `plan.md`, `test-contract.md`, `builder-log-*.md`, `judge-brief-*.md`, source files under `{target_dir}` | Reads outside `{target_dir}` or queue root, especially `~/.claude/**`, `~/vault/**`, other project dirs |
| `Write` | `README.md`, job files in queue dirs, `.heartbeat-callback`, commit via git | Writes outside `{target_dir}` or queue root; any write to `~/.claude/settings*.json`, `~/.env*`, `~/.bashrc` |
| `Edit` | Source files under `{target_dir}` only (Builder phase) | Edits outside `{target_dir}` |
| `Agent` / `Skill` | `/self-healing-pipeline` and its Planner/Builder/Judge sub-agents; `pr-review-toolkit:code-reviewer` and `pr-review-toolkit:silent-failure-hunter` for Step 10.5 Ravage review | Any other skill or agent invocation, especially shell-adjacent ones |

If the daemon starts issuing Bash calls you don't recognize, stop it
immediately (`touch shutdown.flag` from a second terminal), read the
scrollback to understand what happened, and decide whether to add a new
deny rule to `.claude/settings.local.json` before restarting. Under
`--dangerously-skip-permissions` the scrollback is your audit log -- you
have to actually read it.

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
- It does NOT retry on review rejection. Step 10.5 Ravage review is pass/fail
  only. A `review_rejected` build goes straight to `FAILED_DIR`. The Builder
  is not re-invoked with review feedback (future enhancement -- would require
  a new retry trigger type in the self-healing-pipeline).
