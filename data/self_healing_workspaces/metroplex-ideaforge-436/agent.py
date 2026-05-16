"""Elder-Care Safety & Support Companion — agent runtime.

This module is the single entrypoint the CCOS runtime calls into. It owns
trigger dispatch: takes a free-text user utterance, decides which skills
should fire, and returns the list of structured skill responses.

Design notes
------------
- Multiple skills can fire on one utterance ("I need help AND want to
  document the incident"). Dispatch returns a list, never a single skill.
- The matcher in `matcher.py` enforces negation-aware, word-boundary,
  Unicode-normalized, case-insensitive matching. agent.py only orchestrates.
- If the input is non-empty but nothing matched, a WARNING is logged so the
  forensic trail required by the spec is preserved.
- No persistent storage, no network. The agent runs entirely on CCOS.
"""

from __future__ import annotations

import logging

from matcher import log_drop, match_any
from skills.incident_documentation import implementation as incident_skill
from skills.safety_protocol import implementation as safety_skill

logger = logging.getLogger("elder_care.agent")


def _fire_safety(user_input: str) -> bool:
    """Decide whether the Safety Protocol skill should fire.

    Two ways the skill activates:
      1. An explicit safety trigger (from `safety_skill.triggers()`).
      2. A physical-danger signal even without an explicit help-ask
         (thrown object, hitting, locked in). The spec requires a
         physical-danger incident to surface the safety-protocol path.

    Negation suppression applies to both: 'I almost called for help' does
    NOT fire, and 'she didn't throw anything' would not fire the
    physical-danger heuristic either.
    """
    if match_any(user_input, safety_skill.triggers()):
        return True
    if match_any(user_input, safety_skill.physical_danger_signals()):
        return True
    return False


def _fire_incident(user_input: str) -> bool:
    return match_any(user_input, incident_skill.triggers())


def dispatch(user_input: str) -> list[dict]:
    """Route a user utterance to whichever skills should fire.

    Returns a list of skill response dicts. Empty list if nothing matched.
    Emits a WARNING log when a non-empty input is dropped (matches nothing).
    """
    if user_input is None:
        return []
    text = str(user_input).strip()
    if not text:
        return []

    responses: list[dict] = []

    if _fire_safety(text):
        responses.append(safety_skill.handle(text))

    if _fire_incident(text):
        responses.append(incident_skill.handle(text))

    if not responses:
        # Spec: log at WARNING level if input is dropped. We log against both
        # skill namespaces — the spec mandates WARNING for either side, and
        # at dispatch time we don't know which skill the user "meant".
        log_drop("incident_documentation", text)
        log_drop("safety_protocol", text)

    return responses


__all__ = ["dispatch"]
