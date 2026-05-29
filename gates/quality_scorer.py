"""
Structural Quality Scorer - Phase 14b
Computes a 0-100 quality score from static file analysis.
No code execution — all metrics are file-system based.

Score components:
  Static metrics  (60 points max): file structure, tests, docs, hygiene
  Tyrest scores   (40 points max): LLM-assessed quality (if available)

Category gate (added 2026-05-11 per R-A item 4 of ST_METRO_LIFE_DOMAIN_PIVOT):
  When called with ``scoring_rubric='life_domain'``, the scorer first checks
  that the project conforms to the CCOS agent shape:
    - ``agent.yaml`` at project root
    - at least one ``skills/<name>/SKILL.md``
    - at least one detectable E2E test
  If any check fails, the result has ``category_failed=True``,
  ``total_score=0.0``, and a ``category_failure_reason`` string. No further
  numeric scoring is performed. Without the rubric arg (or with
  ``scoring_rubric='tech'``), the gate is bypassed entirely.
"""
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Reuse ReviewGate constants for consistency
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".rb", ".php", ".cs", ".cpp", ".c", ".swift", ".kt",
}

TEST_PATTERNS = {
    "test_", "_test.", ".test.", ".spec.", "tests/", "test/",
    "__tests__/",
}

CONFIG_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".lock",
}

IGNORED_DIRS = {
    "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", ".next", ".nuxt", ".output",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "target", "vendor", ".git",
}


# Category-gate constants
VALID_RUBRICS = {"life_domain", "tech"}

# E2E test file recognized by filename or by being under an e2e/ directory.
# Python: test_e2e.py, test_e2e_anything.py, anything_e2e_test.py
# JS/TS:  *.e2e.ts, *.e2e.spec.ts, *.e2e.test.ts (and .js/.tsx/.jsx variants)
_E2E_FILENAME_RE = re.compile(
    r"^("
    r"test_e2e.*\.py"                                   # python
    r"|.*_e2e_test\.py"                                 # python alt
    r"|.*\.e2e(?:\.(?:spec|test))?\.(?:js|jsx|ts|tsx)"  # js/ts e2e
    r"|e2e\.(?:spec|test)\.(?:js|jsx|ts|tsx)"           # bare e2e.spec.ts
    r")$",
    re.IGNORECASE,
)

# Test-shaped file (any language) when located under an e2e/ directory.
_TEST_UNDER_E2E_DIR_RE = re.compile(
    r"^("
    r"test_.*\.py|.*_test\.py"                          # python
    r"|.*\.(?:spec|test)\.(?:js|jsx|ts|tsx)"            # js/ts spec/test
    r"|.*_test\.(?:js|jsx|ts|tsx)"                      # go-style js/ts
    r")$",
    re.IGNORECASE,
)

# Extensions scanned when looking for E2E tests. Mirrors CODE_EXTENSIONS for
# test-bearing languages used in the ClaudeClaw agent ecosystem.
_E2E_SCAN_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}


@dataclass
class QualityBreakdown:
    """Breakdown of quality score components."""
    # Static metrics (60 max)
    has_source_code: float = 0.0     # 10 pts
    has_readme: float = 0.0          # 8 pts
    has_gitignore: float = 0.0       # 4 pts
    has_tests: float = 0.0           # 12 pts
    test_ratio: float = 0.0          # 8 pts (test files / source files)
    no_secrets: float = 0.0          # 8 pts
    file_count_healthy: float = 0.0  # 6 pts
    has_dependency_file: float = 0.0 # 4 pts

    # Tyrest scores (40 max)
    tyrest_overall: float = 0.0      # 40 pts (scaled from 0-1)

    # Metadata
    source_file_count: int = 0
    test_file_count: int = 0
    total_file_count: int = 0
    tyrest_available: bool = False

    # Category gate (R-A item 4)
    category_failed: bool = False
    category_failure_reason: str | None = None

    @property
    def static_score(self) -> float:
        """Sum of static metric points (0-60)."""
        return (
            self.has_source_code + self.has_readme + self.has_gitignore +
            self.has_tests + self.test_ratio + self.no_secrets +
            self.file_count_healthy + self.has_dependency_file
        )

    @property
    def total_score(self) -> float:
        """Total quality score (0-100). 0.0 when category_failed."""
        if self.category_failed:
            return 0.0
        return round(self.static_score + self.tyrest_overall, 1)


SECRET_FILE_PATTERNS = {
    ".env", ".env.local", ".env.production",
    "credentials.json", "service-account.json",
    "id_rsa", "id_ed25519",
}

DEPENDENCY_FILES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "Cargo.toml", "go.mod", "Gemfile",
    "pom.xml", "build.gradle",
}


