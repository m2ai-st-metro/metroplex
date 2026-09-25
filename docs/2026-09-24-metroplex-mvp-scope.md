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
- Reasoning turns default to DeepInfra (already wired in Metroplex); projects with
  `hosted_allowed: false` use local reasoning only, with no Jev call.
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
