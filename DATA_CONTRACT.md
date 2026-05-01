# Metroplex Data Contract

Stable read interface for downstream consumers (Sky-Lynx, dashboards). This document defines the schema and access patterns for Metroplex's persistent state.

## Database: `data/metroplex.db`

SQLite 3 database. Consumers **must** open in read-only mode:

```python
import sqlite3
conn = sqlite3.connect("file:/path/to/metroplex.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
```

### Tables

#### `cycles`

One row per completed Metroplex cycle.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `cycle_id` | TEXT | NOT NULL UNIQUE | Cycle identifier (e.g. `cycle-20260223-112005-807398`) |
| `started_at` | TEXT | NOT NULL | ISO 8601 datetime |
| `completed_at` | TEXT | nullable | ISO 8601 datetime (NULL if incomplete) |
| `triage_count` | INTEGER | DEFAULT 0 | Number of triage decisions in this cycle |
| `build_count` | INTEGER | DEFAULT 0 | Number of build jobs queued in this cycle |
| `patch_count` | INTEGER | DEFAULT 0 | Number of patches processed in this cycle |
| `publish_count` | INTEGER | DEFAULT 0 | Number of publishes completed in this cycle |
| `errors` | TEXT | DEFAULT '[]' | JSON array of error strings |

**Index**: `idx_cycles_started` on `started_at`

#### `triage_decisions`

One row per idea evaluated by Gate 1.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `idea_id` | INTEGER | NOT NULL | IdeaForge idea ID |
| `title` | TEXT | NOT NULL | Idea title |
| `weighted_score` | REAL | NOT NULL | Raw score from IdeaForge (0-10 scale) |
| `scaled_score` | REAL | NOT NULL | Scaled score (0-100 scale) |
| `decision` | TEXT | NOT NULL, CHECK IN ('approve','reject','defer') | Triage outcome |
| `reason` | TEXT | DEFAULT '' | Human-readable reason |
| `decided_at` | TEXT | NOT NULL | ISO 8601 datetime |

**Indexes**: `idx_triage_decisions_idea` on `idea_id`, `idx_triage_decisions_decision` on `decision`

#### `build_jobs`

One row per build job queued by Gate 2.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `idea_id` | INTEGER | NOT NULL | IdeaForge idea ID |
| `title` | TEXT | NOT NULL | Idea title |
| `spec_path` | TEXT | NOT NULL | Path to generated app spec file |
| `queue_job_id` | TEXT | NOT NULL | queue_runner.py job identifier |
| `status` | TEXT | NOT NULL, CHECK IN ('queued','started','completed','failed') | Job status |
| `queued_at` | TEXT | NOT NULL | ISO 8601 datetime |
| `project_dir` | TEXT | nullable | Actual project directory (set by UM bridge on completion) |
| `review_status` | TEXT | nullable | `reviewed` (passed Gate 4.5) or `review_failed` |
| `retry_count` | INTEGER | DEFAULT 0 | Number of automatic retries attempted |
| `next_retry_at` | TEXT | nullable | ISO 8601 datetime for next retry (exponential backoff) |
| `quality_score` | REAL | nullable | Structural quality score (0-100) from Phase 14b scorer |
| `estimated_cost` | REAL | nullable | Estimated API cost in USD |
| `actual_cost_usd` | REAL | nullable | Actual cost in USD — sum of `cost_ledger.estimated_cost` entries with matching `queue_job_id`. Populated by orchestrator on build completion. NULL until the build reaches a terminal status (`completed` or `failed`). |
| `feasibility_score` | REAL | DEFAULT NULL | Pre-build feasibility score from L5 B2 predictor |
| `test_ratio` | REAL | DEFAULT NULL | Test file count / source file count (L5 D2 coverage enforcement) |
| `base_job_id` | TEXT | DEFAULT NULL | Groups retry attempts — matches `queue_job_id` of the first attempt |

**Indexes**: `idx_build_jobs_status` on `status`, `idx_build_jobs_base_job_id` on `base_job_id`

**Notes**:
- `next_retry_at` is a TEXT column that normally holds an ISO 8601 datetime. The sentinel value `'abandoned'` is stored here to mark builds that have exhausted all retries — this is **not** a `status` enum value.
- `queue_job_id` format: `metroplex-{source}-{id}` for first attempts, `metroplex-{source}-{id}-r{N}` for retry N. Use `base_job_id` to group all attempts for the same idea.

#### `patch_applications`

