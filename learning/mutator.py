"""EGO mutator -- generates variant failure-feedback constraint mappings.

First target: the failure_category -> actionable constraint mapping in
gates/llm_expander.py's format_failure_feedback(). This is the text injected
into spec prompts to help specs avoid repeating past failure patterns.
"""

import json
import logging
import os
from typing import Optional

from openai import OpenAI

from .config import EGO_MAX_TOKENS, EGO_MODEL

logger = logging.getLogger(__name__)

MUTATION_PROMPT = """\
You are an optimization engine for an autonomous software build pipeline.

The pipeline generates app specifications from ideas, then an AI coding agent builds them.
When builds fail, a postmortem classifies the failure into categories. These categories
are fed back into the next spec generation as constraints to prevent repeating the same mistakes.

Below is the CURRENT constraint mapping (failure_category -> actionable constraint text
injected into spec prompts) and RECENT BUILD OUTCOME DATA showing which failure categories
are still causing problems.

## Current Constraint Mapping

{current_mapping}

## Recent Build Outcomes (last {window} builds)

Total builds: {total_builds}
Successful: {successful_builds} ({success_rate:.0%})
Failed: {failed_builds}

### Failure Category Breakdown
{failure_breakdown}

### Sample Error Signatures (most recent failures)
{error_samples}

## Your Task

Generate an IMPROVED constraint mapping. Rules:
1. Keep all existing failure categories -- do not remove any.
2. You may adjust the constraint text for any category.
3. Focus improvements on categories with the HIGHEST failure counts.
4. Constraints must be specific, actionable instructions for a spec writer.
5. Each constraint should be 1-2 sentences max.
6. If a category has zero recent failures, its current constraint is working -- keep it as-is.

Output ONLY a valid JSON object mapping category names to constraint strings.
No markdown fencing, no explanation, no preamble. Just the JSON.

Example format:
{{"spec_unclear": "Every feature MUST have...", "dependency_error": "Limit to..."}}
"""


def get_current_constraint_mapping() -> dict[str, str]:
    """Extract the current failure-to-constraint mapping from llm_expander.py.

    Rather than parsing the source file, this defines the mapping directly.
    When a variant is applied, this function's return value is what gets compared.
    """
    return {
        "spec_unclear": (
            "Every feature MUST have concrete CLI commands with exact expected output."
        ),
        "dependency_error": (
            "Limit to 3-5 well-known pip packages. Specify exact versions."
        ),
        "timeout": (
            "Reduce scope. Max 2-3 features. Each feature < 100 lines of code."
        ),
        "test_failure": (
            "Test steps must be unambiguous with literal expected output strings."
        ),
        "build_error": (
            "Keep code patterns simple. Avoid complex generics or metaclasses."
        ),
    }


def generate_variant(
    current_mapping: dict[str, str],
    build_stats: dict,
    failure_breakdown: list[dict],
    error_samples: list[str],
    api_key: Optional[str] = None,
) -> dict[str, str]:
    """Generate a variant constraint mapping using LLM judge.

    Args:
        current_mapping: Current category -> constraint text.
        build_stats: Dict with total, successful, failed, success_rate keys.
        failure_breakdown: List of dicts with category, count keys.
        error_samples: List of recent error signature strings.
        api_key: Optional DeepInfra API key override.

    Returns:
        New mapping dict, or the original mapping if generation fails.
    """
    resolved_key = api_key or os.environ.get("DEEPINFRA_API_KEY")
    if not resolved_key:
        logger.error("DEEPINFRA_API_KEY not set -- cannot generate variant")
        return current_mapping

    client = OpenAI(
        api_key=resolved_key,
        base_url="https://api.deepinfra.com/v1/openai",
    )

    # Format inputs
    mapping_str = json.dumps(current_mapping, indent=2)
    breakdown_str = "\n".join(
        f"- {fb['category']}: {fb['count']} failures"
        for fb in sorted(failure_breakdown, key=lambda x: -x["count"])
    ) or "No failures recorded."
    samples_str = "\n".join(
        f"- {s[:200]}" for s in error_samples[:5]
    ) or "No error samples available."

    prompt = MUTATION_PROMPT.format(
        current_mapping=mapping_str,
        window=build_stats.get("total", 0),
        total_builds=build_stats.get("total", 0),
        successful_builds=build_stats.get("successful", 0),
        success_rate=build_stats.get("success_rate", 0),
        failed_builds=build_stats.get("failed", 0),
        failure_breakdown=breakdown_str,
        error_samples=samples_str,
    )

    try:
        response = client.chat.completions.create(
            model=EGO_MODEL,
            max_tokens=EGO_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content or ""

        # Strip markdown fencing if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()

        variant = json.loads(raw)

        # Validate: must be a dict with string values, must contain all original keys
        if not isinstance(variant, dict):
            logger.warning("Variant is not a dict: %s", type(variant))
            return current_mapping

        missing = set(current_mapping.keys()) - set(variant.keys())
        if missing:
            logger.warning("Variant missing categories: %s -- merging with baseline", missing)
            for k in missing:
                variant[k] = current_mapping[k]

        # Validate all values are non-empty strings
        for k, v in variant.items():
            if not isinstance(v, str) or not v.strip():
                logger.warning("Invalid variant value for '%s': %r -- keeping baseline", k, v)
                variant[k] = current_mapping.get(k, "")

        logger.info("Generated variant with %d constraint entries", len(variant))
        return variant

    except json.JSONDecodeError as e:
        logger.warning("Failed to parse variant JSON: %s -- raw: %s", e, raw[:200])
        return current_mapping
    except Exception as e:
        logger.error("Variant generation failed: %s", e)
        return current_mapping
