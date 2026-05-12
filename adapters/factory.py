"""
Adapter Factory — selects the appropriate build adapter based on config.
"""
import logging

from build_adapter import BuildAdapter
from config import Config

logger = logging.getLogger(__name__)


def create_adapter(config: Config, event_emitter=None) -> BuildAdapter:
    """Create the appropriate build adapter based on config.build_target.

    Valid targets (post-CLEANUP-B 2026-05-12):
      - 'self_healing': SelfHealingAdapter (Claude Code daemon, default)
      - 'cloud': OzAdapter (Oz cloud agent)

    The legacy 'local' (yce-harness queue_runner subprocess), 'a2a'
    (Google A2A protocol via yce-harness/a2a_server.py), and 'auto'
    (a2a/local fallback chain) targets were retired with yce-harness.
    event_emitter is preserved in the signature for backward compatibility
    with adapter constructors that accept it.
    """
    if config.build_target == "cloud":
        from adapters.oz_adapter import OzAdapter
        return OzAdapter(config)
    elif config.build_target == "self_healing":
        from adapters.self_healing_adapter import SelfHealingAdapter
        return SelfHealingAdapter(config)
    raise ValueError(
        f"Unknown build_target {config.build_target!r}. "
        "Valid options: 'cloud', 'self_healing'."
    )
