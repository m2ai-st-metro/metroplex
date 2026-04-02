"""EGO evaluator -- LLM judge that scores constraint mappings against build outcomes.

Uses Nemotron-3 via DeepInfra to evaluate whether a variant constraint mapping
would better prevent the failure patterns observed in recent builds.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI

from .config import EGO_MAX_TOKENS, EGO_MODEL, IMPROVEMENT_THRESHOLD

logger = logging.getLogger(__name__)


@dataclass
class Comparison:
    """Result of comparing baseline vs variant constraint mappings."""

    baseline_score: float
    variant_score: float
    improvement_pct: float
    is_winner: bool
    is_valid: bool
    reason: str


JUDGE_PROMPT = """\
You are a quality judge for an autonomous software build pipeline.

The pipeline injects "failure feedback constraints" into spec generation prompts to prevent
repeating past build failures. You must evaluate which of two constraint mappings (A or B)
would better prevent the failure patterns shown in the build outcome data.

## Recent Build Failure Data

### Failure Category Breakdown
{failure_breakdown}

### Sample Error Signatures
{error_samples}

## Constraint Mapping A (Baseline)
{mapping_a}

## Constraint Mapping B (Variant)
{mapping_b}

## Scoring Criteria

Rate each mapping on a 0-100 scale across these dimensions:
1. **Specificity** (0-25): Are constraints actionable and precise? Generic advice scores low.
2. **Coverage** (0-25): Do constraints address the categories with the most failures?
3. **Feasibility** (0-25): Are constraints achievable by an AI spec writer in practice?
4. **Novelty** (0-25): Does the mapping offer new strategies vs just repeating the same advice?

Output ONLY a JSON object with this exact structure:
{{"score_a": <number>, "score_b": <number>, "reasoning": "<1-2 sentences>"}}

No markdown fencing. No preamble.
"""


def evaluate(
    baseline_mapping: dict[str, str],
    variant_mapping: dict[str, str],
    failure_breakdown: list[dict],
    error_samples: list[str],
    api_key: Optional[str] = None,
) -> Comparison:
    """Use LLM judge to compare baseline vs variant constraint mappings.

    Returns a Comparison with scores and winner determination.
    """
    resolved_key = api_key or os.environ.get("DEEPINFRA_API_KEY")
    if not resolved_key:
        logger.error("DEEPINFRA_API_KEY not set -- cannot evaluate")
        return Comparison(
            baseline_score=0,
            variant_score=0,
            improvement_pct=0,
            is_winner=False,
            is_valid=False,
            reason="No API key available",
        )

    client = OpenAI(
        api_key=resolved_key,
        base_url="https://api.deepinfra.com/v1/openai",
    )

    breakdown_str = "\n".join(
        f"- {fb['category']}: {fb['count']} failures"
        for fb in sorted(failure_breakdown, key=lambda x: -x["count"])
    ) or "No failures recorded."
    samples_str = "\n".join(
        f"- {s[:200]}" for s in error_samples[:5]
    ) or "No error samples available."

    prompt = JUDGE_PROMPT.format(
        failure_breakdown=breakdown_str,
        error_samples=samples_str,
        mapping_a=json.dumps(baseline_mapping, indent=2),
        mapping_b=json.dumps(variant_mapping, indent=2),
    )

    try:
        response = client.chat.completions.create(
            model=EGO_MODEL,
            max_tokens=EGO_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content or ""

        # Strip markdown fencing
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[: raw.rfind("```")]
        raw = raw.strip()

        result = json.loads(raw)

        score_a = float(result.get("score_a", 0))
        score_b = float(result.get("score_b", 0))
        reasoning = result.get("reasoning", "No reasoning provided")

        # Calculate improvement
        if score_a > 0:
            improvement_pct = (score_b - score_a) / score_a
        else:
            improvement_pct = 1.0 if score_b > 0 else 0.0

        is_winner = improvement_pct >= IMPROVEMENT_THRESHOLD

        logger.info(
            "Judge scores: A=%.1f B=%.1f improvement=%.1f%% winner=%s -- %s",
            score_a, score_b, improvement_pct * 100, is_winner, reasoning,
        )

        return Comparison(
            baseline_score=score_a,
            variant_score=score_b,
            improvement_pct=improvement_pct,
            is_winner=is_winner,
            is_valid=True,
            reason=reasoning,
        )

    except json.JSONDecodeError as e:
        logger.warning("Failed to parse judge response: %s -- raw: %s", e, raw[:200])
        return Comparison(
            baseline_score=0,
            variant_score=0,
            improvement_pct=0,
            is_winner=False,
            is_valid=False,
            reason=f"JSON parse error: {e}",
        )
    except Exception as e:
        logger.error("Evaluation failed: %s", e)
        return Comparison(
            baseline_score=0,
            variant_score=0,
            improvement_pct=0,
            is_winner=False,
            is_valid=False,
            reason=f"Evaluation error: {e}",
        )