One row per persona patch processed by Gate 3.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `patch_id` | TEXT | NOT NULL | ST Records patch identifier |
| `persona_id` | TEXT | NOT NULL | Target persona (e.g. `sky-lynx`) |
| `from_version` | TEXT | nullable | Previous persona version |
| `to_version` | TEXT | nullable | New persona version |
| `status` | TEXT | NOT NULL, CHECK IN ('applied','failed','skipped') | Patch outcome |
| `reason` | TEXT | DEFAULT '' | Human-readable reason |
| `applied_at` | TEXT | NOT NULL | ISO 8601 datetime |

#### `gate_status`

One row per gate (triage, build, patch, publish). Circuit breaker state.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `gate` | TEXT | PRIMARY KEY, CHECK IN ('triage','build','patch','publish') | Gate name |
| `consecutive_failures` | INTEGER | DEFAULT 0 | Consecutive failure count |
| `halted` | INTEGER | DEFAULT 0 | 1 = halted, 0 = active |
| `last_error` | TEXT | nullable | Most recent error message |

#### `publish_jobs`

One row per Gate 4 GitHub publish attempt.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `build_job_id` | TEXT | NOT NULL UNIQUE | Corresponding `queue_job_id` from `build_jobs` |
| `title` | TEXT | NOT NULL | Project title |
| `repo_name` | TEXT | NOT NULL | GitHub repository name |
| `repo_url` | TEXT | nullable | Full GitHub URL (set after publish) |
| `status` | TEXT | NOT NULL, CHECK IN ('pending','published','failed') | Publish outcome |
| `error` | TEXT | nullable | Error message if failed |
| `project_dir` | TEXT | NOT NULL | Local project directory path |
| `created_at` | TEXT | NOT NULL | ISO 8601 datetime |
| `published_at` | TEXT | nullable | ISO 8601 datetime (set on success) |

**Index**: `idx_publish_jobs_status` on `status`

#### `cost_ledger`

One row per LLM API call cost record.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `timestamp` | TEXT | NOT NULL | ISO 8601 datetime |
| `source` | TEXT | NOT NULL | Caller context (e.g. `spec_gen`, `triage`) |
| `model` | TEXT | NOT NULL | LLM model identifier |
| `input_tokens` | INTEGER | NOT NULL, DEFAULT 0 | Input token count |
| `output_tokens` | INTEGER | NOT NULL, DEFAULT 0 | Output token count |
| `estimated_cost` | REAL | NOT NULL, DEFAULT 0.0 | Estimated cost in USD |
| `queue_job_id` | TEXT | nullable | Associated build job (if applicable) |
| `details` | TEXT | DEFAULT '{}' | JSON blob for extra metadata |

**Index**: `idx_cost_ledger_timestamp` on `timestamp`

### Cost ledger source naming convention

The `cost_ledger.source` column identifies which gate or component incurred the cost. Canonical sources currently emitted:

| Source | Component | Per-build attribution? |
|--------|-----------|------------------------|
| `spec_expander` | `gates/llm_expander.py:expand()` — initial spec generation | YES (passes queue_job_id) |
| `spec_simplifier` | `gates/llm_expander.py:expand_simplified()` — Tyrest-rejection retry | YES |
| `readme_generation` | `gates/readme.py` — Gate 4.7 README/infographic prompt | YES |
| `readiness_topics` | `gates/readiness.py:_fix_generate_topics` — Gate 4.9 (3-attempt retry loop, recorded as totals) | YES |
| `readiness_description` | `gates/readiness.py:_fix_generate_description` — Gate 4.9 (3-attempt retry loop, recorded as totals) | YES |
| `ego_mutator` | `learning/mutator.py` — EGO constraint variant generation | NO (pre-build experimentation) |
| `ego_evaluator` | `learning/evaluator.py` — EGO judge | NO (pre-build experimentation) |
| `yce_build` | Post-completion estimate from build subprocess (legacy) | YES |

**Per-build attribution rule**: Any `record_cost(...)` call inside a per-build code path (a path that has a `queue_job_id` in scope) MUST pass `queue_job_id=...` so `update_build_actual_cost(queue_job_id)` can roll the totals onto `build_jobs.actual_cost_usd`. Pre-build code (EGO learning, scheduled jobs, etc.) leaves `queue_job_id=None` — those costs still appear in daily totals but are not attributed to any specific build.

When adding a new source, append a row to this table and choose a snake_case `<gate>_<action>` name. Sources should be stable identifiers (used in dashboards) — never rename without coordinating with downstream consumers.

