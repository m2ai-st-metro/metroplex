#!/usr/bin/env python
"""
Recovery script for build 38 (WorkflowHub).

Rebuilds the build_jobs row that was destroyed by the old
reset_stale_queued_build() DELETE path, re-linking it to the
completed YCE generation on disk so the publish gate can pick it up.

Usage:
    python scripts/recover_build_38.py [--dry-run]

Safe to run multiple times: uses INSERT OR IGNORE on queue_job_id.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Hard-coded for this specific recovery (see Planner plan).
IDEA_ID = 38
TITLE = "WorkflowHub"
QUEUE_JOB_ID = "metroplex-ideaforge-38"
BASE_JOB_ID = "metroplex-ideaforge-38"

METROPLEX_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = METROPLEX_ROOT / "data" / "metroplex.db"
YCE_GENERATIONS = Path("/home/apexaipc/projects/yce-harness/generations")
PROJECT_DIR = YCE_GENERATIONS / QUEUE_JOB_ID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found", file=sys.stderr)
        return 1

    if not PROJECT_DIR.is_dir():
        print(
            f"WARNING: {PROJECT_DIR} does not exist. Recovery will still insert the row,"
            " but publish gate may not find source code.",
            file=sys.stderr,
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check if the row already exists
    cursor.execute(
        "SELECT id, status, next_retry_at, project_dir FROM build_jobs WHERE queue_job_id = ?",
        (QUEUE_JOB_ID,),
    )
    existing = cursor.fetchone()
    if existing:
        print(f"Row already exists: id={existing['id']} status={existing['status']} "
              f"next_retry_at={existing['next_retry_at']} project_dir={existing['project_dir']}")
        # If abandoned or failed, offer to flip to completed
        if existing["status"] != "completed":
            if args.dry_run:
                print(f"[DRY RUN] Would UPDATE build_jobs set status='completed', "
                      f"next_retry_at=NULL, project_dir='{PROJECT_DIR}' WHERE id={existing['id']}")
            else:
                cursor.execute(
                    "UPDATE build_jobs SET status = 'completed', next_retry_at = NULL, "
                    "project_dir = ? WHERE id = ?",
                    (str(PROJECT_DIR), existing["id"]),
                )
                conn.commit()
                print(f"Updated existing row to completed, project_dir={PROJECT_DIR}")
        else:
            print("Row already completed. Nothing to do.")
        conn.close()
        return 0

    # Otherwise insert a fresh row
    now_iso = datetime.now().isoformat()
    if args.dry_run:
        print(
            f"[DRY RUN] Would INSERT into build_jobs: "
            f"queue_job_id={QUEUE_JOB_ID} idea_id={IDEA_ID} title={TITLE!r} "
            f"status=completed project_dir={PROJECT_DIR}"
        )
    else:
        cursor.execute(
            """
            INSERT INTO build_jobs (
                idea_id, title, spec_path, queue_job_id, base_job_id,
                status, queued_at, project_dir, retry_count
            ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, 0)
            """,
            (
                IDEA_ID,
                TITLE,
                "",
                QUEUE_JOB_ID,
                BASE_JOB_ID,
                now_iso,
                str(PROJECT_DIR),
            ),
        )
        conn.commit()
        print(f"Inserted build_jobs row for {QUEUE_JOB_ID} -> {PROJECT_DIR}")

    # Also flip priority_queue back so publish gate can see it
    cursor.execute(
        "SELECT id, status FROM priority_queue WHERE source = 'ideaforge' AND source_id = ?",
        (str(IDEA_ID),),
    )
    pq = cursor.fetchone()
    if pq:
        print(f"priority_queue row: id={pq['id']} status={pq['status']}")
        if args.dry_run:
            print(f"[DRY RUN] Would set priority_queue.status='completed' for id={pq['id']}")
        else:
            cursor.execute(
                "UPDATE priority_queue SET status = 'completed' WHERE id = ?",
                (pq["id"],),
            )
            conn.commit()
            print(f"Updated priority_queue id={pq['id']} to completed")
    else:
        print(f"No priority_queue row found for idea {IDEA_ID} — publish gate may need manual trigger")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
