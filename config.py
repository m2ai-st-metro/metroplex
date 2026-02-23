"""
Metroplex Configuration
Loads all settings from environment variables with fallback defaults.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Metroplex configuration loaded from environment variables."""

    # Database paths
    ideaforge_db: str = field(default="/home/apexaipc/projects/ideaforge/data/ideaforge.db")
    um_db: str = field(default="/home/apexaipc/projects/ultra-magnus/idea-factory/data/idea-factory.db")
    stfactory_db: str = field(default="/home/apexaipc/projects/st-factory/data/persona_metrics.db")

    # Directory paths
    yce_dir: str = field(default="/home/apexaipc/projects/yce-harness")

    # GitHub repo
    academy_repo: str = field(default="m2ai-portfolio/agent-persona-academy")

    # Model settings
    build_model: str = field(default="opus")

    # Scoring thresholds
    approve_threshold: int = field(default=70)
    reject_threshold: int = field(default=40)

    # Cycle limits
    max_approve_per_cycle: int = field(default=3)
    max_patches_per_cycle: int = field(default=5)

    # Circuit breaker
    circuit_breaker_threshold: int = field(default=3)

    def __post_init__(self):
        """Load values from environment variables."""
        self.ideaforge_db = os.environ.get("METROPLEX_IDEAFORGE_DB", self.ideaforge_db)
        self.um_db = os.environ.get("METROPLEX_UM_DB", self.um_db)
        self.stfactory_db = os.environ.get("METROPLEX_STFACTORY_DB", self.stfactory_db)
        self.yce_dir = os.environ.get("METROPLEX_YCE_DIR", self.yce_dir)
        self.academy_repo = os.environ.get("METROPLEX_ACADEMY_REPO", self.academy_repo)
        self.build_model = os.environ.get("METROPLEX_BUILD_MODEL", self.build_model)

        # Integer conversions with fallback to defaults
        try:
            self.approve_threshold = int(os.environ.get("METROPLEX_APPROVE_THRESHOLD", self.approve_threshold))
        except ValueError:
            pass

        try:
            self.reject_threshold = int(os.environ.get("METROPLEX_REJECT_THRESHOLD", self.reject_threshold))
        except ValueError:
            pass

        try:
            self.max_approve_per_cycle = int(os.environ.get("METROPLEX_MAX_APPROVE_PER_CYCLE", self.max_approve_per_cycle))
        except ValueError:
            pass

        try:
            self.max_patches_per_cycle = int(os.environ.get("METROPLEX_MAX_PATCHES_PER_CYCLE", self.max_patches_per_cycle))
        except ValueError:
            pass

        try:
            self.circuit_breaker_threshold = int(os.environ.get("METROPLEX_CIRCUIT_BREAKER_THRESHOLD", self.circuit_breaker_threshold))
        except ValueError:
            pass

    def validate(self) -> list[str]:
        """
        Validate configuration settings.
        Returns list of warnings (non-fatal, enables dry-run without real DBs).
        """
        warnings = []

        # Check database paths
        db_paths = [
            ("IdeaForge DB", self.ideaforge_db),
            ("Ultra-Magnus DB", self.um_db),
            ("ST Factory DB", self.stfactory_db),
        ]

        for name, path in db_paths:
            if not Path(path).exists():
                warnings.append(f"{name} not found at {path}")

        # Check yce_dir
        if not Path(self.yce_dir).exists():
            warnings.append(f"YCE directory not found at {self.yce_dir}")

        # Validate thresholds
        if self.approve_threshold <= self.reject_threshold:
            warnings.append(f"approve_threshold ({self.approve_threshold}) must be > reject_threshold ({self.reject_threshold})")

        if not (0 <= self.approve_threshold <= 100):
            warnings.append(f"approve_threshold ({self.approve_threshold}) must be between 0 and 100")

        if not (0 <= self.reject_threshold <= 100):
            warnings.append(f"reject_threshold ({self.reject_threshold}) must be between 0 and 100")

        return warnings
