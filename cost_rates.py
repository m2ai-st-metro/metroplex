"""
Cost Rates — Per-model token pricing for budget tracking.

Prices are per million tokens. Override defaults via METROPLEX_COST_RATES_JSON
environment variable (JSON dict mapping model name -> {input, output} rates).
"""
import json
import os

# Default rates: USD per million tokens (as of 2026-03)
MODEL_RATES: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-20250414": {"input": 0.80, "output": 4.0},
    # Aliases used in config
    "opus": {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "haiku": {"input": 0.80, "output": 4.0},
    # DeepInfra (spec_expander, ego_evaluator, readme, readiness)
    # Verified against the DeepInfra dashboard 2026-05-04. Pricing API returns
    # an empty block, so values are kept literal here. Override via
    # METROPLEX_COST_RATES_JSON when DeepInfra adjusts.
    "Qwen/Qwen2.5-72B-Instruct": {"input": 0.36, "output": 0.40},
    # Qwen3 generation on DeepInfra — rates from DeepInfra published pricing
    "Qwen/Qwen3-235B-A22B-Instruct-2507": {"input": 0.18, "output": 0.54},
    "Qwen/Qwen3-32B": {"input": 0.10, "output": 0.30},
}


def _load_custom_rates() -> dict[str, dict[str, float]]:
    """Merge user-provided rate overrides from env."""
    rates = dict(MODEL_RATES)
    custom_json = os.environ.get("METROPLEX_COST_RATES_JSON", "")
    if custom_json:
        try:
            overrides = json.loads(custom_json)
            rates.update(overrides)
        except (json.JSONDecodeError, TypeError):
            pass
    return rates


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a single API call.

    Args:
        model: Model name (e.g. 'claude-sonnet-4-20250514' or 'sonnet')
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens

    Returns:
        Estimated cost in USD. Returns 0.0 for unknown models.
    """
    rates = _load_custom_rates()
    rate = rates.get(model)
    if rate is None:
        return 0.0
    return (input_tokens * rate["input"] + output_tokens * rate["output"]) / 1_000_000
