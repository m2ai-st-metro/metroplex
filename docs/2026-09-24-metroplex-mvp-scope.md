# Metroplex decision layer: MVP scope

Date: 2026-09-24. Decisions 8 and 9 in `~/vault/decisions/2026-09-24-metroplex-cos-router.md`.
Full design: `2026-09-24-metroplex-decision-layer-plan.md` (Fable). Base spec:
`2026-09-24-metroplex-cos-spec.md`. This file says what the MVP builds and what waits.

## MVP outcome

Matthew sends an idea to `@m2ai_metroplex_bot`, answers at most one question, taps Yes. A
granted project exists, Metroplex decomposes it into uniform tasks, Jev picks an agent for each
ready task, stalled and unassigned work is caught by the sweep, and anything needing Matthew
arrives as one 3-line message. `/pause` stops new work. Quality scoring and the RCL loop are
not in the MVP.

## Struck (decision 8)

`provider_call_limit` on cards, decision M8 (allowance), allowance escalation reason, and every
acceptance example that asserts on allowance. The pilot's optional `providerCallLimit` domain
field is left untouched and never set by Metroplex.

## Defaults taken (Matthew may override)

- Card `hosted_allowed` defaults to `true`, so Jev (TypeSafe hosted API) can route.
- Metroplex drafts the first decomposition (S2); owners may refine within scope.
- Reasoning turns run on local Qwen only (`qwen3.5-122b-a10b` on the M5 llama-server),
  decided by Matthew 2026-09-24. An earlier draft defaulted to DeepInfra without his
  decision; that was wrong and is removed. Projects with `hosted_allowed: false` also skip Jev.
- Jev thresholds v1: choice `minConfidence 0.60`, noul `readyThreshold 0.70`.
- Stall thresholds v1: progress 45 min, heartbeat 10 min, unassigned 10 min, orphan 15 min.
- Card expiry 7 days.

## In the MVP

Decisions (plan section 1): I1, I2, I3, I4, S1, S2, S3 (single default template), S4, R1, R2,
R3, R5 (static table), R6, M1, M2, M3, M4, M5, M7, E1, E2, E3.

Teletraan deltas (pilot branch `feat/organization-continuity-pair`):

| ID | Delta |
|---|---|
| T1 | `cos` principal, operation allowlist enforced in the domain |
| T2 | `approval.record`; `project.create` by `cos` must cite an unused matching approval |
| D1 | task `spec` validated at `task.create` (checkpoints, qualityChecks, doneWhen, reservedAction, humanOnly, synthetic, cardGoalId) |
| D2 | `proposal.file`, inert, open to any authenticated principal |
| D3 | `wake_pending` service action |
| D4 | `task.assign` may cite a current `judgmentId` |
| D5 | `attempt.queue` rejects human-only and synthetic tasks for non-operators |
| D6 | project `reservedActions`; reserved tasks cannot queue without an approval |
| D7 | `project.pause` / `project.resume` |
| D8 | `project.create` grants `cos` `[work, assign]` |
| D9 | non-operator `task.create` must cite a card goal or an owned parent |
| D10 | `wake.raise` for `cos` with reasons `stalled, unassigned, orphan, unblock` |

Metroplex (Python): strip retired gates and adapters; `teletraan_client.py`, `jev_client.py`
(D12), `spec.py` (D14), `cards.py`, `bot_intake.py`, `turn_runner.py` with `decompose` and
`route` modes (D15), `motion.py` for M1 to M5 (D13), `safety.py` generalization with
`BudgetEnforcer` deleted (D19), `/pause` `/resume` `/stop`.

Acceptance examples: C1 to C6 (spec), J1, J2, J3, J5, J6, J7, P1 to P5, K1, K2, K4, K5, and
AE3, AE5, AE6 re-proven. The full Teletraan suite must pass, not a selected subset.

## After the MVP

Q1 to Q4 quality scoring, L1 to L4 and the whole RCL loop (D16, D17, D18, D20, D21, D22,
D11 version fields), I5 sub-concepts, R4 recruit, M6 retire, E4 batching, briefing digest,
F1 to F3, J4, K3.

## Build order

1. Teletraan deltas with focused tests, then the full suite (`npm test`).
2. Metroplex strip (archive tag first) and charter rewrite.
3. `teletraan_client.py` and the wake/sweep loops with safety (K4, K5, AE6).
4. `jev_client.py` and `turn_runner.py` route mode (J1 to J7, AE3, AE5).
5. `spec.py`, `cards.py`, `bot_intake.py`, decompose mode (C1 to C3, C6, P1 to P5).
6. Escalation path (C4) and motion checks (K1, K2).
7. Integrated isolated run against a separate Teletraan state dir and socket, then independent review.

## Build status (2026-09-24)

Built on branches, nothing pushed, nothing activated:
- Teletraan pilot `feat/organization-continuity-pair`: `9b935bf` (stale test fix), `fc333f6`
  (MVP deltas), `87050bd` (judgment on contribution), `415ffac` (motion timestamps),
  `0178310` (cos agent visibility). Full suite 95/95. CCOS pilot Teletraan-backed tests 18/18.
- Metroplex `feature/cos-mvp` (worktree `~/projects/worktrees/metroplex-cos-mvp`): `b077759`,
  `75dc09b`, `2cfa07f`, `09d6921`. 68 tests against a real Teletraan work service, ruff clean.
  mypy is not installed in the venv, so types were not checked.

Deviations from the plan, each deliberate:
- Routing creates a contribution for the chosen worker and never calls `task.assign`: the
  owner stays accountable (continuity plan R2). The judgment link moved to
  `contribution.create` (new Teletraan check).
- D9 tightened: citing a card goal needs the `assign` grant; work-only agents add children
  only under tasks they own.
- Stalled attempts (M1/M2): owner and Matthew are notified once per stall episode; no assist
  contributor is added in the MVP. Assist moves to after the MVP (K1 re-scoped).
- Thrash cap is per task (3 routing turns per 15 minutes), not per project.
- `cos` sees every active persistent agent (identities only) so it can name card owners.
- Teletraan now records `raisedAt`/`acknowledgedAt` on wakes and `startedAt` on attempts.
- Found in passing: the pilot's approval runs used selected test subsets only; one test in
  the full suite was stale and failing. Fixed and committed separately.

Not yet done (step 7): an integrated isolated run (real bot, real Jev, real reasoning model
against a separate Teletraan state dir) and an independent review of both branches. Both
need Matthew's go-ahead: the run sends Telegram messages and calls hosted APIs.