#### `build_postmortems`

One row per structured failure analysis (L5 B1).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `queue_job_id` | TEXT | NOT NULL UNIQUE | Failed build's job ID |
| `idea_id` | INTEGER | NOT NULL | IdeaForge idea ID |
| `title` | TEXT | NOT NULL | Idea title |
| `failure_category` | TEXT | NOT NULL | Categorized failure type |
| `failure_stage` | TEXT | nullable | Pipeline stage where failure occurred |
| `error_signature` | TEXT | nullable | Normalized error fingerprint |
| `spec_path` | TEXT | nullable | Path to the app spec used |
| `idea_weighted_score` | REAL | nullable | Original IdeaForge weighted score |
| `idea_artifact_type` | TEXT | nullable | Artifact type from IdeaForge |
| `created_at` | TEXT | NOT NULL | ISO 8601 datetime |

**Index**: `idx_postmortems_category` on `failure_category`

#### `feasibility_predictions`

One row per pre-build feasibility prediction (L5 B2).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `queue_job_id` | TEXT | NOT NULL | Build job ID |
| `feasibility_score` | REAL | NOT NULL | Predicted feasibility (0-1) |
| `predicted_outcome` | TEXT | NOT NULL | Predicted result |
| `actual_outcome` | TEXT | nullable | Actual result (set after build completes) |
| `correct` | INTEGER | nullable | 1 = prediction correct, 0 = incorrect |
| `feature_weights` | TEXT | DEFAULT '{}' | JSON blob of feature importance |
| `created_at` | TEXT | NOT NULL | ISO 8601 datetime |
| `resolved_at` | TEXT | nullable | ISO 8601 datetime when actual outcome recorded |

**Index**: `idx_feasibility_queue_job` on `queue_job_id`

#### `readme_jobs`

One row per Gate 4.7 README generation attempt.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `publish_job_id` | TEXT | nullable | Associated publish job |
| `build_job_id` | TEXT | nullable | Associated build job |
| `repo_url` | TEXT | nullable | GitHub repository URL |
| `status` | TEXT | DEFAULT 'pending' | Job status |
| `error` | TEXT | nullable | Error message if failed |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | Creation time |
| `completed_at` | TIMESTAMP | nullable | Completion time |

**Index**: `idx_readme_jobs_build` on `build_job_id`

## Audit Log: `data/decisions.log`

Append-only JSON Lines file. Each line is a complete JSON object.

### Schema

```json
{
  "timestamp": "2026-02-23T11:20:05.825788",
  "gate": "triage|build|patch|cycle",
  "action": "approve|reject|defer|queue_build|applied|failed|skipped|error|start|end",
  "details": { }
}
```

### Action Types

| Gate | Action | Details Keys |
|------|--------|-------------|
| `cycle` | `start` | `cycle_id` |
| `cycle` | `end` | `cycle_id`, `triage_count`, `build_count`, `patch_count`, `errors` |
| `triage` | `approve`/`reject`/`defer` | `idea_id`, `title`, `weighted_score`, `scaled_score`, `reason` |
| `build` | `queue_build` | `idea_id`, `title`, `spec_path`, `queue_job_id` |
| `patch` | `applied`/`failed`/`skipped` | `patch_id`, `persona_id`, `reason` |
| any | `error` | `message` |

## Access Patterns for Sky-Lynx

### Recent cycle metrics (last 7 days)

```sql
SELECT cycle_id, started_at, completed_at,
       triage_count, build_count, patch_count, errors
FROM cycles
WHERE started_at >= datetime('now', '-7 days')
ORDER BY started_at DESC;
```

### Decision distribution

```sql
SELECT decision, COUNT(*) as count
FROM triage_decisions
WHERE decided_at >= datetime('now', '-7 days')
GROUP BY decision;
```

### Gate health

```sql
SELECT gate, consecutive_failures, halted, last_error
FROM gate_status;
```

### Build success rate

```sql
SELECT status, COUNT(*) as count
FROM build_jobs
GROUP BY status;
```

## Stability Guarantees

- Column names and types are stable. New columns may be added but existing columns will not be renamed or removed.
- CHECK constraints are stable. New values may be added to enums but existing values will not change.
- Indexes are stable. New indexes may be added.
- The `decisions.log` JSONL format is append-only. New action types may be added.
- Always use `?mode=ro` to prevent accidental writes to the Metroplex state DB.
