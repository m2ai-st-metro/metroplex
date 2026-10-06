"""Path 1: idea -> card -> Matthew's yes -> granted, decomposed project (I1-I4, S1-S4).

Authority is created in exactly one place: `approve`, on Matthew's Yes for the
exact card hash he was shown. Everything before it is inert (a card in the
local store, or a Teletraan proposal). Decomposition then creates uniform,
spec'd tasks that each trace to an approved goal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import textwrap
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from cos.reasoning import Reasoner, ReasoningUnavailable
from cos.spec import (
    RESERVED_ACTIONS,
    SPEC_TEMPLATE_VERSION,
    card_digest,
    default_checks,
    scope_path,
    scope_within,
    source_ref,
    validate_card,
    validate_task_spec,
)
from cos.store import LocalStore
from cos.teletraan_client import TeletraanClient, WorkError

log = logging.getLogger(__name__)
Send = Callable[[str, list[list[dict[str, str]]] | None], Any]

CLASSIFY_SYSTEM = ("You triage one message Matthew sent to his chief of staff. Label it 'note' (an idea to keep, not work to start now) "
                   "or 'new_objective' (he wants a project done). Default question to null. Ask exactly one short question only when "
                   "the message is new_objective and no observable done-when can be inferred from it at all (for example it points at "
                   "something he did not attach, or names no outcome). Never ask about implementation details such as fixtures, test "
                   "data, file names, libraries or approach: the card and its owner decide those. If he states a done-when, question "
                   "must be null. JSON: {\"label\": \"note\"|\"new_objective\", \"question\": str|null, \"title\": str}.")
CARD_SYSTEM = ("Draft a project card for Matthew's approval. Fields: title (<=80 chars), objective (one imperative paragraph), "
               "doneWhen (observable by an outsider without asking Matthew; when statedDoneWhen is given, use it as written), "
               "scopeIn (list), scopeOut (list), goals (1-7 of "
               "{goalId: 'g1'.., statement, doneWhen}), owner (one id from the agents list), kill (condition that ends the project), "
               "writableScope (paths relative to the repository root that this work may change, taken from the idea's writable-scope text; "
               "[\".\"] when it names no narrower area). Keep it small. JSON object only.")
DECOMPOSE_SYSTEM = ("Break an approved project into the smallest set of tasks that covers every goal: usually one to three per goal, "
                    "never more than {max} in total. Each task: {{goalId, title, objective, acceptance, dependsOn: [indexes of earlier tasks], "
                    "reservedAction: null | publish_push_deploy | external_contact | live_fleet_config, writableScope: [paths inside the card's "
                    "writableScope this task may change, or [] for the whole card scope]}}. `source` is the approved proposal: name its specific "
                    "requirements in objectives and acceptance. Prefer tasks that can run in parallel; "
                    "use dependsOn only when a task truly needs another task's output. Set reservedAction when the step publishes, pushes, "
                    "deploys, contacts anyone outside, or changes live systems. Every task must serve exactly one listed goal. "
                    "JSON: {{\"tasks\": [...]}}.")

_S = {"type": "string"}
# Grammar for the card and decompose turns (llama-server `json_schema`): the
# output is valid JSON with these fields, so a proposal that quotes code can
# no longer turn a draft into REASONING_NOT_JSON (2026-10-06).
CARD_SCHEMA = {
    "type": "object",
    "properties": {"title": _S, "objective": _S, "doneWhen": _S, "scopeIn": {"type": "array", "items": _S}, "scopeOut": {"type": "array", "items": _S},
                   "goals": {"type": "array", "items": {"type": "object", "properties": {"goalId": _S, "statement": _S, "doneWhen": _S}, "required": ["goalId", "statement", "doneWhen"]}},
                   "owner": _S, "kill": _S, "writableScope": {"type": "array", "items": _S}},
    "required": ["title", "objective", "doneWhen", "scopeIn", "scopeOut", "goals", "owner", "kill", "writableScope"],
}
DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {"type": "array", "items": {"type": "object", "properties": {
        "goalId": _S, "title": _S, "objective": _S, "acceptance": _S, "dependsOn": {"type": "array", "items": {"type": "integer"}},
        "reservedAction": {"anyOf": [{"type": "null"}, {"enum": ["publish_push_deploy", "external_contact", "live_fleet_config"]}]},
        "writableScope": {"type": "array", "items": _S},
    }, "required": ["goalId", "title", "objective", "acceptance", "dependsOn", "reservedAction", "writableScope"]}}},
    "required": ["tasks"],
}

# Code-side backstop for reserved steps the model did not tag. When unsure,
# err toward reserved: a wrong tag costs Matthew one tap, a missed one costs
# the approval gate (live E2E finding 4).
_RESERVED_WORDS = {
    "live_fleet_config": ("systemd", "systemctl", "crontab", "cron job", "restart", "credential", "api key", "secret", "rotate",
                          "env.shared", "pm2", "kubectl", "helm", "ansible", "live config", "production config"),
    "publish_push_deploy": ("publish", "republish", "deploy", "redeploy", "release", "roll out", "rollout", "git push", "push the", "push to", "docker push",
                            "merge", "pull request", "open a pr", "pypi", "npm publish", "ship to", "go live", "go-live", "upload to",
                            "terraform", "dns", "production"),
    "external_contact": ("email", "e-mail", "send the", "send a", "message the", "contact", "post to", "post on", "post the", "tweet",
                         "slack", "notify customer", "reach out", "reply to", "newsletter", "announce", "invite", "sms", "whatsapp"),
}

# Keywords match at the start of a word, with any ending: "deploys" and
# "emailing" still count, but "mechanisms" is not "sms" and "emergency" is not
# "merge" (2026-10-05: a build task on card-cc97709c was tagged external_contact).
_RESERVED_PATTERNS = {action: tuple(re.compile(r"(?<![a-z0-9])" + re.escape(w)) for w in words) for action, words in _RESERVED_WORDS.items()}


# A line that starts with "done when" (or done-when, donewhen, doneWhen, with an
# optional bullet, bold markers and colon) is Matthew stating the done-when
# himself. Line start only, so "I'll be done when..." mid-sentence does not
# count; a bare "Done when?" does not count either.
_DONE_WHEN = re.compile(r"^(?P<indent>[ \t]*)(?P<bullet>[-*>][ \t]+|[->])?\**done[ _-]?when\**(?:[ \t]*:\**[ \t]*|[ \t]+)(?P<first>.*)$", re.IGNORECASE | re.MULTILINE)
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def stated_done_when(text: str) -> str | None:
    """The done-when Matthew wrote into the message, or None. Code-side backstop
    for the classifier: a stated done-when means drafting never stops to ask
    (2026-10-02: he wrote 'Done when: node --test ... passes' and was still
    asked which fixture to use).

    The whole block is returned: the header's own text (or the first line after
    it), then every following list item or indented line up to a blank line or
    an unindented line of prose. Under a bulleted header only deeper-indented
    lines belong to it, so a sibling bullet is not swallowed. 2026-10-05: a
    'Done when:' header over five bullets kept only the first, and the card
    Matthew approved dropped the other four."""
    m = _DONE_WHEN.search(text or "")
    if not m:
        return None
    nested, header_indent = bool(m.group("bullet")), _indent(m.group("indent"))
    first, lines = m.group("first").strip(), (text[m.end():].split("\n")[1:])
    if not first:
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and not nested and not _LIST_ITEM.match(lines[0]):
            first = lines.pop(0).strip()
    block: list[str] = []
    for line in lines:
        if not line.strip() or (_indent(line) <= header_indent if nested else _indent(line) == 0 and not _LIST_ITEM.match(line)):
            break
        block.append(line.rstrip())
    stated = "\n".join(([first] if first else []) + textwrap.dedent("\n".join(block)).splitlines()).strip()
    core = _LIST_ITEM.sub("", stated, count=1).strip()
    return stated if len(core) >= 3 and not core.startswith("?") else None


# Labels a proposal or message can state a card field with, at a line start
# (optional bullet and bold). "Done when" is listed only so it ends the block
# above it; stated_done_when owns that field.
_FIELD = re.compile(
    r"^[ \t]*(?:[-*>][ \t]+)?\**(?P<label>title|objective|goal[ \t]*(?P<n>\d+)|writable[ \t]+scope|scope[ \t]+in|in[ \t]+scope"
    r"|out[ \t]+of[ \t]+scope|scope[ \t]+out|done[ _-]?when)\**[ \t]*:\**[ \t]*(?P<first>.*)$",
    re.IGNORECASE,
)
_FIELD_KEYS = {"title": "title", "objective": "objective", "writable scope": "scopeIn", "scope in": "scopeIn", "in scope": "scopeIn",
               "out of scope": "scopeOut", "scope out": "scopeOut"}


def _items(first: str, lines: list[str]) -> list[str]:
    """A block as items: one per list bullet (wrapped lines joined), or the
    whole paragraph as one item when it has no bullets."""
    if not any(_LIST_ITEM.match(line) for line in lines):
        return [" ".join(" ".join([first, *lines]).split())]
    items = [first] if first else []
    for line in lines:
        if _LIST_ITEM.match(line):
            items.append(_LIST_ITEM.sub("", line, count=1))
        elif items:
            items[-1] += " " + line.strip()
    return [" ".join(i.split()) for i in items]


def stated_fields(text: str) -> dict[str, Any]:
    """Card fields the text states itself: title, objective, goals (by number),
    scopeIn, scopeOut. The card carries them as written; the model fills only
    what is unstated. 2026-10-06: a live run of the side-walk proposal kept the
    stated done-when but paraphrased these in 3 of 6 drafts (the repo path left
    scopeIn, 'original attachments' left scopeOut, the app's name left the
    objective). A block runs to a blank line or the next label."""
    lines = (text or "").split("\n")
    found: dict[str, Any] = {}
    goals: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _FIELD.match(line)
        if not m:
            continue
        label = " ".join(m.group("label").lower().split())
        first, rest = m.group("first").strip(), lines[i + 1:]
        if not first:
            while rest and not rest[0].strip():
                rest = rest[1:]
        block: list[str] = []
        for nxt in rest:
            if not nxt.strip() or _FIELD.match(nxt):
                break
            block.append(nxt.strip() if not _LIST_ITEM.match(nxt) else nxt.lstrip())
        items = [x for x in _items(first, block) if len(x) >= 3 and not x.startswith("?")]
        if not items:
            continue
        if m.group("n"):
            goals.setdefault(int(m.group("n")), " ".join(items))
        elif label in _FIELD_KEYS:
            key = _FIELD_KEYS[label]
            if key not in found:
                found[key] = items if key.startswith("scope") else " ".join(items)
    if goals:
        found["goals"] = [goals[n] for n in sorted(goals)]
    return found


# An effective proposal (source plus Matthew's amendments, newest last) is marked
# by this header. Its precedence line says the amendment wins, so no field is
# carried verbatim from the text above it (Codex F9).
AMENDMENT_HEADER = "## Amendment from Matthew ("
_AMENDMENT = re.compile(r"^" + re.escape(AMENDMENT_HEADER), re.MULTILINE)
AMENDMENT_PRECEDENCE = "Where an amendment conflicts with the text above, the amendment wins."
SOURCE_PROMPT_CHARS = 20_000


def is_amended(text: str) -> bool:
    return bool(_AMENDMENT.search(text or ""))


def infer_reserved(title: str, objective: str, acceptance: str = "") -> str | None:
    text = f"{title} {objective} {acceptance}".lower()
    for action, patterns in _RESERVED_PATTERNS.items():
        if any(p.search(text) for p in patterns):
            return action
    return None


def _clip(value: str, n: int) -> str:
    """At most n characters, cut at a word boundary and marked with an ellipsis
    so a shortened title never ends mid-word ('...(Devastato', 2026-10-05)."""
    value = value.strip()
    if len(value) <= n:
        return value
    head = value[: n - 1]
    if not value[n - 1].isspace() and re.search(r"\s", head):
        head = head.rsplit(None, 1)[0]
    return (head.rstrip(" \t-:;,(/[{") or value[: n - 1]) + "\u2026"


def _slug(text: str, n: int = 24) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "project"


def _short(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


class Intake:
    def __init__(self, client: TeletraanClient, store: LocalStore, reasoner: Reasoner, send: Send, config: Any, clock: Callable[[], float] = time.time):
        self.client, self.store, self.reasoner, self.send, self.config, self.clock = client, store, reasoner, send, config, clock

    # ------------------------------------------------------------------ I1/I2
    def handle_text(self, text: str, message_id: str) -> str:
        # "card <id>" is a command only when <id> resolves to a known card or
        # proposal (any tool's id format); anything else is an idea (N9, R3).
        command = re.match(r"^\s*card\s+(\S+)\s*$", text, re.IGNORECASE)
        if command and self._resolves(command.group(1)):
            return self.card_command(command.group(1))
        open_card = self.store.get("open_question")
        if open_card:
            self.store.set("open_question", None)
            card = self.store.get_card(open_card)
            if card and card["status"] == "drafting":
                return self._draft(card["card_id"], answer=text, message_id=message_id)
        try:
            result = self.reasoner.complete_json(CLASSIFY_SYSTEM, text, max_tokens=256)
        except ReasoningUnavailable:
            return self._file_note(text, message_id, "classification unavailable, kept as a proposal")
        if result.get("label") != "new_objective":
            return self._file_note(text, message_id, "kept as a proposal")
        card_id = f"card-{_short(message_id + text)}"
        question = result.get("question")
        if question and stated_done_when(text):
            log.info("dropping classifier question %r: the message states its done-when", question)
            question = None
        # Every card binds a filed proposal (decision 2026-10-06), so the source a
        # worker later reads is exactly what Matthew's yes covered.
        proposal = self._file_proposal(f"Q-metroplex-{_short(message_id + text)}", result.get("title") or text[:80], text, f"telegram:{message_id}")
        self.store.set(f"card_source:{card_id}", proposal["id"])
        self.store.put_card(card_id, {"title": result.get("title") or text[:80]}, "", "drafting", source=proposal["body"], question=question)
        if question:
            self.store.set("open_question", card_id)
            self.send(f"One question before I draft the card:\n{question}", None)
            return "asked"
        return self._draft(card_id)

    def _resolves(self, ref: str) -> bool:
        if self.store.get_card(ref):
            return True
        return any(p["id"] == ref for p in self.client.snapshot().get("proposal", []))

    def card_command(self, ref: str) -> str:
        """'card <id>': redraft a card that is still drafting, or turn an inert
        proposal (any tool may have filed it) into a card for Matthew's yes."""
        stored = self.store.get_card(ref)
        if stored is None and not ref.startswith("card-"):
            stored = self.store.get_card(f"card-{_short(ref)}")  # the card made from this proposal
        if stored and stored["status"] == "drafting":
            return self._draft(stored["card_id"])
        if stored:
            # Never rewrite a card that is waiting, granting, or granted (review N4).
            self.send(f"{ref} is already {stored['status']}; nothing to redraft.", None)
            return "closed"
        proposal = next((p for p in self.client.snapshot().get("proposal", []) if p["id"] == ref), None)
        if not proposal:
            self.send(f"No card or proposal named {ref}.", None)
            return "unknown"
        card_id = f"card-{_short(ref)}"
        self.store.set(f"card_source:{card_id}", ref)
        self.store.put_card(card_id, {"title": proposal["title"]}, "", "drafting", source=proposal["body"])
        return self._draft(card_id, title=proposal["title"])

    def _file_note(self, text: str, message_id: str, why: str) -> str:
        pid = f"Q-metroplex-{_short(message_id + text)}"
        self._file_proposal(pid, text[:80], text, f"telegram:{message_id}")
        self.send(f"Noted ({why}): {pid}. Say 'card {pid}' when you want it turned into a project.", None)
        return "note"

    # ------------------------------------------------------------------ card sources (A)
    def _proposal(self, pid: str) -> dict[str, Any] | None:
        return next((p for p in self.client.snapshot().get("proposal", []) if p["id"] == pid), None)

    def _file_proposal(self, pid: str, title: str, body: str, ref: str) -> dict[str, Any]:
        """File a proposal (or find it already filed) and return the STORED one.
        Its body, not the text sent, is what a card's source hash binds:
        Teletraan trims bodies, and a retry may rebuild a body with a new
        timestamp under the same id; the first filing wins (Codex F7)."""
        try:
            self.client.command("proposal.file", pid, 0, {"title": title, "body": body, "source": {"type": "metroplex-bot", "ref": ref}}, command_id=f"cos:proposal:{pid}")
        except WorkError as e:
            if e.code not in ("ALREADY_EXISTS", "COMMAND_ID_CONFLICT"):
                raise
        stored = self._proposal(pid)
        if stored is None:
            raise WorkError("SOURCE_PROPOSAL_MISSING")
        return stored

    def _source(self, card_id: str) -> tuple[str, str]:
        """(proposal id, stored body) a card drafts from. A card left from before
        source binding is filed first, so every card binds a proposal."""
        pid = self.store.get(f"card_source:{card_id}")
        proposal = self._proposal(pid) if pid else None
        if proposal is None:
            stored = self.store.get_card(card_id) or {"source": "", "card": {}}
            text = stored["source"] or stored["card"].get("title", "") or card_id
            proposal = self._file_proposal(f"Q-metroplex-{_short(card_id + text)}", text[:80], text, f"card:{card_id}")
            self.store.set(f"card_source:{card_id}", proposal["id"])
        return proposal["id"], proposal["body"]

    def _amend(self, card_id: str, answer: str, message_id: str) -> tuple[str, str]:
        """An answer or Edit that shapes the card becomes source too: a new
        immutable proposal holding the current source, the amendment verbatim,
        and its precedence rule. The yes then binds one document a worker can
        read without guessing which text wins (Codex F3)."""
        base_id, body = self._source(card_id)
        base = self._proposal(base_id) or {"title": card_id}
        when = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds")
        amended = f"{body}\n\n{AMENDMENT_HEADER}{when}, telegram:{message_id})\n{answer}\n\n{AMENDMENT_PRECEDENCE}"
        proposal = self._file_proposal(f"Q-metroplex-{_short(base_id + answer)}", base["title"], amended, f"telegram:{message_id}")
        self.store.set(f"card_source:{card_id}", proposal["id"])
        return proposal["id"], proposal["body"]

    # ------------------------------------------------------------------ I3/S1
    def _agents(self) -> list[dict[str, Any]]:
        return [{"id": a["id"], "name": a.get("name"), "capabilities": a.get("capabilities", []), "reportsTo": a.get("reportsTo")} for a in self.client.snapshot().get("agent", []) if a.get("lifecycle", "persistent") == "persistent" and a.get("status") != "retired"]

    @staticmethod
    def _owners(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Owner candidates: agents that report to no one. A worker (reportsTo
        set) never owns, so it can never accept its own contribution. The
        model only picks among these; it cannot name a worker (2026-09-26:
        Qwen named worker kup as owner and the card was approved)."""
        return [a for a in agents if not a.get("reportsTo")]

    def _draft(self, card_id: str, answer: str | None = None, message_id: str = "", title: str | None = None) -> str:
        agents = self._owners(self._agents())
        if not agents:
            self.store.set_card_status(card_id, "dropped")
            self.send("I cannot draft a card: no persistent agent that reports to no one exists in Teletraan to own it.", None)
            return "no_agents"
        source_id, source = self._amend(card_id, answer, message_id) if answer is not None else self._source(card_id)
        self.send("Drafting a card for this on the local model; it usually takes a minute or two.", None)
        amended = is_amended(source)
        stated = None if amended else stated_done_when(source)
        prompt = json.dumps({"idea": source, "statedDoneWhen": stated, "answer": answer, "agents": agents, "reservedActionsDefault": list(RESERVED_ACTIONS)})
        card, problems = None, ["no draft"]
        for attempt in range(2):
            try:
                draft = self.reasoner.complete_json(CARD_SYSTEM, prompt if attempt == 0 else prompt + "\nFix these problems: " + "; ".join(problems), max_tokens=1024, schema=CARD_SCHEMA)
            except ReasoningUnavailable as e:
                self.send(f"Card drafting is unavailable right now ({e}). Your idea is kept; say 'card {card_id}' to retry.", None)
                return "unavailable"
            card = self._normalize(draft, agents)
            if not amended:
                # "Use it as written" is enforced here, not trusted to the model:
                # the card carries every field the source states verbatim. An
                # amended source (an answer or Edit, on any drafting path) says
                # the amendment wins, so the model's draft wins then.
                self._carry_stated(card, source, stated, title)
            card["source"] = source_ref(source_id, source)
            problems = validate_card(card)
            if not problems:
                break
        if problems:
            self.send("I could not draft a complete card: " + "; ".join(problems) + ". Reply with more detail to retry.", None)
            self.store.set("open_question", card_id)
            return "incomplete"
        digest = card_digest(card)
        self.store.put_card(card_id, card, digest, "awaiting_yes", source=source)
        self.send(self.render(card), [[{"text": "Yes", "callback_data": f"yes:{card_id}:{digest[:16]}"}, {"text": "Edit", "callback_data": f"edit:{card_id}"}, {"text": "Drop", "callback_data": f"drop:{card_id}"}]])
        return "card"

    @staticmethod
    def _carry_stated(card: dict[str, Any], source: str, stated_done: str | None, title: str | None) -> None:
        fixed = stated_fields(source)
        if stated_done:
            card["doneWhen"] = stated_done
        if fixed.get("title") or title:
            card["title"] = _clip(fixed.get("title") or title or "", 80)
        for key in ("objective", "scopeIn", "scopeOut"):
            if key in fixed:
                card[key] = fixed[key]
        if "goals" in fixed:
            # Stated goal statements win; the model's per-goal done-when is
            # kept by position, and a goal it skipped falls back to the card's.
            model = card["goals"]
            card["goals"] = [{"goalId": f"g{i + 1}", "statement": s,
                              "doneWhen": (model[i]["doneWhen"] if i < len(model) and model[i]["doneWhen"] else card["doneWhen"])}
                             for i, s in enumerate(fixed["goals"][:7])]

    @staticmethod
    def _normalize(draft: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
        """Fields Metroplex controls are set here, not by the model: defaults
        for reserved actions and hosted processing (MVP scope defaults)."""
        goals = [{"goalId": str(g.get("goalId") or f"g{i + 1}"), "statement": str(g.get("statement", "")).strip(), "doneWhen": str(g.get("doneWhen", "")).strip()} for i, g in enumerate(draft.get("goals") or []) if isinstance(g, dict)]
        owner = draft.get("owner") if draft.get("owner") in {a["id"] for a in agents} else agents[0]["id"]
        # Writable paths are cleaned like Teletraan's and never left empty: the
        # card always shows Matthew an explicit list, "." meaning everything.
        scope: list[str] = []
        for value in draft.get("writableScope") or []:
            path = scope_path(value)
            if path and path not in scope:
                scope.append(path)
        return {
            "title": _clip(str(draft.get("title", "")), 80),
            "objective": str(draft.get("objective", "")).strip(),
            "doneWhen": str(draft.get("doneWhen", "")).strip(),
            "scopeIn": [str(s) for s in draft.get("scopeIn") or []],
            "scopeOut": [str(s) for s in draft.get("scopeOut") or []],
            "goals": goals,
            "owner": owner,
            "kill": str(draft.get("kill") or "Matthew cancels, or 30 days pass with no accepted contribution").strip(),
            "reservedActions": list(RESERVED_ACTIONS),
            "hostedAllowed": True,
            "humanOnly": False,
            "specTemplate": SPEC_TEMPLATE_VERSION,
            "writableScope": ["."] if not scope or "." in scope else scope,
        }

    RENDERED_KEYS = frozenset({"title", "objective", "doneWhen", "owner", "goals", "scopeIn", "scopeOut", "reservedActions", "kill", "hostedAllowed", "humanOnly", "specTemplate", "source", "writableScope"})

    @classmethod
    def render(cls, card: dict[str, Any]) -> str:
        """Every field covered by the approval hash is on screen: a yes may only
        grant what Matthew was shown (review finding B1)."""
        unknown = set(card) - cls.RENDERED_KEYS
        if unknown:
            raise ValueError(f"card has fields the approval screen does not show: {sorted(unknown)}")
        goals = "\n".join(f"  {g['goalId']}. {g['statement']} (done: {g['doneWhen']})" for g in card["goals"])
        lst = lambda items: "\n".join(f"  - {i}" for i in items) or "  - (none listed)"
        return (f"CARD: {card['title']}\n{card['objective']}\n\nDone when: {card['doneWhen']}\nOwner: {card['owner']}\nGoals:\n{goals}\n"
                f"In scope:\n{lst(card.get('scopeIn', []))}\nOut of scope:\n{lst(card.get('scopeOut', []))}\n"
                + (f"Writable paths: {', '.join(card['writableScope'])}{' (whole repository)' if card['writableScope'] == ['.'] else ''}\n" if "writableScope" in card else "")
                + (f"Source: {card['source']['id']} (sha256 {card['source']['sha256'][:12]}); its full text binds the work.\n" if "source" in card else "")
                + f"Reserved for you: {', '.join(a.replace('_', ' ') for a in card['reservedActions']) or 'none'}\nKill: {card['kill']}\n"
                f"Hosted routing via Jev (TypeSafe): {'yes' if card.get('hostedAllowed') else 'no'}\n"
                f"Human-only (never routed): {'yes' if card.get('humanOnly') else 'no'}\nSpec template: {card.get('specTemplate')}\n\n"
                f"Yes grants this project. Nothing runs before you tap it.")

    # ------------------------------------------------------------------ I4
    def approve(self, card_id: str, hash_prefix: str, from_id: str, chat_id: str, message_id: str) -> str:
        """Every step is replay-safe (deterministic command ids, stored plan), and
        the card is 'granted' only after decomposition finishes. A crash or error
        leaves it 'granting': tapping Yes again, or the hourly resume, finishes it."""
        stored = self.store.get_card(card_id)
        if not stored or stored["status"] not in ("awaiting_yes", "granting"):
            return "That card is no longer waiting for a yes."
        if not stored["card_hash"].startswith(hash_prefix):
            return "That button belongs to an older version of the card."
        if stored["status"] == "awaiting_yes" and self.clock() - stored["created_at"] > self.config.thresholds.card_expiry_s:
            self.store.set_card_status(card_id, "expired")
            return "That card expired. Send the idea again for a fresh card."
        card = stored["card"]
        if card_digest(card) != stored["card_hash"]:
            return "The stored card changed after drafting; not approving."
        if stored["status"] == "awaiting_yes":
            # Record intent locally BEFORE any Teletraan command, so a crash at
            # any later point leaves a 'granting' card the hourly resume finishes
            # (review round 2, Codex). The attestation is stored so replays send
            # byte-identical commands.
            project_id = f"p-{_slug(card['title'])}-{card_id[-4:]}"
            # The first yes for this card version is the attestation; retries
            # replay it byte-identically instead of conflicting (round-3 R2).
            key = f"approval_payload:{card_id}:{stored['card_hash'][:12]}"
            if not self.store.get(key):
                self.store.set(key, {"fromId": from_id, "chatId": chat_id, "messageId": message_id})
            self.store.set_card_status(card_id, "granting", project_id)
        return self._finish_grant(card_id)

    def _finish_grant(self, card_id: str) -> str:
        stored = self.store.get_card(card_id)
        card, digest, project_id = stored["card"], stored["card_hash"], stored["project_id"]
        attest = self.store.get(f"approval_payload:{card_id}:{digest[:12]}")
        if not attest:
            # No stored yes for this version (e.g. a card left over from older
            # code): fail closed and ask again rather than replaying blind (R6).
            self.store.set_card_status(card_id, "awaiting_yes")
            self.send(f"{card['title']}: I have no record of your yes for this version; tap Yes again.", None)
            return "Waiting for a fresh yes."
        approval_id = f"approval-{card_id}-{digest[:12]}"
        try:
            self.client.command("approval.record", approval_id, 0, {"scope": "project", "cardHash": digest, **attest}, command_id=f"cos:approval:{card_id}:{digest[:12]}")
        except WorkError as e:
            if e.command_rejected and e.code != "COMMAND_ID_CONFLICT":
                # Teletraan refused this yes (e.g. approver not recognized): it
                # recorded nothing, so forget it; the next yes is a fresh one (Q1).
                self.store.set(f"approval_payload:{card_id}:{digest[:12]}", None)
                self.store.set_card_status(card_id, "awaiting_yes")
            raise
        try:
            self.client.command("project.create", project_id, 0, {"card": card, "approvalId": approval_id}, command_id=f"cos:project:{card_id}:{digest[:12]}")
        except WorkError as e:
            if e.command_rejected and e.code not in ("COMMAND_ID_CONFLICT",):
                # Proven no-effect domain rejection: the yes did not take; ask again.
                self.store.set_card_status(card_id, "awaiting_yes")
            raise
        if self.store.get(f"plan:{project_id}") is None:
            self.send(f"Approved. Setting up {project_id}: breaking it into tasks takes a minute or two.", None)
        created = self.decompose(project_id, card)
        self.store.set_card_status(card_id, "granted", project_id)
        return f"Granted {project_id}. {created} tasks created and queued for routing."

    def resume_granting(self) -> list[str]:
        """Finish any approved card whose decomposition was interrupted."""
        done = []
        for c in self.store.cards_in("granting"):
            try:
                result = self._finish_grant(c["card_id"])
                if self.store.get_card(c["card_id"])["status"] == "granted":
                    self.send(result.replace("Granted", "Finished setting up", 1), None)
                    done.append(c["card_id"])
            except Exception as e:  # noqa: BLE001 - retried next hour; Matthew is told
                self.send(f"{c['project_id']} is approved but setup is still incomplete ({e}). I retry hourly; tapping Yes again also retries.", None)
        return done

    # ------------------------------------------------------------------ S2-S4
    def plan(self, card: dict[str, Any]) -> list[dict[str, Any]]:
        """One bounded reasoning pass, validated field by field (model JSON is
        untrusted). Any approved goal the model skipped gets a fallback task, so
        a partial answer can never silently drop work Matthew approved."""
        goals = {g["goalId"]: g for g in card["goals"]}
        limit = self.config.caps.max_decompose_tasks
        tasks: list[dict[str, Any]] = []
        source = self._proposal(card["source"]["id"]) if card.get("source") else None
        user = {"card": card, **({"source": source["body"][:SOURCE_PROMPT_CHARS]} if source else {})}
        try:
            raw = self.reasoner.complete_json(DECOMPOSE_SYSTEM.format(max=limit), json.dumps(user), max_tokens=2048, schema=DECOMPOSE_SCHEMA)
            items = raw.get("tasks") if isinstance(raw, dict) else None
            for t in items if isinstance(items, list) else []:
                if not isinstance(t, dict) or not isinstance(t.get("goalId"), str) or t["goalId"] not in goals:
                    continue
                goal = goals[t["goalId"]]
                deps = t.get("dependsOn") if isinstance(t.get("dependsOn"), list) else []
                title, objective = _clip(str(t.get("title") or goal["statement"]), 120), str(t.get("objective") or goal["statement"])
                acceptance = str(t.get("acceptance") or goal["doneWhen"])
                raw_tag = t.get("reservedAction")
                if raw_tag in RESERVED_ACTIONS:
                    tagged = raw_tag
                elif raw_tag:
                    # The model said "reserved" with a name we do not know: keep
                    # the signal, never erase it (E2E review E1).
                    tagged = infer_reserved(str(raw_tag), title, objective + " " + acceptance) or "publish_push_deploy"
                else:
                    tagged = infer_reserved(title, objective, acceptance)
                tasks.append({"goalId": goal["goalId"], "title": title, "objective": objective, "acceptance": acceptance,
                              "dependsOn": [j for j in deps if isinstance(j, int) and not isinstance(j, bool) and 0 <= j < len(tasks)], "reservedAction": tagged,
                              "writableScope": self._task_scope(t.get("writableScope"), card)})
                if len(tasks) >= limit:
                    break
        except ReasoningUnavailable:
            tasks = []
        covered = {t["goalId"] for t in tasks}
        for g in card["goals"]:
            if g["goalId"] not in covered:
                tasks.append({"goalId": g["goalId"], "title": _clip(g["statement"], 120), "objective": g["statement"], "acceptance": g["doneWhen"], "dependsOn": [], "reservedAction": infer_reserved(g["statement"], "", g["doneWhen"]),
                              "writableScope": card.get("writableScope")})
        return tasks

    @staticmethod
    def _task_scope(raw: Any, card: dict[str, Any]) -> list[str] | None:
        """A task's writable paths: the model's subset when every path sits inside
        the card's, else the card-wide scope (Matthew's decision 2026-10-06: a
        task is never unbounded). None only for legacy cards without a scope."""
        card_scope = card.get("writableScope")
        if not card_scope:
            return None
        paths: list[str] = []
        for value in raw if isinstance(raw, list) else []:
            path = scope_path(value)
            if path and path not in paths:
                paths.append(path)
        return paths if paths and scope_within(paths, card_scope) else list(card_scope)

    def decompose(self, project_id: str, card: dict[str, Any]) -> int:
        """Create the planned tasks. The plan is stored before any task exists, so
        a resume replays the same plan instead of asking the model again."""
        tasks = self.store.get(f"plan:{project_id}")
        if tasks is None:
            tasks = self.plan(card)
            self.store.set(f"plan:{project_id}", tasks)
        goals = {g["goalId"]: g for g in card["goals"]}
        ids = [f"{project_id}:t{i + 1}" for i in range(len(tasks))]
        existing = {t["id"]: t for t in self.client.snapshot().get("task", [])}
        for task_id, t in zip(ids, tasks):
            if task_id not in existing:
                checkpoints, quality = default_checks(goals[t["goalId"]])
                # Always explicit: a plan stored before scopes existed falls back to the card's.
                scope = t.get("writableScope") or card.get("writableScope")
                spec = validate_task_spec({"cardGoalId": t["goalId"], "doneWhen": t["acceptance"], "checkpoints": checkpoints, "qualityChecks": quality, "reservedAction": t["reservedAction"] if "reservedAction" in t else infer_reserved(t["title"], t["objective"], t["acceptance"]), "humanOnly": False, "synthetic": False,
                                           **({"writableScope": scope} if scope else {})})
                receipt = self.client.command("task.create", task_id, 0, {"projectId": project_id, "owner": card["owner"], "title": t["title"], "objective": t["objective"], "acceptance": spec["doneWhen"], "spec": spec}, command_id=f"cos:decompose:{task_id}")
                existing[task_id] = next(o for o in receipt["objects"] if o.get("kind") == "task" and o["id"] == task_id)
            # Dependencies right after each task, not in a second pass: dependsOn
            # only points at earlier tasks, which already exist (review N3).
            deps = [ids[j] for j in t["dependsOn"]]
            if deps and not existing[task_id].get("dependencies"):
                self.client.command("task.dependencies", task_id, existing[task_id]["revision"], {"dependencies": deps}, command_id=f"cos:deps:{task_id}")
        return len(ids)

    # ------------------------------------------------------------------ Edit / Drop / expiry
    def edit(self, card_id: str) -> str:
        stored = self.store.get_card(card_id)
        if not stored or stored["status"] != "awaiting_yes":
            return "That card is not editable."
        self.store.set_card_status(card_id, "drafting")
        self.store.set("open_question", card_id)
        return "Send the change you want; I will redraft the card."

    def drop(self, card_id: str) -> str:
        stored = self.store.get_card(card_id)
        if stored and stored["status"] == "granting":
            # A stuck 'granting' card can be dropped only if no project exists
            # yet; an existing project is cancelled in Teletraan instead (R2).
            if any(p["id"] == stored["project_id"] for p in self.client.snapshot().get("project", [])):
                return f"{stored['project_id']} already exists; cancel it in Teletraan instead."
            self.store.set_card_status(card_id, "dropped")
            return "Dropped. No project had been created."
        if not stored or stored["status"] not in ("drafting", "awaiting_yes"):
            return "That card is already closed."
        self.store.set_card_status(card_id, "dropped")
        return "Dropped. Nothing was created."

    def expire_cards(self) -> list[str]:
        expired = []
        for c in self.store.cards_in("awaiting_yes"):
            if self.clock() - c["created_at"] > self.config.thresholds.card_expiry_s:
                self.store.set_card_status(c["card_id"], "expired")
                self._file_note(c["source"] or c["card"].get("title", ""), c["card_id"], "card expired unapproved")
                expired.append(c["card_id"])
        return expired

    # ------------------------------------------------------------------ reserved actions (C4)
    def approve_reserved(self, task_id: str, scope_revision: int, from_id: str, chat_id: str, message_id: str, *, block_revision: int) -> str:
        """One approval per block episode (review N1). The button maps to the task,
        its scope revision and the revision it was blocked at; the action comes
        from the task spec, never the button. Each episode gets its own approval
        and command ids, so a re-ask after a failed attempt can be approved."""
        task = next((t for t in self.client.snapshot().get("task", []) if t["id"] == task_id), None)
        if not task or task["scopeRevision"] != scope_revision or task["status"] != "blocked" or task["revision"] != block_revision:
            return "That task changed since you were asked; I will ask again if it still needs you."
        action = (task.get("spec") or {}).get("reservedAction")
        if not action:
            return "That task no longer needs a reserved action."
        episode = f"{task_id}:r{block_revision}"
        self.client.command("approval.record", f"reserved-{task_id}-r{block_revision}", 0, {"scope": "reserved_action", "taskId": task_id, "action": action, "scopeRevision": scope_revision, "fromId": from_id, "chatId": chat_id, "messageId": message_id}, command_id=f"cos:reserved:{episode}")
        self.client.command("task.resume", task_id, block_revision, {}, command_id=f"cos:reserved-resume:{episode}")
        return f"Approved {action.replace('_', ' ')} for {task['title']}. Routing it now."
