# CLAUDE.md — Metroplex

L5 autonomy layer for the ST Metro ecosystem. Closes all human gates in the feedback loop: triage ideas, build projects, and publish to GitHub.

## Setup

```bash
cd /home/apexaipc/projects/metroplex
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
mkdir -p data/
```

All API keys sourced from `~/.env.shared` — no separate `.env` needed.

## Commands

### Individual Gates

```bash
source venv/bin/activate
python metroplex.py triage [--dry-run]                          # Gate 1: score & threshold
python metroplex.py build [--dry-run] [--idea-id N]             # Gate 2: spec + adapter dispatch
python metroplex.py publish [--dry-run]                         # Gate 4: GitHub repo push
```

### Full Cycle

```bash
python metroplex.py run-all --dry-run --cycles 1    # Single dry-run cycle
python metroplex.py run-all --cycles 3               # Run 3 live cycles
python metroplex.py run-all --cycles 0               # Continuous (systemd mode)
```

### Operations

```bash
python metroplex.py status                           # Gate health + cycle history
python metroplex.py queue                            # Priority queue status
python metroplex.py builds                           # Build job status
python metroplex.py retry --build-id <id>            # Retry a failed build
python metroplex.py reset --gate triage              # Reset one circuit breaker
python metroplex.py reset --gate all                 # Reset all circuit breakers
python metroplex.py dispatch [--idea-id N] [--worker-type TYPE] --dry-run
```

## Testing

```bash
pytest tests/ -v                          # All 315 tests
pytest tests/test_orchestrator.py -v      # Orchestrator
pytest tests/test_triage.py -v            # Triage gate
pytest tests/test_build.py -v             # Build gate
pytest tests/test_dispatcher.py -v        # EA-Claude dispatch
pytest tests/test_continuous.py -v        # Systemd + circuit breakers
pytest tests/test_safety.py -v            # Circuit breaker, caps, shutdown
```

## Systemd Service

```bash
./deploy/install.sh                       # Install user service
systemctl --user start metroplex
systemctl --user status metroplex
systemctl --user enable metroplex         # Auto-start on login
journalctl --user -u metroplex -f         # Follow logs
```

Service unit: `deploy/metroplex.service` — runs `run-all --cycles 0` with `Restart=always`, graceful SIGTERM shutdown (90s timeout).

## Architecture

### Gates

| Gate | Class | Purpose |
|------|-------|---------|
| 1 Triage | `gates/triage.py` | Score IdeaForge ideas against thresholds, approve/reject/defer |
| 2 Build | `gates/build.py` | Generate spec via LLM, dispatch via the configured BuildAdapter (SelfHealing or Oz) |
| 4 Publish | `gates/publish.py` | Create repos on configured hosts (GitHub `m2ai-portfolio` org and/or GitLab `m2ai-portfolio` group), push completed builds. First entry in `publish_targets` is primary; subsequent entries are mirrors (their URLs go in `publish_jobs.mirror_urls`, per-target outcome in `targets_status`). |
| 4.5 Review | `gates/review.py` | Automated quality checks before publish (source code, README, no secrets, no large files) |

### Readers (Upstream DBs)

| Reader | DB | Access |
|--------|----|--------|
| `readers/ideaforge_reader.py` | ideaforge.db | Read + claim (status='classified') |
| `readers/skylynx_reader.py` | persona_metrics.db | Read-only (recommendations) |

### Priority Queue

All approved/recommended items compete via weighted scores:
- IdeaForge: weight 1.0
- Sky-Lynx: weight 1.5

### Build Adapter Dispatch

Build gate generates an app spec via LLM, then dispatches via the configured BuildAdapter selected by `METROPLEX_BUILD_TARGET`. Valid targets:

| Target | Adapter | Runtime |
|--------|---------|---------|
| `self_healing` (default) | `SelfHealingAdapter` | Long-running Claude Code daemon processing the `/self-healing-pipeline` skill |
| `cloud` | `OzAdapter` | Oz cloud agent via `oz_bridge.submit_to_oz()` |

Legacy targets (`local` yce-harness queue_runner, `a2a` Google A2A protocol via `yce-harness/a2a_server.py`, `auto` a2a/local fallback chain) were retired in CLEANUP-B 2026-05-12. The Google A2A path never dispatched a production build; CCOS / ClaudeClaw owns inter-agent communication via its own `delegateToAgent` primitive.

Timeout watchdog kills builds after 90 min (configurable: `METROPLEX_BUILD_TIMEOUT_SECONDS`).

