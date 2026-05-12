# Metroplex — Blueprint

L5 autonomy layer for the ST Metro ecosystem. Closes all three human gates in the feedback loop: idea triage, build orchestration, and persona patch application.

## Phase 1: Build via yce-harness

- [x] Write app spec (`prompts/example_app_specs/app_spec_metroplex.txt`)
- [x] Queue and run build via `queue_runner.py` (opus, ~95 min)
- [x] Fix `mcp-remote` → native HTTP MCP transport for Arcade gateway
- [x] Fix `ClaudeAgentOptions` — remove unsupported `thinking`/`effort` params
- [x] All 8 features generated and 97 tests passing

## Phase 2: Post-Build Fixes

- [x] Install `python3.12-venv` for working venvs in future builds
- [x] Fix artifact_type hardcoding — orchestrator and CLI now look up full idea data from IdeaForge reader instead of hardcoding `"tool"`
- [x] Add missing `test_config.py` (15 tests: defaults, env overrides, validation)
- [x] Add missing `test_db.py` (23 tests: table creation, CRUD, constraints, connection lifecycle)
- [x] Full suite: **135 tests passing** (6.71s)

## Phase 3: Integration Testing

- [x] Bootstrap upstream DBs: UM (restored .bak, 13 ideas/3 builds/7 evals), IdeaForge (init, empty), ST Records (rebuilt from JSONL, 2 outcomes/6 recs/2 patches)
- [x] `triage --dry-run` — 0 decisions (IdeaForge has no scored ideas yet, correct)
- [x] `build --dry-run` — "No approved ideas to build" (correct, no approvals in state DB)
- [x] `patch --dry-run` — 2 patches found, both skipped (0 operations in raw_json, correct)
- [x] `status` — all 3 gates [OK], 0 pending builds, cycle history displayed
- [x] `run-all --dry-run --cycles 1` — full cycle completed, 0 errors, all 3 gates executed

## Phase 4: Live Deployment

- [x] Moved from `generations/metroplex/` to `projects/metroplex/`
- [x] Cleaned build artifacts, fresh git repo on `main`
- [x] Pushed to `m2ai-portfolio/metroplex` (private)
- [x] Fresh venv, 135 tests passing
- [x] `run-all --cycles 1` (live) — 0 triage, 0 builds, 2 patches skipped (no ops), 0 errors
- [x] Audit log (`data/decisions.log`) — structured JSON lines: cycle start/end, patch skip decisions
- [x] State DB (`data/metroplex.db`) — cycles, gate status, patch applications recorded
- [ ] **Deferred**: Linear issues + build queueing require scored ideas in IdeaForge (pipeline not yet run)

## Phase 5: Continuous Operation

- [x] systemd user service (`deploy/metroplex.service`) + installer (`deploy/install.sh`)
- [x] `cycle_sleep_seconds` configurable via `METROPLEX_CYCLE_SLEEP_SECONDS` (default 60, warning if < 10)
- [x] Circuit breaker persistence validated across DB reconnections (file-based DB)
- [x] Per-cycle caps tested: triage max 3 approvals, patch max 5 per cycle
- [x] SIGTERM graceful shutdown confirmed via subprocess test
- [x] Sky-Lynx data contract (`DATA_CONTRACT.md`) — stable read interface for `metroplex.db` and `decisions.log`
- [x] 12 new tests in `tests/test_continuous.py` + 3 in `tests/test_config.py`

## Phase 6: IdeaForge Integration

- [x] Fix IdeaForge reader to query `classified` ideas (not `scored`)
- [x] Stop triage re-processing ideas that already have decisions
- [x] Absolute spec paths threaded through build gate

## Phase 7: Triage Gate Proven

- [x] End-to-end triage: IdeaForge scored idea → Metroplex triage → approve/reject/defer
- [x] IdeaForge reader verified against live `ideaforge.db`

## Phase 8: Pipeline Parallelization

- [x] Level 1: `--parallel --max-workers N` flags passed to `queue_runner.py`
- [x] Level 2: `METROPLEX_MAX_CONCURRENT_BUILDS` capacity-based dispatch
- [x] `start_queue_background()` — Popen-based non-blocking dispatch with PID tracking
- [x] `poll_and_sync_status()` — sync build_jobs + priority_queue from runner status
- [x] `run_from_queue()` — capacity-aware dispatch from priority queue
- [x] `is_runner_active()` — PID file existence + liveness check
- [x] 170 tests passing (12 new Level 2 tests)

## Phase 9: Priority Queue + Notifications + Schedule

