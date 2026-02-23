# Metroplex

Level 5 autonomy layer for the ST Metro ecosystem. Closes all three human gates in the feedback loop: idea triage, build orchestration, and persona patch application.

## Architecture

```
IdeaForge (signals+scores) --> Gate 1: Triage --> approve/reject/defer
                                                       |
                                                  approved ideas
                                                       |
                                                       v
                                         Gate 2: Spec Gen + Build
                                         Jinja2 template -> app spec
                                         queue_runner.py subprocess
                                                       |
ST Factory (persona_patches) --> Gate 3: Patcher --> git clone/commit/push
                                         YAML ops on Academy repo
```

## Setup

```bash
chmod +x init.sh && ./init.sh
# or manually:
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p data/
```

## Usage

```bash
source venv/bin/activate

# Individual gates
python metroplex.py triage [--dry-run]              # Gate 1: score & threshold decisions
python metroplex.py build [--dry-run] [--idea-id N]  # Gate 2: spec gen + build queue
python metroplex.py patch [--dry-run]                # Gate 3: persona YAML patches

# Full cycle
python metroplex.py run-all --dry-run --cycles 1     # Single dry-run cycle
python metroplex.py run-all --cycles 3                # Run 3 live cycles
python metroplex.py run-all --cycles 0                # Continuous (until SIGTERM/SIGINT)

# Operations
python metroplex.py status                            # Gate health, recent cycles, pending builds
python metroplex.py reset --gate triage               # Reset circuit breaker for one gate
python metroplex.py reset --gate all                  # Reset all circuit breakers
```

## Continuous Operation (systemd)

```bash
# Install as systemd user service
./deploy/install.sh

# Service management
systemctl --user start metroplex        # Start daemon
systemctl --user stop metroplex         # Graceful shutdown (SIGTERM)
systemctl --user status metroplex       # Check status
systemctl --user enable metroplex       # Auto-start on login
journalctl --user -u metroplex -f       # Follow logs
```

The service runs `metroplex.py run-all --cycles 0` and handles SIGTERM gracefully (finishes current cycle, then exits). Restarts automatically on crash (`Restart=on-failure`, 30s delay).

## Configuration

All settings via environment variables (prefix `METROPLEX_`). Set in `~/.env.shared` or the systemd `EnvironmentFile`.

| Variable | Default | Description |
|----------|---------|-------------|
| `METROPLEX_IDEAFORGE_DB` | `.../ideaforge/data/ideaforge.db` | IdeaForge database path |
| `METROPLEX_UM_DB` | `.../ultra-magnus/.../idea-factory.db` | Ultra-Magnus database path |
| `METROPLEX_STFACTORY_DB` | `.../st-factory/data/persona_metrics.db` | ST Factory database path |
| `METROPLEX_YCE_DIR` | `.../yce-harness` | YCE Harness directory |
| `METROPLEX_ACADEMY_REPO` | `m2ai-portfolio/agent-persona-academy` | GitHub repo for persona YAMLs |
| `METROPLEX_BUILD_MODEL` | `opus` | Claude model for spec generation |
| `METROPLEX_APPROVE_THRESHOLD` | `70` | Score threshold for approval (0-100) |
| `METROPLEX_REJECT_THRESHOLD` | `40` | Score threshold for rejection (0-100) |
| `METROPLEX_MAX_APPROVE_PER_CYCLE` | `3` | Max approvals per cycle |
| `METROPLEX_MAX_PATCHES_PER_CYCLE` | `5` | Max patches per cycle |
| `METROPLEX_CIRCUIT_BREAKER_THRESHOLD` | `3` | Consecutive failures before gate halt |
| `METROPLEX_CYCLE_SLEEP_SECONDS` | `60` | Sleep between cycles (warning if < 10) |

## Project Structure

```
metroplex/
├── metroplex.py                # CLI entry point (argparse)
├── orchestrator.py             # Cycle lifecycle, gate sequencing
├── config.py                   # Env var config with defaults
├── db.py                       # metroplex.db state management
├── models.py                   # Pydantic v2 data models
├── safety.py                   # Circuit breaker, caps, shutdown handler
├── audit.py                    # JSON Lines audit logger
├── gates/
│   ├── triage.py               # Gate 1: score + threshold decisions
│   ├── build.py                # Gate 2: spec gen + queue_runner subprocess
│   └── patcher.py              # Gate 3: YAML patches via git
├── readers/
│   ├── ideaforge_reader.py     # IdeaForge SQLite (read-only)
│   ├── stfactory_reader.py     # ST Factory SQLite (read + patch status write)
│   └── um_reader.py            # Ultra-Magnus SQLite (read-only)
├── spec_templates/
│   └── app_spec_template.md    # Jinja2 template for generated specs
├── deploy/
│   ├── metroplex.service       # systemd user service unit
│   └── install.sh              # Service installer
├── tests/                      # pytest suite (147+ tests)
├── data/                       # Runtime state (git-ignored)
│   ├── metroplex.db            # SQLite state database
│   ├── decisions.log           # JSON Lines audit log
│   └── specs/                  # Generated app specs
├── BLUEPRINT.md                # Phase tracker
├── DATA_CONTRACT.md            # Sky-Lynx data interface contract
└── requirements.txt            # Python dependencies
```

## Testing

```bash
source venv/bin/activate
pytest tests/ -v                     # All tests
pytest tests/test_config.py -v       # Config tests
pytest tests/test_continuous.py -v   # Phase 5 continuous operation tests
```

## Safety Systems

- **Circuit Breaker**: Per-gate halt after N consecutive failures (default 3). Other gates continue. Manual reset via `metroplex.py reset`.
- **Cycle Caps**: Max 3 approvals and 5 patches per cycle to prevent runaway behavior.
- **Shutdown Handler**: Catches SIGTERM/SIGINT, finishes current cycle, then exits cleanly.
- **Read-Only Upstream**: All upstream DB access uses `?mode=ro` (except one status write in ST Factory).

## License

Proprietary - ST Metro Ecosystem
