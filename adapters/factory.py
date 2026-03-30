"""
Adapter Factory — selects the appropriate build adapter based on config.
"""
from build_adapter import BuildAdapter
from config import Config


def create_adapter(config: Config) -> BuildAdapter:
    """Create the appropriate build adapter based on config.build_target."""
    if config.build_target == "cloud":
        from adapters.oz_adapter import OzAdapter
        return OzAdapter(config)
    else:
        # "local" or "auto" — default to local
        from adapters.local_adapter import LocalAdapter
        return LocalAdapter(config)
