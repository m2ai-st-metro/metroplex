"""
Review Gate - Gate 4.5 (between Build and Publish)
Automated quality checks on completed builds before they can be published.
Lightweight file-system checks — no LLM calls.
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import Config
from db import StateDB
from audit import AuditLogger

logger = logging.getLogger(__name__)

# Files that indicate a project has documentation
DOC_FILES = {"README.md", "README.rst", "README.txt", "README"}

# Extensions that indicate source code
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".rb", ".php", ".cs", ".cpp", ".c", ".swift", ".kt",
}

# Patterns that suggest leaked secrets (filenames)
SECRET_FILE_PATTERNS = {
    ".env", ".env.local", ".env.production",
    "credentials.json", "service-account.json",
    "id_rsa", "id_ed25519",
}

# Max file size that suggests a binary blob was committed (10MB)
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


@dataclass
class ReviewResult:
    """Result of reviewing a single build."""
    queue_job_id: str
    title: str
    verdict: str  # "pass", "fail", "skip"
    checks_passed: list[str]
    checks_failed: list[str]
    reviewed_at: datetime


class ReviewGate:
    """Gate 4.5: Automated code review for completed builds."""

    def __init__(
        self,
        config: Config,
        state_db: StateDB,
        audit_logger: AuditLogger,
    ):
        self.config = config
        self.state_db = state_db
        self.audit_logger = audit_logger

    def run(self, dry_run: bool = False) -> list[ReviewResult]:
        """
        Review completed builds that haven't been reviewed yet.

        Builds must pass review before they're eligible for publish.
        A build that fails review is marked 'review_failed' and skipped
        by the publish gate.

        Returns:
            List of ReviewResult objects
        """
        reviewable = self.state_db.get_reviewable_builds()
        if not reviewable:
            return []

        results = []
        for build in reviewable:
            queue_job_id = build["queue_job_id"]
            title = build["title"]
            project_dir = build.get("project_dir")

            if not project_dir or not Path(project_dir).is_dir():
                result = ReviewResult(
                    queue_job_id=queue_job_id,
                    title=title,
                    verdict="skip",
                    checks_passed=[],
                    checks_failed=["project_dir_missing"],
                    reviewed_at=datetime.now(),
                )
                results.append(result)
                if not dry_run:
                    self.state_db.update_build_review_status(queue_job_id, "review_failed")
                    self.audit_logger.log_decision(
                        gate="review",
                        action="skip",
                        details={"queue_job_id": queue_job_id, "reason": "project dir not found"},
                    )
                continue

            passed, failed = self._run_checks(Path(project_dir))
            verdict = "pass" if not failed else "fail"

            result = ReviewResult(
                queue_job_id=queue_job_id,
                title=title,
                verdict=verdict,
                checks_passed=passed,
                checks_failed=failed,
                reviewed_at=datetime.now(),
            )
            results.append(result)

            if dry_run:
                status_label = "PASS" if verdict == "pass" else "FAIL"
                print(f"[DRY RUN] Review {status_label}: {title}")
                if failed:
                    print(f"  Failed: {', '.join(failed)}")
                continue

            new_status = "reviewed" if verdict == "pass" else "review_failed"
            self.state_db.update_build_review_status(queue_job_id, new_status)
            self.audit_logger.log_decision(
                gate="review",
                action=verdict,
                details={
                    "queue_job_id": queue_job_id,
                    "title": title,
                    "passed": passed,
                    "failed": failed,
                },
            )

            status_label = "PASS" if verdict == "pass" else "FAIL"
            print(f"  Review {status_label}: {title}")
            if failed:
                print(f"    Failed: {', '.join(failed)}")

        return results

    def _run_checks(self, project_dir: Path) -> tuple[list[str], list[str]]:
        """
        Run all quality checks on a project directory.

        Returns:
            Tuple of (passed_check_names, failed_check_names)
        """
        passed = []
        failed = []

        # 1. Has source code files
        if self._has_source_code(project_dir):
            passed.append("has_source_code")
        else:
            failed.append("has_source_code")

        # 2. Has documentation (README)
        if self._has_readme(project_dir):
            passed.append("has_readme")
        else:
            failed.append("has_readme")

        # 3. No secret files committed
        secrets = self._find_secret_files(project_dir)
        if not secrets:
            passed.append("no_secrets")
        else:
            failed.append(f"no_secrets({','.join(secrets)})")

        # 4. No oversized files (binary blobs)
        large_files = self._find_large_files(project_dir)
        if not large_files:
            passed.append("no_large_files")
        else:
            failed.append(f"no_large_files({len(large_files)} files >10MB)")

        # 5. Has git history (initialized repo)
        if (project_dir / ".git").is_dir():
            passed.append("has_git")
        else:
            failed.append("has_git")

        # 6. Reasonable file count (not empty, not bloated)
        file_count = sum(1 for _ in project_dir.rglob("*") if _.is_file() and ".git" not in _.parts)
        if 1 <= file_count <= 5000:
            passed.append(f"file_count_ok({file_count})")
        elif file_count == 0:
            failed.append("file_count_zero")
        else:
            failed.append(f"file_count_excessive({file_count})")

        return passed, failed

    def _has_source_code(self, project_dir: Path) -> bool:
        """Check if project has at least one source code file."""
        for f in project_dir.rglob("*"):
            if f.is_file() and f.suffix in CODE_EXTENSIONS and ".git" not in f.parts:
                return True
        return False

    def _has_readme(self, project_dir: Path) -> bool:
        """Check if project has a README file."""
        for name in DOC_FILES:
            if (project_dir / name).exists():
                return True
        return False

    def _find_secret_files(self, project_dir: Path) -> list[str]:
        """Find files that look like secrets."""
        found = []
        for f in project_dir.rglob("*"):
            if f.is_file() and f.name in SECRET_FILE_PATTERNS and ".git" not in f.parts:
                found.append(str(f.relative_to(project_dir)))
        return found

    def _find_large_files(self, project_dir: Path) -> list[str]:
        """Find files larger than MAX_FILE_SIZE_BYTES."""
        found = []
        for f in project_dir.rglob("*"):
            if f.is_file() and ".git" not in f.parts:
                try:
                    if f.stat().st_size > MAX_FILE_SIZE_BYTES:
                        found.append(str(f.relative_to(project_dir)))
                except OSError:
                    pass
        return found