def _walk_project_files(project_dir: Path) -> list[Path]:
    """Walk project files, skipping dependency/build/VCS directories."""
    files = []
    for f in project_dir.rglob("*"):
        if f.is_file() and not (IGNORED_DIRS & set(f.relative_to(project_dir).parts)):
            files.append(f)
    return files


def _is_test_file(path: Path, project_root: Path | None = None) -> bool:
    """Check if a file is a test file based on naming patterns.

    Uses filename + relative path from project root (not absolute path,
    to avoid false positives from parent directory names).
    """
    name = path.name.lower()
    # Check filename patterns
    if any(p in name for p in ("test_", "_test.", ".test.", ".spec.")):
        return True
    # Check if file is under a tests/ or __tests__/ directory
    if project_root is not None:
        try:
            rel_parts = path.relative_to(project_root).parts
            return any(p in ("tests", "test", "__tests__") for p in rel_parts[:-1])
        except ValueError:
            pass
    return False


def _has_agent_yaml(project_dir: Path) -> bool:
    """Check for agent.yaml at the project root.

    Does not follow symlinks (defends against symlink-to-outside-tree
    spoofing the gate). Empty files pass — YAML parsing is out of scope
    for this presence-only gate.
    """
    candidate = project_dir / "agent.yaml"
    try:
        # Path.is_file() follows symlinks by default; reject symlinks
        # explicitly so a symlink pointing outside the project tree
        # cannot satisfy the gate.
        if candidate.is_symlink():
            return False
        return candidate.is_file()
    except OSError:
        return False


def _has_skill_manifest(project_dir: Path) -> bool:
    """Check for at least one ``skills/<name>/SKILL.md`` two levels deep.

    A bare ``SKILL.md`` at project root or ``skills/SKILL.md`` (one level
    deep) does NOT qualify — the bundled-skills convention is exactly
    ``skills/<name>/SKILL.md``.
    """
    skills_root = project_dir / "skills"
    if not skills_root.is_dir() or skills_root.is_symlink():
        return False
    try:
        for child in skills_root.iterdir():
            if child.is_symlink():
                continue  # don't follow symlinks across the gate
            if not child.is_dir():
                continue
            manifest = child / "SKILL.md"
            if manifest.is_file() and not manifest.is_symlink():
                return True
    except OSError:
        return False
    return False


def _has_e2e_test(project_dir: Path) -> bool:
    """Detect at least one E2E test via filesystem heuristics.

    Qualifying patterns (any language in the ClaudeClaw agent ecosystem):
      - Python filename matches ``^test_e2e.*\\.py$`` (case-insensitive)
      - Python filename matches ``.*_e2e_test\\.py$`` (case-insensitive)
      - JS/TS filename matches ``*.e2e[.spec|test]?.{js,jsx,ts,tsx}``
      - JS/TS filename matches ``e2e.{spec|test}.{js,jsx,ts,tsx}``
      - file lives under any directory segment named ``e2e`` AND has a
        test-shaped filename for its language

    Symlinks are NOT followed. Excludes IGNORED_DIRS (node_modules,
    venv, etc.).
    """
    try:
        for f in project_dir.rglob("*"):
            if f.suffix.lower() not in _E2E_SCAN_EXTENSIONS:
                continue
            try:
                rel = f.relative_to(project_dir)
            except ValueError:
                continue
            if IGNORED_DIRS & set(rel.parts):
                continue
            if not f.is_file() or f.is_symlink():
                continue
            name = f.name
            if _E2E_FILENAME_RE.match(name):
                return True
            # Under an e2e/ directory, treat any test-shaped file as E2E
            if "e2e" in [p.lower() for p in rel.parts[:-1]]:
                if _TEST_UNDER_E2E_DIR_RE.match(name):
                    return True
    except OSError:
        return False
    return False


def _check_category_gate(project_dir: Path) -> str | None:
    """Apply the life_domain category gate.

    Returns:
        None if all three shape checks pass; otherwise a reason string
        identifying the first failed check. Failure precedence:
        agent_yaml -> skill_manifest -> e2e_test.
    """
    if not _has_agent_yaml(project_dir):
        return "missing_agent_yaml"
    if not _has_skill_manifest(project_dir):
        return "missing_skill_manifest"
    if not _has_e2e_test(project_dir):
        return "missing_e2e_test"
    return None


