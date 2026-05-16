"""Trigger matching for the Elder-Care Safety & Support Companion.

Responsibilities:
  - Unicode NFC normalization (smart apostrophes/quotes -> ASCII equivalents)
  - Case-insensitive substring matching anchored at word boundaries
  - Negation-aware: looks at preceding tokens in the same clause for negation cues
  - WARNING log when input is non-empty but no triggers match

The single public entry point is `match_any(user_input, trigger_set) -> bool`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable

logger = logging.getLogger("elder_care.matcher")

# Negation cues — tokens which, when appearing before a candidate match in the
# same clause, suppress that match. Order does not matter; presence does.
NEGATION_CUES: frozenset[str] = frozenset(
    {
        "not",
        "no",
        "never",
        "almost",
        "nor",
        # Contracted forms (after apostrophe stripping below)
        "dont",
        "doesnt",
        "didnt",
        "wont",
        "wouldnt",
        "couldnt",
        "shouldnt",
        "wasnt",
        "werent",
        "isnt",
        "arent",
        "aint",
        "havent",
        "hasnt",
        "hadnt",
        "cant",
        "cannot",
    }
)

# Multi-token negation phrases. Searched as a unit before tokenization.
NEGATION_PHRASES = (
    "no longer",
    "no more",
    "not asking",
)

# How many tokens before a candidate match to scan for a negation cue.
NEGATION_WINDOW = 6

# Clause-splitting punctuation (negation does not cross these boundaries).
CLAUSE_SPLIT = re.compile(r"[.!?;]")

# Smart-quote to ASCII mapping.
SMART_QUOTE_MAP = {
    "‘": "'",  # LEFT SINGLE QUOTATION MARK
    "’": "'",  # RIGHT SINGLE QUOTATION MARK
    "‚": "'",  # SINGLE LOW-9 QUOTATION MARK
    "‛": "'",  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    "“": '"',  # LEFT DOUBLE QUOTATION MARK
    "”": '"',  # RIGHT DOUBLE QUOTATION MARK
    "„": '"',  # DOUBLE LOW-9 QUOTATION MARK
    "‟": '"',  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    "′": "'",  # PRIME
    "″": '"',  # DOUBLE PRIME
    " ": " ",  # NON-BREAKING SPACE
    "–": "-",  # EN DASH
    "—": "-",  # EM DASH
}


def normalize(text: str) -> str:
    """Return a casefolded, NFC-normalized, smart-quote-folded version of `text`."""
    if not text:
        return ""
    nfc = unicodedata.normalize("NFC", text)
    for src, dst in SMART_QUOTE_MAP.items():
        if src in nfc:
            nfc = nfc.replace(src, dst)
    return nfc.casefold()


def _strip_apostrophes(token: str) -> str:
    """Strip ASCII apostrophes from a token (so 'didn't' -> 'didnt')."""
    return token.replace("'", "")


def _split_clauses(text: str) -> list[str]:
    """Split into clauses on sentence-ending punctuation."""
    return [c.strip() for c in CLAUSE_SPLIT.split(text) if c.strip()]


def _has_negation_phrase(clause_before_match: str) -> bool:
    """Check for multi-token negation phrases anywhere before the match in this clause."""
    return any(phrase in clause_before_match for phrase in NEGATION_PHRASES)


def _has_negation_token(clause_before_match: str) -> bool:
    """Check for single-token negation within NEGATION_WINDOW tokens before the match.

    Tokens are extracted including ASCII apostrophes so 'didn't' stays one token,
    then apostrophes are stripped per-token before comparison against NEGATION_CUES.
    """
    tokens = re.findall(r"[A-Za-z']+", clause_before_match)
    # Strip apostrophes (and stray ones at edges) so "didn't" -> "didnt".
    tokens = [_strip_apostrophes(t) for t in tokens if t and t != "'"]
    window = tokens[-NEGATION_WINDOW:] if len(tokens) > NEGATION_WINDOW else tokens
    for tok in window:
        if tok in NEGATION_CUES:
            return True
    return False


# Filler words allowed between trigger tokens in a phrase match. These do not
# add or remove meaning ("document THE incident", "document THIS incident",
# "document AN incident"). The matcher allows up to FILLER_LIMIT filler tokens
# between consecutive trigger tokens.
FILLER_WORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "this", "that", "these", "those", "my", "his", "her", "their", "our", "any", "some"}
)
FILLER_LIMIT = 2


def _trigger_pattern(trigger: str) -> re.Pattern:
    """Build a regex that matches `trigger` as a sequence of whole words,
    allowing up to FILLER_LIMIT filler tokens (e.g. 'the', 'this', 'a')
    between consecutive trigger tokens. Single-word triggers compile to a
    plain word-boundary anchored pattern.
    """
    parts = [p for p in re.split(r"\s+", trigger.strip()) if p]
    if not parts:
        # No tokens — never match
        return re.compile(r"(?!)")
    if len(parts) == 1:
        return re.compile(rf"\b{re.escape(parts[0])}\b", re.IGNORECASE)
    # Multi-token trigger: insert an optional filler sequence between each pair.
    filler_alt = "|".join(re.escape(f) for f in FILLER_WORDS)
    # \s+ then 0..FILLER_LIMIT repetitions of "filler-word + whitespace"
    filler_gap = rf"\s+(?:(?:{filler_alt})\b\s+){{0,{FILLER_LIMIT}}}"
    body = filler_gap.join(re.escape(p) for p in parts)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


def match_any(user_input: str, triggers: Iterable[str]) -> bool:
    """Return True if any trigger in `triggers` matches `user_input` non-negated.

    A trigger matches when:
      1. Its tokens appear at word boundaries (so 'document' will not match
         'documentary' or 'documented').
      2. The match is not preceded by a negation cue within the same clause.
      3. Smart Unicode punctuation is normalized away first.
      4. The match is case-insensitive.
    """
    if not user_input or not user_input.strip():
        return False
    normalized = normalize(user_input)
    clauses = _split_clauses(normalized)
    triggers = list(triggers)
    if not triggers:
        return False
    for clause in clauses:
        for trigger in triggers:
            norm_trigger = normalize(trigger)
            pattern = _trigger_pattern(norm_trigger)
            for m in pattern.finditer(clause):
                clause_before = clause[: m.start()]
                if _has_negation_phrase(clause_before):
                    continue
                if _has_negation_token(clause_before):
                    continue
                return True
    return False


def log_drop(skill_name: str, user_input: str) -> None:
    """Emit a WARNING log entry for a dropped input — the user typed something
    but no trigger matched. Used by the dispatcher when an utterance is
    suspicious enough that we want a forensic trail (every non-empty input
    that does not match any skill).
    """
    logger.warning(
        "elder_care.dispatch dropped input: skill=%s input=%r",
        skill_name,
        user_input,
    )
