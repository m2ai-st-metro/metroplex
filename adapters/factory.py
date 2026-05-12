"""
Adapter Factory — selects the appropriate build adapter based on config.
"""
import logging

from build_adapter import BuildAdapter
from config import Config

logger = logging.getLogger(__name__)


def create_adapter(config: Config, event_emitter=None) -> BuildAdapter:
    """Create the appropriate build adapter based on config.build_target.

    Valid targets (post-CLEANUP-B Scope B1 2026-05-12):
      - 'cloud': OzAdapter (Oz cloud agent)
      - 'self_healing': SelfHealingAdapter (Claude Code daemon, default)
      - 'local': LocalAdapter (yce-harness queue_runner; deprecated,
        removed in Scope B2 next)

    The 'a2a' (Google A2A protocol via yce-harness/a2a_server.py) and
    'auto' (a2a/local fallback chain) targets were retired in B1.
    event_emitter is preserved in the signature for backward compatibility
    with adapters that accept it; LocalAdapter ignores it.
    """
    if config.build_target == "cloud":
        from adapters.oz_adapter import OzAdapter
        return OzAdapter(config)
    elif config.build_target == "self_healing":
        from adapters.self_healing_adapter import SelfHealingAdapter
        return SelfHealingAdapter(config)
    else:
        # "local" — default (deprecated, will be removed in Scope B2)
        from adapters.local_adapter import LocalAdapter
        return LocalAdapter(config)