### Auto-Retry (Phase 13f)

Failed builds are automatically retried up to 5 times (`db.py MAX_RETRIES = 5`) with exponential backoff (5, 20, 60, 120, 240 min). Tracked via `retry_count` and `next_retry_at` columns on `build_jobs`. Retryable builds are checked each cycle. (Corrected 2026-07-11, Q-20260711-0013: this doc previously said 3 retries; code says 5 and code wins.)

### Safety Systems

- **Circuit breaker**: 3 consecutive failures halts a gate. Reset via `metroplex.py reset`.
- **Cycle caps**: Max 3 approvals, 3 publishes per cycle.
- **Shutdown handler**: SIGTERM triggers finish-current-cycle then clean exit.
- **Schedule windows**: Hour range + day-of-week filters.

### Dispatcher (EA-Claude)

Non-buildable items routed to EA-Claude workers via `WORKER_ROUTES` dict. Writes to `claudeclaw.db` dispatch queue.

## Database

**State DB**: `data/metroplex.db` — see `DATA_CONTRACT.md` for full schema.

**Upstream DBs** (env var configurable):
| Variable | Default Path |
|----------|-------------|
| `METROPLEX_IDEAFORGE_DB` | `~/projects/ideaforge/data/ideaforge.db` |
| `METROPLEX_ST_RECORDS_DB` | `~/projects/st-records/data/persona_metrics.db` |
| `METROPLEX_DISPATCH_DB` | `~/projects/claudeclaw/store/claudeclaw.db` |

## Log Files

| File | Content |
|------|---------|
| `data/decisions.log` | JSON Lines audit trail (every gate action) |
| `data/metroplex.log` | Python logging output |
| `data/build_logs/` | Per-build dispatch logs (timestamped) |
| `data/specs/` | Generated app spec files |

## Key Environment Variables

### Thresholds
- `METROPLEX_APPROVE_THRESHOLD` (68) — scaled score to approve
- `METROPLEX_REJECT_THRESHOLD` (40) — scaled score to reject
- `METROPLEX_MAX_DEFERRALS` (3) — max deferrals before auto-reject

### Cycle Limits
- `METROPLEX_MAX_APPROVE_PER_CYCLE` (3)
- `METROPLEX_MAX_PUBLISH_PER_CYCLE` (3)
- `METROPLEX_MAX_CONCURRENT_BUILDS` (1)

### Build
- `METROPLEX_BUILD_MODEL` (opus)
- `METROPLEX_BUILD_TIMEOUT_SECONDS` (5400 = 90 min)
- `METROPLEX_SPEC_USE_LLM` (true)
- `METROPLEX_SPEC_LLM_MODEL` (see ~/.env.shared for current value)

### Notifications
- `METROPLEX_TELEGRAM_BOT_TOKEN` — dedicated @m2ai_metroplex_bot
- `METROPLEX_TELEGRAM_CHAT_ID`

### Schedule
- `METROPLEX_SCHEDULE_START` (0) / `METROPLEX_SCHEDULE_END` (24)
- `METROPLEX_ACTIVE_DAYS` (0,1,2,3,4,5,6)
- `METROPLEX_CYCLE_SLEEP_SECONDS` (60)
- `METROPLEX_CIRCUIT_BREAKER_THRESHOLD` (3)

### Publish targets
- `METROPLEX_PUBLISH_TARGETS` (`github,gitlab`) — comma-sep, first is primary, rest are mirrors. Valid values: `github`, `gitlab`. Primary failure fails the publish job; mirror failure leaves status=`published` with the failure recorded in `targets_status` and surfaced in `error`.
- `METROPLEX_GITHUB_ORG` (`m2ai-portfolio`)
- `METROPLEX_GITLAB_HOST` (`gitlab.com`)
- `METROPLEX_GITLAB_NAMESPACE` (`m2ai-portfolio`)
- `METROPLEX_GITLAB_NAMESPACE_ID` (`130681062`) — numeric group id; required by GitLab project-create API
- `METROPLEX_PUBLISH_VISIBILITY` (`private`)
- `GITLAB_TOKEN` — GitLab personal access token, sourced from `~/.env.shared`. Required when `gitlab` is in `publish_targets`.

After changing any of these, restart the metroplex service (`systemctl --user restart metroplex`) so the running daemon picks up the new env.

## Design Decisions

