"""Reasoning turns behind a provider adapter (OpenAI-compatible chat endpoints).

Hosted: DeepInfra by default. Local: any OpenAI-compatible server (e.g. Qwen on
the M5), used for projects that disallow hosted processing. A turn returns JSON
only; callers validate every field against Teletraan state before acting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


class ReasoningUnavailable(RuntimeError):
    pass


class Reasoner(Protocol):
    def complete_json(self, system: str, user: str) -> dict[str, Any]: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Tolerate code fences and leading prose; reject anything that is not one
    JSON object (chain-of-thought mixed into output is a known failure mode)."""
    if not isinstance(text, str) or not text.strip():
        raise ReasoningUnavailable("REASONING_EMPTY")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as e:
        raise ReasoningUnavailable("REASONING_NOT_JSON") from e
    if not isinstance(value, dict):
        raise ReasoningUnavailable("REASONING_NOT_OBJECT")
    return value


@dataclass
class OpenAICompatibleReasoner:
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 120.0
    temperature: float = 0.2

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover - dependency is in requirements.txt
            raise ReasoningUnavailable("OPENAI_CLIENT_MISSING") from e
        client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout_s, max_retries=0)
        try:
            resp = client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[{"role": "system", "content": system + "\nRespond with one JSON object and nothing else."}, {"role": "user", "content": user}],
            )
        except Exception as e:  # noqa: BLE001 - any transport/provider failure is unavailability
            raise ReasoningUnavailable(f"REASONING_PROVIDER_ERROR: {type(e).__name__}") from e
        content = (resp.choices[0].message.content or "") if resp.choices else ""
        return parse_json_object(content)


class NoReasoner:
    """Used when no model is configured: every turn is unavailable, so callers
    take their safe fallback (wait or escalate) instead of guessing."""

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        raise ReasoningUnavailable("REASONING_NOT_CONFIGURED")


def reasoner_for(config: Any, hosted_allowed: bool) -> Reasoner:
    if hosted_allowed and config.reasoning_api_key and config.reasoning_model:
        return OpenAICompatibleReasoner(config.reasoning_base_url, config.reasoning_api_key, config.reasoning_model)
    if config.local_base_url and config.local_model:
        return OpenAICompatibleReasoner(config.local_base_url, "local", config.local_model)
    return NoReasoner()
