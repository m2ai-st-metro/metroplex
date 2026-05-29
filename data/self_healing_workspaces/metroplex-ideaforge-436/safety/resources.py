"""Safety resources surfaced to the user when fear or risk is detected.

Resources are NOT loaded from a remote registry — they're a static table
bundled with the agent (spec constraint: no external HTTP, no remote
runtime deps).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    name: str
    contact: str
    notes: str


# Static resource table. Phone numbers are public US hotlines.
SAFETY_RESOURCES: list[Resource] = [
    Resource(
        name="National Domestic Violence Hotline",
        contact="1-800-799-7233",
        notes="24/7, confidential. Can be reached by adult children fearing harm from a parent.",
    ),
    Resource(
        name="National Adult Protective Services (APS) Locator",
        contact="https://eldercare.acl.gov or 1-800-677-1116",
        notes=(
            "Eldercare Locator routes you to your state's APS office. "
            "APS investigates suspected elder abuse and self-neglect; if "
            "the parent's behavior is putting either of you at risk, this "
            "is the first call."
        ),
    ),
    Resource(
        name="988 Suicide & Crisis Lifeline",
        contact="988",
        notes="If you feel unsafe in your own home tonight, call or text 988.",
    ),
    Resource(
        name="Family Caregiver Alliance",
        contact="https://www.caregiver.org",
        notes="Educational resources and a state-by-state Family Care Navigator for caregivers of aging family members.",
    ),
]


# Phrases that, when present, indicate the user is expressing fear or
# elevated risk and need to see safety resources REGARDLESS of whether
# an incident token matched.
_FEAR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bscared\b", re.IGNORECASE),
    re.compile(r"\bafraid\b", re.IGNORECASE),
    re.compile(r"\bfear(?:ing|ful)?\b", re.IGNORECASE),
    re.compile(r"\bunsafe\b", re.IGNORECASE),
    re.compile(r"\bin danger\b", re.IGNORECASE),
    re.compile(r"\bneed help\b", re.IGNORECASE),
    re.compile(r"\bhelp me\b", re.IGNORECASE),
    re.compile(r"\bworried for my safety\b", re.IGNORECASE),
    re.compile(r"\bdon'?t feel safe\b", re.IGNORECASE),
]

# Phrases that indicate the user is expressing guilt / ambivalence about
# seeking help. We validate these BEFORE the resource list, never instead
# of it.
_GUILT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:i\s+)?feel\s+guilty\b", re.IGNORECASE),
    re.compile(r"\b(?:i\s+)?feel\s+bad\b", re.IGNORECASE),
    re.compile(r"\babandoning\s+(?:her|him|my)\b", re.IGNORECASE),
    re.compile(r"\bguilty\s+about\b", re.IGNORECASE),
]


def _normalize(text: str) -> str:
    nfc = unicodedata.normalize("NFC", text)
    for fancy in ("’", "‘", "ʼ"):
        nfc = nfc.replace(fancy, "'")
    return nfc


def user_expresses_fear(text: str) -> bool:
    norm = _normalize(text)
    return any(p.search(norm) for p in _FEAR_PATTERNS)


def user_expresses_guilt(text: str) -> bool:
    norm = _normalize(text)
    return any(p.search(norm) for p in _GUILT_PATTERNS)


def surface_safety_resources(text: str) -> list[Resource]:
    """Return the full resource list when the user expresses fear, guilt,
    or seeks help. Returns empty list only when the input is unambiguously
    informational.
    """
    if user_expresses_fear(text) or user_expresses_guilt(text):
        return list(SAFETY_RESOURCES)
    return []


def assess_risk_level(text: str) -> str:
    """Map user phrasing to a coarse risk_level for downstream UI.

    Returns one of: low | elevated | high.
    """
    norm = _normalize(text).lower()
    high_signals = [
        "hurt me",
        "kill",
        "weapon",
        "knife",
        "gun",
        "choked",
        "strangled",
        "blood",
    ]
    if any(s in norm for s in high_signals):
        return "high"
    if user_expresses_fear(text):
        return "elevated"
    return "low"
