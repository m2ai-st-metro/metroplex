"""Motion sweep: pure detection on snapshots, plus one live wake.raise round trip."""

from __future__ import annotations

from datetime import datetime, timezone

from cos.config import Thresholds
from cos.motion import find, sweep
from tests.test_routing import add_task, grant_project

T = Thresholds()
NOW = 1_800_000_000.0


def iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc).isoformat()


def snap(tasks=(), attempts=(), wakes=(), project=None):
    return {"project": [project or {"id": "p", "paused": False, "humanOnly": False}], "task": list(tasks), "attempt": list(attempts), "wake": list(wakes)}


def task(tid="t1", status="ready", **extra):
    return {"id": tid, "projectId": "p", "status": status, "dependencies": [], "spec": {}, "revision": 1, **extra}


def test_m1_live_heartbeat_without_progress_is_stalled_not_dead():
    a = {"id": "a1", "taskId": "t1", "status": "running", "heartbeatAt": iso(30), "startedAt": iso(46 * 60)}
    [f] = find(snap([task(status="active")], [a]), T, NOW)
    assert f.check == "M1 stalled" and f.reason == "stalled"


def test_m1_progress_within_window_is_left_alone():
    a = {"id": "a1", "taskId": "t1", "status": "running", "heartbeatAt": iso(30), "startedAt": iso(3 * 3600), "progressAt": iso(10 * 60)}
    assert find(snap([task(status="active")], [a]), T, NOW) == []


def test_m2_missing_heartbeat_is_dead():
    a = {"id": "a1", "taskId": "t1", "status": "running", "heartbeatAt": iso(11 * 60), "startedAt": iso(20 * 60)}
    [f] = find(snap([task(status="active")], [a]), T, NOW)
    assert f.check == "M2 dead"


def test_m3_ready_quiet_task_is_unassigned_but_fresh_or_pending_is_not():
    acked = {"id": "p:t1:created", "taskId": "t1", "reason": "created", "cycle": 1, "acknowledged": True, "acknowledgedAt": iso(11 * 60)}
    [f] = find(snap([task()], wakes=[acked]), T, NOW)
    assert f.check == "M3 unassigned"
    assert find(snap([task()], wakes=[{**acked, "acknowledgedAt": iso(60)}]), T, NOW) == []
    pending = {**acked, "id": "p:t1:scope", "reason": "scope", "acknowledged": False, "raisedAt": iso(60)}
    assert find(snap([task()], wakes=[acked, pending]), T, NOW) == []


def test_m5_blocked_on_a_done_task_raises_unblock():
    tasks = [task("t1", status="blocked", reason="dep: t0"), task("t0", status="done")]
    [f] = find(snap(tasks), T, NOW)
    assert f.check == "M5 unblock" and f.task_id == "t1"


def test_m4_orphaned_wake_notifies_without_raising():
    w = {"id": "p:t1:created", "taskId": "t1", "reason": "created", "cycle": 1, "acknowledged": False, "raisedAt": iso(16 * 60)}
    [f] = find(snap([task()], wakes=[w]), T, NOW)
    assert f.check == "M4 orphan" and f.reason is None


def test_guard_human_only_synthetic_and_paused_are_invisible():
    acked = {"id": "w", "taskId": "t1", "reason": "created", "cycle": 1, "acknowledged": True, "acknowledgedAt": iso(3600)}
    assert find(snap([task(spec={"humanOnly": True})], wakes=[acked]), T, NOW) == []
    assert find(snap([task(spec={"synthetic": True})], wakes=[acked]), T, NOW) == []
    assert find(snap([task()], wakes=[acked], project={"id": "p", "paused": True}), T, NOW) == []


def test_live_sweep_raises_a_fenced_unassigned_wake(work, store):
    grant_project(work)
    add_task(work, "t1")
    work.cmd("cos", "wake.ack", "p:t1:created", {"decision": "wait: test"})
    notes = []
    later = datetime.fromisoformat(work.get("wake", "p:t1:created")["acknowledgedAt"].replace("Z", "+00:00")).timestamp() + 11 * 60
    found = sweep(work["cos"], store, T, max_wakes=10, notify=lambda *a: notes.append(a), now=later)
    assert [f.check for f in found] == ["M3 unassigned"]
    w = work.get("wake", "p:t1:unassigned")
    assert w["cycle"] == 1 and not w["acknowledged"]


class FakeClient:
    def __init__(self, s):
        self.s, self.raised = s, []

    def snapshot(self):
        return self.s

    def command(self, op, oid, rev, payload):
        self.raised.append((op, oid, payload["reason"]))


def test_a_stall_episode_raises_once_until_progress_moves(store):
    a = {"id": "a1", "taskId": "t1", "status": "running", "heartbeatAt": iso(30), "startedAt": iso(46 * 60)}
    client = FakeClient(snap([task(status="active")], [a]))
    for _ in range(3):
        sweep(client, store, T, 10, lambda *x: None, now=NOW)
    assert client.raised == [("wake.raise", "t1", "stalled")]
    a["progressAt"] = iso(50 * 60 - 1)  # a later checkpoint, still stale: a new episode
    sweep(client, store, T, 10, lambda *x: None, now=NOW + 10 * 60)
    assert len(client.raised) == 2


# ---------------------------------------------------------------- Fix B (2026-10-02)
# No dispatcher starts "api" attempts since 2026-10-01; only `ttn run` does.

def test_m6_api_attempt_notice_names_the_ttn_run_command_not_a_runtime():
    queued = {"id": "t2:created:1:a1", "taskId": "t2", "status": "queued", "queuedAt": iso(16 * 60), "runtime": "api"}
    [f] = find(snap([task("t2", status="active", title="Build the view")], [queued]), T, NOW)
    assert f.check == "M6 queued" and f.reason is None and f.ref == "queued:t2:created:1:a1"
    assert "waiting for you to run `ttn run t2:created:1:a1`" in f.detail and "Build the view" in f.detail
    assert "is its runtime" not in f.detail
    other = {**queued, "runtime": "t3"}
    [g] = find(snap([task("t2", status="active")], [other]), T, NOW)
    assert "is its runtime (t3) running?" in g.detail, "a runtime that does start attempts keeps the old question"


def test_m6_notice_is_sent_once_per_attempt(store):
    queued = {"id": "a2", "taskId": "t2", "status": "queued", "queuedAt": iso(16 * 60), "runtime": "api"}
    s = snap([task("t2", status="active")], [queued])

    class C:
        def snapshot(self):
            return s
    sent = []
    for i in range(3):
        sweep(C(), store, T, 10, lambda text, b=None: sent.append(text) or "ok", now=NOW + i * 300)
    assert len(sent) == 1 and "`ttn run a2`" in sent[0] and "queued attempts not started yet" in sent[0]
    assert store.get("sent:queued:a2") is True
