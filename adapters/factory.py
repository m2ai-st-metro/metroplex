"""
Adapter Factory — selects the appropriate build adapter based on config.
"""
import logging

from build_adapter import BuildAdapter
from config import Config

logger = logging.getLogger(__name__)


def create_adapter(config: Config, event_emitter=None) -> BuildAdapter:
    """Create the appropriate build adapter based on config.build_target."""
    if config.build_target == "cloud":
        from adapters.oz_adapter import OzAdapter
        return OzAdapter(config)
    elif config.build_target == "a2a":
        from adapters.a2a_adapter import A2AAdapter
        return A2AAdapter(config, event_emitter=event_emitter)
    elif config.build_target == "self_healing":
        from adapters.self_healing_adapter import SelfHealingAdapter
        return SelfHealingAdapter(config)
    elif config.build_target == "auto":
        # Try A2A first, fall back to local
        from adapters.a2a_adapter import A2AAdapter
        a2a = A2AAdapter(config, event_emitter=event_emitter)
        if a2a.is_active():
            logger.info("Auto-selected A2A adapter (server reachable)")
            return a2a
        logger.info("A2A server not reachable, falling back to local adapter")
        from adapters.local_adapter import LocalAdapter
        return LocalAdapter(config)
    else:
        # "local" — default
        from adapters.local_adapter import LocalAdapter
        return LocalAdapter(config)
