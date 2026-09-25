"""Keeping Teletraan in motion (decisions M1-M5, plan section 4).

The sweep only observes and raises wakes. It never assigns, stops, or resumes
work itself: every action goes through a fenced `wake.raise`, so the routing
turn decides with fresh state and the decision shows up in priorDecisions.

Anti fake-work guard (the Paperclip "board is live" failure): only tasks in
projects granted to `cos` are visible, proposals are not tasks, and human-only
or synthetic tasks are skipped here and rejected by Teletraan at action time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from cos.store import LocalStore
from cos.teletraan_client import TeletraanClient, WorkError

log = logging.getLogger(__name__)
LIVE = {"queued", "running", "stopping"}


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


@dataclass
class Finding:
    check: str
    task_id: str
    reason: str | None  # wake reason to raise, or None for notify-only
    detail: str
    ref: str | None = None  # dedupe key: one wake per stall episode, not per sweep


def find(snap: dict[str, Any], thresholds: Any, now: float) -> list[Finding]:
    projects = {p["id"]: p for p in snap.get("project", [])}
    tasks = {t["id"]: t for t in snap.get("task", [])}
    wakes = snap.get("wake", [])
    pending = {(w["taskId"], w["reason"]) for w in wakes if not w.get("acknowledged")}
    findings: list[Finding] = []

    def routable(t: dict[str, Any]) -> bool:
        p = projects.get(t["projectId"])
        spec = t.get("spec") or {}
        return bool(p) and not p.get("paused") and not p.get("humanOnly") and not spec.get("humanOnly") and not spec.get("synthetic")

    # M1 stalled / M2 dead: judged on observed times, never on silence alone.
    for a in snap.get("attempt", []):
        t = tasks.get(a["taskId"])
        if a["status"] != "running" or not t or not routable(t) or (t["id"], "stalled") in pending:
            continue
        heartbeat = _ts(a.get("heartbeatAt"))
        progress = _ts(a.get("progressAt")) or _ts(a.get("startedAt"))
        if heartbeat is not None and now - heartbeat > thresholds.dead_heartbeat_s:
            findings.append(Finding("M2 dead", t["id"], "stalled", f"attempt {a['id']} has no heartbeat for {int((now - heartbeat) // 60)} min", f"dead:{a['id']}:{a.get('heartbeatAt')}"))
        elif progress is not None and now - progress > thresholds.stall_progress_s:
            findings.append(Finding("M1 stalled", t["id"], "stalled", f"attempt {a['id']} alive but no checkpoint for {int((now - progress) // 60)} min", f"stalled:{a['id']}:{a.get('progressAt') or a.get('startedAt')}"))

    live_tasks = {a["taskId"] for a in snap.get("attempt", []) if a["status"] in LIVE}
    last_ack: dict[str, float] = {}
    for w in wakes:
        acked = _ts(w.get("acknowledgedAt"))
        if acked is not None:
            last_ack[w["taskId"]] = max(acked, last_ack.get(w["taskId"], 0))

    for t in tasks.values():
        if not routable(t):
            continue
        deps_done = all(tasks.get(d, {}).get("status") == "done" for d in t.get("dependencies", []))
        # M3 unassigned: ready, unblocked, nothing running, nothing pending, quiet.
        if t["status"] == "ready" and deps_done and t["id"] not in live_tasks and not any(k[0] == t["id"] for k in pending):
            quiet_since = last_ack.get(t["id"])
            if quiet_since is not None and now - quiet_since > thresholds.unassigned_s:
                findings.append(Finding("M3 unassigned", t["id"], "unassigned", f"ready with nothing running for {int((now - quiet_since) // 60)} min"))
        # M5 unblock: a declared blocker is now done.
        reason = t.get("reason") or ""
        if t["status"] == "blocked" and reason.startswith("dep:") and (t["id"], "unblock") not in pending:
            blocker = tasks.get(reason[4:].strip())
            if blocker and blocker["status"] == "done" and deps_done:
                findings.append(Finding("M5 unblock", t["id"], "unblock", f"blocker {blocker['id']} is done"))

    # M4 orphaned wake: pending longer than the orphan window. The wake loop
    # retries pending wakes every poll, so an orphan means that loop is failing:
    # tell Matthew once instead of raising more wakes.
    for w in wakes:
        raised = _ts(w.get("raisedAt"))
        t = tasks.get(w["taskId"])
        if not w.get("acknowledged") and raised is not None and now - raised > thresholds.orphan_wake_s and t and routable(t):
            findings.append(Finding("M4 orphan", w["taskId"], None, f"wake {w['id']} cycle {w['cycle']} unhandled for {int((now - raised) // 60)} min"))
    return findings


def sweep(client: TeletraanClient, store: LocalStore, thresholds: Any, max_wakes: int, notify: Callable[[str, Any], Any], now: float | None = None, dry_run: bool = False) -> list[Finding]:
    now = time.time() if now is None else now
    snap = client.snapshot()
    findings = find(snap, thresholds, now)
    raised = 0
    tasks = {t["id"]: t for t in snap.get("task", [])}
    for f in findings:
        if f.reason is None:
            key = f"orphan_notified:{f.detail.split(' cycle ')[0]}"
            if not dry_run and not store.get(key):
                notify(f"Metroplex: {f.detail}. Check `metroplex status`.", None)
                store.set(key, True)
            continue
        if f.ref and store.get(f"raised:{f.ref}"):
            continue  # already raised for this stall episode; new progress starts a new one
        if raised >= max_wakes:
            log.info("sweep cap reached; %s deferred", f.task_id)
            continue
        if dry_run:
            raised += 1
            continue
        try:
            client.command("wake.raise", f.task_id, tasks[f.task_id]["revision"], {"reason": f.reason})
            raised += 1
            if f.ref:
                store.set(f"raised:{f.ref}", True)
        except WorkError as e:
            log.info("wake.raise %s %s skipped: %s", f.task_id, f.reason, e.code)
    return findings
