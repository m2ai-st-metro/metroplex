"""Safety Protocol skill implementation.

Public interface:
    triggers() -> list[str]      — phrases that activate this skill
    handle(user_input) -> dict   — return safety steps + resource referrals

No external services. The resource list is static and bundled with the agent.
"""

from __future__ import annotations

SKILL_NAME = "Safety Protocol"

_TRIGGERS: tuple[str, ...] = (
    "need help",
    "i need help",
    "please help",
    "i'm scared",
    "im scared",
    "i need someone",
    "this isn't safe",
    "this isnt safe",
    "this is not safe",
    "call somebody",
    "call someone",
    "help me",
    "i need to get out",
)

# Phrasings that signal physical danger even without an explicit help-ask.
_PHYSICAL_DANGER_SIGNALS: tuple[str, ...] = (
    "threw a", "threw something", "threw the", "throwing things",
    "hit me", "hit my", "slapped me", "shoved me", "pushed me",
    "kicked me", "punched me", "bit me",
    "locked me", "locked in",
)

_DEFAULT_ACTIONS: tuple[str, ...] = (
    "Move to a room you can lock if she is still escalating.",
    "Keep your phone with you and stay where you can call out.",
    "If you are in immediate physical danger, call 911 — you are allowed to do this.",
    "Write down what just happened while it's fresh — the Incident Documentation skill can help.",
)

_DEFAULT_RESOURCES: tuple[dict, ...] = (
    {
        "name": "Adult Protective Services",
        "kind": "hotline",
        "note": "For suspected elder abuse, self-neglect, or financial exploitation. "
                "Search 'Adult Protective Services <your state>' for the local intake line.",
    },
    {
        "name": "National Domestic Violence Hotline",
        "kind": "hotline",
        "number": "1-800-799-7233",
        "note": "24/7. Trained counselors. They handle parent-child situations too.",
    },
    {
        "name": "988 Suicide & Crisis Lifeline",
        "kind": "hotline",
        "number": "988",
        "note": "For mental-health crisis — Sarah's OR her mother's.",
    },
    {
        "name": "Local non-emergency police",
        "kind": "phone",
        "note": "If you need a welfare check or a presence in the home without "
                "starting with 911.",
    },
)


def triggers() -> list[str]:
    return list(_TRIGGERS)


def physical_danger_signals() -> list[str]:
    """Return phrases the dispatcher uses to fire Safety Protocol even when
    the user did not explicitly ask for help."""
    return list(_PHYSICAL_DANGER_SIGNALS)


def handle(user_input: str) -> dict:
    """Return the safety protocol payload for Sarah."""
    return {
        "skill": SKILL_NAME,
        "actions": list(_DEFAULT_ACTIONS),
        "resources": [dict(r) for r in _DEFAULT_RESOURCES],
        "opener": (
            "You're doing the right thing reaching out. "
            "Here is a short list of steps and people who can help right now."
        ),
        "input": user_input,
    }