def score_project(
    project_dir: Path,
    tyrest_overall: float | None = None,
    scoring_rubric: str | None = None,
    strict_rubric: bool = False,
) -> QualityBreakdown:
    """
    Score a project directory for structural quality.

    Args:
        project_dir: Path to the project root
        tyrest_overall: Tyrest overall score (0.0-1.0) if available
        scoring_rubric: Optional rubric selector. When ``'life_domain'``,
            applies the category gate (agent.yaml + skills/<n>/SKILL.md
            + E2E test); on failure returns category_failed result with
            total_score=0.0. When ``'tech'`` or ``None`` (default), no
            gate is applied — existing behavior is preserved exactly.
            Unknown values fall open to default behavior (logged as a
            WARNING so misconfigurations are visible in operator logs;
            failing closed would unnecessarily reject good builds on the
            publish path).
        strict_rubric: When True, an unknown ``scoring_rubric`` value
            raises ``ValueError`` instead of falling open. Defaults
            False to preserve fail-open behavior on the publish path;
            opt-in for callers in security-sensitive paths or for tests.

    Caller wiring (R-A item 3, separate self-healing-claudex run):
        This function accepts the rubric; production callers in
        ``metroplex.py`` and ``orchestrator.py`` do NOT yet forward
        ``ideaforge.scoring_rubric`` from the build queue. Until R-A
        item 3 lands, the gate is callable but unused in production —
        intentional: R-A item 4 (this run) ships the gate only.

    Returns:
        QualityBreakdown with per-metric scores and total. When
        category_failed, all numeric fields stay at defaults (0.0) and
        category_failure_reason is populated.

    Raises:
        ValueError: only when ``strict_rubric=True`` and ``scoring_rubric``
            is not in the set of valid rubrics.
    """
    breakdown = QualityBreakdown()

    if not project_dir.is_dir():
        return breakdown

    # Category gate — opt-in via scoring_rubric='life_domain'.
    # Default policy on unknown rubric: fail OPEN with WARNING (publish-path
    # safety). Callers in security-sensitive paths can pass strict_rubric=True
    # to convert this into a hard ValueError instead.
    if scoring_rubric is not None and scoring_rubric not in VALID_RUBRICS:
        if strict_rubric:
            raise ValueError(
                f"unknown scoring_rubric={scoring_rubric!r}; "
                f"expected one of {sorted(VALID_RUBRICS)} or None"
            )
        logger.warning(
            "score_project: unknown scoring_rubric=%r; falling open to "
            "default behavior (no category gate)",
            scoring_rubric,
        )
        scoring_rubric = None

    if scoring_rubric == "life_domain":
        reason = _check_category_gate(project_dir)
        if reason is not None:
            breakdown.category_failed = True
            breakdown.category_failure_reason = reason
            logger.info(
                "category_failed: project=%s reason=%s",
                project_dir, reason,
            )
            return breakdown

    files = _walk_project_files(project_dir)
    breakdown.total_file_count = len(files)

    source_files = [f for f in files if f.suffix in CODE_EXTENSIONS]
    test_files = [f for f in source_files if _is_test_file(f, project_dir)]
    non_test_source = [f for f in source_files if not _is_test_file(f, project_dir)]

    breakdown.source_file_count = len(source_files)
    breakdown.test_file_count = len(test_files)

    # 1. Has source code (10 pts)
    if source_files:
        breakdown.has_source_code = 10.0

    # 2. Has README (8 pts)
    readme_names = {"README.md", "README.rst", "README.txt", "README"}
    if any((project_dir / name).exists() for name in readme_names):
        breakdown.has_readme = 8.0

    # 3. Has .gitignore (4 pts)
    if (project_dir / ".gitignore").exists():
        breakdown.has_gitignore = 4.0

    # 4. Has test files (12 pts)
    if test_files:
        breakdown.has_tests = 12.0

    # 5. Test ratio (8 pts) — scales linearly from 0% to 30%+ test files
    if non_test_source:
        ratio = len(test_files) / len(non_test_source)
        # 30% or more test files = full points
        breakdown.test_ratio = round(min(ratio / 0.3, 1.0) * 8.0, 1)

    # 6. No secrets (8 pts)
    secret_files = [f for f in files if f.name in SECRET_FILE_PATTERNS]
    if not secret_files:
        breakdown.no_secrets = 8.0

    # 7. Healthy file count (6 pts) — penalize empty or bloated
    fc = len(files)
    if 3 <= fc <= 500:
        breakdown.file_count_healthy = 6.0
    elif 1 <= fc < 3:
        breakdown.file_count_healthy = 3.0
    elif 500 < fc <= 2000:
        breakdown.file_count_healthy = 3.0
    # else 0 (empty or >2000)

    # 8. Has dependency file (4 pts)
    if any((project_dir / name).exists() for name in DEPENDENCY_FILES):
        breakdown.has_dependency_file = 4.0

    # Tyrest component (40 pts)
    if tyrest_overall is not None:
        breakdown.tyrest_available = True
        breakdown.tyrest_overall = round(tyrest_overall * 40.0, 1)

    return breakdown