1. **No cross-project imports** — reads upstream SQLite directly, no IdeaForge/ST Records code imports
2. **Subprocess isolation** — git ops run as subprocesses, never in-process
3. **Score scaling** — IdeaForge 0-10 scaled to 0-100 for threshold intuition (guard validates range)
4. **Fire-and-forget builds** — BuildAdapter dispatches asynchronously; results polled on next cycle
5. **Per-cycle caps** — hard limits prevent runaway autonomy
6. **Circuit breaker per-gate** — one gate failure doesn't halt others

## Dependencies

```
pydantic>=2.0
pyyaml
pytest
anthropic>=0.40.0
```

## Operations Runbook

### Diagnosing Stuck Builds

```sql
-- Find builds stuck in 'started' for over 2 hours
sqlite3 data/metroplex.db "
  SELECT queue_job_id, title, queued_at, status, retry_count
  FROM build_jobs
  WHERE status = 'started'
    AND queued_at < datetime('now', '-2 hours')
  ORDER BY queued_at DESC;"

-- Find all failed builds with retry info
sqlite3 data/metroplex.db "
  SELECT queue_job_id, base_job_id, retry_count, next_retry_at, quality_score
  FROM build_jobs
  WHERE status = 'failed'
  ORDER BY queued_at DESC LIMIT 20;"
```

### Recovering Abandoned Builds

Builds with `next_retry_at = 'abandoned'` are permanently skipped. To allow re-evaluation:

```sql
-- List abandoned builds
sqlite3 data/metroplex.db "
  SELECT queue_job_id, title, retry_count
  FROM build_jobs
  WHERE next_retry_at = 'abandoned';"

-- Reset a specific build for retry (sets retry_count back, clears sentinel)
sqlite3 data/metroplex.db "
  UPDATE build_jobs
  SET next_retry_at = NULL, retry_count = 0, status = 'failed'
  WHERE queue_job_id = 'JOB_ID' AND next_retry_at = 'abandoned';"
```

#### ⚠ When the UPDATE recipe is insufficient

The `UPDATE` recipe above only works when `count_failed_builds(base_job_id) < MAX_RETRIES` (default 3). `get_retryable_builds()` counts `status='failed'` rows directly — once you hit 3 failed rows for a `base_job_id`, the build is permanently excluded from retry no matter what `next_retry_at` says.

Use `metroplex.py recover` for this case. It deletes the most-recent failed rows so the count drops below MAX_RETRIES, then re-pends the matching `priority_queue` row.

```bash
# Re-enable a fully-exhausted build (idea 427, base_job_id derived without -rN suffix)
python metroplex.py recover --base-job-id metroplex-ideaforge-427

# Also nuke workspace + failed/ queue files for a clean dispatch
python metroplex.py recover --base-job-id metroplex-ideaforge-427 \
  --clean-workspace --clean-failed-queue --yes
```

Note: `data/self_healing_queue/failed/` filenames can appear in two forms:
- `metroplex-ideaforge-427-r2.json` (queue file moved by daemon after Judge fail)
- `metroplex-ideaforge-427-r2_attemptN_TIMESTAMP.json` (queue file moved by orchestrator after Ravage review rejection — the `_attemptN_TIMESTAMP` suffix matches `build_jobs.retry_count`)

`metroplex.py recover --clean-failed-queue` globs `<queue_job_id>*.json` so both variants are removed.

### Resetting Circuit Breakers

Gates halt after 3 consecutive failures. Check and reset:

```bash
# Check gate health
python metroplex.py status

# Reset a single gate
python metroplex.py reset --gate build

# Reset all gates
python metroplex.py reset --gate all
```

Use `reset` when the root cause of failures has been fixed (e.g., API key rotated, upstream DB restored). Resetting without fixing the cause just burns through the breaker again.

### Self-Healing Daemon (build_target=self_healing)

When `METROPLEX_BUILD_TARGET=self_healing`, the build gate dispatches jobs to
a long-running Claude Code daemon session via file queue. The daemon runs the
`/self-healing-pipeline` Planner/Builder/Judge loop per build and pays boot
tax once at session startup instead of per invocation. Billing stays on Max
base subscription; Agent SDK and `claude -p` headless paths are deliberately
avoided.

**Starting the daemon (once per day, recommended):**

```bash
# In a dedicated terminal, NOT the one you use for other Claude Code work.
# Must be started from inside the metroplex project dir — the skill is
# project-scoped and only loads when claude finds .claude/skills/self-healing-daemon/.
# --dangerously-skip-permissions (alias: dsp) is REQUIRED — without it the daemon
# stalls on the first permission prompt the sub-agents hit mid-build.
(cd /home/apexaipc/projects/metroplex && claude --dangerously-skip-permissions)
```

