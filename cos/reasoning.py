"""Reasoning turns on the local Qwen llama-server (M5), OpenAI-compatible.

Local only by Matthew's decision (2026-09-24): no hosted reasoning provider.
Qwen3.5 is a thinking model, so thinking is disabled per request (otherwise
`content` can come back empty with the budget spent on reasoning). The server
context is 16k tokens; oversized inputs are refused rather than truncated.
A turn returns JSON only; callers validate every field before acting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


class ReasoningUnavailable(RuntimeError):
    pass


class Reasoner(Protocol):
    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict[str, Any]: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Tolerate code fences and leading prose; reject anything that is not one
    JSON object (chain-of-thought mixed into output is a known failure mode)."""
    if not isinstance(text, str) or not text.strip():
        raise ReasoningUnavailable("REASONING_EMPTY")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
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
    timeout_s: float = 180.0
    temperature: float = 0.2
    max_input_chars: int = 40_000  # ~10k tokens, inside the 16k context with room to answer

    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict[str, Any]:
        """`max_tokens` is sized per call: at ~24 tok/s on the M5, output length
        is the latency (live E2E 2026-09-25: three 2048-token calls took ~5 min)."""
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover - dependency is in requirements.txt
            raise ReasoningUnavailable("OPENAI_CLIENT_MISSING") from e
        if len(system) + len(user) > self.max_input_chars:
            raise ReasoningUnavailable("REASONING_INPUT_TOO_LARGE")
        client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout_s, max_retries=0)
        try:
            resp = client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=max_tokens,
                messages=[{"role": "system", "content": system + "\nRespond with one JSON object and nothing else."}, {"role": "user", "content": user}],
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        except Exception as e:  # any transport/provider failure is unavailability
            raise ReasoningUnavailable(f"REASONING_PROVIDER_ERROR: {type(e).__name__}") from e
        content = (resp.choices[0].message.content or "") if resp.choices else ""
        return parse_json_object(content)


class NoReasoner:
    """Used when no model is configured: every turn is unavailable, so callers
    take their safe fallback (wait or escalate) instead of guessing."""

    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict[str, Any]:
        raise ReasoningUnavailable("REASONING_NOT_CONFIGURED")


def reasoner_for(config: Any, hosted_allowed: bool = False) -> Reasoner:
    """Always the local server. `hosted_allowed` is accepted for call-site
    symmetry with Jev but never selects a hosted reasoning provider."""
    if config.local_base_url and config.local_model:
        return OpenAICompatibleReasoner(config.local_base_url, "local", config.local_model)
    return NoReasoner()
