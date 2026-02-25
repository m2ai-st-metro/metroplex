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

    # Continuous operation
    cycle_sleep_seconds: int = field(default=60)

    # Priority queue source weights
    ideaforge_weight: float = field(default=1.0)
    skylynx_weight: float = field(default=1.5)
    linear_weight: float = field(default=2.0)

    # Telegram notifications (optional)
    telegram_bot_token: str = field(default="")
    telegram_chat_id: str = field(default="")

    # Schedule windows (24h clock, 0-23)
    schedule_start: int = field(default=0)    # midnight
    schedule_end: int = field(default=24)     # 24 = always on
    active_days: str = field(default="0,1,2,3,4,5,6")  # 0=Mon, 6=Sun

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

        try:
            self.cycle_sleep_seconds = int(os.environ.get("METROPLEX_CYCLE_SLEEP_SECONDS", self.cycle_sleep_seconds))
        except ValueError:
            pass

        # Priority queue weights
        try:
            self.ideaforge_weight = float(os.environ.get("METROPLEX_IDEAFORGE_WEIGHT", self.ideaforge_weight))
        except ValueError:
            pass
        try:
            self.skylynx_weight = float(os.environ.get("METROPLEX_SKYLYNX_WEIGHT", self.skylynx_weight))
        except ValueError:
            pass
        try:
            self.linear_weight = float(os.environ.get("METROPLEX_LINEAR_WEIGHT", self.linear_weight))
        except ValueError:
            pass

        # Telegram
        self.telegram_bot_token = os.environ.get("METROPLEX_TELEGRAM_BOT_TOKEN", self.telegram_bot_token)
        self.telegram_chat_id = os.environ.get("METROPLEX_TELEGRAM_CHAT_ID", self.telegram_chat_id)

        # Schedule
        try:
            self.schedule_start = int(os.environ.get("METROPLEX_SCHEDULE_START", self.schedule_start))
        except ValueError:
            pass
        try:
            self.schedule_end = int(os.environ.get("METROPLEX_SCHEDULE_END", self.schedule_end))
        except ValueError:
            pass
        self.active_days = os.environ.get("METROPLEX_ACTIVE_DAYS", self.active_days)

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

        if self.cycle_sleep_seconds < 10:
            warnings.append(f"cycle_sleep_seconds ({self.cycle_sleep_seconds}) is below 10 — may cause excessive resource usage")

        return warnings
