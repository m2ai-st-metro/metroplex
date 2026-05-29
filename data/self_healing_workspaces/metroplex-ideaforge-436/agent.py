"""Elder-Care Safety & Support Companion — public entry point.

Exposes :func:`handle_message` for tests, plus a ``__main__`` block so
the agent can be invoked from a shell via ``mission-cli`` or
``claude --agent eldercare`` style runners.

Spec source: ``spec.md``; per-claim breakdown in
``.self-healing-pipeline/spec-claims.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from matcher import IncidentMatcher, MatchResult
from safety.resources import (
    Resource,
    assess_risk_level,
    surface_safety_resources,
    user_expresses_fear,
    user_expresses_guilt,
)
from skills.incident_logging.implementation import (
    log_incident,
    log_silent_drop,
)


# ----------------------------------------------------------------------
# Public types


@dataclass
class AgentState:
    """Per-call agent state. ``data_dir`` is where incidents and silent
    drops are persisted. Tests pass in ``tmp_path``."""

    data_dir: Path


@dataclass
class Response:
    text: str
    incident_logged: bool = False
    resources: list[Resource] = field(default_factory=list)
    risk_level: str = "low"
    match_result: Optional[MatchResult] = None


# ----------------------------------------------------------------------
# Guardrails


_DIAGNOSTIC_BLOCKLIST = (
    "diagnose",
    "diagnosis",
    "disorder",
    "borderline personality disorder",
    "schizophrenia",
    "bipolar",
)

_CONFRONT_BLOCKLIST = (
    "confront her",
    "confront him",
    "restrain her",
    "restrain him",
    "fight back",
    "hit back",
    "physically engage",
    "stand up to her physically",
)


def _strip_blocklisted(text: str) -> str:
    """Best-effort guardrail: refuse to emit blocklisted phrases.

    For this T1 build the agent is templated, not LLM-generated, so the
    guardrail is enforced by *construction* (we don't write the phrases
    in our templates). This function is a belt-and-suspenders pass for
    any future LLM-generated responses.
    """
    low = text.lower()
    for phrase in _DIAGNOSTIC_BLOCKLIST + _CONFRONT_BLOCKLIST:
        if phrase in low:
            # Defensive: rebuild without the phrase.
            text = text.replace(phrase, "[redacted]").replace(
                phrase.capitalize(), "[redacted]"
            )
    return text


# ----------------------------------------------------------------------
# Response templates


_ACK_OPENERS = {
    "match": (
        "Thank you for documenting this. I hear you. I've logged the "
        "incident exactly as you described, and your record is now safe "
        "in your local agent storage."
    ),
    "negated": (
        "Thank you for checking in. Nothing was logged as an incident — "
        "your message indicates this didn't happen. If anything changes, "
        "tell me and we'll record it then."
    ),
    "compound": (
        "Thank you for sharing this. I didn't log a formal incident "
        "because the phrasing 'passive-aggression' overlaps with "
        "patterns I want to be careful about. Could you describe one "
        "specific moment — what was said, what was done? Then I can log "
        "what actually happened."
    ),
    "no_match": (
        "Thank you for checking in. I read your message but didn't pick "
        "up a specific incident token I recognize. If something happened, "
        "tell me what was said or done — even a short sentence — and I "
        "can log it."
    ),
    "empty": (
        "I didn't receive any text to review. When you're ready, type a "
        "short description of what happened and I'll log it."
    ),
}

_GUILT_PREFACE = (
    "What you're feeling is valid and very common — adult children "
    "supporting an aging parent through behavioral changes often feel "
    "guilty for protecting themselves. Reaching out for resources is "
    "not abandoning your mother."
)


def _render_response(
    text_parts: list[str],
    resources: list[Resource],
) -> str:
    body = "\n\n".join(p for p in text_parts if p)
    if resources:
        body += "\n\nResources you can reach right now:\n"
        for r in resources:
            body += f"  - {r.name}: {r.contact}\n      {r.notes}\n"
    return _strip_blocklisted(body)


# ----------------------------------------------------------------------
# Public API


def handle_message(text: str, state: AgentState) -> Response:
    """Main entry point. Classifies the message, persists outcomes,
    surfaces safety resources, and returns a structured Response.
    """
    matcher = IncidentMatcher()
    result = matcher.classify(text)

    parts: list[str] = []

    # Validate guilt phrasing FIRST (before any resource list).
    if user_expresses_guilt(text):
        parts.append(_GUILT_PREFACE)

    incident_logged = False
    if result.matched:
        try:
            log_incident(
                {
                    "raw_input": text,
                    "matched_tokens": result.matched_tokens,
                    "tiers": list(result.tiers),
                },
                state.data_dir,
            )
            incident_logged = True
            parts.append(_ACK_OPENERS["match"])
        except IOError:
            # write-then-read verification failed → no false confirmation
            parts.append(
                "I tried to log this incident but the local store didn't "
                "confirm the write. Please re-send so we can try again."
            )
    else:
        reason = result.silent_drop_reason or "no_match"
        log_silent_drop(reason, text, state.data_dir)
        if reason == "negated":
            parts.append(_ACK_OPENERS["negated"])
        elif reason == "compound_needs_context":
            parts.append(_ACK_OPENERS["compound"])
        elif reason == "empty":
            parts.append(_ACK_OPENERS["empty"])
        else:
            parts.append(_ACK_OPENERS["no_match"])

    resources = surface_safety_resources(text)
    risk_level = assess_risk_level(text)

    response_text = _render_response(parts, resources)
    return Response(
        text=response_text,
        incident_logged=incident_logged,
        resources=resources,
        risk_level=risk_level,
        match_result=result,
    )


# ----------------------------------------------------------------------
# CLI entry point (claude --agent eldercare / mission-cli)


def _cli_data_dir() -> Path:
    """Default runtime data directory for CLI invocations."""
    return Path(__file__).resolve().parent / "data"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eldercare",
        description="Elder-Care Safety & Support Companion (T1).",
    )
    parser.add_argument(
        "--message",
        type=str,
        help="Message text to process. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_cli_data_dir(),
        help="Directory where incidents.jsonl and silent_drops.jsonl live.",
    )
    args = parser.parse_args(argv)

    text = args.message if args.message is not None else sys.stdin.read()
    state = AgentState(data_dir=args.data_dir)
    response = handle_message(text, state)

    payload = {
        "text": response.text,
        "incident_logged": response.incident_logged,
        "risk_level": response.risk_level,
        "resources": [
            {"name": r.name, "contact": r.contact, "notes": r.notes}
            for r in response.resources
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
