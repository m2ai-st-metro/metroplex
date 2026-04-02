"""EGO configuration constants."""

import os

# LLM settings (DeepInfra / Nemotron-3, consistent with rest of pipeline)
EGO_MODEL = os.environ.get(
    "EGO_MODEL", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
)
EGO_MAX_TOKENS = int(os.environ.get("EGO_MAX_TOKENS", "4096"))

# Experiment thresholds
IMPROVEMENT_THRESHOLD = float(os.environ.get("EGO_IMPROVEMENT_THRESHOLD", "0.15"))
MIN_BUILDS_FOR_EXPERIMENT = int(os.environ.get("EGO_MIN_BUILDS", "10"))
MIN_FAILURES_FOR_EXPERIMENT = int(os.environ.get("EGO_MIN_FAILURES", "5"))

# Safety
AUTO_APPLY_ENABLED = os.environ.get("EGO_AUTO_APPLY", "").lower() in ("1", "true", "yes")

# Cadence: run EGO every N orchestrator cycles (0 = disabled)
RUN_EVERY_N_CYCLES = int(os.environ.get("EGO_RUN_EVERY_N_CYCLES", "50"))

# Rollback safety: if build success rate drops by this much after applying
# a variant, auto-revert.
ROLLBACK_THRESHOLD = float(os.environ.get("EGO_ROLLBACK_THRESHOLD", "0.10"))
ROLLBACK_WINDOW_BUILDS = int(os.environ.get("EGO_ROLLBACK_WINDOW", "10"))
