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

- [x] Bootstrap upstream DBs: UM (restored .bak, 13 ideas/3 builds/7 evals), IdeaForge (init, empty), ST Factory (rebuilt from JSONL, 2 outcomes/6 recs/2 patches)
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

## Architecture

```
IdeaForge (signals+scores) ──→ Gate 1: Triage ──→ approve/reject/defer
                                                        │
                                                   approved ideas
                                                        │
                                                        ▼
                                          Gate 2: Spec Gen + Build
                                          Jinja2 template → app spec
                                          queue_runner.py subprocess
                                                        │
ST Factory (persona_patches) ──→ Gate 3: Patcher ──→ git clone/commit/push
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
| Single external write | Only `persona_patches.status` in ST Factory |

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
| `readers/ideaforge_reader.py` | IdeaForge SQLite (read-only) |
| `readers/stfactory_reader.py` | ST Factory SQLite (read + patch status write) |
| `readers/um_reader.py` | Ultra-Magnus SQLite (read-only) |
| `spec_templates/app_spec_template.md` | Jinja2 template for generated specs |
