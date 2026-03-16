"""
Structural Quality Scorer - Phase 14b
Computes a 0-100 quality score from static file analysis.
No code execution — all metrics are file-system based.

Score components:
  Static metrics  (60 points max): file structure, tests, docs, hygiene
  Tyrest scores   (40 points max): LLM-assessed quality (if available)
"""
import logging
from dataclasses import dataclass, field
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
        """Total quality score (0-100)."""
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


def score_project(
    project_dir: Path,
    tyrest_overall: float | None = None,
) -> QualityBreakdown:
    """
    Score a project directory for structural quality.

    Args:
        project_dir: Path to the project root
        tyrest_overall: Tyrest overall score (0.0-1.0) if available

    Returns:
        QualityBreakdown with per-metric scores and total
    """
    breakdown = QualityBreakdown()

    if not project_dir.is_dir():
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
