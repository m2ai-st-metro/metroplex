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

**Index**: `idx_build_jobs_status` on `status`

#### `patch_applications`

One row per persona patch processed by Gate 3.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Internal row ID |
| `patch_id` | TEXT | NOT NULL | ST Factory patch identifier |
| `persona_id` | TEXT | NOT NULL | Target persona (e.g. `sky-lynx`) |
| `from_version` | TEXT | nullable | Previous persona version |
| `to_version` | TEXT | nullable | New persona version |
| `status` | TEXT | NOT NULL, CHECK IN ('applied','failed','skipped') | Patch outcome |
| `reason` | TEXT | DEFAULT '' | Human-readable reason |
| `applied_at` | TEXT | NOT NULL | ISO 8601 datetime |

#### `gate_status`

One row per gate (triage, build, patch). Circuit breaker state.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `gate` | TEXT | PRIMARY KEY, CHECK IN ('triage','build','patch') | Gate name |
| `consecutive_failures` | INTEGER | DEFAULT 0 | Consecutive failure count |
| `halted` | INTEGER | DEFAULT 0 | 1 = halted, 0 = active |
| `last_error` | TEXT | nullable | Most recent error message |

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
