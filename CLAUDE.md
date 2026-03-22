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
| 3 Patch | `gates/patcher.py` | Apply ST Factory persona YAML patches via git clone/commit/push |
| 4 Publish | `gates/publish.py` | Create GitHub repo in m2ai-portfolio org, push completed builds |
| 4.5 Review | `gates/review.py` | Automated quality checks before publish (source code, README, no secrets, no large files) |

### Readers (Upstream DBs)

| Reader | DB | Access |
|--------|----|--------|
| `readers/ideaforge_reader.py` | ideaforge.db | Read + claim (status='classified') |
| `readers/stfactory_reader.py` | persona_metrics.db | Read + patch status updates |
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
| `METROPLEX_STFACTORY_DB` | `~/projects/st-factory/data/persona_metrics.db` |
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

1. **No cross-project imports** — reads upstream SQLite directly, no IdeaForge/ST Factory code imports
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
