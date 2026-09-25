"""Path 2: one routing turn per unacknowledged Teletraan wake (decisions R1-R6, E1-E2).

Every effect is a Teletraan command with a deterministic command id derived from
the wake id and cycle, so a crash between commands replays the same receipts
instead of creating a second worker (AE6). Routing adds a contributor; the task
owner stays accountable and is the only one who can accept the work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from cos import jev_client
from cos.reasoning import ReasoningUnavailable, reasoner_for
from cos.safety import CircuitBreaker, CycleCaps
from cos.store import LocalStore
from cos.teletraan_client import TeletraanClient, WorkError

log = logging.getLogger(__name__)

LIVE = {"queued", "running", "stopping"}
TERMINAL = {"done", "cancelled"}
ROUTABLE_REASONS = {"created", "scope", "stopped", "imported", "stalled", "unassigned", "orphan", "ready"}
QUESTION_VERSION = "route-v1"
ESCAPES = {
    "wait": "a live attempt is progressing, a dependency is not done, or nobody should start yet",
    "none_fit": "no listed agent fits this task, or it needs Matthew (intent or authority)",
}

Notify = Callable[[str, list[list[dict[str, str]]] | None], Any]
ESCALATION_KINDS = ("intent", "authority", "reserved")
# Teletraan rejections that mean "not now", not "broken": the turn waits.
NOT_NOW = {"PROJECT_CAPACITY_EXHAUSTED", "WRITABLE_SCOPE_CONFLICT", "BINDING_BUSY", "DEPENDENCY_NOT_DONE", "RESERVED_ACTION_REQUIRES_APPROVAL", "PROJECT_PAUSED", "ATTEMPT_STILL_LIVE"}


def notice_key(task: dict[str, Any], kind: str) -> str:
    """One notice per block episode. A block writes a new task revision, so a
    task blocked again (after a failed attempt or an operator resume) is a new
    episode with a new notice, even at the same scope revision (review N1/N5)."""
    return f"notified:{task['id']}:r{task['revision']}:{kind}"


def awaiting_review(snap: dict[str, Any], task: dict[str, Any]) -> bool:
    """A current-scope result or accepted contribution is the owner's to review
    or complete; routing must never send a second worker (review N2)."""
    rev = task.get("scopeRevision")
    attempts = {a["id"]: a for a in snap.get("attempt", [])}
    for c in snap.get("contribution", []):
        if c["taskId"] != task["id"]:
            continue
        if (c.get("accepted") or {}).get("scopeRevision") == rev:
            return True  # accepted: the owner completes the task
        # Only the contribution's CURRENT attempt counts: a rework the owner
        # queued that then failed is not awaiting review (round-3 R1).
        a = attempts.get(c.get("currentAttempt"))
        if a and a["status"] == "completed" and a.get("resultCurrent") and a.get("scopeRevision") == rev:
            return True
    return False


def escalation_notice(store: LocalStore, task: dict[str, Any], project: dict[str, Any], kind: str, need: str) -> tuple[str, list[list[dict[str, str]]] | None]:
    """Text and buttons for an escalation. Reserved-action buttons carry a short
    token (Telegram caps callback data at 64 bytes); the token maps to the task
    and scope revision in the local store, never to an action."""
    buttons = None
    if kind == "reserved":
        token = hashlib.sha256(f"{task['id']}:{task['scopeRevision']}:{task['revision']}".encode()).hexdigest()[:12]
        store.set(f"cb:{token}", {"taskId": task["id"], "scopeRevision": task["scopeRevision"], "blockRevision": task["revision"]})
        buttons = [[{"text": "Approve", "callback_data": f"r:{token}"}, {"text": "Hold", "callback_data": f"hold:{token}"}]]
    return f"{project['title']}: {task['title']}\nNeeds you ({kind}): {need}.\nOther work continues.", buttons


@dataclass
class Outcome:
    decision: str
    acked: bool
    commands: list[str] = field(default_factory=list)


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in rows}


class Router:
    def __init__(
        self,
        client: TeletraanClient,
        config: Any,
        store: LocalStore,
        breaker: CircuitBreaker,
        caps: CycleCaps,
        notify: Notify,
        jev_post: Callable[[str, dict[str, Any]], Any] = jev_client.http_post,
        reasoner_factory: Callable[[Any, bool], Any] = reasoner_for,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.config = config
        self.store = store
        self.breaker = breaker
        self.caps = caps
        self.notify = notify
        self.jev_post = jev_post
        self.reasoner_factory = reasoner_factory
        self.clock = clock

    # ------------------------------------------------------------------ R1
    def candidates(self, snap: dict[str, Any], task: dict[str, Any], project: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        """Eligible workers, ranked, capped. Also returns how many members hold
        the work grant at all, so 'everyone is busy' (wait) is distinguishable
        from 'nobody can do this' (escalate)."""
        grants = {g["principal"]: g for g in snap.get("grant", []) if g["projectId"] == project["id"] and g.get("active")}
        agents = _by_id(snap.get("agent", []))
        live_by_worker: dict[str, int] = {}
        for a in snap.get("attempt", []):
            if a["status"] in LIVE:
                live_by_worker[a["worker"]] = live_by_worker.get(a["worker"], 0) + 1
        done_by_worker: dict[str, int] = {}
        for c in snap.get("contribution", []):
            if c.get("accepted"):
                done_by_worker[c["worker"]] = done_by_worker.get(c["worker"], 0) + 1
        prior = [c["worker"] for c in snap.get("contribution", []) if c["taskId"] == task["id"]]
        eligible, total = [], 0
        for member in project.get("members", []):
            g, agent = grants.get(member), agents.get(member)
            if not g or "work" not in g.get("actions", []) or not agent or agent.get("status") == "retired":
                continue
            if agent.get("lifecycle") == "temporary" and agent.get("projectId") != project["id"]:
                continue
            total += 1
            if live_by_worker.get(member, 0) >= 1:
                continue
            eligible.append({"agent": member, "capabilities": agent.get("capabilities", []), "liveAttempts": live_by_worker.get(member, 0), "acceptedContributions": done_by_worker.get(member, 0), "priorOnThisTask": prior.count(member)})
        eligible.sort(key=lambda c: (c["priorOnThisTask"], -c["acceptedContributions"], c["agent"]))
        return eligible[: self.config.caps.max_candidates], total

    # ------------------------------------------------------------------ Jev frame
    def jev_request(self, task: dict[str, Any], project: dict[str, Any], cands: list[dict[str, Any]], wake: dict[str, Any]) -> dict[str, Any]:
        spec = task.get("spec") or {}
        criteria = {c["agent"]: f"capabilities: {', '.join(c['capabilities']) or 'none listed'}; live attempts {c['liveAttempts']}; accepted contributions {c['acceptedContributions']}; earlier contributions on this task {c['priorOnThisTask']}" for c in cands}
        criteria.update(ESCAPES)
        state = {
            "task": {"title": task["title"], "objective": task["objective"], "acceptance": task["acceptance"], "doneWhen": spec.get("doneWhen"), "checkpoints": spec.get("checkpoints"), "qualityChecks": spec.get("qualityChecks"), "wakeReason": wake["reason"]},
            "project": {"objective": project["objective"], "goals": [g["statement"] for g in project.get("goals", [])]},
            "candidates": cands,
            "priorDecisions": wake.get("priorDecisions", []),
        }
        return {
            "model": self.config.jev_model,
            "state": state,
            "questions": {
                "route": {"type": "choice", "instructions": "Pick the single best agent to contribute to this task now, or an escape label.", "criteria": criteria},
                "ready": {"type": "noul", "instructions": "Is this task actionable now by the chosen agent without asking Matthew anything?"},
            },
        }

    # ------------------------------------------------------------------ turn
    def handle(self, wake: dict[str, Any], snap: dict[str, Any]) -> Outcome:
        tasks, projects = _by_id(snap.get("task", [])), _by_id(snap.get("project", []))
        task, project = tasks.get(wake["taskId"]), projects.get(wake["projectId"])
        key = f"{wake['id']}:{wake['cycle']}"
        out = Outcome(decision="", acked=False)
        if not task or not project:
            return self._ack(wake, key, "noop: task or project not visible", out)
        if project.get("paused"):
            out.decision = "hold: project paused"
            return out  # resume raises fresh wakes; leave this one for later
        if project["id"] in {c["project_id"] for c in self.store.cards_in("granting")}:
            out.decision = "hold: project setup not finished"
            return out  # decomposition and dependencies are not all in place yet
        spec = task.get("spec") or {}
        if task["status"] in TERMINAL:
            return self._ack(wake, key, f"noop: task {task['status']}", out)
        if project.get("humanOnly") or spec.get("humanOnly") or spec.get("synthetic"):
            return self._ack(wake, key, "noop: human-only or synthetic task is never routed", out)
        if task["status"] == "blocked":
            if wake["reason"] == "unblock" and self._deps_done(task, tasks):
                self._cmd(out, "task.resume", task["id"], task["revision"], {}, f"cos:{key}:resume")
                return self._ack(wake, key, "resume: blocker cleared", out)
            self.ensure_notice(task, project)
            return self._ack(wake, key, f"noop: blocked ({task.get('reason') or 'no reason'})", out)
        if task["status"] == "paused":
            return self._ack(wake, key, "noop: task paused", out)
        live = [a for a in snap.get("attempt", []) if a["taskId"] == task["id"] and a["status"] in LIVE]
        if live:
            if wake["reason"] == "stalled":
                self.notify(f"Stalled: {task['title']} ({project['title']}). Attempt {live[0]['id']} is alive but has not checkpointed. Owner {task['owner']} should look; Metroplex will not replace a live worker.", None)
                return self._ack(wake, key, f"wait: attempt {live[0]['id']} live but stalled; owner and Matthew notified", out)
            return self._ack(wake, key, "wait: live attempt on task", out)
        if wake["reason"] == "result" or awaiting_review(snap, task):
            return self._ack(wake, key, f"await: owner {task['owner']} reviews the result", out)
        if wake["reason"] not in ROUTABLE_REASONS:
            return self._ack(wake, key, f"noop: reason {wake['reason']}", out)
        if not self._deps_done(task, tasks):
            return self._ack(wake, key, "wait: dependencies not done", out)
        reserved = spec.get("reservedAction")
        if reserved and not self._reserved_approved(snap, task, reserved):
            return self._escalate(wake, key, task, project, "reserved", f"needs your approval for {reserved.replace('_', ' ')} before any agent starts", out, approve_reserved=reserved)
        contribution_id = f"{task['id']}:{wake['reason']}:{wake['cycle']}"
        prior = _by_id(snap.get("contribution", [])).get(contribution_id)
        if prior:
            # Crash replay: this wake already chose a worker. Finish that choice;
            # re-deciding could pick someone else and collide on the command id.
            return self._assign(wake, key, task, project, prior["worker"], prior.get("judgmentId"), "resumed after restart", snap, out)
        # Thrash guard (plan 4.2): a task re-routed 3 times in one cycle is held.
        if not self.caps.take(f"turns:{task['id']}", self.config.caps.turns_per_project_per_cycle, now=self.clock()):
            out.decision = "hold: task routed 3 times this cycle (thrash guard)"
            return out
        limit = project.get("concurrencyLimit")
        if limit is not None and sum(1 for a in snap.get("attempt", []) if a["projectId"] == project["id"] and a["status"] in LIVE) >= limit:
            return self._ack(wake, key, "wait: project at its concurrency limit", out)
        cands, total = self.candidates(snap, task, project)
        if not cands:
            if total:
                return self._ack(wake, key, "wait: every eligible agent is busy", out)
            return self._escalate(wake, key, task, project, "authority", "no project member can do this work (recruiting is not in the MVP)", out)
        choice, judgment_id, why = self._decide(wake, key, task, project, cands, snap)
        if choice == "wait":
            return self._ack(wake, key, f"wait: {why}", out)
        if choice == "none_fit" or choice not in {c["agent"] for c in cands}:
            return self._escalate(wake, key, task, project, "intent", why or "the routing turn found no fitting agent", out)
        # Fresh-snapshot guard: the chosen agent must still be free (plan 2.3).
        fresh = self.client.snapshot()
        fresh_task = _by_id(fresh.get("task", [])).get(task["id"])
        # Also refuse if anyone (e.g. the owner) started work on this task meanwhile.
        busy = any((a["worker"] == choice or a["taskId"] == task["id"]) and a["status"] in LIVE for a in fresh.get("attempt", []))
        if not fresh_task or fresh_task["scopeRevision"] != task["scopeRevision"] or busy:
            return self._ack(wake, key, f"wait: guard_override ({choice} busy or task changed)", out)
        return self._assign(wake, key, task, project, choice, judgment_id, why, fresh, out)

    # ------------------------------------------------------------------ R2
    def _decide(self, wake, key, task, project, cands, snap) -> tuple[str, str | None, str]:
        """Jev first when allowed; a reasoning turn when Jev is low-confidence,
        unavailable, skipped, or disallowed. Returns (choice, judgment_id, why)."""
        th = self.config.thresholds
        judgment = None
        jid = f"route:{key}"
        existing = _by_id(snap.get("judgment", [])).get(jid)
        inflight = self.store.get(f"jev_inflight:{jid}")
        if existing:
            judgment = existing  # crash replay: reuse the recorded answer
        elif project.get("hostedAllowed") and not self.breaker.is_open("jev", now=self.clock()) and self.caps.take("jev", self.config.caps.jev_per_cycle, now=self.clock()):
            request = self.jev_request(task, project, cands, wake)
            # A crash after the HTTP call but before judgment.record must not buy
            # a second call: mark in flight first; a replay records "unknown".
            post = self.jev_post
            if inflight:
                def post(key, req):  # noqa: ARG001 - replay never calls the provider
                    raise RuntimeError("JEV_OUTCOME_UNKNOWN_AFTER_RESTART")
            self.store.set(f"jev_inflight:{jid}", True)
            judgment = jev_client.evaluate(
                judgment_id=jid, task_id=task["id"], scope_revision=task["scopeRevision"], context_revision=task["revision"],
                question_version=QUESTION_VERSION, threshold_version=th.version, min_confidence=th.jev_min_confidence,
                hosted_allowed=True, request=request, api_key=self.config.typesafe_api_key,
                persist=lambda rec: self.client.command("judgment.record", jid, 0, {**rec, "taskId": task["id"]}, command_id=f"cos:{jid}"),
                post=post, env=self.config.env,
            )
            if judgment["status"] == "unavailable":
                self.breaker.record_failure("jev", now=self.clock())
            else:
                self.breaker.record_success("jev")
        if judgment and judgment.get("status") == "judged":
            route, ready = judgment["answers"]["route"], judgment["answers"]["ready"]
            if ready["noul"] >= th.jev_ready_threshold or route["choice"] in ESCAPES:
                return route["choice"], jid, f"Jev chose {route['choice']} ({route['confidence']:.2f})"
        return self._reason(task, project, cands, judgment, jid if judgment else None)

    def _reason(self, task, project, cands, judgment, jid) -> tuple[str, str | None, str]:
        if self.breaker.is_open("reasoning", now=self.clock()):
            return "wait", jid, "local reasoning is paused by its breaker (`metroplex reset reasoning`)"
        reasoner = self.reasoner_factory(self.config, bool(project.get("hostedAllowed")))
        system = ("You route one task to one agent for Metroplex, Matthew's chief of staff. Choose exactly one agent id from the candidates, "
                  "or 'wait', or 'none_fit'. Never invent an agent. JSON: {\"choice\": str, \"reason\": str}.")
        user = json.dumps({"task": {k: task.get(k) for k in ("title", "objective", "acceptance", "spec")}, "projectObjective": project["objective"], "candidates": cands, "jev": judgment and {k: judgment.get(k) for k in ("status", "answers", "error")}})
        try:
            result = reasoner.complete_json(system, user)
            self.breaker.record_success("reasoning")
        except ReasoningUnavailable as e:
            if self.breaker.record_failure("reasoning", now=self.clock()):
                self.notify(f"Metroplex: local Qwen reasoning failed 3 times in a row ({e}). Routing continues on confident Jev answers only; other tasks wait. `metroplex reset reasoning` after fixing.", None)
            return "wait", jid, f"no Jev verdict and reasoning unavailable ({e})"
        choice = result.get("choice")
        if choice not in {c["agent"] for c in cands} | set(ESCAPES):
            return "wait", jid, f"reasoning returned an unknown choice {choice!r}"
        return choice, jid, f"reasoning turn: {str(result.get('reason', ''))[:200]}"

    # ------------------------------------------------------------------ effects
    def _assign(self, wake, key, task, project, agent, judgment_id, why, snap, out: Outcome) -> Outcome:
        contribution_id = f"{task['id']}:{wake['reason']}:{wake['cycle']}"
        payload: dict[str, Any] = {"taskId": task["id"], "worker": agent, "deliverable": (task.get("spec") or {}).get("doneWhen") or task["acceptance"]}
        judgment = _by_id(snap.get("judgment", [])).get(judgment_id) if judgment_id else None
        if judgment and judgment.get("current"):
            payload["judgmentId"] = judgment_id
        if contribution_id not in _by_id(snap.get("contribution", [])):
            self._cmd(out, "contribution.create", contribution_id, 0, payload, f"cos:{key}:contribution")
        binding = next((b for b in snap.get("binding", []) if b.get("projectId") == project["id"] and b.get("agentId") == agent and b.get("enrolled") and b.get("control") == "managed"), None)
        queue = {"contributionId": contribution_id, "runtime": "t3" if binding else "api"}
        if binding:
            queue["bindingId"] = binding["id"]
        if f"{contribution_id}:a1" not in _by_id(snap.get("attempt", [])):
            try:
                self._cmd(out, "attempt.queue", f"{contribution_id}:a1", 0, queue, f"cos:{key}:queue")
            except WorkError as e:
                if e.code not in NOT_NOW:
                    raise
                return self._ack(wake, key, f"wait: {e.code} while queuing {agent}", out)
        return self._ack(wake, key, f"assign: {agent} ({why})", out)

    def _escalate(self, wake, key, task, project, kind: str, need: str, out: Outcome, approve_reserved: str | None = None) -> Outcome:
        reason = f"{kind}: {need}"
        receipt = self._cmd(out, "task.block", task["id"], task["revision"], {"reason": reason}, f"cos:{key}:block")
        blocked = next((o for o in receipt.get("objects", []) if o.get("kind") == "task" and o.get("id") == task["id"]), {**task, "reason": reason, "revision": task["revision"] + 1})
        self.ensure_notice(blocked, project)
        return self._ack(wake, key, f"escalate: {reason}", out)

    def ensure_notice(self, task: dict[str, Any], project: dict[str, Any]) -> bool:
        """Send the escalation notice for a Metroplex-blocked task unless one was
        already delivered for this scope revision. Delivery is recorded only when
        Telegram accepted the message, so a crash or send failure is retried by
        the next wake or sweep instead of leaving the task blocked in silence."""
        kind, _, need = (task.get("reason") or "").partition(": ")
        if kind not in ESCALATION_KINDS or self.store.get(notice_key(task, kind)):
            return False
        text, buttons = escalation_notice(self.store, task, project, kind, need)
        if self.notify(text, buttons):
            self.store.set(notice_key(task, kind), True)
            return True
        return False

    def _ack(self, wake, key, decision: str, out: Outcome) -> Outcome:
        self._cmd(out, "wake.ack", wake["id"], wake["revision"], {"decision": decision[:500]}, f"cos:{key}:ack")
        out.decision, out.acked = decision, True
        return out

    def _cmd(self, out: Outcome, op: str, object_id: str, expected: int, payload: dict[str, Any], command_id: str) -> dict[str, Any]:
        receipt = self.client.command(op, object_id, expected, payload, command_id=command_id)
        out.commands.append(op)
        return receipt or {}

    @staticmethod
    def _deps_done(task, tasks) -> bool:
        return all(tasks.get(d, {}).get("status") == "done" for d in task.get("dependencies", []))

    @staticmethod
    def _reserved_approved(snap, task, action) -> bool:
        """An unused approval for this task, action and scope revision. Each one
        covers exactly one attempt (Teletraan enforces the same)."""
        return any(a.get("scope") == "reserved_action" and a.get("taskId") == task["id"] and a.get("action") == action and a.get("scopeRevision") == task["scopeRevision"] and not a.get("usedBy") for a in snap.get("approval", []))


def route_pending(router: Router, client: TeletraanClient) -> list[Outcome]:
    """One pass over pending wakes, one turn at a time, fresh state per turn.
    STALE_* rejections mean state moved under us; the next poll retries fresh."""
    pending = client.wake_pending().get("wakes", [])
    if not pending:
        router.breaker.record_success("turn")  # a quiet pass is a clean pass
        return []
    snap = client.snapshot()
    outcomes, failed = [], False
    # Sequential on purpose: each turn sees the previous turn's effects.
    for wake in sorted(pending, key=lambda w: (w["projectId"], w["taskId"], w["reason"])):
        try:
            outcomes.append(router.handle(wake, snap))
            snap = client.snapshot()
        except WorkError as e:
            if e.code.startswith("STALE_") or e.code == "ALREADY_EXISTS":
                log.info("wake %s skipped: %s", wake["id"], e.code)
                continue
            failed = f"{e.code} on {wake['id']}"
            log.error("wake %s failed: %s", wake["id"], e.code)
    # The breaker counts failed passes, not failed wakes: three bad wakes in one
    # pass are one failure; three consecutive failed passes open it (review N10).
    if failed:
        if router.breaker.record_failure("turn"):
            router.notify(f"Metroplex routing stopped after 3 consecutive failed passes (last: {failed}). `metroplex reset turn` after fixing.", None)
    else:
        router.breaker.record_success("turn")
    return outcomes
