"""Jev (TypeSafe hosted judgment model) client: a Python port of the CCOS pilot's
jev-adapter.ts. Evidence only: the result is persisted as a Teletraan judgment
and never issues a command by itself.

Rules carried over verbatim:
- no call at all unless the project allows hosted processing;
- no automatic retry (a failed request may already have incurred usage);
- low confidence -> status needs_review, any error -> status unavailable, both
  with fallback reasoning_review;
- persistence failures propagate and are never disguised as provider failures.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from typing import Any, Callable

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_S = 30


class JevValidationError(ValueError):
    pass


def _prob(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def validate_request(request: dict[str, Any]) -> None:
    if not isinstance(request.get("model"), str) or not request["model"]:
        raise JevValidationError("JEV_REQUEST_INVALID")
    if not isinstance(request.get("state"), (str, dict)):
        raise JevValidationError("JEV_REQUEST_INVALID")
    questions = request.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise JevValidationError("JEV_REQUEST_INVALID")
    for q in questions.values():
        if q.get("type") not in ("noul", "choice", "score") or not q.get("instructions"):
            raise JevValidationError("JEV_REQUEST_INVALID")
        if q["type"] == "choice" and (not isinstance(q.get("criteria"), dict) or len(q["criteria"]) < 2):
            raise JevValidationError("JEV_REQUEST_INVALID")
        if q["type"] == "score" and (not isinstance(q.get("criteria"), list) or len(q["criteria"]) < 2):
            raise JevValidationError("JEV_REQUEST_INVALID")


def _distribution(d: Any) -> bool:
    return isinstance(d, dict) and all(_prob(v) for v in d.values()) and abs(sum(d.values()) - 1) < 0.001


def validate_response(request: dict[str, Any], response: Any) -> dict[str, Any]:
    """Mirror of validateJevResponse: shape, one answer per question with the same
    type, choice labels from the criteria set, score within the rubric."""
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not isinstance(response.get("answers"), dict):
        raise JevValidationError("JEV_RESPONSE_INVALID")
    usage = response.get("usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("input_tokens"), int) or usage["input_tokens"] < 0:
        raise JevValidationError("JEV_RESPONSE_INVALID")
    answers, questions = response["answers"], request["questions"]
    if len(answers) != len(questions):
        raise JevValidationError("JEV_QUESTION_MISMATCH")
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict) or a.get("type") != q["type"]:
            raise JevValidationError("JEV_QUESTION_MISMATCH")
        if a["type"] == "noul":
            if not _prob(a.get("noul")):
                raise JevValidationError("JEV_RESPONSE_INVALID")
        elif a["type"] == "choice":
            labels = list(q["criteria"])
            probs = a.get("probabilities")
            if not _prob(a.get("confidence")) or not _distribution(probs):
                raise JevValidationError("JEV_RESPONSE_INVALID")
            if a.get("choice") not in labels or len(probs) != len(labels) or any(label not in probs for label in labels):
                raise JevValidationError("JEV_LABEL_MISMATCH")
        else:
            probs = a.get("probabilities")
            score = a.get("score")
            if not _prob(a.get("confidence")) or not _distribution(probs) or not isinstance(score, (int, float)) or score < 0:
                raise JevValidationError("JEV_RESPONSE_INVALID")
            n = len(q["criteria"])
            if score > n - 1 or len(probs) != n or any(str(i) not in probs for i in range(n)):
                raise JevValidationError("JEV_SCORE_MISMATCH")
    return response


def _confidence(answer: dict[str, Any]) -> float:
    return max(answer["noul"], 1 - answer["noul"]) if answer["type"] == "noul" else answer["confidence"]


def http_post(api_key: str, request: dict[str, Any]) -> Any:
    req = urllib.request.Request(ENDPOINT, data=json.dumps(request).encode(), method="POST", headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"JEV_HTTP_{e.code}") from e


def evaluate(
    *,
    judgment_id: str,
    task_id: str,
    scope_revision: int,
    context_revision: int,
    question_version: str,
    threshold_version: str,
    min_confidence: float,
    hosted_allowed: bool,
    request: dict[str, Any],
    api_key: str | None,
    persist: Callable[[dict[str, Any]], Any],
    post: Callable[[str, dict[str, Any]], Any] = http_post,
    env: str = "pilot",
) -> dict[str, Any]:
    """Call Jev once and persist exactly one judgment record, whatever happens."""
    base = {
        "id": judgment_id,
        "taskId": task_id,
        "scopeRevision": scope_revision,
        "contextRevision": context_revision,
        "questionVersion": question_version,
        "thresholdVersion": threshold_version,
        "requestedModel": request.get("model"),
        "inputHash": hashlib.sha256(json.dumps(request, separators=(",", ":")).encode()).hexdigest(),
        "mock": False,
    }
    if not hosted_allowed:
        record = {**base, "status": "unavailable", "fallback": "reasoning_review", "error": "HOSTED_JUDGMENT_NOT_AUTHORIZED"}
        persist(record)
        return record
    validate_request(request)
    if not _prob(min_confidence):
        raise JevValidationError("INVALID_MIN_CONFIDENCE")
    try:
        if not api_key:
            raise RuntimeError("JEV_KEY_MISSING")
        response = validate_response(request, post(api_key, request))
        if response.get("model") == "mock" and env == "live":
            raise RuntimeError("JEV_MOCK_IN_LIVE")
        low = any(_confidence(a) < min_confidence for a in response["answers"].values())
        record = {
            **base,
            "returnedModel": response["model"],
            "answers": response["answers"],
            "inputTokens": response["usage"]["input_tokens"],
            "status": "needs_review" if low else "judged",
            "fallback": "reasoning_review" if low else None,
            "mock": response.get("model") == "mock",
        }
    except Exception as e:  # noqa: BLE001 - every provider failure becomes an unavailable record
        record = {**base, "status": "unavailable", "fallback": "reasoning_review", "error": str(e) or type(e).__name__}
    persist(record)  # failures propagate by design
    return record
