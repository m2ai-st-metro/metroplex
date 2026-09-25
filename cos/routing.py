"""Path 2: one routing turn per unacknowledged Teletraan wake (decisions R1-R6, E1-E2).

Every effect is a Teletraan command with a deterministic command id derived from
the wake id and cycle, so a crash between commands replays the same receipts
instead of creating a second worker (AE6). Routing adds a contributor; the task
owner stays accountable and is the only one who can accept the work.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from cos import jev_client
from cos.reasoning import ReasoningUnavailable, reasoner_for
from cos.safety import CircuitBreaker, CycleCaps
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
        breaker: CircuitBreaker,
        caps: CycleCaps,
        notify: Notify,
        jev_post: Callable[[str, dict[str, Any]], Any] = jev_client.http_post,
        reasoner_factory: Callable[[Any, bool], Any] = reasoner_for,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.config = config
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
        spec = task.get("spec") or {}
        if task["status"] in TERMINAL:
            return self._ack(wake, key, f"noop: task {task['status']}", out)
        if project.get("humanOnly") or spec.get("humanOnly") or spec.get("synthetic"):
            return self._ack(wake, key, "noop: human-only or synthetic task is never routed", out)
        if task["status"] == "blocked":
            if wake["reason"] == "unblock" and self._deps_done(task, tasks):
                self._cmd(out, "task.resume", task["id"], task["revision"], {}, f"cos:{key}:resume")
                return self._ack(wake, key, "resume: blocker cleared", out)
            return self._ack(wake, key, f"noop: blocked ({task.get('reason') or 'no reason'})", out)
        if task["status"] == "paused":
            return self._ack(wake, key, "noop: task paused", out)
        live = [a for a in snap.get("attempt", []) if a["taskId"] == task["id"] and a["status"] in LIVE]
        if live:
            if wake["reason"] == "stalled":
                self.notify(f"Stalled: {task['title']} ({project['title']}). Attempt {live[0]['id']} is alive but has not checkpointed. Owner {task['owner']} should look; Metroplex will not replace a live worker.", None)
                return self._ack(wake, key, f"wait: attempt {live[0]['id']} live but stalled; owner and Matthew notified", out)
            return self._ack(wake, key, "wait: live attempt on task", out)
        if wake["reason"] == "result":
            return self._ack(wake, key, f"await: owner {task['owner']} reviews the result", out)
        if wake["reason"] not in ROUTABLE_REASONS:
            return self._ack(wake, key, f"noop: reason {wake['reason']}", out)
        if not self._deps_done(task, tasks):
            return self._ack(wake, key, "wait: dependencies not done", out)
        reserved = spec.get("reservedAction")
        if reserved and reserved in project.get("reservedActions", []) and not self._reserved_approved(snap, task, reserved):
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
        busy = any(a["worker"] == choice and a["status"] in LIVE for a in fresh.get("attempt", []))
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
        use_jev = project.get("hostedAllowed") and not self.breaker.is_open("jev", now=self.clock()) and self.caps.take("jev", self.config.caps.jev_per_cycle, now=self.clock())
        if existing:
            judgment = existing  # crash replay: never pay for the same question twice
        elif use_jev:
            request = self.jev_request(task, project, cands, wake)
            judgment = jev_client.evaluate(
                judgment_id=jid, task_id=task["id"], scope_revision=task["scopeRevision"], context_revision=task["revision"],
                question_version=QUESTION_VERSION, threshold_version=th.version, min_confidence=th.jev_min_confidence,
                hosted_allowed=True, request=request, api_key=self.config.typesafe_api_key,
                persist=lambda rec: self.client.command("judgment.record", jid, 0, {**rec, "taskId": task["id"]}, command_id=f"cos:{jid}"),
                post=self.jev_post, env=self.config.env,
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
        reasoner = self.reasoner_factory(self.config, bool(project.get("hostedAllowed")))
        system = ("You route one task to one agent for Metroplex, Matthew's chief of staff. Choose exactly one agent id from the candidates, "
                  "or 'wait', or 'none_fit'. Never invent an agent. JSON: {\"choice\": str, \"reason\": str}.")
        user = json.dumps({"task": {k: task.get(k) for k in ("title", "objective", "acceptance", "spec")}, "projectObjective": project["objective"], "candidates": cands, "jev": judgment and {k: judgment.get(k) for k in ("status", "answers", "error")}})
        try:
            result = reasoner.complete_json(system, user)
            self.breaker.record_success("turn")
        except ReasoningUnavailable as e:
            self.breaker.record_failure("turn", now=self.clock())
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
            self._cmd(out, "attempt.queue", f"{contribution_id}:a1", 0, queue, f"cos:{key}:queue")
        return self._ack(wake, key, f"assign: {agent} ({why})", out)

    def _escalate(self, wake, key, task, project, kind: str, need: str, out: Outcome, approve_reserved: str | None = None) -> Outcome:
        reason = f"{kind}: {need}"
        self._cmd(out, "task.block", task["id"], task["revision"], {"reason": reason}, f"cos:{key}:block")
        buttons = [[{"text": "Approve", "callback_data": f"r:{task['id']}:{task['scopeRevision']}"}, {"text": "Hold", "callback_data": f"hold:{task['id']}"}]] if approve_reserved else None
        self.notify(f"{project['title']}: {task['title']}\nNeeds you ({kind}): {need}.\nOther work continues.", buttons)
        return self._ack(wake, key, f"escalate: {reason}", out)

    def _ack(self, wake, key, decision: str, out: Outcome) -> Outcome:
        self._cmd(out, "wake.ack", wake["id"], wake["revision"], {"decision": decision[:500]}, f"cos:{key}:ack")
        out.decision, out.acked = decision, True
        return out

    def _cmd(self, out: Outcome, op: str, object_id: str, expected: int, payload: dict[str, Any], command_id: str) -> None:
        self.client.command(op, object_id, expected, payload, command_id=command_id)
        out.commands.append(op)

    @staticmethod
    def _deps_done(task, tasks) -> bool:
        return all(tasks.get(d, {}).get("status") == "done" for d in task.get("dependencies", []))

    @staticmethod
    def _reserved_approved(snap, task, action) -> bool:
        return any(a.get("scope") == "reserved_action" and a.get("taskId") == task["id"] and a.get("action") == action and a.get("scopeRevision") == task["scopeRevision"] for a in snap.get("approval", []))


def route_pending(router: Router, client: TeletraanClient) -> list[Outcome]:
    """One pass over pending wakes, one turn at a time, fresh state per turn.
    STALE_* rejections mean state moved under us; the next poll retries fresh."""
    pending = client.wake_pending().get("wakes", [])
    if not pending:
        return []
    snap = client.snapshot()
    outcomes = []
    # Sequential on purpose: each turn sees the previous turn's effects.
    for wake in sorted(pending, key=lambda w: (w["projectId"], w["taskId"], w["reason"])):
        try:
            outcomes.append(router.handle(wake, snap))
            snap = client.snapshot()
        except WorkError as e:
            if e.code.startswith("STALE_") or e.code == "ALREADY_EXISTS":
                log.info("wake %s skipped: %s", wake["id"], e.code)
                continue
            router.breaker.record_failure("turn")
            log.error("wake %s failed: %s", wake["id"], e.code)
    return outcomes
