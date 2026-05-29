"""
Review Gate - Gate 4.5 (between Build and Publish)
Automated quality checks on completed builds before they can be published.
Lightweight file-system checks — no LLM calls.
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import Config
from db import StateDB
from audit import AuditLogger
from quality_ratchet import get_test_coverage_threshold

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

# Directories to skip when scanning (dependency/build artifacts, not project source)
IGNORED_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", ".next", ".nuxt", ".output",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "target", "vendor", ".git",
}

# Directory names that indicate a runtime/persisted data store (per-user state,
# incident logs, append-only audit trails). Presence of one of these gates the
# safety hard-checks below — projects with no data dir are unaffected.
DATA_DIR_NAMES = {"data", "logs", "log", "var", "store", "storage"}

# Marker that lets a fixture/maintainer opt a file out of the PII/data-path
# hard-checks (e.g. a legitimate sample log). Must appear in the file's first
# 4KB. Kept as an explicit, greppable allowlist to avoid silent false positives.
SAFETY_ALLOWLIST_MARKER = "metroplex:review-allow"

# Static source-scan signal: a default data path that resolves to the install
# directory. Any of these substrings in a *.py source file is a hard-fail —
# per-user runtime data must never default into the package install dir
# (the #436 #4 cross-user comingling failure).
INSTALL_DIR_DATA_PATTERNS = (
    "Path(__file__).parent / 'data'",
    'Path(__file__).parent / "data"',
    "Path(__file__).parent/'data'",
    'Path(__file__).parent/"data"',
    "Path(__file__).resolve().parent / 'data'",
    'Path(__file__).resolve().parent / "data"',
    "os.path.dirname(__file__), 'data'",
    'os.path.dirname(__file__), "data"',
)


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

            project_path = Path(project_dir)
            passed, failed = self._run_checks(project_path)
            verdict = "pass" if not failed else "fail"

            # Compute and store test_ratio for ratchet tracking (Phase D2)
            files = list(self._walk_project_files(project_path))
            _, test_ratio, _, _ = self._check_test_coverage(project_path, files)
            if not dry_run:
                self.state_db.update_build_test_ratio(queue_job_id, test_ratio)

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

    def _walk_project_files(self, project_dir: Path):
        """Walk project files, skipping dependency/build/VCS directories."""
        for f in project_dir.rglob("*"):
            if f.is_file() and not (IGNORED_DIRS & set(f.relative_to(project_dir).parts)):
                yield f

    def _run_checks(self, project_dir: Path) -> tuple[list[str], list[str]]:
        """
        Run all quality checks on a project directory.
        Skips dependency/build directories (node_modules, venv, etc.).

        Returns:
            Tuple of (passed_check_names, failed_check_names)
        """
        passed = []
        failed = []

        # Collect files once for all checks
        files = list(self._walk_project_files(project_dir))

        # 1. Has source code files
        if any(f.suffix in CODE_EXTENSIONS for f in files):
            passed.append("has_source_code")
        else:
            failed.append("has_source_code")

        # 2. Has documentation (README)
        if self._has_readme(project_dir):
            passed.append("has_readme")
        else:
            failed.append("has_readme")

        # 3. No secret files committed
        secrets = [str(f.relative_to(project_dir)) for f in files if f.name in SECRET_FILE_PATTERNS]
        if not secrets:
            passed.append("no_secrets")
        else:
            failed.append(f"no_secrets({','.join(secrets)})")

        # 4. No oversized files (binary blobs)
        large_files = []
        for f in files:
            try:
                if f.stat().st_size > MAX_FILE_SIZE_BYTES:
                    large_files.append(str(f.relative_to(project_dir)))
            except OSError:
                pass
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
        file_count = len(files)
        if 1 <= file_count <= 5000:
            passed.append(f"file_count_ok({file_count})")
        elif file_count == 0:
            failed.append("file_count_zero")
        else:
            failed.append(f"file_count_excessive({file_count})")

        # 7. Has adequate tests (Phase D2 — test coverage enforcement)
        test_ok, test_ratio, test_count, non_test_count = self._check_test_coverage(
            project_dir, files
        )
        if test_ok:
            passed.append(f"has_adequate_tests(ratio={test_ratio:.2f},tests={test_count})")
        else:
            threshold = get_test_coverage_threshold(self.state_db)
            failed.append(
                f"has_adequate_tests(ratio={test_ratio:.2f}<{threshold:.2f},"
                f"tests={test_count},src={non_test_count})"
            )

        # 8-10. Safety hard-checks (zero-LLM, conditional on artifact presence).
        # These run only when the relevant artifact exists, so a minimal project
        # with no data dir / .gitignore is unaffected.
        self._check_safety(project_dir, files, passed, failed)

        return passed, failed

    def _is_data_artifact(self, path: Path, project_dir: Path) -> bool:
        """True if path lives under a runtime data dir OR is a *.jsonl log."""
        try:
            parts = path.relative_to(project_dir).parts
        except ValueError:
            return False
        if path.suffix == ".jsonl":
            return True
        return any(p in DATA_DIR_NAMES for p in parts[:-1])

    def _is_allowlisted(self, path: Path) -> bool:
        """True if the file carries the explicit review-allow marker."""
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            return False
        return SAFETY_ALLOWLIST_MARKER in head

    def _check_safety(
        self,
        project_dir: Path,
        files: list[Path],
        passed: list[str],
        failed: list[str],
    ) -> None:
        """Publish-time safety hard-checks (each blocking, clear message).

        Conditional on artifact presence:
          (a) runtime data dir / *.jsonl logs present  -> .gitignore must cover them
          (b) no tracked populated *.jsonl under a data dir (PII-shaped content)
          (c) default data path must not resolve to the install dir (static scan)

        Files carrying SAFETY_ALLOWLIST_MARKER are exempt from (a)/(b).
        """
        data_artifacts = [
            f for f in files
            if self._is_data_artifact(f, project_dir) and not self._is_allowlisted(f)
        ]

        # (a) gitignore-covers-data: only when a data artifact actually exists.
        if data_artifacts:
            gitignore = project_dir / ".gitignore"
            covered = False
            if gitignore.exists():
                try:
                    ign = gitignore.read_text(encoding="utf-8")
                except OSError:
                    ign = ""
                patterns = [
                    ln.strip() for ln in ign.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                rels = {
                    "/".join(f.relative_to(project_dir).parts) for f in data_artifacts
                }
                for pat in patterns:
                    norm = pat.strip("/").rstrip("/")
                    if not norm:
                        continue
                    if norm.endswith("*.jsonl") or norm == "*.jsonl":
                        if any(r.endswith(".jsonl") for r in rels):
                            covered = True
                            break
                    if any(norm == r.split("/")[0] or r.startswith(norm + "/") or norm in r.split("/")
                           for r in rels):
                        covered = True
                        break
            if covered:
                passed.append("gitignore_covers_data")
            else:
                failed.append(
                    "gitignore_covers_data(runtime data dir / *.jsonl present "
                    "but not covered by .gitignore — would publish per-user data)"
                )

        # (b) no populated PII-shaped jsonl tracked at publish.
        populated_logs = []
        for f in data_artifacts:
            if f.suffix != ".jsonl":
                continue
            try:
                if f.stat().st_size > 0 and f.read_text(encoding="utf-8", errors="replace").strip():
                    populated_logs.append("/".join(f.relative_to(project_dir).parts))
            except OSError:
                pass
        if data_artifacts:
            if populated_logs:
                failed.append(
                    f"no_pii_artifact(populated runtime log(s) tracked at publish: "
                    f"{','.join(populated_logs)})"
                )
            else:
                passed.append("no_pii_artifact")

        # (c) default data path must not resolve to the install dir.
        offenders = []
        for f in files:
            if f.suffix != ".py":
                continue
            if self._is_test_file(f, project_dir):
                continue
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if SAFETY_ALLOWLIST_MARKER in src:
                continue
            if any(pat in src for pat in INSTALL_DIR_DATA_PATTERNS):
                offenders.append("/".join(f.relative_to(project_dir).parts))
        if offenders:
            failed.append(
                f"data_path_not_install_dir(default data path resolves to install "
                f"dir in: {','.join(offenders)})"
            )
        else:
            passed.append("data_path_not_install_dir")

    def _is_test_file(self, path: Path, project_root: Path) -> bool:
        """Check if a file is a test file based on naming patterns.

        Matches quality_scorer._is_test_file logic for consistency.
        """
        name = path.name.lower()
        if any(p in name for p in ("test_", "_test.", ".test.", ".spec.")):
            return True
        try:
            rel_parts = path.relative_to(project_root).parts
            return any(p in ("tests", "test", "__tests__") for p in rel_parts[:-1])
        except ValueError:
            pass
        return False

    def _check_test_coverage(
        self, project_dir: Path, files: list[Path]
    ) -> tuple[bool, float, int, int]:
        """Check test file coverage against dynamic threshold.

        Returns:
            (passed, test_ratio, test_count, non_test_count)
        """
        source_files = [f for f in files if f.suffix in CODE_EXTENSIONS]
        test_files = [f for f in source_files if self._is_test_file(f, project_dir)]
        non_test_files = [f for f in source_files if not self._is_test_file(f, project_dir)]

        test_count = len(test_files)
        non_test_count = len(non_test_files)

        # Exemption: fewer than 3 non-test source files
        if non_test_count < 3:
            return True, 0.0, test_count, non_test_count

        # Compute ratio
        test_ratio = test_count / non_test_count if non_test_count > 0 else 0.0

        # Hard floor: at least 1 test file
        if test_count < 1:
            return False, test_ratio, test_count, non_test_count

        # Dynamic threshold from ratchet
        threshold = get_test_coverage_threshold(self.state_db)

        passed = test_ratio >= threshold
        return passed, test_ratio, test_count, non_test_count

    def _has_readme(self, project_dir: Path) -> bool:
        """Check if project has a README file."""
        for name in DOC_FILES:
            if (project_dir / name).exists():
                return True
        return False
