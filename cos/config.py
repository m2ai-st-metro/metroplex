"""Runtime configuration, read once from the environment (~/.env.shared via systemd)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Thresholds:
    """v1 values from the MVP scope; versioned so later tuning is measurable."""

    version: str = "v1"
    jev_min_confidence: float = 0.60
    jev_ready_threshold: float = 0.70
    stall_progress_s: int = 45 * 60
    dead_heartbeat_s: int = 10 * 60
    unassigned_s: int = 10 * 60
    orphan_wake_s: int = 15 * 60
    card_expiry_s: int = 7 * 24 * 3600


@dataclass(frozen=True)
class Caps:
    turns_per_project_per_cycle: int = 3
    cycle_s: int = 15 * 60
    wakes_per_sweep: int = 10
    jev_per_cycle: int = 60
    max_candidates: int = 6
    max_decompose_tasks: int = 12
    breaker_threshold: int = 3


@dataclass(frozen=True)
class Config:
    data_dir: Path
    work_socket: Path
    cos_token_file: Path
    bot_token: str | None
    approver_ids: tuple[str, ...]
    chat_id: str | None
    typesafe_api_key: str | None
    jev_model: str
    reasoning_base_url: str
    reasoning_api_key: str | None
    reasoning_model: str | None
    local_base_url: str | None
    local_model: str | None
    env: str
    wake_poll_s: int
    sweep_s: int
    thresholds: Thresholds = field(default_factory=Thresholds)
    caps: Caps = field(default_factory=Caps)

    @property
    def is_live(self) -> bool:
        return self.env == "live"

    @classmethod
    def from_env(cls) -> "Config":
        home = Path.home()
        data_dir = Path(os.environ.get("METROPLEX_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
        approvers = tuple(s.strip() for s in os.environ.get("METROPLEX_APPROVER_IDS", "").split(",") if s.strip())
        return cls(
            data_dir=data_dir,
            work_socket=Path(os.environ.get("METROPLEX_WORK_SOCKET", str(home / ".local/state/teletraan-work/work.sock"))),
            cos_token_file=Path(os.environ.get("METROPLEX_COS_TOKEN_FILE", str(home / ".config/metroplex/cos.token"))),
            bot_token=os.environ.get("METROPLEX_TELEGRAM_BOT_TOKEN") or None,
            approver_ids=approvers,
            chat_id=os.environ.get("METROPLEX_TELEGRAM_CHAT_ID") or None,
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY") or None,
            jev_model=os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest"),
            reasoning_base_url=os.environ.get("METROPLEX_REASONING_BASE_URL", "https://api.deepinfra.com/v1/openai"),
            reasoning_api_key=os.environ.get("DEEPINFRA_API_KEY") or None,
            reasoning_model=os.environ.get("METROPLEX_REASONING_MODEL") or None,
            local_base_url=os.environ.get("METROPLEX_LOCAL_BASE_URL") or None,
            local_model=os.environ.get("METROPLEX_LOCAL_MODEL") or None,
            env=os.environ.get("METROPLEX_ENV", "pilot"),
            wake_poll_s=_int("METROPLEX_WAKE_POLL_SECONDS", 60),
            sweep_s=_int("METROPLEX_SWEEP_SECONDS", 300),
            thresholds=Thresholds(
                jev_min_confidence=_float("METROPLEX_JEV_MIN_CONFIDENCE", 0.60),
                jev_ready_threshold=_float("METROPLEX_JEV_READY_THRESHOLD", 0.70),
            ),
        )
