#!/usr/bin/env python3
"""Pass 6 part B — agent-promote.

Promote a metroplex self-healing workspace into a claudeclaw agent.
Validates the workspace conforms to the canonical CCOS-agent shape
(per gates/quality_scorer.py:_check_category_gate), then copies the
relevant files into <claudeclaw>/agents/<slug>/.

Usage:
    python scripts/agent_promote.py \\
        --workspace data/self_healing_workspaces/metroplex-ideaforge-427-r2 \\
        [--target /home/apexaipc/projects/claudeclaw/agents] \\
        [--name nighttime-triage] \\
        [--dry-run] \\
        [--shape-only] \\
        [--force]

Modes:
    --shape-only   Validate shape; print report; do not copy. Exit 0 on
                   pass, 1 on fail.
    --dry-run      Validate + print the exact copy plan; do not write.
    (default)      Validate + copy files into target. Refuses to publish
                   a build whose state.json indicates review_rejected /
                   escalated / failed unless --force is passed.

Safety: never publish a build the orchestrator already rejected. The
Pass 6 part B convention is "use rejected workspaces as SHAPE
FIXTURES" — run --shape-only or --dry-run against them to prove the
script works; never --force them into the agents directory.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gates.quality_scorer import (  # noqa: E402
    _has_agent_yaml,
    _has_e2e_test,
    _has_skill_manifest,
)


COPY_EXCLUDE_DIRS = {
    ".self-healing-pipeline",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "venv",
    ".venv",
    "node_modules",
}

COPY_EXCLUDE_FILES = {
    ".heartbeat-callback",
}


REJECTED_STATUSES = {
    "review_rejected",
    "escalated",
    "failed",
}


@dataclass
class ShapeReport:
    workspace: Path
    has_agent_yaml: bool
    has_skill_manifest: bool
    has_e2e_test: bool
    extras_present: list[str] = field(default_factory=list)
    extras_missing: list[str] = field(default_factory=list)
    build_status: Optional[str] = None
    agent_name: Optional[str] = None

    @property
    def shape_ok(self) -> bool:
        return self.has_agent_yaml and self.has_skill_manifest and self.has_e2e_test


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unnamed-agent"


def _read_agent_name(workspace: Path) -> Optional[str]:
    """Read the `name:` field from agent.yaml without depending on PyYAML."""
    agent_yaml = workspace / "agent.yaml"
    if not agent_yaml.is_file():
        return None
    try:
        for line in agent_yaml.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*name\s*:\s*(.+?)\s*$", line)
            if m:
                v = m.group(1).strip()
                if (v.startswith('"') and v.endswith('"')) or (
                    v.startswith("'") and v.endswith("'")
                ):
                    v = v[1:-1]
                return v
    except OSError:
        pass
    return None


def _read_telegram_env_var(workspace: Path) -> Optional[str]:
    agent_yaml = workspace / "agent.yaml"
    if not agent_yaml.is_file():
        return None
    try:
        for line in agent_yaml.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*telegram_bot_token_env\s*:\s*(\S+)", line)
            if m:
                return m.group(1).strip().strip("\"'")
    except OSError:
        pass
    return None


def _read_build_status(workspace: Path) -> Optional[str]:
    state_file = workspace / ".self-healing-pipeline" / "state.json"
    if not state_file.is_file():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if state.get("review_verdict") == "rejected":
        return "review_rejected"
    return state.get("status")


def validate_shape(workspace: Path) -> ShapeReport:
    report = ShapeReport(
        workspace=workspace,
        has_agent_yaml=_has_agent_yaml(workspace),
        has_skill_manifest=_has_skill_manifest(workspace),
        has_e2e_test=_has_e2e_test(workspace),
    )
    for extra in ("README.md", "requirements.txt", "episode_log.py"):
        if (workspace / extra).is_file():
            report.extras_present.append(extra)
        else:
            report.extras_missing.append(extra)
    report.build_status = _read_build_status(workspace)
    report.agent_name = _read_agent_name(workspace)
    return report


def _iter_copy_plan(workspace: Path):
    for src in sorted(workspace.rglob("*")):
        if not src.is_file() or src.is_symlink():
            continue
        try:
            rel = src.relative_to(workspace)
        except ValueError:
            continue
        if COPY_EXCLUDE_DIRS & set(rel.parts):
            continue
        if rel.name in COPY_EXCLUDE_FILES:
            continue
        yield src, rel


def _format_shape_report(report: ShapeReport) -> str:
    bar = "=" * 68
    return "\n".join(
        [
            bar,
            f"Shape report — {report.workspace}",
            bar,
            f"  agent.yaml:           {'OK' if report.has_agent_yaml else 'MISSING'}",
            f"  skills/<n>/SKILL.md:  {'OK' if report.has_skill_manifest else 'MISSING'}",
            f"  e2e test:             {'OK' if report.has_e2e_test else 'MISSING'}",
            f"  build status:         {report.build_status or '(no state.json)'}",
            f"  agent name:           {report.agent_name or '(unspecified)'}",
            f"  extras present:       {', '.join(report.extras_present) or '(none)'}",
            f"  extras missing:       {', '.join(report.extras_missing) or '(none)'}",
            "",
            f"  Verdict:              {'SHAPE OK' if report.shape_ok else 'SHAPE INVALID'}",
            bar,
        ]
    )


def promote(
    workspace: Path,
    target_root: Path,
    slug: str,
    *,
    dry_run: bool,
    force: bool,
) -> tuple[bool, str]:
    dest = target_root / slug
    if dest.exists() and not force:
        return False, f"Destination {dest} already exists; pass --force to overwrite."

    plan = list(_iter_copy_plan(workspace))

    if dry_run:
        lines = [
            f"[DRY RUN] Would create: {dest}",
            f"[DRY RUN] Would copy {len(plan)} file(s):",
            *[f"  - {rel}" for _, rel in plan],
        ]
        return True, "\n".join(lines)

    dest.mkdir(parents=True, exist_ok=True)
    for src, rel in plan:
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True, f"Promoted {len(plan)} file(s) into {dest}"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Promote a metroplex self-healing workspace into a claudeclaw "
            "agent (Pass 6 part B)."
        )
    )
    p.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Path to the self-healing workspace to promote.",
    )
    p.add_argument(
        "--target",
        type=Path,
        default=Path("/home/apexaipc/projects/claudeclaw/agents"),
        help="Claudeclaw agents/ directory (default: %(default)s).",
    )
    p.add_argument(
        "--name",
        help="Slug for the agent directory. If omitted, derived from agent.yaml 'name'.",
    )
    p.add_argument(
        "--shape-only",
        action="store_true",
        help="Only validate shape; do not copy. Exit 0 on pass, 1 on fail.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + show copy plan; do not write.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Allow promoting from a rejected/escalated build and overwrite "
            "existing destinations. Use sparingly."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"ERROR: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2

    report = validate_shape(workspace)
    print(_format_shape_report(report))

    if not report.shape_ok:
        print(
            "\nSHAPE INVALID — cannot promote. Fix the missing artifact and re-run.",
            file=sys.stderr,
        )
        return 1

    if args.shape_only:
        return 0

    if report.build_status in REJECTED_STATUSES and not args.force:
        print(
            f"\nREFUSED: build_status={report.build_status} indicates the orchestrator"
            f" rejected this build. Pass --force to publish anyway, or use --shape-only"
            f" / --dry-run for shape-fixture validation. This is the documented"
            f" Pass 6 part B safety: never publish a build Ravage already failed.",
            file=sys.stderr,
        )
        return 1

    slug = args.name or _slugify(report.agent_name or workspace.name)
    print(f"\nPromoting workspace -> {args.target / slug}\n")
    ok, msg = promote(
        workspace,
        args.target,
        slug,
        dry_run=args.dry_run,
        force=args.force,
    )
    print(msg)

    env_var = _read_telegram_env_var(workspace)
    if env_var:
        print(
            f"\nNext step: add {env_var}=... to claudeclaw .env "
            f"(create the bot via @BotFather first)."
        )

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
