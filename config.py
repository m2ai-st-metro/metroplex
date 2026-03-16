"""
Metroplex Configuration
Loads all settings from environment variables with fallback defaults.
Sources .env file if present in project root.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env from project root if it exists
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# Load ~/.env.shared for Oz and shared settings
_shared_env = Path.home() / ".env.shared"
if _shared_env.exists():
    for line in _shared_env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Config:
    """Metroplex configuration loaded from environment variables."""

    # Database paths
    ideaforge_db: str = field(default="/home/apexaipc/projects/ideaforge/data/ideaforge.db")
    um_db: str = field(default="")  # Deprecated: UM removed from pipeline (2026-03-15)
    stfactory_db: str = field(default="/home/apexaipc/projects/st-factory/data/persona_metrics.db")

    # Directory paths
    yce_dir: str = field(default="/home/apexaipc/projects/yce-harness")

    # GitHub repo
    academy_repo: str = field(default="m2ai-portfolio/agent-persona-academy")

    # Model settings
    build_model: str = field(default="opus")

    # Parallel build settings
    build_parallel: bool = field(default=False)
    build_max_workers: int = field(default=2)
    max_concurrent_builds: int = field(default=1)

    # Scoring thresholds
    approve_threshold: int = field(default=68)
    reject_threshold: int = field(default=40)
    max_deferrals: int = field(default=3)

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
    academy_weight: float = field(default=2.0)

    # Academy integration (persona -> agent promotions)
    academy_dir: str = field(default="/home/apexaipc/projects/agent-persona-academy")
    academy_promotions_path: str = field(default="/home/apexaipc/projects/agent-persona-academy/data/promotions.jsonl")

    # Telegram notifications (optional)
    telegram_bot_token: str = field(default="")
    telegram_chat_id: str = field(default="")
    notify_mode: str = field(default="all")

    # Linear integration (via Arcade SDK)
    linear_team: str = field(default="")
    linear_label_filter: str = field(default="metroplex")
    linear_poll_states: str = field(default="Backlog,Todo")

    # Spec generation (LLM expansion)
    spec_use_llm: bool = field(default=True)
    spec_llm_model: str = field(default="claude-sonnet-4-20250514")
    spec_llm_max_tokens: int = field(default=8192)

    # Dispatch (EA-Claude worker queue)
    dispatch_db: str = field(default="/home/apexaipc/projects/claudeclaw/store/claudeclaw.db")
    dispatch_chat_id: str = field(default="")

    # Publish gate (Gate 4)
    github_org: str = field(default="m2ai-portfolio")
    publish_visibility: str = field(default="private")
    max_publish_per_cycle: int = field(default=3)
    require_review: bool = field(default=True)

    # Budget controls
    daily_cost_limit: float = field(default=50.0)
    monthly_cost_limit: float = field(default=500.0)
    cost_alert_threshold: float = field(default=0.8)
    build_cost_estimate: float = field(default=3.0)

    # Tyrest QA gate (GPT-based independent review)
    tyrest_enabled: bool = field(default=True)
    tyrest_model: str = field(default="gpt-4o")
    tyrest_approve_confidence: float = field(default=0.75)
    tyrest_reject_confidence: float = field(default=0.75)

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

        # Parallel build settings
        self.build_parallel = os.environ.get("METROPLEX_BUILD_PARALLEL", "").lower() in ("1", "true", "yes")
        try:
            self.build_max_workers = int(os.environ.get("METROPLEX_BUILD_MAX_WORKERS", self.build_max_workers))
        except ValueError:
            pass
        try:
            self.max_concurrent_builds = int(os.environ.get("METROPLEX_MAX_CONCURRENT_BUILDS", self.max_concurrent_builds))
        except ValueError:
            pass

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
            self.max_deferrals = int(os.environ.get("METROPLEX_MAX_DEFERRALS", self.max_deferrals))
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
        try:
            self.academy_weight = float(os.environ.get("METROPLEX_ACADEMY_WEIGHT", self.academy_weight))
        except ValueError:
            pass

        # Academy
        self.academy_dir = os.environ.get("METROPLEX_ACADEMY_DIR", self.academy_dir)
        self.academy_promotions_path = os.environ.get("METROPLEX_ACADEMY_PROMOTIONS_PATH", self.academy_promotions_path)

        # Linear
        self.linear_team = os.environ.get("METROPLEX_LINEAR_TEAM", self.linear_team)
        self.linear_label_filter = os.environ.get("METROPLEX_LINEAR_LABEL_FILTER", self.linear_label_filter)
        self.linear_poll_states = os.environ.get("METROPLEX_LINEAR_POLL_STATES", self.linear_poll_states)

        # Spec generation (LLM expansion)
        self.spec_use_llm = os.environ.get("METROPLEX_SPEC_USE_LLM", "").lower() not in ("0", "false", "no")
        self.spec_llm_model = os.environ.get("METROPLEX_SPEC_LLM_MODEL", self.spec_llm_model)
        try:
            self.spec_llm_max_tokens = int(os.environ.get("METROPLEX_SPEC_LLM_MAX_TOKENS", self.spec_llm_max_tokens))
        except ValueError:
            pass

        # Dispatch
        self.dispatch_db = os.environ.get("METROPLEX_DISPATCH_DB", self.dispatch_db)
        self.dispatch_chat_id = os.environ.get("METROPLEX_DISPATCH_CHAT_ID", self.dispatch_chat_id)

        # Tyrest QA gate
        self.tyrest_enabled = os.environ.get("TYREST_ENABLED", "true").lower() in ("true", "1", "yes")
        self.tyrest_model = os.environ.get("TYREST_MODEL", self.tyrest_model)
        try:
            self.tyrest_approve_confidence = float(os.environ.get("TYREST_APPROVE_MIN_CONFIDENCE", self.tyrest_approve_confidence))
        except ValueError:
            pass
        try:
            self.tyrest_reject_confidence = float(os.environ.get("TYREST_REJECT_MIN_CONFIDENCE", self.tyrest_reject_confidence))
        except ValueError:
            pass

        # Telegram
        self.telegram_bot_token = os.environ.get("METROPLEX_TELEGRAM_BOT_TOKEN", self.telegram_bot_token)
        self.telegram_chat_id = os.environ.get("METROPLEX_TELEGRAM_CHAT_ID", self.telegram_chat_id)
        self.notify_mode = os.environ.get("METROPLEX_NOTIFY_MODE", self.notify_mode)

        # Publish gate
        self.github_org = os.environ.get("METROPLEX_GITHUB_ORG", self.github_org)
        self.publish_visibility = os.environ.get("METROPLEX_PUBLISH_VISIBILITY", self.publish_visibility)
        self.require_review = os.environ.get("METROPLEX_REQUIRE_REVIEW", "").lower() not in ("0", "false", "no")
        try:
            self.max_publish_per_cycle = int(os.environ.get("METROPLEX_MAX_PUBLISH_PER_CYCLE", self.max_publish_per_cycle))
        except ValueError:
            pass

        # Budget controls
        try:
            self.daily_cost_limit = float(os.environ.get("METROPLEX_DAILY_COST_LIMIT", self.daily_cost_limit))
        except ValueError:
            pass
        try:
            self.monthly_cost_limit = float(os.environ.get("METROPLEX_MONTHLY_COST_LIMIT", self.monthly_cost_limit))
        except ValueError:
            pass
        try:
            self.cost_alert_threshold = float(os.environ.get("METROPLEX_COST_ALERT_THRESHOLD", self.cost_alert_threshold))
        except ValueError:
            pass
        try:
            self.build_cost_estimate = float(os.environ.get("METROPLEX_BUILD_COST_ESTIMATE", self.build_cost_estimate))
        except ValueError:
            pass

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

        # Oz Cloud Agent settings
        self.build_target = os.environ.get("METROPLEX_BUILD_TARGET", self.build_target)
        if self.build_target not in ("local", "cloud", "auto"):
            self.build_target = "local"
        self.oz_environment_id = os.environ.get("METROPLEX_OZ_ENVIRONMENT_ID", self.oz_environment_id)
        self.oz_build_model = os.environ.get("METROPLEX_OZ_BUILD_MODEL", self.oz_build_model)


    def validate(self) -> list[str]:
        """
        Validate configuration settings.
        Returns list of warnings (non-fatal, enables dry-run without real DBs).
        """
        warnings = []

        # Check database paths
        db_paths = [
            ("IdeaForge DB", self.ideaforge_db),
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

        if self.notify_mode not in ("all", "anomaly", "summary"):
            warnings.append(f"notify_mode '{self.notify_mode}' invalid — must be all/anomaly/summary")

        return warnings

    # Oz Cloud Agent settings
    build_target: str = field(default="local")  # local|cloud|auto
    oz_environment_id: str = field(default="")
    oz_build_model: str = field(default="claude-sonnet-4-20250514")

