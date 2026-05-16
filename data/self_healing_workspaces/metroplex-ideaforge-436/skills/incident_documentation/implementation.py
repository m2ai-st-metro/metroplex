"""Incident Documentation skill implementation.

Public interface:
    triggers() -> list[str]      — phrases that activate this skill
    handle(user_input) -> dict   — return a structured record of the incident

The skill is intentionally synchronous and bundled — no external services,
no DB writes, no API calls. It returns a structured record that the agent
runtime is free to persist or render however it chooses.
"""

from __future__ import annotations

from typing import Iterable

SKILL_NAME = "Incident Documentation"

# Canonical trigger from SKILL.md frontmatter + lay-register synonyms.
_TRIGGERS: tuple[str, ...] = (
    "document incident",
    "write this down",
    "log this",
    "record what just happened",
    "record what happened",
    "make a note",
)

# Keywords that hint at incident category. Order is lookup-only; categorization
# scans the user input for each category in turn.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "physical": (
        "threw", "throwing", "thrown", "hit", "hitting", "struck", "shoved",
        "pushed", "slapped", "kicked", "punched", "bit", "scratched",
        "vase", "glass", "object", "weapon",
    ),
    "verbal": (
        "yelled", "screamed", "screaming", "cursed", "insulted", "called me",
        "name-calling", "name calling",
    ),
    "self_neglect": (
        "refused medication", "wouldn't eat", "won't eat", "hasn't eaten",
        "stopped eating", "didn't bathe", "wouldn't take her meds",
        "won't take her meds", "skipped meds",
    ),
    "financial": (
        "money", "credit card", "checks", "savings", "bank", "spent",
    ),
}


def triggers() -> list[str]:
    """Return the trigger phrase list — used by the agent dispatcher."""
    return list(_TRIGGERS)


def _categorize(user_input: str) -> str:
    """Pick a category by keyword scan. Defaults to 'other' if no keyword hits."""
    low = user_input.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in low:
                return category
    return "other"


def handle(user_input: str) -> dict:
    """Record the incident and return a structured response.

    Args:
        user_input: free-text utterance describing the incident.

    Returns:
        dict with keys:
            skill   — "Incident Documentation"
            logged  — True (this skill never silently drops once invoked)
            category — one of physical | verbal | self_neglect | financial | other
            acknowledgement — calm confirmation string for the caregiver
            recorded_text — the original utterance, preserved verbatim
    """
    category = _categorize(user_input)
    return {
        "skill": SKILL_NAME,
        "logged": True,
        "category": category,
        "recorded_text": user_input,
        "acknowledgement": (
            "I have logged this incident in your record. "
            "You can review and share it with your care team whenever you're ready."
        ),
    }