Inside that session, type:
```
/self-healing-daemon start
```

Safety net: `.claude/settings.local.json` in the metroplex project dir
contains a `permissions.deny` list (rm -rf /, mkfs, dd of=/dev/…, sudo,
shutdown, etc.) that blocks destructive patterns even under
`--dangerously-skip-permissions`. Deny always wins over skip. Update
that file first if you need to add new denied patterns.

The daemon will create queue directories under `data/self_healing_queue/`,
touch `heartbeat-worker-1.txt`, and enter its main loop. From a second
terminal you can verify it is alive:

```bash
stat -c '%Y %n' /home/apexaipc/projects/metroplex/data/self_healing_queue/heartbeat-worker-1.txt
# age should be under 120s while the daemon is running
```

**Stopping the daemon:**

Prefer the external shutdown path — from any other shell (ProBook, Surface,
mobile app):

```bash
touch /home/apexaipc/projects/metroplex/data/self_healing_queue/shutdown.flag
```

The daemon exits within ~10s (one idle-sleep chunk) when flag is set, or
immediately after the current build finishes if one is in flight.

Typing `/self-healing-daemon stop` into the daemon session itself works
but is fragile: the Claude session won't accept keyboard input while a
Bash tool call (including the idle `sleep 10`) is running, so there's up
to ~10s of input-lockout before the command lands. The external
`touch shutdown.flag` path avoids the lockout and is the recommended
way to stop a running daemon.

**Daily restart discipline:** the parent session grows ~5-10k tokens per build.
Past ~20 builds in a 200k session or ~100 in a 1M session, Claude Code
auto-compaction fires. Compaction is safe but adds latency mid-build. Restart
the daemon each morning to keep latency predictable and scrollback clean.

**Mobile access:** the daemon session shows up in the Claude mobile app with
a green dot (global remote control is enabled). Use the app to watch the
daemon — scroll the session history to see what it is building — but do NOT
type prompts into it. Each typed prompt interrupts the loop and eats context.
For chat, open a separate session.

**Diagnosing a stalled daemon:**

```bash
# Is the heartbeat fresh?
find /home/apexaipc/projects/metroplex/data/self_healing_queue -name 'heartbeat-*.txt' -mmin -2

# What's in the queue?
ls -la /home/apexaipc/projects/metroplex/data/self_healing_queue/pending/
ls -la /home/apexaipc/projects/metroplex/data/self_healing_queue/in_flight/worker-1/
ls /home/apexaipc/projects/metroplex/data/self_healing_queue/completed/ | wc -l
ls /home/apexaipc/projects/metroplex/data/self_healing_queue/failed/ | wc -l
```

If the heartbeat is stale, `SelfHealingAdapter.is_active()` returns False and
`start()` logs a clear warning instead of silently queuing. Metroplex's build
gate will not dispatch new jobs until the daemon is running again.

**Smoke test:** validate the adapter<->daemon seam end-to-end with a trivial
build. With the daemon running in a separate terminal, from the metroplex
venv run:

```bash
python scripts/smoke_test_self_healing.py
```

The script queues a hand-crafted calculator spec, polls until terminal
state, and prints a green SUCCESS block (exit 0) or a FAILED/TIMEOUT block
with the workspace path for debugging. Exit codes: 0 pass, 1 fail, 2
timeout, 3 daemon-down. Run weekly or before any change to
`adapters/self_healing_adapter.py` or the self-healing-daemon skill.

### Manual Retry

```bash
# Retry a specific failed build
python metroplex.py retry --build-id metroplex-ideaforge-42

# Check the retry was queued
python metroplex.py builds
```

This creates a new build_jobs row with the `-r{N}` suffix and resets status to `queued`.

### Quality Ratchet Recalibration

The quality ratchet auto-advances the minimum quality threshold as builds improve. If the threshold drifts too high (starving the pipeline), force-recalibrate:

```bash
# Preview what recalibration would do
python metroplex.py recalibrate

# Apply without confirmation prompt
python metroplex.py recalibrate --yes
```

### Checking Service Health

```bash
# Service status
systemctl --user status metroplex

# Follow live logs
journalctl --user -u metroplex -f

# Last 100 log lines
journalctl --user -u metroplex -n 100 --no-pager

# Check decision audit log
tail -20 data/decisions.log | python3 -m json.tool

# Check the self-healing daemon heartbeat
stat -c '%Y %n' data/self_healing_queue/heartbeat-worker-1.txt
```
