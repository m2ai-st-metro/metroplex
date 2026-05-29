"""
Publish Gate - Gate 4
Creates repos for completed builds and pushes code to one or more git hosts
(GitHub m2ai-portfolio org and/or GitLab m2ai-portfolio group). Configurable via
config.publish_targets — first entry is primary (its URL becomes repo_url and
its failure fails the job); subsequent entries are mirrors (failures recorded
in targets_status but do not fail the job).
"""
import json
import os
import re
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path

from config import Config
from models import PublishJob
from db import StateDB
from audit import AuditLogger


class PublishGate:
    """Gate 4: Publish -- Push completed builds to configured git hosts."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        audit_logger: AuditLogger,
    ):
        self.config = config
        self.state_db = state_db
        self.audit_logger = audit_logger

    def run(self, dry_run: bool = False) -> list[PublishJob]:
        """Run publish gate on completed but unpublished builds."""
        unpublished = self.state_db.get_unpublished_builds(
            require_review=self.config.require_review
        )

        if not unpublished:
            return []

        max_pub = self.config.max_publish_per_cycle
        if len(unpublished) > max_pub:
            unpublished = unpublished[:max_pub]

        results = []
        consecutive_failures = 0

        for build in unpublished:
            queue_job_id = build["queue_job_id"]
            title = build["title"]
            stored_dir = build.get("project_dir")

            project_dir = self._resolve_project_dir(queue_job_id, title, stored_dir)
            if not project_dir:
                job = PublishJob(
                    build_job_id=queue_job_id,
                    title=title,
                    repo_name="",
                    status="failed",
                    error=f"project directory not found for {queue_job_id}",
                    project_dir="",
                )
                results.append(job)
                if not dry_run:
                    self.state_db.record_publish_job(job)
                    self.audit_logger.log_decision(
                        gate="publish",
                        action="failed",
                        details={"queue_job_id": queue_job_id, "reason": "project dir not found"},
                    )
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print("! Publish gate: 3 consecutive failures, halting remaining")
                    break
                continue

            repo_name = self._derive_repo_name(title)
            targets = self.config.publish_targets

            if dry_run:
                print(f"[DRY RUN] Would publish: {queue_job_id}")
                print(f"  Title:   {title}")
                print(f"  Repo:    {repo_name}")
                print(f"  Targets: {targets} (primary={targets[0]})")
                for t in targets:
                    print(f"    - {self._target_repo_url(t, repo_name)}")
                print(f"  Dir:        {project_dir}")
                print(f"  Visibility: {self.config.publish_visibility}")
                job = PublishJob(
                    build_job_id=queue_job_id,
                    title=title,
                    repo_name=repo_name,
                    status="pending",
                    project_dir=str(project_dir),
                )
                results.append(job)
                continue

            job = self._publish_with_mirrors(project_dir, repo_name, title, queue_job_id, targets)
            results.append(job)
            self.state_db.record_publish_job(job)

            self.audit_logger.log_decision(
                gate="publish",
                action=job.status,
                details={
                    "queue_job_id": queue_job_id,
                    "repo_name": repo_name,
                    "repo_url": job.repo_url,
                    "mirror_urls": job.mirror_urls,
                    "targets_status": job.targets_status,
                    "error": job.error,
                },
            )

            if job.status == "published":
                consecutive_failures = 0
                summary = ", ".join(f"{t}={s}" for t, s in job.targets_status.items())
                print(f"+ Published: {repo_name} [{summary}]")
                if job.error:
                    # Mirror partial-failure warning
                    print(f"  ! warning: {job.error}")
            else:
                consecutive_failures += 1
                print(f"x Failed to publish {queue_job_id}: {job.error}")
                if consecutive_failures >= 3:
                    print("! Publish gate: 3 consecutive failures, halting remaining")
                    break

        return results

    def _publish_with_mirrors(
        self,
        project_dir: Path,
        repo_name: str,
        title: str,
        queue_job_id: str,
        targets: list[str],
    ) -> PublishJob:
        """Publish to primary target, then to each mirror. Primary failure fails the job."""
        if not targets:
            return PublishJob(
                build_job_id=queue_job_id,
                title=title,
                repo_name=repo_name,
                status="failed",
                error="no publish targets configured",
                project_dir=str(project_dir),
            )

        primary = targets[0]
        mirrors = targets[1:]
        targets_status: dict[str, str] = {}
        mirror_urls: list[str] = []

        primary_status, primary_url, primary_error = self._publish_to_target(
            primary, project_dir, repo_name, title, remote_name="origin"
        )
        targets_status[primary] = primary_status if primary_status == "published" else f"failed: {primary_error}"

        if primary_status != "published":
            return PublishJob(
                build_job_id=queue_job_id,
                title=title,
                repo_name=repo_name,
                repo_url=None,
                status="failed",
                error=f"{primary} (primary): {primary_error}",
                project_dir=str(project_dir),
                targets_status=targets_status,
                mirror_urls=[],
            )

        mirror_errors: list[str] = []
        for m in mirrors:
            m_status, m_url, m_error = self._publish_to_target(
                m, project_dir, repo_name, title, remote_name=m
            )
            targets_status[m] = m_status if m_status == "published" else f"failed: {m_error}"
            if m_status == "published" and m_url:
                mirror_urls.append(m_url)
            else:
                mirror_errors.append(f"{m}: {m_error}")

        return PublishJob(
            build_job_id=queue_job_id,
            title=title,
            repo_name=repo_name,
            repo_url=primary_url,
            status="published",
            error=("mirror failures: " + "; ".join(mirror_errors)) if mirror_errors else None,
            project_dir=str(project_dir),
            published_at=datetime.now(),
            targets_status=targets_status,
            mirror_urls=mirror_urls,
        )

    def _publish_to_target(
        self,
        target: str,
        project_dir: Path,
        repo_name: str,
        title: str,
        remote_name: str,
    ) -> tuple[str, str | None, str | None]:
        """Dispatch to the per-host publisher. Returns (status, repo_url, error)."""
        if target == "github":
            return self._publish_github(project_dir, repo_name, title, remote_name)
        if target == "gitlab":
            return self._publish_gitlab(project_dir, repo_name, title, remote_name)
        return ("failed", None, f"unknown publish target: {target}")

    # --- target URL helpers --------------------------------------------------

    def _target_repo_url(self, target: str, repo_name: str) -> str:
        if target == "github":
            return f"https://github.com/{self.config.github_org}/{repo_name}"
        if target == "gitlab":
            return f"https://{self.config.gitlab_host}/{self.config.gitlab_namespace}/{repo_name}"
        return ""

    def _target_git_url(self, target: str, repo_name: str) -> str:
        if target == "github":
            return f"https://github.com/{self.config.github_org}/{repo_name}.git"
        if target == "gitlab":
            return f"git@{self.config.gitlab_host}:{self.config.gitlab_namespace}/{repo_name}.git"
        return ""

    # --- GitHub publisher ---------------------------------------------------

    def _publish_github(
        self, project_dir: Path, repo_name: str, title: str, remote_name: str
    ) -> tuple[str, str | None, str | None]:
        full_name = f"{self.config.github_org}/{repo_name}"
        repo_url = self._target_repo_url("github", repo_name)
        git_url = self._target_git_url("github", repo_name)

        try:
            if not self._github_repo_exists(repo_name):
                private = self.config.publish_visibility != "public"
                # Use org-scoped POST to bypass `gh repo create` owner-probe (404 under classic PAT)
                create_result = subprocess.run(
                    [
                        "gh", "api",
                        f"orgs/{self.config.github_org}/repos",
                        "-f", f"name={repo_name}",
                        "-F", f"private={str(private).lower()}",
                        "-f", f"description={title[:255]}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if create_result.returncode != 0:
                    stderr = create_result.stderr.strip()
                    if "already exists" not in stderr and "name already exists" not in stderr:
                        return ("failed", None, f"gh api create failed: {stderr}")

            return self._push_to_remote(
                project_dir, remote_name, git_url, repo_url,
                target="github", repo_name=repo_name,
            )

        except subprocess.TimeoutExpired:
            return ("failed", None, "gh command timed out (60s)")
        except Exception as e:
            return ("failed", None, f"unexpected error: {e}")

    def _github_repo_exists(self, repo_name: str) -> bool:
        full_name = f"{self.config.github_org}/{repo_name}"
        result = subprocess.run(
            ["gh", "repo", "view", full_name, "--json", "name"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0

    # --- GitLab publisher ---------------------------------------------------

    def _publish_gitlab(
        self, project_dir: Path, repo_name: str, title: str, remote_name: str
    ) -> tuple[str, str | None, str | None]:
        token = os.environ.get("GITLAB_TOKEN", "").strip()
        if not token:
            return ("failed", None, "GITLAB_TOKEN not set in environment")

        repo_url = self._target_repo_url("gitlab", repo_name)
        git_url = self._target_git_url("gitlab", repo_name)
        api_base = f"https://{self.config.gitlab_host}/api/v4"

        try:
            if not self._gitlab_project_exists(repo_name, token, api_base):
                visibility = "public" if self.config.publish_visibility == "public" else "private"
                create_result = subprocess.run(
                    [
                        "curl", "-sS", "--max-time", "60",
                        "-o", "/dev/null", "-w", "%{http_code}",
                        "-X", "POST",
                        "-H", f"PRIVATE-TOKEN: {token}",
                        "-H", "Content-Type: application/json",
                        f"{api_base}/projects",
                        "-d", json.dumps({
                            "name": repo_name,
                            "path": repo_name,
                            "namespace_id": self.config.gitlab_namespace_id,
                            "visibility": visibility,
                            "description": title[:255],
                            "initialize_with_readme": False,
                        }),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=70,
                )
                http_code = create_result.stdout.strip()
                # 201 = created, 400 with "has already been taken" handled separately
                if http_code != "201":
                    if not self._gitlab_project_exists(repo_name, token, api_base):
                        return ("failed", None, f"gitlab create failed: HTTP {http_code} {create_result.stderr.strip()}")

            return self._push_to_remote(
                project_dir, remote_name, git_url, repo_url,
                target="gitlab", repo_name=repo_name,
            )

        except subprocess.TimeoutExpired:
            return ("failed", None, "gitlab api timed out")
        except Exception as e:
            return ("failed", None, f"unexpected error: {e}")

    def _gitlab_project_exists(self, repo_name: str, token: str, api_base: str) -> bool:
        path = urllib.parse.quote(f"{self.config.gitlab_namespace}/{repo_name}", safe="")
        result = subprocess.run(
            [
                "curl", "-sS", "--max-time", "30",
                "-o", "/dev/null", "-w", "%{http_code}",
                "-H", f"PRIVATE-TOKEN: {token}",
                f"{api_base}/projects/{path}",
            ],
            capture_output=True,
            text=True,
            timeout=35,
        )
        return result.stdout.strip() == "200"

    # --- shared git push -----------------------------------------------------

    def _push_to_remote(
        self,
        project_dir: Path,
        remote_name: str,
        git_url: str,
        repo_url: str,
        target: str = "github",
        repo_name: str | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Push local main (if present) + current feature branch to a named remote.

        After pushing, ensure the host's default_branch is `main` via a PATCH
        call (gh api for GitHub, glab api for GitLab). PATCH failures are
        logged as warnings but do NOT fail the publish — the artifact is
        already on the remote and default-branch can be repaired out-of-band.
        Idempotent: re-running on an already-correct repo is a no-op.
        """
        try:
            existing = subprocess.run(
                ["git", "-C", str(project_dir), "remote", "get-url", remote_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if existing.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(project_dir), "remote", "set-url", remote_name, git_url],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                subprocess.run(
                    ["git", "-C", str(project_dir), "remote", "add", remote_name, git_url],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            branch_result = subprocess.run(
                ["git", "-C", str(project_dir), "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            current_branch = branch_result.stdout.strip() or "main"

            # 1. Push local `main` first (if it exists), so GitHub auto-elects
            #    main as default_branch on first push to a fresh repo. Idempotent
            #    against existing remote main (push is a no-op if up-to-date).
            local_main_exists = subprocess.run(
                [
                    "git", "-C", str(project_dir),
                    "rev-parse", "--verify", "--quiet", "refs/heads/main",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            ).returncode == 0

            if local_main_exists:
                push_main = subprocess.run(
                    ["git", "-C", str(project_dir), "push", "-u", remote_name, "main"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if push_main.returncode != 0:
                    return (
                        "failed",
                        None,
                        f"git push main to {remote_name} failed: {push_main.stderr.strip()}",
                    )

            # 2. Push the currently checked-out branch (feature branch in the
            #    self-healing-daemon path). Skip if it's the same as main —
            #    we already pushed it above.
            if current_branch and current_branch != "main":
                push_result = subprocess.run(
                    ["git", "-C", str(project_dir), "push", "-u", remote_name, current_branch],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if push_result.returncode != 0:
                    return (
                        "failed",
                        None,
                        f"git push to {remote_name} failed: {push_result.stderr.strip()}",
                    )
            elif not local_main_exists:
                # Edge case: no local main AND current branch is "main"-named or empty.
                # Push current_branch (defaulting to "main") so we don't leave the repo empty.
                push_result = subprocess.run(
                    ["git", "-C", str(project_dir), "push", "-u", remote_name, current_branch],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if push_result.returncode != 0:
                    return (
                        "failed",
                        None,
                        f"git push to {remote_name} failed: {push_result.stderr.strip()}",
                    )

            # 3. Defensively PATCH default_branch=main on the remote. Idempotent —
            #    if main is already the default, GitHub/GitLab return 200 with no
            #    change. If main does not yet exist on the remote (e.g. local
            #    repo had no main and we only pushed a feature branch), the API
            #    will reject the PATCH; we log a warning and continue.
            self._ensure_default_branch_main(target, repo_name)

            return ("published", repo_url, None)

        except subprocess.TimeoutExpired:
            return ("failed", None, f"git push to {remote_name} timed out (120s)")
        except Exception as e:
            return ("failed", None, f"push error: {e}")

    def _ensure_default_branch_main(self, target: str, repo_name: str | None) -> None:
        """Best-effort PATCH of default_branch=main on the remote host.

        Errors here NEVER fail the publish — the artifact has already been
        pushed by the time we reach this call. A failure means the repo's
        default_branch may still be wrong, but that's recoverable out-of-band
        and is strictly better than failing the whole publish over a
        post-push fix-up.
        """
        if not repo_name:
            return
        try:
            if target == "github":
                full_name = f"{self.config.github_org}/{repo_name}"
                result = subprocess.run(
                    [
                        "gh", "api",
                        f"repos/{full_name}",
                        "--method", "PATCH",
                        "-f", "default_branch=main",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    print(
                        f"  ! warning: failed to set default_branch=main on github/{full_name}: "
                        f"{result.stderr.strip()}"
                    )
            elif target == "gitlab":
                # `glab api projects/:fullpath --method PUT -f default_branch=main`
                # The GitLab API accepts URL-encoded "namespace%2Frepo" as the
                # project identifier. glab handles URL-encoding via the
                # `projects/<encoded>` form.
                project_path = urllib.parse.quote(
                    f"{self.config.gitlab_namespace}/{repo_name}", safe=""
                )
                result = subprocess.run(
                    [
                        "glab", "api",
                        f"projects/{project_path}",
                        "--method", "PUT",
                        "-f", "default_branch=main",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    print(
                        f"  ! warning: failed to set default_branch=main on "
                        f"gitlab/{self.config.gitlab_namespace}/{repo_name}: "
                        f"{result.stderr.strip()}"
                    )
        except subprocess.TimeoutExpired:
            print(f"  ! warning: default_branch PATCH on {target}/{repo_name} timed out")
        except Exception as e:
            print(f"  ! warning: default_branch PATCH on {target}/{repo_name} raised: {e}")

    # --- project dir + name helpers (unchanged from original) ----------------

    def _resolve_project_dir(
        self, queue_job_id: str, title: str = "", stored_dir: str | None = None
    ) -> Path | None:
        # CLEANUP-B (2026-05-12): the legacy yce-harness/generations fallback
        # path was removed. SelfHealingAdapter and OzAdapter both persist
        # project_dir via build_jobs.project_dir, which the caller passes
        # in as stored_dir. If stored_dir is missing or not a git repo,
        # there's nothing to fall back to anymore.
        if stored_dir:
            stored = Path(stored_dir)
            if stored.is_dir() and (stored / ".git").is_dir():
                return stored

        return None

    def _derive_repo_name(self, title: str) -> str:
        name = title.split(":")[0].strip()
        name = name.lower()
        name = re.sub(r"[^a-z0-9-]", "-", name)
        name = re.sub(r"-+", "-", name)
        name = name.strip("-")
        if len(name) > 100:
            name = name[:100].rstrip("-")
        return name or "unnamed-project"
