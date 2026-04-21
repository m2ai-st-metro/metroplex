"""
Publish Gate - Gate 4
Creates GitHub repos for completed YCE builds and pushes code to m2ai-portfolio org.
This is the L5 "last mile" -- turning generated code into visible GitHub repos.
"""
import re
import subprocess
from datetime import datetime
from pathlib import Path

from config import Config
from models import PublishJob
from db import StateDB
from audit import AuditLogger


class PublishGate:
    """Gate 4: Publish -- Push completed builds to GitHub."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        audit_logger: AuditLogger,
    ):
        """
        Initialize Publish Gate.

        Args:
            config: Metroplex configuration
            state_db: State database manager
            audit_logger: Audit logger
        """
        self.config = config
        self.state_db = state_db
        self.audit_logger = audit_logger

    def run(self, dry_run: bool = False) -> list[PublishJob]:
        """
        Run publish gate on completed but unpublished builds.

        Process flow:
        1. Query build_jobs for completed builds not in publish_jobs
        2. For each unpublished build:
           a. Resolve project directory from generations/<queue_job_id>/
           b. Derive repo name from title (kebab-case)
           c. Check if repo exists in org (skip if so, or update)
           d. Create GitHub repo via gh CLI
           e. Set git remote and push
           f. Record publish_jobs entry
        3. Enforce max_publish_per_cycle cap

        Args:
            dry_run: If True, print what would happen but don't create repos

        Returns:
            List of PublishJob objects
        """
        unpublished = self.state_db.get_unpublished_builds(
            require_review=self.config.require_review
        )

        if not unpublished:
            return []

        # Enforce per-cycle cap
        max_pub = self.config.max_publish_per_cycle
        if len(unpublished) > max_pub:
            unpublished = unpublished[:max_pub]

        results = []
        consecutive_failures = 0

        for build in unpublished:
            queue_job_id = build["queue_job_id"]
            title = build["title"]
            stored_dir = build.get("project_dir")

            # Resolve project directory
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

            # Derive repo name
            repo_name = self._derive_repo_name(title)

            if dry_run:
                print(f"[DRY RUN] Would publish: {queue_job_id}")
                print(f"  Title: {title}")
                print(f"  Repo:  {self.config.github_org}/{repo_name}")
                print(f"  Dir:   {project_dir}")
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

            # Actually publish
            status, repo_url, error = self._publish_to_github(
                project_dir, repo_name, title
            )

            job = PublishJob(
                build_job_id=queue_job_id,
                title=title,
                repo_name=repo_name,
                repo_url=repo_url,
                status=status,
                error=error,
                project_dir=str(project_dir),
                published_at=datetime.now() if status == "published" else None,
            )
            results.append(job)
            self.state_db.record_publish_job(job)

            self.audit_logger.log_decision(
                gate="publish",
                action=status,
                details={
                    "queue_job_id": queue_job_id,
                    "repo_name": repo_name,
                    "repo_url": repo_url,
                    "error": error,
                },
            )

            if status == "published":
                consecutive_failures = 0
                print(f"+ Published: {self.config.github_org}/{repo_name}")
            else:
                consecutive_failures += 1
                print(f"x Failed to publish {queue_job_id}: {error}")
                if consecutive_failures >= 3:
                    print("! Publish gate: 3 consecutive failures, halting remaining")
                    break

        return results

    def _resolve_project_dir(
        self, queue_job_id: str, title: str = "", stored_dir: str | None = None
    ) -> Path | None:
        """
        Resolve the generation directory for a build job.

        Resolution order:
        1. Stored project_dir from build_jobs (set by UM bridge writeback)
        2. generations/<queue_job_id>/ (direct queue_runner builds)
        3. Scan um-* directories for matching UM idea UUID prefix

        Args:
            queue_job_id: The build job queue ID
            title: Project title (unused, kept for interface compat)
            stored_dir: Pre-resolved path from build_jobs.project_dir

        Returns:
            Path to the project directory, or None if not found
        """
        # 1. Stored path from UM bridge writeback
        if stored_dir:
            stored = Path(stored_dir)
            if stored.is_dir() and (stored / ".git").is_dir():
                return stored

        # 2. Direct match (queue_runner builds)
        generations_dir = Path(self.config.yce_dir) / "generations"
        project_dir = generations_dir / queue_job_id
        if project_dir.is_dir() and (project_dir / ".git").is_dir():
            return project_dir

        return None

    def _derive_repo_name(self, title: str) -> str:
        """
        Derive a GitHub repo name from a project title.

        Rules:
        - Lowercase
        - Strip everything after first colon
        - Replace non-alphanumeric with hyphens
        - Collapse multiple hyphens
        - Truncate to 100 chars
        - Strip leading/trailing hyphens

        Args:
            title: Project title

        Returns:
            Kebab-case repo name
        """
        # Take text before colon if present
        name = title.split(":")[0].strip()

        # Lowercase
        name = name.lower()

        # Replace non-alphanumeric (except hyphens) with hyphens
        name = re.sub(r"[^a-z0-9-]", "-", name)

        # Collapse multiple hyphens
        name = re.sub(r"-+", "-", name)

        # Strip leading/trailing hyphens
        name = name.strip("-")

        # Truncate
        if len(name) > 100:
            name = name[:100].rstrip("-")

        return name or "unnamed-project"

    def _repo_exists(self, repo_name: str) -> bool:
        """
        Check if a repo already exists in the GitHub org.

        Args:
            repo_name: Repository name (without org prefix)

        Returns:
            True if repo exists
        """
        full_name = f"{self.config.github_org}/{repo_name}"
        result = subprocess.run(
            ["gh", "repo", "view", full_name, "--json", "name"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0

    def _publish_to_github(
        self, project_dir: Path, repo_name: str, title: str
    ) -> tuple[str, str | None, str | None]:
        """
        Create a GitHub repo and push code.

        Args:
            project_dir: Local path to the project
            repo_name: Desired repo name
            title: Project title (used as repo description)

        Returns:
            Tuple of (status, repo_url, error)
            status is 'published' or 'failed'
        """
        full_name = f"{self.config.github_org}/{repo_name}"
        repo_url = f"https://github.com/{full_name}"

        try:
            # Check if repo already exists
            if self._repo_exists(repo_name):
                # Repo exists -- just set remote and push (handles re-runs)
                return self._push_existing(project_dir, full_name, repo_url)

            # Create new repo via the org-scoped API endpoint.
            # NOTE: `gh repo create <org>/<name>` probes /users/<owner> to resolve
            # owner type before creating, which 404s for orgs under classic-PAT
            # auth (observed on gh v2.88.1). Calling POST /orgs/<org>/repos
            # directly bypasses the probe.
            private = self.config.publish_visibility != "public"
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
                # If "already exists" (422), fall through to push_existing
                if "already exists" in stderr or "name already exists" in stderr:
                    return self._push_existing(project_dir, full_name, repo_url)
                return ("failed", None, f"gh api create failed: {stderr}")

            # Repo created -- now set remote and push
            return self._push_existing(project_dir, full_name, repo_url)

        except subprocess.TimeoutExpired:
            return ("failed", None, "gh command timed out (60s)")
        except Exception as e:
            return ("failed", None, f"unexpected error: {str(e)}")

    def _push_existing(
        self, project_dir: Path, full_name: str, repo_url: str
    ) -> tuple[str, str | None, str | None]:
        """
        Push to an existing repo (set remote origin and push).

        Args:
            project_dir: Local project directory
            full_name: Full repo name (org/repo)
            repo_url: GitHub URL

        Returns:
            Tuple of (status, repo_url, error)
        """
        git_url = f"https://github.com/{full_name}.git"

        try:
            # Check if remote 'origin' exists
            remote_result = subprocess.run(
                ["git", "-C", str(project_dir), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if remote_result.returncode == 0:
                # Remote exists -- update it
                subprocess.run(
                    ["git", "-C", str(project_dir), "remote", "set-url", "origin", git_url],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                # Add remote
                subprocess.run(
                    ["git", "-C", str(project_dir), "remote", "add", "origin", git_url],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            # Push
            push_result = subprocess.run(
                ["git", "-C", str(project_dir), "push", "-u", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if push_result.returncode != 0:
                stderr = push_result.stderr.strip()
                # Try pushing whatever the current branch is
                branch_result = subprocess.run(
                    ["git", "-C", str(project_dir), "branch", "--show-current"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                branch = branch_result.stdout.strip() or "main"
                if branch != "main":
                    push_result = subprocess.run(
                        ["git", "-C", str(project_dir), "push", "-u", "origin", branch],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if push_result.returncode != 0:
                        return ("failed", None, f"git push failed: {push_result.stderr.strip()}")
                else:
                    return ("failed", None, f"git push failed: {stderr}")

            return ("published", repo_url, None)

        except subprocess.TimeoutExpired:
            return ("failed", None, "git push timed out (120s)")
        except Exception as e:
            return ("failed", None, f"push error: {str(e)}")
