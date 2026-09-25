"""Card and task spec schemas, validated here (S1/S4) and again by Teletraan (D1).

Two layers, neither trusts the other. `card_digest` must match Teletraan's
`cardDigest` byte for byte: Matthew's yes is bound to this hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

RESERVED_ACTIONS = ("publish_push_deploy", "external_contact", "live_fleet_config")
QUALITY_VERIFICATION = ("receipt", "test", "review")


class SpecError(ValueError):
    pass


def _canonical(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    return value


def card_digest(card: dict[str, Any]) -> str:
    """sha256 over sorted-key compact JSON. None is kept as JSON null, matching
    Teletraan, which drops only `undefined` (a value Python cannot produce).
    Cards carry strings, booleans, ints and lists only: no floats, so number
    formatting cannot diverge between the two languages."""
    text = json.dumps(_canonical(card), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"INVALID_{name}")
    return value.strip()


def validate_card(card: dict[str, Any]) -> list[str]:
    """Return a list of problems (empty = complete). S1 blocks showing the card
    until this is empty; Teletraan re-validates at project.create."""
    problems: list[str] = []
    if not isinstance(card, dict):
        return ["card is not an object"]
    for key in ("title", "objective", "doneWhen", "owner"):
        if not isinstance(card.get(key), str) or not card[key].strip():
            problems.append(f"{key} is missing")
    goals = card.get("goals")
    if not isinstance(goals, list) or not 1 <= len(goals) <= 7:
        problems.append("goals must list 1 to 7 goals")
    else:
        seen: set[str] = set()
        for i, g in enumerate(goals):
            if not isinstance(g, dict):
                problems.append(f"goal {i + 1} is not an object")
                continue
            gid = g.get("goalId")
            if not isinstance(gid, str) or not gid.strip() or gid in seen:
                problems.append(f"goal {i + 1} needs a unique goalId")
            else:
                seen.add(gid)
            for key in ("statement", "doneWhen"):
                if not isinstance(g.get(key), str) or not g[key].strip():
                    problems.append(f"goal {i + 1} {key} is missing")
    reserved = card.get("reservedActions")
    if not isinstance(reserved, list) or any(a not in RESERVED_ACTIONS for a in reserved):
        problems.append("reservedActions must be a subset of " + ", ".join(RESERVED_ACTIONS))
    for key in ("hostedAllowed", "humanOnly"):
        if key in card and not isinstance(card[key], bool):
            problems.append(f"{key} must be true or false")
    if not _hash_safe(card):
        problems.append("card must contain only text, true/false, whole numbers and lists (hash parity with Teletraan)")
    return problems


def _hash_safe(value: Any) -> bool:
    """Values whose canonical JSON is byte-identical in Python and JS: no floats,
    no integer-like object keys (JS orders them first), no lone surrogates."""
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, int):
        return abs(value) <= 2**53 - 1
    if isinstance(value, float):
        return False
    if isinstance(value, str):
        return not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, list):
        return all(_hash_safe(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and not k.isdigit() and _hash_safe(k) and _hash_safe(v) for k, v in value.items())
    return False


def validate_task_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """S4 lint. Raises SpecError with Teletraan's code on the first problem."""
    if not isinstance(spec, dict):
        raise SpecError("INVALID_TASK_SPEC")
    checkpoints = spec.get("checkpoints")
    quality = spec.get("qualityChecks", [])
    if not isinstance(checkpoints, list) or not 1 <= len(checkpoints) <= 5:
        raise SpecError("INVALID_TASK_SPEC")
    if not isinstance(quality, list) or len(quality) > 5:
        raise SpecError("INVALID_TASK_SPEC")
    reserved = spec.get("reservedAction")
    if reserved is not None and reserved not in RESERVED_ACTIONS:
        raise SpecError("INVALID_TASK_SPEC")
    try:
        _text(spec.get("doneWhen"), "TASK_SPEC")
        for c in checkpoints:
            _text(c.get("name"), "TASK_SPEC")
            _text(c.get("evidenceExpected"), "TASK_SPEC")
        for q in quality:
            _text(q.get("name"), "TASK_SPEC")
            if q.get("howVerified") not in QUALITY_VERIFICATION:
                raise SpecError("INVALID_TASK_SPEC")
        if spec.get("cardGoalId") is not None:
            _text(spec["cardGoalId"], "TASK_SPEC")
    except (SpecError, AttributeError) as e:
        raise SpecError("INVALID_TASK_SPEC") from e
    for key in ("humanOnly", "synthetic"):
        if key in spec and not isinstance(spec[key], bool):
            raise SpecError("INVALID_TASK_SPEC")
    return spec


# Single default template (S3, MVP). Later versions come from accepted Sky-Lynx
# proposals keyed by spec template version.
SPEC_TEMPLATE_VERSION = "v1"


def default_checks(goal: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    checkpoints = [
        {"name": "plan", "evidenceExpected": "short plan of the change recorded as a checkpoint"},
        {"name": "result", "evidenceExpected": f"evidence that: {goal['doneWhen']}"},
    ]
    quality = [{"name": "done_when verified", "howVerified": "review"}]
    return checkpoints, quality