- [x] `PriorityItem` model (models.py) — ranked task items from any input source
- [x] `priority_queue` table (db.py) — with indexes on status, score, and unique source constraint
- [x] DB methods: `enqueue_item()`, `get_next_pending()`, `update_item_status()`, `get_queue_summary()`, `update_build_job_status()`
- [x] Triage enqueues approved ideas into priority queue with source weight
- [x] Config: `METROPLEX_IDEAFORGE_WEIGHT`, `METROPLEX_SKYLYNX_WEIGHT`, `METROPLEX_LINEAR_WEIGHT`
- [x] `notifier.py` — `Notifier` protocol + `TelegramNotifier` (urllib, zero deps) + `LogNotifier` fallback
- [x] `create_notifier()` factory wired into `metroplex.py` initialization
- [x] Orchestrator sends notifications: triage approvals, build queued/failed, cycle summary, gate halt alerts
- [x] Empty cycle notifications suppressed (no noise)
- [x] Schedule windows: `METROPLEX_SCHEDULE_START`, `METROPLEX_SCHEDULE_END`, `METROPLEX_ACTIVE_DAYS`
- [x] `is_within_schedule()` — supports normal ranges, overnight wraps, day-of-week filtering
- [x] `queue` CLI subcommand — shows priority queue contents by score
- [x] Fix: CWD bug in 7 CLI/SIGTERM subprocess tests
- [x] **221 tests passing** (51 new Phase 9 tests: 15 queue DB, 16 notifier, 8 schedule, 5 notification integration, 3 status, 1 CLI queue, 3 triage-queue integration)

## Phase 9.5: Dispatcher-Orchestrator Integration

- [x] Added `dispatcher` parameter to `CycleOrchestrator.__init__`
- [x] `dispatch_queue_items()` — routes non-buildable queue items (skylynx) to ClaudeClaw workers via `EAClaudeDispatcher`
- [x] Wired into `run_cycle()` between build status sync and Gate 4 (publish)
- [x] `initialize_components()` in `metroplex.py` creates dispatcher from config
- [x] Per-cycle cap respected (`max_approve_per_cycle`)
- [x] Dry-run safe (no dispatches, no status updates)
- [x] Non-fatal — dispatch errors logged but don't halt cycle
- [x] Notifications on successful dispatch
- [x] Dispatch count included in cycle summary notifications
- [x] Fixed pre-existing test assertions (gate count 3→4 from publish gate addition)
- [x] **8 new tests** in `TestDispatchIntegration` (dispatch skylynx items, skip buildable sources, dry run, cycle integration, error handling, notifications, per-cycle cap)
- [x] **229+ tests passing** (88 orchestrator+dispatcher+safety verified)

## Architecture

```
IdeaForge (signals+scores) ──→ Gate 1: Triage ──→ approve/reject/defer
                                                        │
                                                   approved ideas
                                                        │
                                                        ▼
                                          Gate 2: Spec Gen + Build
                                          LLM expander → agent spec
                                          queue_runner.py subprocess
                                                        │
ST Records (persona_patches) ──→ Gate 3: Patcher ──→ git clone/commit/push
                                          YAML ops on Academy repo
                                          Updates patch status → "applied"
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| No cross-project imports | Standalone — reads upstream SQLite directly (`?mode=ro`) |
| subprocess for externals | queue_runner.py, git — all via `subprocess.run()` |
| Score scaling 0-10 → 0-100 | IdeaForge uses 0-10, thresholds are more intuitive at 0-100 |
| Per-cycle caps (3/5) | Prevent runaway autonomous behavior |
| Circuit breaker (3 failures) | Gate-level halt, other gates continue |
| Single external write | Only `persona_patches.status` in ST Records |

## File Manifest

| File | Purpose |
|------|---------|
| `metroplex.py` | CLI entry point (argparse) |
| `orchestrator.py` | Cycle lifecycle, gate sequencing |
| `config.py` | Env vars with defaults |
| `db.py` | metroplex.db state management |
| `models.py` | Pydantic v2 models |
| `safety.py` | Circuit breaker, caps, shutdown |
| `audit.py` | JSON lines audit logger |
| `gates/triage.py` | Gate 1: score + threshold decisions |
| `gates/build.py` | Gate 2: spec gen + queue_runner subprocess |
| `gates/patcher.py` | Gate 3: YAML patches via git |
| `notifier.py` | Telegram + log notification backends |
| `readers/ideaforge_reader.py` | IdeaForge SQLite (read-only) |
| `readers/st_records_reader.py` | ST Records SQLite (read + patch status write) |
| `readers/um_reader.py` | Ultra-Magnus SQLite (read-only) |
| `spec_templates/fixtures/agent_spec_golden.md` | Golden agent spec fixture (LLM expander anchor) |
