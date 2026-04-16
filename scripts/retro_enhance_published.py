#!/usr/bin/env python3
"""
Retro-Enhance Published Repos

Re-runs the README + infographic + topics + description gates against
already-published repos using the new prompts (Problem section, plain-speak
description, audience-aware topics, vibrant infographic).

Use this after changing prompts in gates/readme.py or gates/readiness.py to
backfill repos that were published with the old prompts.

Usage:
    python scripts/retro_enhance_published.py --dry-run
    python scripts/retro_enhance_published.py --repo personal-knowledge-retrieval-system
    python scripts/retro_enhance_published.py --limit 3
    python scripts/retro_enhance_published.py             # all published repos

Strategy:
    For each target publish job:
      1. Resolve the on-disk project_dir. If it doesn't exist, clone the repo
         into a fresh temp dir.
      2. Run ReadmeGate._process_one() against it. This regenerates README.md,
         creates a new infographic, commits, and pushes.
      3. Run ReadinessGate._fix_generate_topics() and _fix_generate_description()
         directly (skipping the rest of the readiness check pipeline -- we know
         we want fresh values).

Notes:
    * The script never deletes anything. It commits new files on top of the
      existing repo HEAD.
    * IdeaForge context is fetched per-build via load_idea_context(); builds
      from non-IdeaForge sources fall back to current behavior.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Make metroplex package importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit import AuditLogger  # noqa: E402
from config import Config  # noqa: E402
from db import StateDB  # noqa: E402
from gates._idea_context import load_idea_context  # noqa: E402
from gates.readme import ReadmeGate  # noqa: E402
from gates.readiness import ReadinessGate  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("retro_enhance")


def select_jobs(
    state_db: StateDB,
    repo_filter: str | None,
    limit: int | None,
) -> list[dict]:
    """Pick the publish_jobs we want to retro-enhance."""
    all_jobs = state_db.get_all_publish_jobs()
    published = [j for j in all_jobs if j.get("status") == "published"]

    if repo_filter:
        published = [j for j in published if j.get("repo_name") == repo_filter]

    # Deduplicate by repo_name — keep the most recent build (list is already
    # ordered DESC by created_at, so first occurrence wins).
    seen = set()
    deduped = []
    for j in published:
        name = j.get("repo_name", "")
        if name not in seen:
            seen.add(name)
            deduped.append(j)
    published = deduped

    if limit:
        published = published[:limit]

    return published


def ensure_local_clone(project_dir: str | None, repo_url: str, repo_name: str) -> Path | None:
    """Return a usable local working copy of the repo.

    Prefers an existing project_dir if it has a .git. Otherwise clones the repo
    fresh into a temp directory.
    """
    if project_dir:
        p = Path(project_dir)
        if p.is_dir() and (p / ".git").is_dir():
            # Detect default branch, fetch, and hard-reset to remote HEAD so
            # we're guaranteed in sync before adding our commit on top.
            branch_res = subprocess.run(
                ["git", "-C", str(p), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            branch = branch_res.stdout.strip() or "main"
            subprocess.run(
                ["git", "-C", str(p), "fetch", "origin"],
                capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "-C", str(p), "reset", "--hard", f"origin/{branch}"],
                capture_output=True, text=True, timeout=30,
            )
            return p

    if not repo_url:
        logger.warning(f"{repo_name}: no project_dir and no repo_url -- skipping")
        return None

    tmp_root = Path(tempfile.mkdtemp(prefix=f"retro-{repo_name}-"))
    target = tmp_root / repo_name
    logger.info(f"{repo_name}: cloning {repo_url} -> {target}")
    result = subprocess.run(
        ["git", "clone", repo_url, str(target)],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        logger.error(f"{repo_name}: clone failed: {result.stderr.strip()}")
        return None
    return target


def process_one(
    job: dict,
    state_db: StateDB,
    config: Config,
    readme_gate: ReadmeGate,
    readiness_gate: ReadinessGate,
    skip_readme: bool,
    skip_readiness: bool,
    dry_run: bool,
) -> dict:
    build_job_id = job["build_job_id"]
    title = job["title"]
    repo_name = job["repo_name"]
    repo_url = job.get("repo_url", "")
    project_dir = job.get("project_dir", "")

    print(f"\n=== {repo_name} ({build_job_id}) ===")
    print(f"  Title:    {title}")
    print(f"  Repo URL: {repo_url}")

    # Diagnostic: show what idea context will be used
    ctx = load_idea_context(state_db, build_job_id, config.ideaforge_db)
    if ctx is None:
        print("  Idea ctx: NONE (non-IdeaForge source or lookup failed)")
    else:
        print(f"  Idea ctx: description={len(ctx['description'])}c "
              f"problem={len(ctx['problem_statement'])}c "
              f"audience={len(ctx['target_audience'])}c")

    if dry_run:
        print("  [DRY RUN] would re-run README + infographic + topics + description")
        return {"repo": repo_name, "status": "dry_run"}

    result = {"repo": repo_name, "readme": "skipped", "topics": "skipped", "description": "skipped"}

    # 1. README + infographic
    if not skip_readme:
        local_dir = ensure_local_clone(project_dir, repo_url, repo_name)
        if not local_dir:
            result["readme"] = "no_local_dir"
        else:
            try:
                # _process_one writes README, generates infographic, commits + pushes
                rm_result = readme_gate._process_one(
                    build_job_id=build_job_id,
                    title=title,
                    project_dir=str(local_dir),
                    repo_url=repo_url,
                )
                result["readme"] = rm_result.get("status", "unknown")
                if rm_result.get("error"):
                    result["readme_error"] = rm_result["error"]
            except Exception as e:
                logger.exception(f"{repo_name}: README gate failed")
                result["readme"] = "error"
                result["readme_error"] = str(e)

    # 2. Topics + description (these talk to GitHub API directly, no local clone needed)
    if not skip_readiness:
        try:
            topics_ok = readiness_gate._fix_generate_topics(
                config.github_org, repo_name, idea_ctx=ctx
            )
            result["topics"] = "ok" if topics_ok else "failed"
        except Exception as e:
            logger.exception(f"{repo_name}: topics fix failed")
            result["topics"] = "error"
            result["topics_error"] = str(e)

        try:
            desc_ok = readiness_gate._fix_generate_description(
                config.github_org, repo_name, idea_ctx=ctx
            )
            result["description"] = "ok" if desc_ok else "failed"
        except Exception as e:
            logger.exception(f"{repo_name}: description fix failed")
            result["description"] = "error"
            result["description_error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without making changes")
    parser.add_argument("--repo", type=str, default=None,
                        help="Only process this repo_name (e.g. personal-knowledge-retrieval-system)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit to the N most recently published repos")
    parser.add_argument("--skip-readme", action="store_true",
                        help="Skip README + infographic regeneration (only re-run topics/description)")
    parser.add_argument("--skip-readiness", action="store_true",
                        help="Skip topics/description regeneration (only re-run README + infographic)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to sleep between repos to avoid DeepInfra 429 rate limits")
    args = parser.parse_args()

    config = Config()  # __post_init__ loads env vars
    state_db = StateDB()
    state_db.connect()
    audit = AuditLogger()
    readme_gate = ReadmeGate(config, state_db, audit)
    readiness_gate = ReadinessGate(config, state_db, audit)

    if not os.environ.get("DEEPINFRA_API_KEY"):
        print("ERROR: DEEPINFRA_API_KEY not set -- cannot run LLM gates.", file=sys.stderr)
        sys.exit(2)

    jobs = select_jobs(state_db, args.repo, args.limit)
    if not jobs:
        print("No matching published jobs found.")
        return

    print(f"Selected {len(jobs)} published repo(s) to process:")
    for j in jobs:
        print(f"  - {j['repo_name']}  ({j['build_job_id']})")

    if not args.dry_run:
        print("\nThis will commit and push to each repo and overwrite its topics/description.")
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    import time
    results = []
    for i, job in enumerate(jobs):
        try:
            if i > 0 and args.delay > 0:
                time.sleep(args.delay)
            results.append(process_one(
                job, state_db, config, readme_gate, readiness_gate,
                skip_readme=args.skip_readme,
                skip_readiness=args.skip_readiness,
                dry_run=args.dry_run,
            ))
        except KeyboardInterrupt:
            print("\nInterrupted.")
            break

    print("\n=== Summary ===")
    for r in results:
        print(f"  {r['repo']:40s}  readme={r.get('readme', '-'):10s}  "
              f"topics={r.get('topics', '-'):8s}  description={r.get('description', '-')}")


if __name__ == "__main__":
    main()
