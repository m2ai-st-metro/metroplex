"""EGO applier -- applies winning constraint variants to llm_expander.py.

Modifies the format_failure_feedback() function's constraint mappings
by rewriting the conditional blocks in gates/llm_expander.py.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

EXPANDER_PATH = Path(__file__).resolve().parent.parent / "gates" / "llm_expander.py"

# Where the active variant is persisted (JSON file, not source code modification)
ACTIVE_VARIANT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "ego_active_variant.json"
)


def get_active_variant() -> dict[str, str] | None:
    """Load the currently active EGO variant, if any.

    Returns None if no variant has been applied (uses hardcoded defaults).
    """
    if not ACTIVE_VARIANT_PATH.exists():
        return None
    try:
        data = json.loads(ACTIVE_VARIANT_PATH.read_text())
        if isinstance(data, dict):
            # New format: {"experiment_id": N, "constraints": {...}}
            constraints = data.get("constraints", data)
            if isinstance(constraints, dict) and all(
                isinstance(v, str) for v in constraints.values()
            ):
                return constraints
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load active variant: %s", e)
    return None


def apply_variant(mapping: dict[str, str], experiment_id: int) -> bool:
    """Apply a variant constraint mapping by writing it to the active variant file.

    This approach avoids modifying source code. The format_failure_feedback()
    function reads the active variant file at runtime, falling back to its
    hardcoded defaults if no file exists.

    Args:
        mapping: The winning constraint mapping to apply.
        experiment_id: The EGO experiment ID for traceability.

    Returns:
        True if successfully written.
    """
    try:
        ACTIVE_VARIANT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "experiment_id": experiment_id,
            "constraints": mapping,
        }
        ACTIVE_VARIANT_PATH.write_text(json.dumps(payload, indent=2))
        logger.info(
            "Applied EGO variant (experiment %d) to %s",
            experiment_id, ACTIVE_VARIANT_PATH,
        )
        return True
    except OSError as e:
        logger.error("Failed to write active variant: %s", e)
        return False


def rollback_variant() -> bool:
    """Remove the active variant, reverting to hardcoded defaults."""
    if ACTIVE_VARIANT_PATH.exists():
        try:
            ACTIVE_VARIANT_PATH.unlink()
            logger.info("Rolled back EGO variant -- reverted to defaults")
            return True
        except OSError as e:
            logger.error("Failed to remove active variant: %s", e)
            return False
    logger.info("No active variant to roll back")
    return True
