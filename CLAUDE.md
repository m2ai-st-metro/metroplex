# CLAUDE.md — Metroplex

L5 autonomy layer for the ST Metro ecosystem. Closes all human gates in the feedback loop: triage ideas, build projects, apply persona patches, and publish to GitHub.

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
python metroplex.py build [--dry-run] [--idea-id N]             # Gate 2: spec + YCE dispatch
python metroplex.py patch [--dry-run]                           # Gate 3: persona YAML patches
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
pytest tests/test_patcher.py -v           # Patch gate
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

### Four Gates

| Gate | Class | Purpose |
|------|-------|---------|
| 1 Triage | `gates/triage.py` | Score IdeaForge ideas against thresholds, approve/reject/defer |
| 2 Build | `gates/build.py` | Generate spec via LLM, dispatch to YCE Harness |
| 3 Patch | `gates/patcher.py` | Apply ST Records persona YAML patches via git clone/commit/push |
| 4 Publish | `gates/publish.py` | Create GitHub repo in m2ai-portfolio org, push completed builds |
| 4.5 Review | `gates/review.py` | Automated quality checks before publish (source code, README, no secrets, no large files) |

### Readers (Upstream DBs)

| Reader | DB | Access |
|--------|----|--------|
| `readers/ideaforge_reader.py` | ideaforge.db | Read + claim (status='classified') |
| `readers/st_records_reader.py` | persona_metrics.db | Read + patch status updates |
| `readers/skylynx_reader.py` | persona_metrics.db | Read-only (recommendations) |
| `readers/linear_reader.py` | Linear API (Arcade) | Read-only |
| `readers/academy_reader.py` | File system | Read-only (promotions) |

### Priority Queue

All approved/recommended items compete via weighted scores:
- IdeaForge: weight 1.0
- Sky-Lynx: weight 1.5
- Linear: weight 2.0
- Academy: weight 2.0

### YCE Dispatch

Build gate generates an app spec via LLM, then dispatches to YCE Harness for autonomous build. Timeout watchdog kills builds after 90 min (configurable: `METROPLEX_BUILD_TIMEOUT_SECONDS`).

### Auto-Retry (Phase 13f)

Failed builds are automatically retried up to 3 times with exponential backoff (5min, 20min, 60min). Tracked via `retry_count` and `next_retry_at` columns on `build_jobs`. Retryable builds are checked each cycle.

### Safety Systems

- **Circuit breaker**: 3 consecutive failures halts a gate. Reset via `metroplex.py reset`.
- **Cycle caps**: Max 3 approvals, 5 patches, 3 publishes per cycle.
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
| `data/runner.log` | YCE queue_runner subprocess output |
| `data/build_logs/` | Per-build YCE dispatch logs (timestamped) |
| `data/specs/` | Generated app spec files |

## Key Environment Variables

### Thresholds
- `METROPLEX_APPROVE_THRESHOLD` (68) — scaled score to approve
- `METROPLEX_REJECT_THRESHOLD` (40) — scaled score to reject
- `METROPLEX_MAX_DEFERRALS` (3) — max deferrals before auto-reject

### Cycle Limits
- `METROPLEX_MAX_APPROVE_PER_CYCLE` (3)
- `METROPLEX_MAX_PATCHES_PER_CYCLE` (5)
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

### GitHub
- `METROPLEX_GITHUB_ORG` (m2ai-portfolio)
- `METROPLEX_PUBLISH_VISIBILITY` (private)

## Design Decisions

1. **No cross-project imports** — reads upstream SQLite directly, no IdeaForge/ST Records code imports
2. **Subprocess isolation** — YCE builds and git ops run as subprocesses, never in-process
3. **Score scaling** — IdeaForge 0-10 scaled to 0-100 for threshold intuition (guard validates range)
4. **Fire-and-forget builds** — YCE dispatch subprocess; results polled on next cycle
5. **Per-cycle caps** — hard limits prevent runaway autonomy
6. **Circuit breaker per-gate** — one gate failure doesn't halt others

## Dependencies

```
pydantic>=2.0
jinja2
pyyaml
pytest
arcadepy
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

### Cleaning YCE queue.json

The YCE queue runner reads `data/yce_queue/queue.json`. Stale entries accumulate when builds are killed or time out.

```bash
# Inspect current queue
cat data/yce_queue/queue.json | python3 -m json.tool

# Remove a specific stale entry (replace JOB_ID)
python3 -c "
import json
with open('data/yce_queue/queue.json') as f: q = json.load(f)
q = [j for j in q if j.get('job_id') != 'JOB_ID']
with open('data/yce_queue/queue.json', 'w') as f: json.dump(q, f, indent=2)
"

# Nuclear option: empty the queue (builds in progress will orphan)
echo '[]' > data/yce_queue/queue.json
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

# Check for orphan build processes
ps aux | grep queue_runner
```
